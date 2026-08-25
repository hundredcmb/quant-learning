"""
申万行业行情数据层 (MarketDataProvider)

- 行情/市值获取: daily / daily_basic, 带按日内存缓存(涨跌幅口径见 shenwan_industry/AGENTS.md 第 3 节)
- 财务指标: fina_indicator_vip 按报告期全市场批拉扣非净利润(profit_dedt), 供单日榜 PE-TTM
  (PIT 按时点过滤 ann_date <= 计算日, 累计值口径与 TTM 规则见 get_ts_code_to_ttm_deducted_profit)
- 停牌自由流通市值回退: 新策略逐股[近730天 → 全窗回到上市日](limit 阶梯控 payload、以 free 为准),
  缺失股票由 resolve_missing_mv 线程池并发补齐; legacy 730 天逻辑保留(见 resolve_* 与
  shenwan_industry/AGENTS.md 第 5 节)
- 交易日历: trade_cal
- 区间逐日行情: 并发拉取 + 固定速率限流 + 重试
- API 调用计数: 构造时包装 pro, snapshot_api_calls() 取快照,
  任务前后快照求差即该任务实际调用次数(缓存命中不计)
"""

import bisect
import math
import os
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd

# 进度回调: (0~100 的百分比, 阶段说明)
ProgressCallback = Callable[[float, str], None]

# 协作式取消检查: 需要取消时抛异常
CancelCheck = Callable[[], None]

# 缺失市值回退策略开关(方便分别跑新旧逻辑对比耗时):
#   "new"   = 逐股[先近730天, 空则全窗回到上市日], limit 阶梯控 payload、以 free 为准、尽量不放弃;
#             缺失股票由 resolve_missing_mv 线程池并发补齐
#   "legacy"= 旧的单股 730 天窗口回退(超 730 天取不到市值返回 None → 仅参与等权榜)
MV_RESOLVE_MODE = os.environ.get("SW_MV_RESOLVE_MODE", "new")
# 逐股残留并发的线程数(与区间行情拉取同量级; 并发把 N 次串行网络往返压到 ~N/workers 倍,
# 实测 2026-07 区间首查 mv 阶段 18.6s → ~2s)。曾评估过"批回填"方案, 实测在并发面前冗余/更差, 已移除
MV_RESOLVE_WORKERS = int(os.environ.get("SW_MV_RESOLVE_WORKERS", "8"))
# 逐股残留"全窗回到上市日"的下界: 早于所有 A 股上市日, 等价于查完全部上市期
_MV_LISTING_FLOOR = "19900101"

# 财务指标(VIP)批拉: 实测按 period 可一次返回全市场(20260331 共 6870 行、20250630 共 8080 行),
# limit 参数生效且如实截断(limit=5999 -> 5999 行), 分页循环直到不足一批; 每接口限流独立(同 7.5/s 节流)
FINA_FETCH_BATCH = 5999
# PE-TTM 报告期窗口: [date-24个月, date] 内所有季末(最多 8 期), 覆盖"最新期+去年年报+去年同季"全部组合
FINA_TTM_WINDOW_MONTHS = 24


def wrap_api_counter(pro) -> dict[str, int]:
    """包装 tushare pro 常用接口以统计调用次数, 返回按接口名计数的 dict"""
    counter: dict[str, int] = {}
    for name in (
        "stock_basic", "index_member_all", "daily", "daily_basic", "trade_cal",
        "dividend", "fina_indicator_vip",
    ):
        orig = getattr(pro, name)

        def make_wrapper(n: str, o):
            def wrapper(**kw):
                counter[n] = counter.get(n, 0) + 1
                return o(**kw)
            return wrapper

        setattr(pro, name, make_wrapper(name, orig))
    return counter


# 并发与限流: 本账号 5000 积分实测单接口 500 次/分钟(60 秒滚动窗口)。
# 逐日行情并发拉取时按固定速率平摊请求, 避免瞬时爆发与官方窗口微小不对齐触发 429。
MAX_DAILY_FETCH_WORKERS = 8   # 并发线程数
MAX_DAILY_FETCH_RATE = 7.5    # 请求开始速率上限(次/秒), 约 450 次/分钟, 留 10% 余量
DAILY_FETCH_RETRY = 3         # 单日失败重试次数(网络抖动/瞬时 429)

# 4.4.14 重整转增识别阈值: D3 = (pre_close_T/close_{T-1})×(1+每股送转) − (1−每股派现/close_{T-1})
# 超过该值判定"除权参考价偏离声明标准比率"(非标准除权=转增股部分对价转让不参与除权)。
# 依据(2026-08-24 全市场扫描 793 条送转实施): 普通送转噪声上界 3.02%(688597 库存股基数口径),
# 真案例景峰 +36.9%、华闻 +110%, 8% 阈值与噪声带分离充分(见 docs/sync_progress.md 4.4.14)
RESTRUCTURE_D3_THRESHOLD = float(os.environ.get("SW_RESTRUCTURE_D3_THRESHOLD", "0.08"))
# 4.4.14 重整转增处理方式(2026-08-24 定稿): **默认以官方 index_member_all 成分断点为准**——
# 官方已把"除权日退出指数、转增股本上市日次一交易日重新计入"编码为成分区间
# out_date=除权日 / in_date=重入日(实测景峰 20260311→0312、华闻 20260622→0623 逐日吻合),
# 项目 date-aware 成分机制(filter_stock_pool 的 not_member/left_mid_range)自动对齐, 无需额外剔除。
# 下方 D3 非标准除权识别保留为**储备开关**(默认关闭): 官方成分未编码的事件(历史缺口/未来漏标)
# 时置 SW_RESTRUCTURE_ENABLED=1 可启用兜底剔除与告警; 与官方编码结果一致、零冲突
RESTRUCTURE_ENABLED = os.environ.get("SW_RESTRUCTURE_ENABLED", "0") == "1"


def _to_float(v) -> float:
    """任意值安全转 float, 缺失/NaN/非有限值 → 0.0(供 dividend 记录字段提取)"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _calc_free_mv(
    close,
    free_share,
    float_share,
) -> float | None:
    """自由流通市值(万元) = free_share × close (自由流通股本×收盘价), 三字段须取自同一交易日

    等价于旧式 circ_mv × free_share / float_share (流通市值×自由流通占比, 恒等推导:
    circ_mv=float_share×close 时两式相等); 实测 daily_basic.close 与 daily.close 逐股完全一致,
    且此式即官方《编制说明》附录「自由流通市值 = 自由流通量 × 市价」的直接表述。
    **free_share > float_share 属 Tushare 自有口径(黑盒), 视为正常、直接采信 free_share×close**
    (实测 2026-07 多只股票长期 free>float, 如 001216 比例 1.43——无法获知背后扣减明细, 不排除不回退);
    仅字段缺失/非有限值或 close/free_share/float_share ≤ 0 时返回 None(该股不参与加权)
    """
    if pd.isna(close) or pd.isna(free_share) or pd.isna(float_share):
        return None
    close_f, free_f, float_f = float(close), float(free_share), float(float_share)
    if not (math.isfinite(close_f) and math.isfinite(free_f) and math.isfinite(float_f)):
        return None
    if close_f <= 0 or float_f <= 0 or free_f <= 0:
        return None
    return free_f * close_f


class MarketDataProvider:
    """行情/市值/交易日历数据层, 构造时包装 pro 并自带按日内存缓存。

    - 单实例缓存跨任务复用(Web 场景), 缓存按日期键分开、只增不减
    - 涨跌幅由 daily 的 close/pre_close(除权参考价口径)自行重算
    - pro 属性为已包装调用计数的实例, 行业树构建等也用它以便统一计数
    """

    def __init__(self, pro):
        self._counter: dict[str, int] = wrap_api_counter(pro)
        self.pro = pro  # 已包装计数器的 tushare pro api
        self.resolve_mode: str = MV_RESOLVE_MODE  # 缺失市值回退策略: "new"(默认) / "legacy"(对比旧逻辑用)
        self.ts_code_to_pct_chg_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股涨跌幅数据
        self.ts_code_to_close_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股收盘价数据
        self.ts_code_to_amount_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股成交额数据(千元)
        self.ts_code_to_free_mv_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股自由流通市值数据
        self.ts_code_to_total_mv_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股总市值数据
        self.ts_code_to_total_share_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股总股本(万股, 供 PB 净资产折算)
        self._ex_div_records_cache: dict[datetime, list[dict]] = {}  # 日期 -> dividend 当日全记录(除息+送转字段)
        self._fina_period_cache: dict[str, dict[str, tuple[str, float, float]]] = {}  # 报告期 -> ts_code -> (ann_date, 扣非净利润, 每股净资产bps)
        self._fina_per_stock_cache: dict[datetime, tuple[list[str], dict]] = {}  # 计算日 -> (报告期列表, 每股各期数据)
        self._ttm_cache: dict[datetime, tuple[dict[str, float], dict[str, int]]] = {}  # 计算日 -> (ttm, 统计)
        self._bps_cache: dict[datetime, tuple[dict[str, float], dict[str, int]]] = {}  # 计算日 -> (bps, 统计)
        self._restructure_identified: set[str] = set()  # 已做过 4.4.14 识别判定的日期(YYYYMMDD)
        self._restructure_windows: dict[str, tuple[str, str]] = {}  # ts_code -> (除权日, 转增股上市日(缺省=除权日))
        self._trade_cal_spans: list[tuple[str, str, list[str]]] = []  # 交易日历跨度缓存: (起, 止, 升序列表), 查询被包含时切片命中
        self._rate_slots: dict[str, list] = {}  # 接口名 -> [锁, 下一请求开始时刻]; 每接口独立 7.5/s 节流

    def _acquire_rate_slot(self, api_name: str) -> None:
        """按**接口独立**的请求开始速率节制: 每接口开始时刻按 MAX_DAILY_FETCH_RATE 平摊
        (≈450 次/分钟, 为 Tushare 每接口 500 次/分钟上限留 10% 余量)。
        **必须放在每个实际请求点**(全市场分页/停牌点查/并发补齐都走这里, 传对应接口名);
        同接口所有请求(批拉+点查+并发补齐)共享同一把节流锁、不各自为政;
        不同接口(Tushare 限额各自独立)可并行、互不等待——行情/市值/除息三池并发预取的前提
        """
        slot = self._rate_slots.get(api_name)
        if slot is None:
            slot = [threading.Lock(), [0.0]]
            self._rate_slots[api_name] = slot
        lock, next_start = slot
        with lock:
            wait = next_start[0] - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            next_start[0] = time.perf_counter() + 1.0 / MAX_DAILY_FETCH_RATE

    def snapshot_api_calls(self) -> dict[str, int]:
        """返回当前 API 调用计数快照(副本), 任务前后快照求差即任务实际调用"""
        return dict(self._counter)

    def get_ts_code_to_pct_chg(self, date: datetime) -> dict[str, float | None]:
        """获取某日的行情数据: ts_code -> 涨跌幅(%), 数据异常时为 None"""
        ts_code_to_pct_chg: dict[str, float | None] = self.ts_code_to_pct_chg_cache.get(date) or {}
        ts_code_to_close: dict[str, float] = self.ts_code_to_close_cache.get(date) or {}
        ts_code_to_amount: dict[str, float] = self.ts_code_to_amount_cache.get(date) or {}
        if ts_code_to_pct_chg and ts_code_to_close:
            return ts_code_to_pct_chg

        offset = 0
        batch_size = 5999
        date_str = date.strftime("%Y%m%d")
        while True:
            self._acquire_rate_slot("daily")
            df = self.pro.daily(trade_date=date_str, offset=offset, limit=batch_size)
            if len(df) == 0:
                break
            for row in df.itertuples(index=False):
                ts_code = row.ts_code
                pre_close = row.pre_close
                close = row.close
                if pd.isna(pre_close) or pd.isna(close):
                    warnings.warn(
                        f"跳过涨跌幅异常数据: {ts_code} {date_str} pre_close={pre_close} close={close}",
                        RuntimeWarning,
                    )
                    ts_code_to_pct_chg[ts_code] = None
                    continue
                pre_close_f = float(pre_close)
                close_f = float(close)
                if not (math.isfinite(pre_close_f) and pre_close_f > 0 and math.isfinite(close_f)):
                    warnings.warn(
                        f"跳过涨跌幅异常数据: {ts_code} {date_str} pre_close={pre_close} close={close}",
                        RuntimeWarning,
                    )
                    ts_code_to_pct_chg[ts_code] = None
                    continue
                pct_chg = (close_f - pre_close_f) / pre_close_f * 100
                ts_code_to_pct_chg[ts_code] = pct_chg
                ts_code_to_close[ts_code] = close_f
                amount = getattr(row, "amount", None)
                if amount is not None and not pd.isna(amount) and math.isfinite(float(amount)):
                    ts_code_to_amount[ts_code] = float(amount)

            offset += len(df)
            if batch_size > len(df):
                break

        if ts_code_to_pct_chg:
            self.ts_code_to_pct_chg_cache[date] = ts_code_to_pct_chg
            self.ts_code_to_close_cache[date] = ts_code_to_close
            self.ts_code_to_amount_cache[date] = ts_code_to_amount

        return ts_code_to_pct_chg

    def get_ts_code_to_close(self, date: datetime) -> dict[str, float]:
        """获取某日的收盘价数据: ts_code -> 收盘价"""
        self.get_ts_code_to_pct_chg(date)
        return self.ts_code_to_close_cache.get(date) or {}

    def get_ts_code_to_amount(self, date: datetime) -> dict[str, float]:
        """获取某日的成交额数据(千元): ts_code -> 成交额"""
        self.get_ts_code_to_pct_chg(date)
        return self.ts_code_to_amount_cache.get(date) or {}

    def get_ts_code_to_free_mv(self, date: datetime) -> dict[str, float]:
        """获取A股某日的自由流通市值数据: ts_code -> 自由流通市值(万元)

        自由流通市值 = free_share × close (自由流通股本×收盘价), 三字段取同一行(同一交易日),
        等价于 circ_mv × free_share / float_share;
        与总市值同一次请求拉取并缓存; 字段缺失/非正的股票不记入(股本比例越界视为正常, 见 _calc_free_mv)
        """
        ts_code_to_free_mv: dict[str, float] = self.ts_code_to_free_mv_cache.get(date) or {}
        if ts_code_to_free_mv:
            return ts_code_to_free_mv

        offset = 0
        batch_size = 5999  # 官方单次上限 6000, 留 1 余量; 全市场一次拉完
        date_str = date.strftime("%Y%m%d")
        ts_code_to_total_mv: dict[str, float] = {}
        ts_code_to_total_share: dict[str, float] = {}
        while True:
            self._acquire_rate_slot("daily_basic")
            df = self.pro.daily_basic(
                ts_code='',
                trade_date=date_str,
                fields='ts_code,close,total_mv,free_share,float_share,total_share',
                offset=offset,
                limit=batch_size,
            )
            for row in df.itertuples(index=False):
                ts_code = row.ts_code
                free_mv = _calc_free_mv(
                    row.close,
                    getattr(row, "free_share", None),
                    getattr(row, "float_share", None),
                )
                if free_mv is not None:
                    ts_code_to_free_mv[ts_code] = free_mv
                total_mv = getattr(row, "total_mv", None)
                if total_mv is not None and not pd.isna(total_mv):
                    ts_code_to_total_mv[ts_code] = float(total_mv)
                total_share = getattr(row, "total_share", None)
                if total_share is not None and not pd.isna(total_share) and float(total_share) > 0:
                    ts_code_to_total_share[ts_code] = float(total_share)  # 万股, 供 PB(H1财报净资产折算)

            offset += len(df)
            if batch_size > len(df):
                break

        if ts_code_to_free_mv:
            self.ts_code_to_free_mv_cache[date] = ts_code_to_free_mv
            self.ts_code_to_total_mv_cache[date] = ts_code_to_total_mv
            self.ts_code_to_total_share_cache[date] = ts_code_to_total_share

        return ts_code_to_free_mv

    def _fill_pct_cache_from_batch(self, day_str: str, data: dict[str, tuple[float, float]]) -> None:
        """把批拉行情回填 pct/close 缓存(与 get_ts_code_to_pct_chg 同一口径, 数据异常行已被跳过)

        键统一为 datetime(链式区间榜逐日复用时不重复发 daily 请求); 已有缓存不覆盖
        """
        day_dt = datetime.strptime(day_str, "%Y%m%d")
        if self.ts_code_to_pct_chg_cache.get(day_dt):
            return
        pct_map: dict[str, float | None] = {}
        close_map: dict[str, float] = {}
        for ts_code, (close, pre_close) in data.items():
            pct_map[ts_code] = (close - pre_close) / pre_close * 100
            close_map[ts_code] = close
        if pct_map:
            self.ts_code_to_pct_chg_cache[day_dt] = pct_map
            self.ts_code_to_close_cache[day_dt] = close_map

    def get_ts_code_to_total_mv(self, date: datetime) -> dict[str, float]:
        """获取A股某日的总市值数据: ts_code -> 总市值(与自由流通市值同一次请求拉取)"""
        self.get_ts_code_to_free_mv(date)
        return self.ts_code_to_total_mv_cache.get(date) or {}

    def get_ts_code_to_total_share(self, date: datetime) -> dict[str, float]:
        """获取A股某日的总股本数据(万股): ts_code -> 总股本(与自由流通/总市值同一次请求拉取)

        供 PB 净资产折算(净资产万元 = bps × 总股本万股); 停牌股由市值回退路径顺带补齐
        """
        self.get_ts_code_to_free_mv(date)
        return self.ts_code_to_total_share_cache.get(date) or {}

    @staticmethod
    def _fina_ann_date_floor(period: str) -> str:
        """报告期公告日缺失时的法定披露截止日推定(YYYYMMDD)

        Q1 -> 当年 04-30; 中报 -> 08-31; 三季报 -> 10-31; 年报 -> 次年 04-30
        """
        year = int(period[:4])
        month_day = period[4:]
        if month_day == "0331":
            return f"{year}0430"
        if month_day == "0630":
            return f"{year}0831"
        if month_day == "0930":
            return f"{year}1031"
        return f"{year + 1}0430"

    def _fetch_fina_period(self, period: str) -> dict[str, tuple[str, float, float]]:
        """按报告期拉全市场财务指标: 返回 {ts_code: (ann_date, profit_dedt, bps)}

        接口: fina_indicator_vip(period, fields='ts_code,ann_date,end_date,profit_dedt,bps'),
        offset 分页循环(单批 FINA_FETCH_BATCH=5999, 实测 limit 生效、全量 6870~8808 行)。
        **数据质量(实测)**: 同一股票同一报告期会返回**多行**(更新行与 NaN 行, 20250630 有 1598 只重复、
        416 行 profit_dedt 为 NaN)——去重为**字段级**独立取最后一条非空值: profit_dedt 与 bps 各有自身
        的最后非空(实测 601318 20260630 两行 bps 均有效但值不同 56.7800/56.7751, 差 0.009%,
        不能整行丢弃); ann_date 取最后一条的非空值。
        接口对 fields 中不存在的字段名**静默忽略**(不报错), 因此必须用 getattr 防御取值。
        profit_dedt 为**年初至今累计值**(实测 601318 五期 302.59/735.71/1420.57/1437.73/239.12 亿),
        不是单季值——TTM 换算见 get_ts_code_to_ttm_deducted_profit;
        bps 为**每股净资产(元)、报告期末时点值**(实测平安 5 期 51.60→56.78 递增), 供 PB 使用
        """
        cached = self._fina_period_cache.get(period)
        if cached is not None:
            return cached
        rows: dict[str, tuple[str, float, float]] = {}
        offset = 0
        while True:
            self._acquire_rate_slot("fina_indicator_vip")
            df = self.pro.fina_indicator_vip(
                period=period,
                fields="ts_code,ann_date,end_date,profit_dedt,bps",
                offset=offset,
                limit=FINA_FETCH_BATCH,
            )
            if df is None or len(df) == 0:
                break
            for row in df.itertuples(index=False):
                ts_code = str(row.ts_code)
                ann_date = str(getattr(row, "ann_date", None) or "")
                profit = getattr(row, "profit_dedt", None)
                bps = getattr(row, "bps", None)
                ann_old, profit_old, bps_old = rows.get(ts_code, ("", None, None))
                rows[ts_code] = (
                    ann_date or ann_old,
                    float(profit) if profit is not None and not pd.isna(profit) else profit_old,
                    float(bps) if bps is not None and not pd.isna(bps) else bps_old,
                )
            offset += len(df)
            if len(df) < FINA_FETCH_BATCH:
                break
        self._fina_period_cache[period] = rows
        return rows

    def _fina_per_stock(self, date: datetime) -> tuple[list[str], dict[str, dict[str, tuple[str, float, float]]]]:
        """拉取计算日 D 的报告期窗口数据并合并为每股各期: (报告期列表升序, {ts_code: {报告期: (ann_date, 扣非, bps)}})

        窗口 = [D-24个月, D] 内所有季末(最多 8 期), 覆盖 PE-TTM 所需"最新期+去年年报+去年同季";
        PB 只需最新期, 同窗口复用。按计算日缓存(报告期数据本身按 period 缓存跨天复用)
        """
        cached = self._fina_per_stock_cache.get(date)
        if cached is not None:
            return cached
        date_str = date.strftime("%Y%m%d")
        start_cut = f"{date.year - 2}{date.month:02d}{date.day:02d}"
        periods: list[str] = []
        for year in (date.year - 2, date.year - 1, date.year):
            for month_day in ("0331", "0630", "0930", "1231"):
                period = f"{year}{month_day}"
                if start_cut <= period <= date_str:
                    periods.append(period)
        per_stock: dict[str, dict[str, tuple[str, float, float]]] = {}
        for period in periods:
            for ts_code, record in self._fetch_fina_period(period).items():
                per_stock.setdefault(ts_code, {})[period] = record
        self._fina_per_stock_cache[date] = (periods, per_stock)
        return periods, per_stock

    def _fina_latest_period(self, by_period: dict[str, tuple[str, float, float]], date_str: str) -> str | None:
        """PIT 选取: 每股 ann_date <= D 的最大报告期(ann_date 缺失按法定披露截止日推定)"""
        latest: str | None = None
        for period in sorted(by_period):
            ann_date, _profit, _bps = by_period[period]
            if not ann_date:
                ann_date = self._fina_ann_date_floor(period)
            if ann_date <= date_str:
                latest = period  # 报告期升序, 取最后一个 = 最新期
        return latest

    def get_ts_code_to_ttm_deducted_profit(self, date: datetime) -> tuple[dict[str, float], dict[str, int]]:
        """获取各股票截至 date 的扣非净利润 TTM(元): (ttm_map, stats)

        ttm_map: ts_code -> TTM 扣非净利润(元); stats: {"periods", "stocks_standard",
        "stocks_annualized", "stocks_missing"} 全市场口径统计。

        时点正确性(PIT): 每股"最新期"取 ann_date <= date 的最大报告期——回看历史日期时只用
        当时已公开的财报, 消除前视偏差(实测 2025-04-07 当天全市场无一家公布 Q1'25, 全部落在年报);
        ann_date 缺失时按法定披露截止日推定(_fina_ann_date_floor), 实测批量接口 ann_date 无缺失。
        报告期窗口: [date-24个月, date] 内所有季末(最多 8 期), 覆盖"最新期+去年年报+去年同季"。

        TTM 规则(算法口径见 AGENTS.md 第 5.1 节与 docs/financial_indicators.md):
          1) 标准式: TTM = 扣非(最新期) + 扣非(去年年报) − 扣非(去年同季)   —— 利润字段为累计值
          2) 不足四期兜底(标准式算不出来, 如新股): TTM = 扣非(最新期) × 4/k,
             k = 最新报告期覆盖的季度数(Q1→1, 中报→2, 三季报→3, 年报→4)
        结果按计算日缓存, 报告期数据跨天复用(财务数据仅在财报季变化)
        """
        cached = self._ttm_cache.get(date)
        if cached is not None:
            return cached

        date_str = date.strftime("%Y%m%d")
        periods, per_stock = self._fina_per_stock(date)

        ttm_map: dict[str, float] = {}
        stats = {
            "periods": len(periods),
            "stocks_standard": 0,
            "stocks_annualized": 0,
            "stocks_missing": 0,
        }
        for ts_code, by_period in per_stock.items():
            latest_period = self._fina_latest_period(by_period, date_str)
            if latest_period is None:
                stats["stocks_missing"] += 1
                continue
            latest_profit = by_period[latest_period][1]
            if latest_profit is None:
                stats["stocks_missing"] += 1
                continue  # 记录存在但该期利润字段全为 NaN(字段级去重保留条目、值为 None)
            prev_year = str(int(latest_period[:4]) - 1)
            prev_annual = by_period.get(f"{prev_year}1231")
            prev_same = by_period.get(f"{prev_year}{latest_period[4:]}")
            if (
                prev_annual is not None and prev_annual[1] is not None
                and prev_same is not None and prev_same[1] is not None
            ):
                ttm_map[ts_code] = latest_profit + prev_annual[1] - prev_same[1]
                stats["stocks_standard"] += 1
            else:
                month_day = latest_period[4:]
                k = 4 if month_day == "1231" else (3 if month_day == "0930" else (2 if month_day == "0630" else 1))
                ttm_map[ts_code] = latest_profit * (4.0 / k)
                stats["stocks_annualized"] += 1

        self._ttm_cache[date] = (ttm_map, stats)
        return ttm_map, stats

    def get_ts_code_to_bps(self, date: datetime) -> tuple[dict[str, float], dict[str, int]]:
        """获取各股票截至 date 的每股净资产(元): (bps_map, stats)

        bps_map: ts_code -> 最新报告期 bps(每股净资产, 元); stats: {"periods",
        "stocks_with_bps", "stocks_missing"}。

        与 PE-TTM 同源同批拉取(fina_indicator_vip 的 bps 字段)、同一 PIT 规则
        (ann_date <= date 的最大报告期, 见 _fina_latest_period); **bps 是报告期末时点值,
        不是累计值**——无需 TTM 滚动、无"不足四期年化"兜底(新股仅一期也直接用其最新期)。
        PB 为时点口径: 行业 PB = Σ总市值 / Σ净资产, 净资产(万元) = bps × 总股本(万股)
        在 daily_pe_ttm/daily_pb 聚合时按当日股本折算(股本变动窗口为近似, 见 known_issues 第 37 条)
        """
        cached = self._bps_cache.get(date)
        if cached is not None:
            return cached

        date_str = date.strftime("%Y%m%d")
        periods, per_stock = self._fina_per_stock(date)

        bps_map: dict[str, float] = {}
        stats = {"periods": len(periods), "stocks_with_bps": 0, "stocks_missing": 0}
        for ts_code, by_period in per_stock.items():
            latest_period = self._fina_latest_period(by_period, date_str)
            if latest_period is None:
                stats["stocks_missing"] += 1
                continue
            bps = by_period[latest_period][2]
            if bps is None:
                stats["stocks_missing"] += 1
                continue
            bps_map[ts_code] = bps
            stats["stocks_with_bps"] += 1

        self._bps_cache[date] = (bps_map, stats)
        return bps_map, stats

    def _fetch_ex_div_records(self, date: datetime) -> list[dict]:
        """拉取并缓存 date 当日(ex_date==date) dividend 全量记录(除息+送转), 返回记录列表

        与除息识别共用同一次请求, 送转记录(4.4.14 重整转增识别)零额外接口成本;
        按日期内存缓存(单日一次 dividend 请求, wrapper 已计数)
        """
        cached = self._ex_div_records_cache.get(date)
        if cached is not None:
            return cached
        records: list[dict] = []
        self._acquire_rate_slot("dividend")
        df = self.pro.dividend(ex_date=date.strftime("%Y%m%d"))
        if df is not None and not df.empty:
            for r in df.itertuples(index=False):
                records.append(
                    {
                        "ts_code": r.ts_code,
                        "cash_div": _to_float(getattr(r, "cash_div", None)),
                        "cash_div_tax": _to_float(getattr(r, "cash_div_tax", None)),
                        "stk_div": _to_float(getattr(r, "stk_div", None)),
                        "stk_bo_rate": _to_float(getattr(r, "stk_bo_rate", None)),
                        "stk_co_rate": _to_float(getattr(r, "stk_co_rate", None)),
                        "div_proc": str(getattr(r, "div_proc", "") or ""),
                        "div_listdate": str(getattr(r, "div_listdate", "") or ""),
                    }
                )
        self._ex_div_records_cache[date] = records
        return records

    def get_ex_div_cash(self, date: datetime) -> dict[str, float]:
        """date 当日除息(ex_date==date)且每股现金分红>0 的股票: ts_code -> 每股派现(元)

        供"官方价格式"市值加权(单日榜, 自由流通/总市值): 除息日把 M_pre 覆盖为昨日实际市值时,
        需先识别当日除息股。现金分红兼容 cash_div / cash_div_tax 两字段
        (实测 688597.SH 派现填在 tax 字段, 主字段为 0)
        """
        result: dict[str, float] = {}
        for rec in self._fetch_ex_div_records(date):
            if rec["div_proc"] != "实施":
                continue  # 只认实施记录(dividend(ex_date=) 天然已过滤, 此处显式防御)
            cash = rec["cash_div"] or rec["cash_div_tax"] or 0.0
            if cash > 0:
                result[rec["ts_code"]] = float(cash)
        return result

    def _resolve_prev_close(self, ts_code: str, date: datetime) -> float | None:
        """取 ts_code 在 date 之前最近有效交易日的收盘价(T-1 停牌时逐日前推, 极限回到上市日)

        快路径: 命中已有 close 缓存(正常单日榜/链式榜流程中 T-1 常已缓存)零请求;
        慢路径: 按"近90天 → 全窗回到上市日"两级区间请求, 响应取 < date 的最近有行情行
        (停牌日无行自动跳过);
        仍无 → 返回 None(调用方跳过该候选并告警)
        """
        date_str = date.strftime("%Y%m%d")
        prev_days = [
            d
            for d in self.get_trading_days(
                (date - timedelta(days=12)).strftime("%Y%m%d"), date_str
            )
            if d < date_str
        ]
        if not prev_days:
            return None
        for day_str in reversed(prev_days):
            day_close = self.ts_code_to_close_cache.get(
                datetime.strptime(day_str, "%Y%m%d"), {}
            ).get(ts_code)
            if day_close is not None:
                return day_close
        for start_str in (
            (date - timedelta(days=90)).strftime("%Y%m%d"),
            _MV_LISTING_FLOOR,
        ):
            self._acquire_rate_slot("daily")
            df = self.pro.daily(ts_code=ts_code, start_date=start_str, end_date=prev_days[-1])
            if df is None or df.empty:
                continue
            before = df[df["trade_date"] < date_str]
            if before.empty:
                continue
            best = before.sort_values("trade_date").iloc[-1]
            close_v = float(best["close"])
            if math.isfinite(close_v) and close_v > 0:
                # 回填缓存(按交易日)供后续复用
                day_dt = datetime.strptime(str(best["trade_date"]), "%Y%m%d")
                self.ts_code_to_close_cache.setdefault(day_dt, {})[ts_code] = close_v
                return close_v
        return None

    def _ensure_restructure_identified(self, date: datetime) -> None:
        """识别 date 当日是否有 4.4.14 重整转增(非标准除权), 命中记入窗口表(每个事件日只判定一次)

        判据(见模块级常量注释): D3 = (pre_close_T/close_{T-1})×(1+每股送转) − (1−每股派现/close_{T-1})
        候选 = 当日 dividend 有送转记录(实施); D3 > 阈值 → 命中 → 窗口 [除权日, 上市日(缺省=除权日)]
        """
        key = date.strftime("%Y%m%d")
        if key in self._restructure_identified:
            return
        self._restructure_identified.add(key)
        for rec in self._fetch_ex_div_records(date):
            ts_code = rec["ts_code"]
            stk_total = rec["stk_div"] or (rec["stk_bo_rate"] + rec["stk_co_rate"])
            if stk_total <= 0 or rec["div_proc"] != "实施" or ts_code in self._restructure_windows:
                continue
            # 除权日=上市日=交易日, 应有行情; pre_close 从 pct/close 缓存反推
            pct = self.ts_code_to_pct_chg_cache.get(date, {}).get(ts_code)
            close_t = self.ts_code_to_close_cache.get(date, {}).get(ts_code)
            if pct is None or close_t is None:
                warnings.warn(
                    f"4.4.14 识别: {ts_code} {key} 当日无行情(异常, 跳过该候选)", RuntimeWarning
                )
                continue
            pre_close = close_t / (1.0 + pct / 100.0)
            close_prev = self._resolve_prev_close(ts_code, date)
            if close_prev is None:
                warnings.warn(
                    f"4.4.14 识别: {ts_code} {key} 前推至上市日仍无收盘价(跳过该候选)", RuntimeWarning
                )
                continue
            cash = rec["cash_div"] or rec["cash_div_tax"] or 0.0
            d3 = (pre_close / close_prev) * (1.0 + stk_total) - (1.0 - cash / close_prev)
            if d3 > RESTRUCTURE_D3_THRESHOLD:
                div_listdate = rec["div_listdate"] or key
                self._restructure_windows[ts_code] = (key, div_listdate)
                print(
                    f"⚠️ 识别到重整转增(官方4.4.14): {ts_code} 除权日={key}"
                    f" 转增股本上市日={div_listdate} D3={d3*100:.2f}%"
                    f"(阈值{RESTRUCTURE_D3_THRESHOLD*100:.0f}%) → 除权日窗口剔除该股"
                )

    def get_restructure_excluded(self, date: datetime) -> set[str]:
        """返回 date 当日处于 4.4.14 重整转增剔除窗口的股票集合

        窗口 = [除权日, 转增股本上市日](含两端); 上市日次一交易日自动放行
        (重入日 M_pre=pre_close×q_t 与官方 4.4.1 回填数值相等, 无需特殊逻辑)。
        仅供 filter_stock_pool 的 restructure_window 剔除类别使用。
        **储备功能、默认关闭**: 4.4.14 实际以官方 index_member_all 成分断点为准(见模块常量注释),
        仅 SW_RESTRUCTURE_ENABLED=1 时启用识别兜底
        """
        if not RESTRUCTURE_ENABLED:
            return set()
        self._ensure_restructure_identified(date)
        key = date.strftime("%Y%m%d")
        return {
            ts_code for ts_code, (ex_date, div_listdate) in self._restructure_windows.items()
            if ex_date <= key <= div_listdate
        }

    def _resolve_mvs_in_window(
        self,
        ts_code: str,
        date: datetime,
        start_str: str,
        cancel_check: CancelCheck | None,
        fast_limits: tuple[int, ...] | None = None,
    ) -> tuple[float | None, float | None, float | None]:
        """在 [start_str, date] 窗口内取停牌前最近有效自由流通市值、总市值与总股本

        响应按 trade_date 降序(实测验证)。fast_limits 提供"阶梯式"取最近 N 行试命中(极小 payload,
        按 5 -> 100 逐级放大), 任何一级命中**自由流通市值**即返回(以 free 为准, 避免 total 命中却
        漏掉 free——free 缺失行(字段缺失/非正)需向前找正常行, 比例越界行现视为正常、直接采信);
        全部未命中才同一窗口全量扫描。free 是决定性字段。
        自由流通市值要求 close/free_share/float_share 取自同一行(同一交易日)计算,
        避免混搭不同日期的股本; 总市值与总股本(万股, 供 PB 净资产折算)取同一行、可独立取最近有效值
        """
        if cancel_check is not None:
            cancel_check()

        def _scan(rows_limit: int | None) -> tuple[float | None, float | None, float | None, bool]:
            kw: dict[str, object] = {
                "ts_code": ts_code,
                "fields": "trade_date,close,total_mv,free_share,float_share,total_share",
                "start_date": start_str,
                "end_date": date.strftime("%Y%m%d"),
            }
            if rows_limit is not None:
                kw["limit"] = rows_limit
            self._acquire_rate_slot("daily_basic")
            df = self.pro.daily_basic(**kw)
            free: float | None = None
            total: float | None = None
            total_share: float | None = None
            for row in df.itertuples(index=False):
                if cancel_check is not None:
                    cancel_check()
                if datetime.strptime(row.trade_date, "%Y%m%d") > date:
                    continue
                if free is None:
                    free = _calc_free_mv(
                        row.close,
                        getattr(row, "free_share", None),
                        getattr(row, "float_share", None),
                    )
                if total is None:
                    cand = getattr(row, "total_mv", None)
                    if cand is not None and not pd.isna(cand):
                        total = float(cand)
                if total_share is None:
                    cand_share = getattr(row, "total_share", None)
                    if cand_share is not None and not pd.isna(cand_share) and float(cand_share) > 0:
                        total_share = float(cand_share)  # 万股, 与 total 同行同口径
                if free is not None and total is not None and total_share is not None:
                    break
            return free, total, total_share, bool(len(df))

        if fast_limits:
            for n in fast_limits:
                free, total, total_share, has_rows = _scan(n)
                if not has_rows:
                    # 该窗口本轮无任何行: 放大 limit / 全扫同样为空, 直接结束(缩短深停牌空探测)
                    return None, None, None
                if free is not None:
                    return free, total, total_share
        free, total, total_share, _ = _scan(None)
        return free, total, total_share

    def _resolve_mvs(
        self,
        ts_code: str,
        date: datetime,
        cancel_check: CancelCheck | None,
    ) -> tuple[float | None, float | None, float | None]:
        """旧逻辑(legacy, 保留以对比耗时): 一次请求查 730 天内最近的有效自由流通市值与总市值, 查不到返回 None

        最多支持连续停牌约 2 年(730 天); 超长停牌取不到 → None → 仅参与等权榜
        """
        start_str = (date - timedelta(days=730)).strftime("%Y%m%d")
        return self._resolve_mvs_in_window(ts_code, date, start_str, cancel_check, fast_limits=None)

    def _resolve_mvs_until_listing(
        self,
        ts_code: str,
        date: datetime,
        cancel_check: CancelCheck | None,
    ) -> tuple[float | None, float | None, float | None]:
        """新策略逐股残留: 先近 730 天快路径(limit=5 极小 payload), 拿不到**自由流通市值**再全窗回到上市日
        (几乎不放弃股票; 以 free 为准, 避免 total 命中却跳过上市日导致 free 被放弃)

        全窗口 [_MV_LISTING_FLOOR(早于所有 A 股上市日), date] 一次请求、降序取最近有效行;
        只有整个上市期都没有 daily_basic 数据时才返回 (None, None, None)(→ 仅参与等权榜并告警)
        """
        free, total, total_share = self._resolve_mvs_in_window(
            ts_code, date, (date - timedelta(days=730)).strftime("%Y%m%d"), cancel_check,
            fast_limits=(1, 100),
        )
        if free is not None:
            return free, total, total_share
        return self._resolve_mvs_in_window(
            ts_code, date, _MV_LISTING_FLOOR, cancel_check, fast_limits=(1, 100)
        )

    def resolve_missing_mv(
        self,
        missing: list[str],
        date: datetime,
        cancel_check: CancelCheck | None = None,
    ) -> None:
        """并发解析 missing 名单缺失的自由流通/总市值并写入 date 缓存(供调用方随后命中)

        用线程池(MV_RESOLVE_WORKERS)并发逐股 _resolve_mvs_until_listing(近730天→全窗回上市日,
        以 free 为准、尽量不放弃), 把 N 次串行网络往返压到 ~N/workers 倍
        (实测 2026-07 区间首查 mv 阶段 18.6s → ~2s)。legacy 模式下不改变行为(由调用方按旧 730 resolve)。
        """
        if self.resolve_mode == "legacy":
            return
        missing = [c for c in missing if c]
        if not missing:
            return
        target_free = self.ts_code_to_free_mv_cache.setdefault(date, {})
        target_total = self.ts_code_to_total_mv_cache.setdefault(date, {})
        target_total_share = self.ts_code_to_total_share_cache.setdefault(date, {})
        with ThreadPoolExecutor(max_workers=MV_RESOLVE_WORKERS) as executor:
            futures = {
                executor.submit(self._resolve_mvs_until_listing, c, date, cancel_check): c
                for c in sorted(set(missing))
            }
            for future in as_completed(futures):
                c = futures[future]
                free, total, total_share = future.result()
                if free is not None:
                    target_free[c] = free
                if total is not None:
                    target_total[c] = total
                if total_share is not None:
                    target_total_share[c] = total_share

    def resolve_free_mv(
        self,
        ts_code: str,
        date: datetime,
        cancel_check: CancelCheck | None = None,
    ) -> float | None:
        """停牌股自由流通市值回退: **优先读当日缓存**(全市场拉取/并发补齐已填充, 同日期重复调用零请求),
        未命中再点查; 一次请求同时回退自由流通市值与总市值(均写回缓存), 返回自由流通市值
        resolve_mode=new 时逐股走 _resolve_mvs_until_listing(回到上市日), legacy 走旧 730 天逻辑
        """
        cached = self.ts_code_to_free_mv_cache.get(date, {}).get(ts_code)
        if cached is not None:
            return cached
        if self.resolve_mode == "legacy":
            free, total, total_share = self._resolve_mvs(ts_code, date, cancel_check)
        else:
            free, total, total_share = self._resolve_mvs_until_listing(ts_code, date, cancel_check)
        if total is not None:
            self.ts_code_to_total_mv_cache.setdefault(date, {})[ts_code] = total
        if total_share is not None:
            self.ts_code_to_total_share_cache.setdefault(date, {})[ts_code] = total_share
        if free is not None:
            self.ts_code_to_free_mv_cache.setdefault(date, {})[ts_code] = free
        return free

    def resolve_total_mv(
        self,
        ts_code: str,
        date: datetime,
        cancel_check: CancelCheck | None = None,
    ) -> float | None:
        """停牌股总市值回退: 优先读缓存(自由流通市值回退/并发补齐已顺带填充), 未命中再发请求"""
        cached = self.ts_code_to_total_mv_cache.get(date, {}).get(ts_code)
        if cached is not None:
            return cached
        if self.resolve_mode == "legacy":
            free, total, total_share = self._resolve_mvs(ts_code, date, cancel_check)
        else:
            free, total, total_share = self._resolve_mvs_until_listing(ts_code, date, cancel_check)
        if total is not None:
            self.ts_code_to_total_mv_cache.setdefault(date, {})[ts_code] = total
        if total_share is not None:
            self.ts_code_to_total_share_cache.setdefault(date, {})[ts_code] = total_share
        return total

    def get_trading_days(self, start_str: str, end_str: str) -> list[str]:
        """获取区间内交易日列表(YYYYMMDD, 升序)

        跨度包含缓存: 已请求过的更宽区间(如链式区间榜预取的 区间±12 天)可被任意子区间切片命中,
        避免除息日 12 天窗口等高频小查询重复请求 trade_cal
        """
        for span_start, span_end, days in self._trade_cal_spans:
            if span_start <= start_str and end_str <= span_end:
                left = bisect.bisect_left(days, start_str)
                right = bisect.bisect_right(days, end_str)
                return days[left:right]
        df = self.pro.trade_cal(
            exchange='SSE',
            start_date=start_str,
            end_date=end_str,
            is_open='1',
            fields='cal_date',
        )
        result = sorted(df['cal_date'].astype(str).tolist())
        self._trade_cal_spans.append((start_str, end_str, result))
        return result

    def fetch_daily_by_date(
        self,
        date_str: str,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, tuple[float, float]]:
        """按交易日拉全市场 daily, 返回 ts_code -> (close, pre_close), 跳过异常数据"""
        result: dict[str, tuple[float, float]] = {}
        offset = 0
        batch_size = 5999
        while True:
            if cancel_check is not None:
                cancel_check()
            self._acquire_rate_slot("daily")
            df = self.pro.daily(
                trade_date=date_str,
                offset=offset,
                limit=batch_size,
                fields='ts_code,close,pre_close',
            )
            if len(df) == 0:
                break
            for row in df.itertuples(index=False):
                ts_code = row.ts_code
                pre_close = row.pre_close
                close = row.close
                if pd.isna(pre_close) or pd.isna(close):
                    warnings.warn(
                        f"跳过涨跌幅异常数据: {ts_code} {date_str} pre_close={pre_close} close={close}",
                        RuntimeWarning,
                    )
                    continue
                pre_close_f = float(pre_close)
                close_f = float(close)
                if not (math.isfinite(pre_close_f) and pre_close_f > 0 and math.isfinite(close_f)):
                    warnings.warn(
                        f"跳过涨跌幅异常数据: {ts_code} {date_str} pre_close={pre_close} close={close}",
                        RuntimeWarning,
                    )
                    continue
                result[ts_code] = (close_f, pre_close_f)

            offset += len(df)
            if batch_size > len(df):
                break

        return result

    def fetch_daily_batch(
        self,
        trading_days: list[str],
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, dict[str, tuple[float, float]]]:
        """
        并发拉取多日 daily, 返回 {日期: {ts_code: (close, pre_close)}}

        - 线程池并发; **请求速率由全局节流器 _acquire_rate_slot 在 fetch_daily_by_date 的实际请求点平摊**
          (与 daily_basic 点查/并发补齐共用同一把锁, 全进程 ≤450 次/分钟, 不会触发 Tushare 500 次/分钟上限)
        - 单日失败自动重试 DAILY_FETCH_RETRY 次, 仍失败则抛错(不静默改变结果)
        """
        results: dict[str, dict[str, tuple[float, float]]] = {}
        total = len(trading_days)
        completed = 0

        def fetch_one(day_str: str) -> tuple[str, dict[str, tuple[float, float]]]:
            if cancel_check is not None:
                cancel_check()
            last_err: Exception | None = None
            for attempt in range(1, DAILY_FETCH_RETRY + 1):
                try:
                    return day_str, self.fetch_daily_by_date(day_str, cancel_check)
                except Exception as err:
                    last_err = err
                    time.sleep(0.5 * attempt)
            raise RuntimeError(
                f"拉取 {day_str} 行情连续失败 {DAILY_FETCH_RETRY} 次: {last_err}"
            )

        with ThreadPoolExecutor(max_workers=MAX_DAILY_FETCH_WORKERS) as executor:
            for day_str, data in executor.map(fetch_one, trading_days):
                if cancel_check is not None:
                    cancel_check()
                results[day_str] = data
                completed += 1
                if progress_callback is not None:
                    pct = completed / total * 100.0 if total else 100.0
                    progress_callback(pct, f"已拉取 {completed}/{total} 个交易日行情")

        # 回填当日 pct/close 缓存: 链式区间榜逐日调用 get_ts_code_to_pct_chg 时零额外请求
        for day_str, data in results.items():
            self._fill_pct_cache_from_batch(day_str, data)
        return results

    def fetch_mv_batch(
        self,
        trading_days: list[str],
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> None:
        """预拉区间内每日全市场 daily_basic(自由流通/总市值同请求双缓存), 后续逐日调用零请求

        线程池并发; 请求速率由全局节流器在 get_ts_code_to_free_mv 的实际请求点平摊
        (与 _scan 点查共用同一把锁); 已有缓存的日期跳过。链式区间榜逐日算权重前调一次即可
        """
        days = [
            d
            for d in trading_days
            if not self.ts_code_to_free_mv_cache.get(datetime.strptime(d, "%Y%m%d"))
        ]
        if not days:
            return
        total = len(days)
        completed = 0

        def fetch_mv(day_str: str) -> str:
            if cancel_check is not None:
                cancel_check()
            last_err: Exception | None = None
            for attempt in range(1, DAILY_FETCH_RETRY + 1):
                try:
                    self.get_ts_code_to_free_mv(datetime.strptime(day_str, "%Y%m%d"))
                    return day_str
                except Exception as err:
                    last_err = err
                    time.sleep(0.5 * attempt)
            raise RuntimeError(
                f"拉取 {day_str} 每日市值连续失败 {DAILY_FETCH_RETRY} 次: {last_err}"
            )

        with ThreadPoolExecutor(max_workers=MAX_DAILY_FETCH_WORKERS) as executor:
            for day_str in executor.map(fetch_mv, days):
                if cancel_check is not None:
                    cancel_check()
                completed += 1
                if progress_callback is not None:
                    pct = completed / total * 100.0 if total else 100.0
                    progress_callback(pct, f"已拉取 {completed}/{total} 个交易日市值")

    def fetch_ex_div_batch(
        self,
        trading_days: list[str],
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> None:
        """预取区间内每日除息识别(dividend 按 ex_date 缓存填充), 后续逐日调用零请求

        线程池并发; 请求速率由全局节流器在 get_ex_div_cash 的实际请求点平摊;
        已有缓存(含空结果)的日期跳过
        """
        days = [
            d
            for d in trading_days
            if self._ex_div_records_cache.get(datetime.strptime(d, "%Y%m%d")) is None
        ]
        if not days:
            return
        total = len(days)
        completed = 0

        def fetch_ex(day_str: str) -> str:
            if cancel_check is not None:
                cancel_check()
            last_err: Exception | None = None
            for attempt in range(1, DAILY_FETCH_RETRY + 1):
                try:
                    self.get_ex_div_cash(datetime.strptime(day_str, "%Y%m%d"))
                    return day_str
                except Exception as err:
                    last_err = err
                    time.sleep(0.5 * attempt)
            raise RuntimeError(
                f"拉取 {day_str} 除息识别连续失败 {DAILY_FETCH_RETRY} 次: {last_err}"
            )

        with ThreadPoolExecutor(max_workers=MAX_DAILY_FETCH_WORKERS) as executor:
            for day_str in executor.map(fetch_ex, days):
                if cancel_check is not None:
                    cancel_check()
                completed += 1
                if progress_callback is not None:
                    pct = completed / total * 100.0 if total else 100.0
                    progress_callback(pct, f"已拉取 {completed}/{total} 个交易日除息识别")
