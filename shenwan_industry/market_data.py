"""
申万行业行情数据层 (MarketDataProvider)

- 行情/市值获取: daily / daily_basic, 带按日内存缓存(涨跌幅口径见 shenwan_industry/AGENTS.md 第 3 节)
- 财务指标: fina_indicator_vip 按报告期全市场批拉, 一次同取扣非净利润(profit_dedt)、非经常性损益
  (extra_item)与每股净资产(bps); **归母净利润 = profit_dedt + extra_item 行内合成**(恒等式经全市场
  实测验证, 见 _fetch_fina_period), 供单日榜 PE(列名"PE"): 归母-TTM 口径 get_ts_code_to_ttm_attr_profit,
  扣非-TTM 口径 get_ts_code_to_ttm_deducted_profit——两法 PIT 同为 ann_date <= 计算日, 累计值口径
  与 TTM 规则一致; **动态口径** get_ts_code_to_dynamic_profit(最新期累计 × 4/k 年化, 归母/扣非两档)
  与 TTM 同批数据零新增请求
- 归母普通股股东权益: balancesheet_vip 按报告期批拉(归母权益−其他权益工具[已含优先股] 行内合成,
  **权威绝对额**、与 bps 分子同口径, 见 _fetch_bs_period), 供单日榜 PB 分母(get_ts_code_to_equity);
  与 fina 池**并行预热**(prefetch_fina_indicators 双池并发、接口限流独立); 旧 bps×当日股本
  口径保留对照(get_ts_code_to_bps)
- 业绩快报(express_vip)第三池: 归母净利润的**提前可用源**——报告期值若快报已发布(ann_date 更早)
  则在年报披露前以快报值参与 PE(归母口径, PIT 合并规则见 _merge_attr_with_express: 审定值优先、
  快报兑现、快报失败退回纯财报), 三池并行预热; 扣非口径与 PB 无快报、不受影响
- 净利润同比(列名"净利润同比"): TTM 口径 get_ts_code_to_ttm_growth_pair 给出(当期, 基期=D-1年)
  TTM 对, 基期走同一机制(窗口自动扩至 [D-36月, D-12月], 预热串行补拉); **动态口径**
  get_ts_code_to_dynamic_growth_pair = 最新期累计 vs 去年同季累计(相位严格对齐; 去年同季优先取
  主窗口 D 视角审定值、停披超一年的股票回落基期窗口兜底, 零新增请求; 不用"动态(D)/动态(D-1年)"
  ——两时点最新期披露节奏可能错位引入失真, 见 docs/financial_indicators.md); 数值/四类显示判定在
  industry_ranking.classify_profit_growth
- ROE(加权平均算法): get_ts_code_to_roes 一次算出四口径(归母/扣非 × TTM/动态)——数据锚为
  fina_indicator_vip 同批带回的披露值 roe_waa(9 号规则加权 ROE), 官方加权分母由 E_waa=归母×100/roe_waa
  反推, TTM 分母分段推导 E_TTM=E_waa(A)+(E_waa(P)-E_waa(S))/2; 全链不接业绩快报, roe_waa 缺失
  四口径全部降级"--"(详见 get_ts_code_to_roes 与 docs/financial_indicators.md 第 6 节)
- 停牌自由流通市值回退: 新策略逐股[近730天 → 全窗回到上市日](limit 阶梯控 payload、以 free 为准),
  缺失股票由 resolve_missing_mv 线程池并发补齐; legacy 730 天逻辑保留(见 resolve_* 与
  shenwan_industry/AGENTS.md 第 5 节)
- 交易日历: trade_cal
- 区间逐日行情: 并发拉取 + 固定速率限流 + 重试
- API 调用计数: 构造时包装 pro, snapshot_api_calls() 取快照,
  任务前后快照求差即该任务实际调用次数(缓存命中不计)
"""

import bisect
import logging
import math
import os
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd

try:
    from .dividend_data import DividendHistory, compute_dividend_dps
    from .share_change_data import RepurchaseHistory, ShareChangeHistory, compute_buyback_amount
except ImportError:  # 直接运行本文件时
    from dividend_data import DividendHistory, compute_dividend_dps
    from share_change_data import RepurchaseHistory, ShareChangeHistory, compute_buyback_amount

logger = logging.getLogger("shenwan_industry.market_data")

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

# 财务指标(VIP)批拉: 实测按 period 全量单期 6870~8808 行; limit 参数生效且上限远高于 daily(实测
# limit=9999/20000 均整批返回无截断、单次 8000+ 行正常), 取 9999 使每期一页(8 期 8 次请求),
# 仍保留分页循环兜底(未来单期超 9999 行时自动翻页); 每接口限流独立(同 7.5/s 节流)
FINA_FETCH_BATCH = 9999
# 财务指标批拉的并发线程数: 各期请求经同一节流器按 7.5/s 错开开始时刻、网络往返并行重叠,
# 8 期总时长 ≈ 限速 8×0.133s + 单次往返(实测 ~1.4s, 串行 ~3.5s); 请求速率上限仍由节流器统一控制
FINA_FETCH_WORKERS = 8
# 资产负债表(VIP)批拉单批行数: 实测 balancesheet_vip 按 period 整批单期 6927 行(20250630)、
# limit=9999 单页回全量无截断; 每期一页, 保留分页循环兜底; 与 fina 池并发时各自独立节流(7.5/s)
BS_FETCH_BATCH = 9999
# 业绩快报(VIP)批拉单批行数: 实测 express_vip 按 period 单期 1409 行(20241231, 覆盖约 21% 的
# 公司)、limit=9999 单页回全量; 每期一页, 保留分页循环兜底; 与 fina/bs 池并发时独立节流(7.5/s)
EXPRESS_FETCH_BATCH = 9999
# PE/净利润同比 报告期窗口: [date-24个月, date] 内所有季末(最多 8 期), 覆盖"最新期+去年年报+去年同季"全部组合
FINA_TTM_WINDOW_MONTHS = 24


def wrap_api_counter(pro) -> dict[str, int]:
    """包装 tushare pro 常用接口以统计调用次数, 返回按接口名计数的 dict"""
    counter: dict[str, int] = {}
    for name in (
        "stock_basic", "index_member_all", "daily", "daily_basic", "trade_cal",
        "dividend", "fina_indicator_vip", "balancesheet_vip", "express_vip",
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
        self.ts_code_to_total_share_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股总股本(万股, 随 daily_basic 同请求缓存; PB 已改用 balancesheet_vip 权威净资产, 股本不再参与 PB)
        self._ex_div_records_cache: dict[datetime, list[dict]] = {}  # 日期 -> dividend 当日全记录(除息+送转字段)
        self._fina_period_cache: dict[str, dict[str, tuple[str, float, float, float, float]]] = {}  # 报告期 -> ts_code -> (ann_date, 扣非净利润, 归母净利润, 每股净资产bps, 加权ROE%)
        self._fina_per_stock_cache: dict[datetime, tuple[list[str], dict]] = {}  # 计算日 -> (报告期列表, 每股各期数据)
        self._ttm_cache: dict[datetime, tuple[dict[str, float], dict[str, int]]] = {}  # 计算日 -> (TTM扣非, 统计)——扣非-TTM 口径
        self._ttm_attr_cache: dict[datetime, tuple[dict[str, float], dict[str, int]]] = {}  # 计算日 -> (TTM归母, 统计)——归母-TTM 口径(PE 默认)
        self._attr_merged_cache: dict[datetime, tuple[dict[str, dict[str, tuple[str, float | None]]], int]] = {}  # 计算日 -> (归母双源合并视图, 快报参与数)——归母 TTM/动态/动态增长对共用
        self._dynamic_profit_cache: dict[tuple[datetime, str], tuple[dict[str, float], dict[str, int]]] = {}  # (计算日, 口径) -> (动态净利润, 统计)——动态口径 PE 分子
        self._growth_pair_cache: dict[tuple[datetime, str], tuple[dict[str, tuple[float, float]], dict[str, int]]] = {}  # (计算日, 口径) -> (TTM增长对, 统计)——净利润同比 TTM 口径
        self._dynamic_growth_pair_cache: dict[tuple[datetime, str], tuple[dict[str, tuple[float, float]], dict[str, int]]] = {}  # (计算日, 口径) -> (动态增长对, 统计)——净利润同比动态口径
        self._roe_cache: dict[datetime, tuple[dict[str, dict[str, tuple[float, float]]], dict[str, int]]] = {}  # 计算日 -> ({basis: {ts_code: (分子, 分母)}}, 统计)——ROE 四口径(加权平均算法)
        self._bps_cache: dict[datetime, tuple[dict[str, float], dict[str, int]]] = {}  # 计算日 -> (bps, 统计)——旧 PB 口径(保留对照)
        self._bs_period_cache: dict[str, dict[str, tuple[str, float]]] = {}  # 报告期 -> ts_code -> (ann_date, 归母普通股股东权益元)
        self._bs_per_stock_cache: dict[datetime, tuple[list[str], dict]] = {}  # 计算日 -> (报告期列表, 每股各期归母普通股股东权益)
        self._equity_cache: dict[datetime, tuple[dict[str, float], dict[str, int]]] = {}  # 计算日 -> (归母普通股股东权益, 统计)——PB 当前口径
        self._express_period_cache: dict[str, dict[str, list[tuple[str, float]]]] = {}  # 报告期 -> ts_code -> [(ann_date, 快报归母净利润元), ...]升序版本
        self._express_per_stock_cache: dict[datetime, tuple[list[str], dict]] = {}  # 计算日 -> (报告期列表, 每股各期快报版本)
        self._restructure_identified: set[str] = set()  # 已做过 4.4.14 识别判定的日期(YYYYMMDD)
        self._restructure_windows: dict[str, tuple[str, str]] = {}  # ts_code -> (除权日, 转增股上市日(缺省=除权日))
        self._trade_cal_spans: list[tuple[str, str, list[str]]] = []  # 交易日历跨度缓存: (起, 止, 升序列表), 查询被包含时切片命中
        self._rate_slots: dict[str, list] = {}  # 接口名 -> [锁, 下一请求开始时刻]; 每接口独立 7.5/s 节流
        self._dividend_history: DividendHistory | None = None  # 分红事件持久缓存(惰性单例, 文件落盘)
        self._div_dps_cache: dict[datetime, tuple[dict[str, float], dict[str, float], dict[str, int]]] = {}  # 计算日 -> (TTM估算DPS, 静态DPS, 统计)——股息率双口径
        self._share_change_history: ShareChangeHistory | None = None  # 股本台阶事件持久缓存(惰性单例, 文件落盘)
        self._repurchase_history: RepurchaseHistory | None = None  # 回购公告持久缓存(惰性单例, 台阶 vol 交叉验证用)
        self._ttm_window_cache: dict[datetime, tuple[dict[str, tuple[str, str]], dict[str, int]]] = {}  # 计算日 -> ({ts_code: (左开端, 右闭端)}, 统计)——每股归母TTM覆盖窗口(注销分量窗口用)
        self._bb_amount_cache: dict[datetime, tuple[dict[str, float], dict[str, int]]] = {}  # 计算日 -> (TTM窗口注销金额万元, 统计)——est_bb 口径分子
        self._index_weight_month_cache: dict[tuple[str, str], dict[str, set[str]]] = {}  # (index_code, YYYYMM) -> {快照日: 样本集}——样本空间月度快照缓存

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
        """获取某日的成交额数据(千元): ts_code -> 成交额

        通常随 get_ts_code_to_pct_chg 的全字段拉取顺带缓存; 但区间链式预取
        (fetch_daily_by_date) 只拉 close/pre_close 回填 pct 缓存, 该路径下 pct 缓存已满
        不会重拉——amount 仍空时单独拉一次全字段 daily 补齐(已缓存则零请求)
        """
        self.get_ts_code_to_pct_chg(date)
        cached = self.ts_code_to_amount_cache.get(date)
        if cached:
            return cached
        date_str = date.strftime("%Y%m%d")
        ts_code_to_amount: dict[str, float] = {}
        offset = 0
        while True:
            self._acquire_rate_slot("daily")
            df = self.pro.daily(trade_date=date_str, offset=offset, limit=5999)
            if len(df) == 0:
                break
            for row in df.itertuples(index=False):
                amount = getattr(row, "amount", None)
                if amount is not None and not pd.isna(amount) and math.isfinite(float(amount)):
                    ts_code_to_amount[str(row.ts_code)] = float(amount)
            offset += len(df)
            if len(df) < 5999:
                break
        self.ts_code_to_amount_cache[date] = ts_code_to_amount
        return ts_code_to_amount

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

    def _fetch_fina_period(self, period: str) -> dict[str, tuple[str, float, float, float, float]]:
        """按报告期拉全市场财务指标: 返回 {ts_code: (ann_date, 扣非净利润, 归母净利润, bps, roe_waa)}

        接口: fina_indicator_vip(period, fields='ts_code,ann_date,end_date,profit_dedt,extra_item,bps,roe_waa'),
        offset 分页循环(单批 FINA_FETCH_BATCH=9999, 实测整批可回全量 6870~8808 行)。
        **归母净利润为行内合成**: 利润表归母口径的绝对额不在本接口(n_income_attr_p 实测静默忽略),
        由恒等式 `归母 = profit_dedt + extra_item`(扣非 + 非经常性损益, 后者自带正负号)得出;
        全市场实测(20250630): 可对齐 6243 只、99.8% 相对误差 <0.1%(P95≈2e-16 浮点精度级)、
        有扣非时 extra_item 缺失率 0%。**只在同一行内两字段齐备时合成**, 否则该行归母=None,
        不跨行拼接(dedt/extra 各自最后非空来自不同行时宁缺)。
        **roe_waa(2026-08-28 新增)= 按证监会《编报规则第 9 号》披露的加权平均净资产收益率(%)**,
        报告期年初至今累计口径, 公司自行按 9 号公式(期初净资产+NP/2+按月加权的增发/回购/分红等
        事件项)计算——实测同请求带回零新增请求、20250630 全市场覆盖 98.2%; 供 ROE 指标
        (get_ts_code_to_roes)作披露锚与官方加权分母反推; 扣非口径加权 ROE 无披露字段
        (roe_kf 等实测被静默忽略)。
        **数据质量(实测)**: 同一股票同一报告期会返回**多行**(更新行与 NaN 行, 20250630 有 1598 只重复、
        416 行 profit_dedt 为 NaN)——去重为**字段级**独立取最后一条非空值: 扣非/归母/bps/roe_waa 各有
        自身的最后非空(实测 601318 20260630 两行 bps 均有效但值不同 56.7800/56.7751, 差 0.009%,
        不能整行丢弃; 实测 600036 20250630 一行 roe_waa 有效一行全 NaN, 字段级去重天然适配);
        ann_date 取最后一条的非空值。
        接口对 fields 中不存在的字段名**静默忽略**(不报错), 因此必须用 getattr 防御取值。
        利润字段均为**年初至今累计值**(实测 601318 五期 302.59/735.71/1420.57/1437.73/239.12 亿),
        不是单季值——TTM 换算见 get_ts_code_to_ttm_attr_profit(归母)/get_ts_code_to_ttm_deducted_profit(扣非);
        bps 为**每股净资产(元)、报告期末时点值**(实测平安 5 期 51.60→56.78 递增), 供 PB 使用
        """
        cached = self._fina_period_cache.get(period)
        if cached is not None:
            return cached
        rows: dict[str, tuple[str, float, float, float, float]] = {}
        offset = 0
        while True:
            self._acquire_rate_slot("fina_indicator_vip")
            df = self.pro.fina_indicator_vip(
                period=period,
                fields="ts_code,ann_date,end_date,profit_dedt,extra_item,bps,roe_waa",
                offset=offset,
                limit=FINA_FETCH_BATCH,
            )
            if df is None or len(df) == 0:
                break
            for row in df.itertuples(index=False):
                ts_code = str(row.ts_code)
                ann_date = str(getattr(row, "ann_date", None) or "")
                deduct = getattr(row, "profit_dedt", None)
                extra = getattr(row, "extra_item", None)
                bps = getattr(row, "bps", None)
                roe_waa = getattr(row, "roe_waa", None)
                # 行内合成归母: 两字段同一行齐备才算出(恒等式见 docstring), 缺一即 None 不猜
                if deduct is not None and not pd.isna(deduct) and extra is not None and not pd.isna(extra):
                    attr_profit = float(deduct) + float(extra)
                else:
                    attr_profit = None
                ann_old, deduct_old, attr_old, bps_old, roe_old = rows.get(
                    ts_code, ("", None, None, None, None)
                )
                rows[ts_code] = (
                    ann_date or ann_old,
                    float(deduct) if deduct is not None and not pd.isna(deduct) else deduct_old,
                    attr_profit if attr_profit is not None else attr_old,
                    float(bps) if bps is not None and not pd.isna(bps) else bps_old,
                    float(roe_waa) if roe_waa is not None and not pd.isna(roe_waa) else roe_old,
                )
            offset += len(df)
            if len(df) < FINA_FETCH_BATCH:
                break
        self._fina_period_cache[period] = rows
        return rows

    def _fetch_bs_period(self, period: str) -> dict[str, tuple[str, float]]:
        """按报告期拉全市场资产负债表: 返回 {ts_code: (ann_date, 归母普通股股东权益元)}

        接口: balancesheet_vip(period, fields='ts_code,ann_date,end_date,report_type,
        total_hldr_eqy_exc_min_int,oth_eqt_tools'), offset 分页循环
        (单批 BS_FETCH_BATCH=9999, 实测 20250630 整批 6927 行、单页回全量)。
        **PB 分母 = 归属于母公司普通股股东的权益(绝对额元, 报告期末时点值)**:
            普通股东权益 = total_hldr_eqy_exc_min_int(归母权益) − oth_eqt_tools(其他权益工具,
            主要为永续债/优先股)
        **oth_eqt_tools 为"其他权益工具合计"、已含优先股**(接口另有 oth_eqt_tools_p_shr
        "其中:优先股"为其子项, 不可重复扣减)。实测与 fina 的 bps 分子**严格同口径**
        (20250407 对账: 招商银行 12260−1804=10456亿 == bps×当日股本 10456亿 分毫不差;
        宁波银行 2332−248=2084亿 ≈ bps×股本 2082亿(0.1% 为可转债转股股本漂移); 华能国际/
        东方航空/大唐发电/深圳能源等永续债大户 (归母−oth)/bps 隐含股本与当日总股本吻合
        到 0.001%, 如华能 1374.1−801.7=572.4亿 == bps×当日总股本 572.4亿)——与 Tushare
        daily_basic.pb 及数据商惯例一致(普通股总市值对应普通股股东权益, 优先股/永续债
        持有人不分享); oth 缺失(NaN)按 0 处理, 首批缺列时告警(fields 静默忽略防御,
        此时退化为含其他权益工具口径)。
        **绝对额不经"每股×股本"折算**: 报告期后送转/增发/回购的股本变动、CDR 股本口径
        错配(实测九号公司 689009.SH: bps 分母与 daily_basic 总股本差 10×, 旧口径 PB 低估
        10 倍)、次新 bps 分母漂移均不再引入近似(旧 bps 口径的偏差见 known_issues 第 37 条)。
        report_type 实测不传时服务端默认只返回 '1'(合并报表, 20250630 全 6927 行均为 '1'),
        代码仍逐行过滤非 '1' 防御(调整/母公司报表类型混入时宁跳过该行)。
        **数据质量(实测, 与 fina 同模式)**: 同股票同报告期返回多行(20250630 有 611 只双行,
        update_flag 0/1、值相同), 去重为**字段级**各自取最后非空(合成后的普通股东权益值);
        普通股东权益为**行内合成**(归母/oth 同行齐备才算, oth 单缺按 0——0 是合法值且实测
        oth 缺失即真无永续债/优先股); ann_date 实测零缺失; fields 静默忽略须 getattr 防御;
        偶见畸形代码(实测 833243!1.BJ)不匹配任何股票池, 无害。PB 用法(PIT/无滚动)见
        get_ts_code_to_equity
        """
        cached = self._bs_period_cache.get(period)
        if cached is not None:
            return cached
        rows: dict[str, tuple[str, float]] = {}
        offset = 0
        while True:
            self._acquire_rate_slot("balancesheet_vip")
            df = self.pro.balancesheet_vip(
                period=period,
                fields="ts_code,ann_date,end_date,report_type,total_hldr_eqy_exc_min_int,oth_eqt_tools",
                offset=offset,
                limit=BS_FETCH_BATCH,
            )
            if df is None or len(df) == 0:
                break
            if offset == 0 and "oth_eqt_tools" not in df.columns:
                logger.warning("balancesheet_vip 未返回 oth_eqt_tools 字段(fields 被静默忽略?), PB 净资产将退化为含其他权益工具口径")
            for row in df.itertuples(index=False):
                report_type = str(getattr(row, "report_type", None) or "")
                if report_type and report_type != "1":
                    continue  # 只取合并报表(实测不传时服务端已默认 '1', 此为防御)
                ts_code = str(row.ts_code)
                ann_date = str(getattr(row, "ann_date", None) or "")
                eq_raw = getattr(row, "total_hldr_eqy_exc_min_int", None)
                if eq_raw is not None and not pd.isna(eq_raw):
                    oth = getattr(row, "oth_eqt_tools", None)
                    oth_v = float(oth) if oth is not None and not pd.isna(oth) else 0.0
                    equity = float(eq_raw) - oth_v
                else:
                    equity = None
                ann_old, equity_old = rows.get(ts_code, ("", None))
                rows[ts_code] = (
                    ann_date or ann_old,
                    equity if equity is not None else equity_old,
                )
            offset += len(df)
            if len(df) < BS_FETCH_BATCH:
                break
        self._bs_period_cache[period] = rows
        return rows

    def _fetch_express_period(self, period: str) -> dict[str, list[tuple[str, float]]]:
        """按报告期拉全市场业绩快报: 返回 {ts_code: [(ann_date, 快报归母净利润元), ...]}(ann_date 升序版本列表)

        接口: express_vip(period, fields='ts_code,ann_date,end_date,n_income'), offset 分页循环
        (单批 EXPRESS_FETCH_BATCH=9999, 实测 20241231 整批 1409 行、单页回全量)。
        **n_income 即归母净利润**(交易所快报模板口径; 实测对账 20241231: 招行快报 1483.91 亿与
        利润表归母分毫不差、与含少数 1495.59 亿不符; 全市场 1160 只与归母/含少数的偏差符号无系统性
        偏正——排除含少数口径), 单位元、年初至今累计值, **未经审计的初步数**(is_audit 实测 0 占
        1392/1402): 与年报审定值中位偏差 ~0.7%、43% 差 >1%、约 10% 差 >10%(减值/公允价值等审计
        时才定), 年报披露后由 PIT 合并规则自动切回审定值(见 _merge_attr_with_express)。
        **覆盖是部分的**(快报非强制披露): 实测 20241231 期 1403 只(占有财报股票约 21%)、
        20240630 期仅 100 只——集中在年报期, 非年报季本池基本沉默。
        **修正多行**: 同股票同报告期可有多行、ann_date 不同(实测 20241231 有 6 只真修正, 如
        601231 先发空值行后补全)——保留**多版本列表**按 ann_date ≤ D 选最新(fina 的同日双行
        去重模式不够用); n_income 为 NaN 的行直接丢弃(该版本无数值, 视为当时未提供)。
        n_income 列整体缺失(fields 被静默忽略)时告警并返回空(归母 TTM 退回纯财报口径)。
        用法与 PIT 合并见 get_ts_code_to_ttm_attr_profit / _merge_attr_with_express
        """
        cached = self._express_period_cache.get(period)
        if cached is not None:
            return cached
        rows: dict[str, list[tuple[str, float]]] = {}
        offset = 0
        while True:
            self._acquire_rate_slot("express_vip")
            df = self.pro.express_vip(
                period=period,
                fields="ts_code,ann_date,end_date,n_income",
                offset=offset,
                limit=EXPRESS_FETCH_BATCH,
            )
            if df is None or len(df) == 0:
                break
            if offset == 0 and "n_income" not in df.columns:
                logger.warning("express_vip 未返回 n_income 字段(fields 被静默忽略?), 归母 TTM 将退回纯财报口径")
                break
            for row in df.itertuples(index=False):
                ts_code = str(row.ts_code)
                ann_date = str(getattr(row, "ann_date", None) or "")
                value = getattr(row, "n_income", None)
                if not ann_date or value is None or pd.isna(value):
                    continue  # 无公告日/无数值版本丢弃
                entry = (ann_date, float(value))
                versions = rows.setdefault(ts_code, [])
                if entry not in versions:
                    versions.append(entry)
            offset += len(df)
            if len(df) < EXPRESS_FETCH_BATCH:
                break
        for versions in rows.values():
            versions.sort()
        self._express_period_cache[period] = rows
        return rows

    @staticmethod
    def _fina_period_window(date: datetime) -> list[str]:
        """财务报告期窗口: [D-24个月, D] 内所有季末(升序, 最多 8 期), fina/balancesheet 两池共用

        覆盖 PE TTM 式所需"最新期+去年年报+去年同季"; PB/动态口径只需最新期, 同窗口复用
        """
        date_str = date.strftime("%Y%m%d")
        start_cut = f"{date.year - 2}{date.month:02d}{date.day:02d}"
        periods: list[str] = []
        for year in (date.year - 2, date.year - 1, date.year):
            for month_day in ("0331", "0630", "0930", "1231"):
                period = f"{year}{month_day}"
                if start_cut <= period <= date_str:
                    periods.append(period)
        return periods

    def _fina_per_stock(self, date: datetime) -> tuple[list[str], dict[str, dict[str, tuple[str, float, float, float, float]]]]:
        """拉取计算日 D 的报告期窗口数据并合并为每股各期: (报告期列表升序, {ts_code: {报告期: (ann_date, 扣非, 归母, bps, roe_waa)}})

        窗口 = _fina_period_window([D-24个月, D] 季末, 最多 8 期), 覆盖 PE TTM 式所需"最新期+去年年报+去年同季";
        PB 只需最新期, 同窗口复用。按计算日缓存(报告期数据本身按 period 缓存跨天复用)
        """
        cached = self._fina_per_stock_cache.get(date)
        if cached is not None:
            return cached
        periods = self._fina_period_window(date)
        per_stock: dict[str, dict[str, tuple[str, float, float, float, float]]] = {}
        # 各期并发拉取: 请求开始时刻由节流器统一错开(7.5/s), 网络往返并行重叠(同 fetch_daily_batch 模式);
        # executor.map 结果按输入顺序产出(zip 回期号), 遇错即抛(与串行时一致, 由调用方降级处理), 不静默吞掉
        with ThreadPoolExecutor(max_workers=min(len(periods), FINA_FETCH_WORKERS)) as executor:
            for period, period_rows in zip(periods, executor.map(self._fetch_fina_period, periods)):
                for ts_code, record in period_rows.items():
                    per_stock.setdefault(ts_code, {})[period] = record
        self._fina_per_stock_cache[date] = (periods, per_stock)
        return periods, per_stock

    def _bs_per_stock(self, date: datetime) -> tuple[list[str], dict[str, dict[str, tuple[str, float]]]]:
        """拉取计算日 D 的资产负债表窗口数据并合并为每股各期: (报告期列表升序, {ts_code: {报告期: (ann_date, 归母普通股股东权益元)}})

        窗口与 _fina_per_stock 共用(_fina_period_window); PB 只需最新期, 同窗口复用。
        各期并发拉取(同 fina 模式: FINA_FETCH_WORKERS 线程、balancesheet_vip 独立节流, 两池可同时
        跑); 按计算日缓存, 报告期数据按 period 缓存跨天复用; executor.map 遇错即抛(由调用方降级)
        """
        cached = self._bs_per_stock_cache.get(date)
        if cached is not None:
            return cached
        periods = self._fina_period_window(date)
        per_stock: dict[str, dict[str, tuple[str, float]]] = {}
        with ThreadPoolExecutor(max_workers=min(len(periods), FINA_FETCH_WORKERS)) as executor:
            for period, period_rows in zip(periods, executor.map(self._fetch_bs_period, periods)):
                for ts_code, record in period_rows.items():
                    per_stock.setdefault(ts_code, {})[period] = record
        self._bs_per_stock_cache[date] = (periods, per_stock)
        return periods, per_stock

    def _express_per_stock(self, date: datetime) -> tuple[list[str], dict[str, dict[str, list[tuple[str, float]]]]]:
        """拉取计算日 D 的业绩快报窗口数据并合并为每股各期: (报告期列表升序, {ts_code: {报告期: 版本列表}})

        窗口与 _fina_per_stock 共用(_fina_period_window); 各期并发拉取(FINA_FETCH_WORKERS 线程、
        express_vip 独立节流, 三池可同时跑); 按计算日缓存, 报告期数据按 period 缓存跨天复用;
        executor.map 遇错即抛(由调用方降级——归母 getter 捕获后退回纯财报口径)
        """
        cached = self._express_per_stock_cache.get(date)
        if cached is not None:
            return cached
        periods = self._fina_period_window(date)
        per_stock: dict[str, dict[str, list[tuple[str, float]]]] = {}
        with ThreadPoolExecutor(max_workers=min(len(periods), FINA_FETCH_WORKERS)) as executor:
            for period, period_rows in zip(periods, executor.map(self._fetch_express_period, periods)):
                for ts_code, versions in period_rows.items():
                    per_stock.setdefault(ts_code, {})[period] = versions
        self._express_per_stock_cache[date] = (periods, per_stock)
        return periods, per_stock

    def prefetch_fina_indicators(self, date: datetime, growth_base_date: datetime | None = None) -> None:
        """后台预热财务数据批拉: fina_indicator_vip(利润/bps) / balancesheet_vip(归母净资产) /
        express_vip(业绩快报) 三池**并行**; growth_base_date 再预热 TTM 增长的基期(fina/express 池)

        三接口节流互相独立(Tushare 限额独立)、各自 8 期并发, 总墙时 ≈ 单池时长(不串行叠加,
        实测三池并行与原单池同量级); 供 run_daily_ranking 在市值/行情就绪后与六条涨幅序列计算
        **并行**运行(线程只写各自财务缓存、与市值缓存互不相交); 重复调用命中缓存立即返回;
        调用方应在 PE/PB 阶段 join 该线程。fina 池异常照旧向上抛(PE/PB 走既有"惰性重拉再失败
        才降级"路径); bs/express 池异常仅告警不影响其他池(线程隔离), PB/归母阶段惰性重拉。
        growth_base_date(=D-1年)的基期预热在主日期 fina 之后**串行**执行: 共享报告期命中
        period 级缓存、仅多拉更早 4 期(+8 次请求), 避免双线程并发双拉同一期; 基期只影响
        净利润同比列, 预热失败该列自行降级
        """
        extra_threads = [
            threading.Thread(target=self._safe_prefetch_bs, args=(date,), daemon=True),
            threading.Thread(target=self._safe_prefetch_express, args=(date,), daemon=True),
        ]
        for thread in extra_threads:
            thread.start()
        try:
            self._fina_per_stock(date)
            if growth_base_date is not None:
                self._fina_per_stock(growth_base_date)
                self._express_per_stock(growth_base_date)
        finally:
            for thread in extra_threads:
                thread.join()

    def _safe_prefetch_bs(self, date: datetime) -> None:
        """bs 池预热包装: 异常吞掉仅告警, 不波及并行运行的其他池(线程隔离)"""
        try:
            self._bs_per_stock(date)
        except Exception as err:
            logger.warning(f"balancesheet_vip 预热失败(PB 阶段将惰性重拉): {err!r}")

    def _safe_prefetch_express(self, date: datetime) -> None:
        """express 池预热包装: 异常吞掉仅告警, 不波及并行运行的其他池(线程隔离)"""
        try:
            self._express_per_stock(date)
        except Exception as err:
            logger.warning(f"express_vip 预热失败(归母 TTM 阶段将退回纯财报口径): {err!r}")

    def _fina_latest_period(self, by_period: dict[str, tuple], date_str: str) -> str | None:
        """PIT 选取: 每股 ann_date <= D 的最大报告期(ann_date 缺失按法定披露截止日推定)"""
        latest: str | None = None
        for period in sorted(by_period):
            ann_date = by_period[period][0]
            if not ann_date:
                ann_date = self._fina_ann_date_floor(period)
            if ann_date <= date_str:
                latest = period  # 报告期升序, 取最后一个 = 最新期
        return latest

    @staticmethod
    def _period_quarters(period: str) -> int:
        """报告期覆盖季度数 k(Q1→1, 中报→2, 三季报→3, 年报→4)——TTM 不足四期 4/k 年化与动态口径共用"""
        month_day = period[4:]
        if month_day == "1231":
            return 4
        if month_day == "0930":
            return 3
        if month_day == "0630":
            return 2
        return 1

    def _compute_ttm(
        self,
        per_stock: dict[str, dict[str, tuple]],
        value_idx: int,
        date_str: str,
        periods_count: int,
    ) -> tuple[dict[str, float], dict[str, int]]:
        """TTM 公共算法(扣非/归母两口径共用): 由每股各期数据滚动最近 12 个月净利润

        value_idx 为每股各期记录中利润字段的下标, 只换字段不换规则——扣非为 fina 4 元组的 1、
        归母为 fina 4 元组的 2(纯财报)或快报合并视图 2 元组的 1(见 _merge_attr_with_express)。
        时点正确性(PIT): 每股"最新期"取 ann_date <= date_str 的最大报告期——回看历史日期时只用
        当时已公开的财报, 消除前视偏差(实测 2025-04-07 当天全市场无一家公布 Q1'25, 全部落年报);
        ann_date 缺失按法定披露截止日推定(_fina_ann_date_floor), 实测批量接口 ann_date 无缺失。
        该最新期的利润字段为 None 时按无财报处理(stocks_missing), 不回看更早期(与原单口径行为一致)。

        TTM 规则(算法口径见 AGENTS.md 第 5.1 节与 docs/financial_indicators.md):
          1) 标准式: TTM = 利润(最新期) + 利润(去年年报) − 利润(去年同季)
             ——利润字段为年初至今累计值, 禁止对多期累计值直接求和(会放大 2~3 倍);
             最新期为年报时自动退化为年报值
          2) 不足四期兜底(去年年报/去年同季缺失, 如新股): TTM = 利润(最新期) × 4/k,
             k = 最新报告期覆盖的季度数(Q1→1, 中报→2, 三季报→3, 年报→4);
             系数按"报告期覆盖季度数"而非"历史期数"(年中起报的股票按 k 缩放更准);
             亏损股负值照此外推保留参与
        stats: {"periods", "stocks_standard", "stocks_annualized", "stocks_missing"} 全市场口径统计
        """
        ttm_map: dict[str, float] = {}
        stats = {
            "periods": periods_count,
            "stocks_standard": 0,
            "stocks_annualized": 0,
            "stocks_missing": 0,
        }
        for ts_code, by_period in per_stock.items():
            latest_period = self._fina_latest_period(by_period, date_str)
            if latest_period is None:
                stats["stocks_missing"] += 1
                continue
            latest_profit = by_period[latest_period][value_idx]
            if latest_profit is None:
                stats["stocks_missing"] += 1
                continue  # 记录存在但该期利润字段全为 NaN(字段级去重保留条目、值为 None)
            prev_year = str(int(latest_period[:4]) - 1)
            prev_annual = by_period.get(f"{prev_year}1231")
            prev_same = by_period.get(f"{prev_year}{latest_period[4:]}")
            if (
                prev_annual is not None and prev_annual[value_idx] is not None
                and prev_same is not None and prev_same[value_idx] is not None
            ):
                ttm_map[ts_code] = latest_profit + prev_annual[value_idx] - prev_same[value_idx]
                stats["stocks_standard"] += 1
            else:
                ttm_map[ts_code] = latest_profit * (4.0 / self._period_quarters(latest_period))
                stats["stocks_annualized"] += 1
        return ttm_map, stats

    def _merge_attr_with_express(
        self,
        fina_per_stock: dict[str, dict[str, tuple[str, float, float, float, float]]],
        express_per_stock: dict[str, dict[str, list[tuple[str, float]]]],
        date_str: str,
    ) -> tuple[dict[str, dict[str, tuple[str, float | None]]], int]:
        """归母净利润双源 PIT 合并: fina 财报(审定值) × express 业绩快报(提前可用), 供归母 TTM

        每股每报告期解析为 (合并可用日, 截至 D 的归母值):
        - 合并可用日 = min(财报可用日, 快报首版日)——"最新期"按此选取, 快报能把年报期提前到
          披露日之前(实测 2025-04-07: 快报已出 1323 只中 1054 只年报未披露, 这些股票的最新期
          从 Q3 提前到年报)
        - 值优先级: **审定值优先**——fina 已可用(ann_date ≤ D)且归母非空用 fina; 否则用快报
          ann_date ≤ D 的最新版本(修正多版本取最新); 再否则 None(fina 可用但归母为 None 的
          组合也回退快报值, 宁可用快报不判无财报)
        - 快报值为未审计初步数(与审定值中位差 ~0.7%、43% 差 >1%), 年报披露后自然切回审定值;
          TTM 三期(最新期/去年年报/去年同季)各自独立按此解析
        返回 (merged, 快报实际参与股票数)——后者为任一期取到快报值的股票数(观测统计用)
        """

        def _express_at_d(versions: list[tuple[str, float]] | None) -> tuple[str, float | None, float | None]:
            """解析快报: 返回 (首版日, ≤D 最新版本值, ≤D 最新版本日)"""
            if not versions:
                return "", None, None
            first_ann = versions[0][0]
            for ann, value in reversed(versions):
                if ann <= date_str:
                    return first_ann, value, ann
            return first_ann, None, None

        merged: dict[str, dict[str, tuple[str, float | None]]] = {}
        express_used: set[str] = set()
        for ts_code in set(fina_per_stock) | set(express_per_stock):
            f_by = fina_per_stock.get(ts_code) or {}
            e_by = express_per_stock.get(ts_code) or {}
            rows: dict[str, tuple[str, float | None]] = {}
            for period in set(f_by) | set(e_by):
                f_rec = f_by.get(period)
                f_attr = f_rec[2] if f_rec is not None else None
                fina_date = (f_rec[0] or self._fina_ann_date_floor(period)) if f_rec is not None else ""
                first_ann, e_value, _e_ann = _express_at_d(e_by.get(period))
                cand = [d for d in (fina_date, first_ann) if d]
                eff_ann = min(cand) if cand else self._fina_ann_date_floor(period)
                if fina_date and fina_date <= date_str and f_attr is not None:
                    attr: float | None = f_attr
                elif e_value is not None:
                    attr = e_value
                    express_used.add(ts_code)
                else:
                    attr = f_attr if (fina_date and fina_date <= date_str) else None
                rows[period] = (eff_ann, attr)
            merged[ts_code] = rows
        return merged, len(express_used)

    def _attr_merged_view(
        self, date: datetime
    ) -> tuple[dict[str, dict[str, tuple[str, float | None]]], int]:
        """归母净利润双源 PIT 合并视图(按计算日缓存): (merged, 快报实际参与股票数)

        供归母 TTM(get_ts_code_to_ttm_attr_profit)与归母动态口径(get_ts_code_to_dynamic_profit /
        get_ts_code_to_dynamic_growth_pair 当期侧)共用, 避免重复构建; 合并规则(合并可用日取
        min、审定值优先、快报取 ≤D 最新修正版本)见 _merge_attr_with_express; express_vip 拉取
        失败退回纯财报口径仅告警(归母 TTM 的既有行为)
        """
        cached = self._attr_merged_cache.get(date)
        if cached is not None:
            return cached
        date_str = date.strftime("%Y%m%d")
        _, fina_per_stock = self._fina_per_stock(date)  # 确保报告期已拉(重复调用命中缓存零成本)
        try:
            _, express_per_stock = self._express_per_stock(date)
        except Exception as err:
            logger.warning(f"express_vip 拉取失败, 归母口径本次退回纯财报: {err!r}")
            express_per_stock = {}
        merged, stocks_express = self._merge_attr_with_express(fina_per_stock, express_per_stock, date_str)
        self._attr_merged_cache[date] = (merged, stocks_express)
        return merged, stocks_express

    def get_ts_code_to_ttm_attr_profit(self, date: datetime) -> tuple[dict[str, float], dict[str, int]]:
        """获取各股票截至 date 的**归母净利润** TTM(元)——PE 归母-TTM 口径(默认): (ttm_map, stats)

        归母净利润为行内合成值(profit_dedt + extra_item, 恒等式与实测覆盖率见 _fetch_fina_period),
        单位元、年初至今累计值; **业绩快报(express_vip)提前可用源**: 报告期值经
        _merge_attr_with_express 双源 PIT 合并——快报已发布(ann_date 更早)则年报披露前以快报
        归母值参与(审定值优先、快报兑现、快报拉取失败退回纯财报口径仅告警)。
        PIT 规则与报告期窗口([date-24个月, date] 季末)见 _compute_ttm; TTM 标准式与不足四期
        兜底同样见 _compute_ttm(合并视图 2 元组 value_idx=1)。
        结果按计算日缓存(_ttm_attr_cache, 与扣非口径分开), 报告期数据跨天复用。
        stats 在 _compute_ttm 四键之外加 "stocks_express"(任一期取到快报值的股票数)。
        聚合公式见 industry_ranking.daily_valuation_metric(kind="pe") 与 docs/financial_indicators.md 第 3 节
        """
        cached = self._ttm_attr_cache.get(date)
        if cached is not None:
            return cached

        periods, _ = self._fina_per_stock(date)
        merged, stocks_express = self._attr_merged_view(date)
        result = self._compute_ttm(merged, 1, date.strftime("%Y%m%d"), len(periods))
        result[1]["stocks_express"] = stocks_express
        self._ttm_attr_cache[date] = result
        return result

    @staticmethod
    def growth_base_date(date: datetime) -> datetime:
        """TTM 增长的基期 = 同日历日去年(2/29 回退 2/28, 公告日按日比较差一天无影响)"""
        try:
            return date.replace(year=date.year - 1)
        except ValueError:
            return date.replace(year=date.year - 1, day=28)

    @staticmethod
    def _growth_pair_category_stats(pairs: dict[str, tuple[float, float]]) -> dict[str, int]:
        """增长对类别统计(参与/扭亏/转亏/加大亏损/减少亏损)——TTM 同比与动态同比共用同一判定规则"""
        stats = {
            "stocks_pair": len(pairs),
            "stocks_turnaround": 0,
            "stocks_turnloss": 0,
            "stocks_widen_loss": 0,
            "stocks_narrow_loss": 0,
        }
        for now_value, last_value in pairs.values():
            if now_value > 0 and last_value > 0:
                continue  # 数值型, 无类别
            if now_value > 0:
                stats["stocks_turnaround"] += 1
            elif last_value > 0:
                stats["stocks_turnloss"] += 1
            elif now_value < last_value:
                stats["stocks_widen_loss"] += 1
            else:
                stats["stocks_narrow_loss"] += 1
        return stats

    def get_ts_code_to_ttm_growth_pair(
        self, date: datetime, profit_kind: str = "attr"
    ) -> tuple[dict[str, tuple[float, float]], dict[str, int]]:
        """获取各股票 TTM 增长对: (pairs, stats)——供单日榜"净利润同比"TTM 口径

        profit_kind: "attr"=归母(默认, 含 express 快报双源合并) / "deduct"=扣非
        (get_ts_code_to_ttm_deducted_profit, 无快报源——年报季时效落后归母一档)。
        两口径共用同一批已拉报告期数据, 扣非仅本地重算零新增请求; 类别(扭亏/转亏/加大亏损/减少亏损)
        按各自口径独立判定(归母扭亏而扣非仍亏真实存在——非经常性收益保壳情形)。
        pairs: ts_code -> (当期 TTM, 基期 TTM)(元)——**两期 TTM 均有才入**(both-or-neither:
        缺基期的新股不进行业 Σ 分子也不进分母, 避免只进分子抬高增速)。基期 = growth_base_date(D-1年),
        走**同一套** TTM 机制(含所选口径的全部规则, 报告期窗口自动落到 [D-36月, D-12月],
        预热由 prefetch_fina_indicators(growth_base_date=...) 串行补拉)。
        注意基期为"当前快照回看"(含此后发布的更正, 与既有 PE 历史回看口径一致, 见 known_issues 第 39 条)。
        stats: {"stocks_pair"(参与), "stocks_turnaround"(扭亏: 基期≤0 当期>0),
        "stocks_turnloss"(转亏: 基期>0 当期≤0), "stocks_widen_loss"(加大亏损: 两期均≤0 且当期更深),
        "stocks_narrow_loss"(减少亏损: 两期均≤0 且当期持平原或收窄),
        "stocks_no_base"(当期有 TTM 而基期无)} 全市场口径。
        个股/行业的数值与四类显示("扭亏"/"转亏"/"加大亏损"/"减少亏损")由
        industry_ranking.classify_profit_growth 统一判定。
        结果按 (计算日, 口径) 缓存(_growth_pair_cache)
        """
        if profit_kind not in ("attr", "deduct"):
            raise ValueError(f"不支持的净利润口径: {profit_kind}")
        cached = self._growth_pair_cache.get((date, profit_kind))
        if cached is not None:
            return cached

        base = self.growth_base_date(date)
        if profit_kind == "deduct":
            now_map, _ = self.get_ts_code_to_ttm_deducted_profit(date)
            last_map, _ = self.get_ts_code_to_ttm_deducted_profit(base)
        else:
            now_map, _ = self.get_ts_code_to_ttm_attr_profit(date)
            last_map, _ = self.get_ts_code_to_ttm_attr_profit(base)

        pairs: dict[str, tuple[float, float]] = {}
        no_base = 0
        for ts_code, now_value in now_map.items():
            last_value = last_map.get(ts_code)
            if last_value is None:
                no_base += 1
                continue
            pairs[ts_code] = (now_value, last_value)
        stats = {**self._growth_pair_category_stats(pairs), "stocks_no_base": no_base}

        self._growth_pair_cache[(date, profit_kind)] = (pairs, stats)
        return pairs, stats

    def _dynamic_growth_views(
        self, date: datetime, profit_kind: str
    ) -> tuple[dict[str, dict[str, tuple]], dict[str, dict[str, tuple]]]:
        """动态增长对两侧数据视图: (当期视图, 基期兜底视图)——当期取 date 主窗口、兜底取 D-1年窗口

        归母口径为双源合并视图(_attr_merged_view, 含业绩快报), 扣非口径为纯财报 fina 视图
        (profit_dedt, 无快报源); 两视图的利润字段下标均为 1(fina 4 元组的扣非 / merged 2 元组的归母)。
        去年同季 = 最新期整数平移 12 个月, **优先从当期视图取**(主窗口 [D-24月, D] 覆盖 latest−12月
        ≥ D−24月 的全部常见情形, 值为 D 视角已披露的审定值——去年同季的披露日可能晚于 D−1年,
        从基期窗口取会被 PIT 过滤为 None); 最新期早于 D−12月 的长期停披股其去年同季 < D−24月
        不在主窗口, 从基期兜底视图取(该期在 D−1年 时点必然已披露, 两段取值语义均完备)。
        全部命中既有预热窗口零新增请求
        """
        base = self.growth_base_date(date)
        if profit_kind == "deduct":
            _, now_view = self._fina_per_stock(date)
            _, base_view = self._fina_per_stock(base)
        else:
            now_view, _ = self._attr_merged_view(date)
            base_view, _ = self._attr_merged_view(base)
        return now_view, base_view

    def get_ts_code_to_dynamic_growth_pair(
        self, date: datetime, profit_kind: str = "attr"
    ) -> tuple[dict[str, tuple[float, float]], dict[str, int]]:
        """获取各股票动态增长对: (pairs, stats)——供单日榜"净利润同比"动态口径

        定义(**同相位累计同比**): 增长 = 最新期累计利润 / 去年同季累计利润 − 1, 去年同季 =
        最新期报告期整数平移 12 个月(20250331↔20240331), 对比相位严格对齐; 数学上等价于
        "动态值 / 去年同期动态值"(分子分母同期, 4/k 年化系数约掉), 与 Tushare
        fina_indicator.netprofit_yoy(累计同比)同口径。
        **不用**"动态(D)/动态(D-1年)"作比——两时点"最新期"的披露节奏可能错位(如 D 日 2024 年报
        已披露而 D-1年 日 2023 年报尚未披露, 分母被迫用三季报×4/3), 相位与年化双重失真且每股
        异向、行业 Σ 混合放大, 见 docs/financial_indicators.md 第 5 节。
        profit_kind: "attr"=归母(含快报双源合并, 当期/基期侧各自独立解析) / "deduct"=扣非
        (无快报源); 与 TTM 同比共用已拉报告期数据零新增请求(去年同季优先取主窗口、停披超一年
        的股票回落 TTM 同比的基期预热窗口, 见 _dynamic_growth_views); 类别判定与统计结构同
        get_ts_code_to_ttm_growth_pair(both-or-neither: 缺去年同季的股票不进分子分母, 计入
        stocks_no_base)。
        结果按 (计算日, 口径) 缓存(_dynamic_growth_pair_cache)
        """
        if profit_kind not in ("attr", "deduct"):
            raise ValueError(f"不支持的净利润口径: {profit_kind}")
        cached = self._dynamic_growth_pair_cache.get((date, profit_kind))
        if cached is not None:
            return cached

        date_str = date.strftime("%Y%m%d")
        now_view, base_view = self._dynamic_growth_views(date, profit_kind)
        pairs: dict[str, tuple[float, float]] = {}
        no_base = 0
        for ts_code, by_period in now_view.items():
            latest = self._fina_latest_period(by_period, date_str)
            if latest is None:
                continue
            now_value = by_period[latest][1]
            if now_value is None:
                continue  # 最新期利润字段全 NaN, 与 TTM 的 stocks_missing 同处理(不入当期)
            last_period = f"{int(latest[:4]) - 1}{latest[4:]}"
            last_record = by_period.get(last_period)  # 优先主窗口(D 视角已披露审定值)
            if last_record is None or last_record[1] is None:
                fallback = (base_view.get(ts_code) or {}).get(last_period)  # 停披超一年的兜底
                if fallback is not None and fallback[1] is not None:
                    last_record = fallback
            last_value = last_record[1] if last_record is not None else None
            if last_value is None:
                no_base += 1
                continue
            pairs[ts_code] = (now_value, last_value)
        stats = {**self._growth_pair_category_stats(pairs), "stocks_no_base": no_base}

        self._dynamic_growth_pair_cache[(date, profit_kind)] = (pairs, stats)
        return pairs, stats

    def get_ts_code_to_dynamic_profit(
        self, date: datetime, profit_kind: str = "attr"
    ) -> tuple[dict[str, float], dict[str, int]]:
        """获取各股票截至 date 的**动态净利润**(元)——PE 动态口径分子: (dyn_map, stats)

        动态净利润 = 最新报告期累计利润 × 4/k(k=最新期覆盖季度数, Q1→1/中报→2/三季报→3/年报→4,
        与 Tushare daily_basic 动态市盈率同法), 即把 TTM 的"不足四期兜底"年化式用于全体股票;
        最新期为年报时 k=4, 动态值即年报值——与 TTM 标准式退化结果相等(年报披露后至下期季报
        披露前, 动态 PE 与 TTM PE 数值相同, 可作一致性自检)。Q1 披露后动态 = Q1×4, 季节性强
        的公司偏差大, 属动态口径固有特性(默认口径仍为归母-TTM, 见 docs/financial_indicators.md)。
        profit_kind: "attr"=归母(双源合并视图, 含业绩快报) / "deduct"=扣非(纯财报 profit_dedt,
        无快报源); 数据与 TTM 同批(主窗口 [D-24月, D] 已拉)零新增请求。
        PIT 与 _compute_ttm 同规则(最新期 ann_date ≤ D, 利润 None 按无财报处理不回看更早期)。
        结果按 (计算日, 口径) 缓存(_dynamic_profit_cache)。
        stats: {"periods", "stocks_dynamic"(参与), "stocks_missing"(无最新期或利润为 None)};
        归母口径另加 "stocks_express"(与 TTM 共享同一合并视图计数)
        """
        if profit_kind not in ("attr", "deduct"):
            raise ValueError(f"不支持的净利润口径: {profit_kind}")
        cached = self._dynamic_profit_cache.get((date, profit_kind))
        if cached is not None:
            return cached

        date_str = date.strftime("%Y%m%d")
        if profit_kind == "deduct":
            periods, view = self._fina_per_stock(date)
            stocks_express: int | None = None
        else:
            view, stocks_express = self._attr_merged_view(date)
            periods, _ = self._fina_per_stock(date)

        dyn_map: dict[str, float] = {}
        missing = 0
        for ts_code, by_period in view.items():
            latest = self._fina_latest_period(by_period, date_str)
            if latest is None:
                missing += 1
                continue
            value = by_period[latest][1]
            if value is None:
                missing += 1
                continue
            dyn_map[ts_code] = value * (4.0 / self._period_quarters(latest))
        stats: dict[str, int] = {
            "periods": len(periods),
            "stocks_dynamic": len(dyn_map),
            "stocks_missing": missing,
        }
        if stocks_express is not None:
            stats["stocks_express"] = stocks_express

        self._dynamic_profit_cache[(date, profit_kind)] = (dyn_map, stats)
        return dyn_map, stats

    def get_ts_code_to_roes(self, date: datetime) -> tuple[dict[str, dict[str, float]], dict[str, int]]:
        """获取各股票截至 date 的 **ROE(%)**——加权平均算法、四口径一次算出: (roes, stats)

        算法 = **加权平均 ROE**(证监会《编报规则第 9 号》, 与财报披露口径一致), 2026-08-28 新增:
        - **数据锚 = 披露值 `roe_waa`**(fina_indicator_vip 同批带回零新增请求, 覆盖/反推对照实测
          见 docs/financial_indicators.md 第 6 节): 报告期"年初至今累计"的加权平均净资产收益率,
          公司按 9 号公式自行计算(期初净资产 + NP/2 + 按月加权的增发/回购/分红等全部事件项)。
          **自建逐笔事件项在本项目数据源下不可拼全**(增发无接口、OCI 无逐笔数据), 故以披露值为
          唯一权威锚; **官方加权分母反推: E_waa(期) = 该期归母累计(纯财报行内合成, 与披露值同口径)
          × 100 ÷ roe_waa**——归母与 roe_waa 同号时商恒正, 反推结果 ≤ 0 视为数据异常按无数据处理。
        - **全链不接业绩快报**: express 无 roe_waa, 分子若用快报利润会与披露分母期次错配
          (宁缺勿错配)——快报窗口内(年报季 1~4 月)ROE 时效停在上一报告期, 落后 PE/净利润同比一档。
        四口径(键与 industry_ranking.PROFIT_BASES 一致, 修改口径表需同步):
        - `attr_dynamic`  = roe_waa(最新期) × 4/k —— 分子分母同乘年化系数, 即"动态净利润 ÷ 官方加权分母"
        - `deduct_dynamic` = (扣非累计 × 4/k) ÷ E_waa(最新期) × 100 —— 扣非无披露值, 分子自建÷官方分母
        - `attr_ttm`  = **纯财报归母 TTM**(不经 express 合并, 与披露分母同源同期) ÷ E_TTM × 100
        - `deduct_ttm` = 扣非 TTM ÷ E_TTM × 100
        **TTM 分母 E_TTM 分段推导**(复用 TTM 标准式同一组报告期 P=最新期/A=去年年报/S=去年同季):
            E_TTM = E_waa(A) + (E_waa(P) − E_waa(S)) ÷ 2
        TTM 区间[去年同期末, 最新期末] = 去年下半年 + 今年年初至今; 全年披露 E_waa(A) 时间加权覆盖
        12 个月、上半年披露 E_waa(S) 覆盖前 6 个月, 展开得 E_下半年 = 2×E_waa(A) − E_waa(S), 与
        E_waa(P)(今年年初至今)按 6/6 等权合并即得上式; 最新期为年报时 S=A 自动退化为上下半年平均
        (E_waa(P)+E_waa(A))/2。A 或 S 的 roe_waa 缺失(如新股/披露不全)时**兜底 E_TTM = E_waa(P)**
        (与 TTM 分子 4/k 年化兜底天然配套); 推导结果 ≤ 0 时同样兜底。
        PIT 与 TTM 同规则(最新期 ann_date ≤ D); **roe_waa 缺失或反推异常的股票四口径全部无数据**
        (显示"—", 不做自建简化式兜底——实测简单平均分母 (E0+E1)/2 与官方加权分母偏差中位 2.9%、
        P90 12.8%、约 1/3 公司 >5%, 披露值缺失时宁缺勿猜)。
        roes: {basis: {ts_code: (分子元, 分母元)}}——**分子/分母对**(与 TTM 增长对同构):
        个股 ROE% = 分子/分母×100, 行业整体法 ROE% = Σ分子/Σ分母×100(见 industry_ranking.daily_roe);
        分子=所选口径净利润(动态=最新期累计×4/k, TTM=纯财报 TTM), 分母=E_waa(P)或 E_TTM, 恒>0。
        stats: {"periods", "stocks_with_roe"(最新期 roe_waa+归母齐备), "stocks_missing",
        "stocks_ttm_full"(E_TTM 三期齐全), "stocks_ttm_fallback"(E_TTM 兜底)} 全市场口径。
        结果按计算日缓存(_roe_cache); 聚合见 industry_ranking.daily_roe
        """
        cached = self._roe_cache.get(date)
        if cached is not None:
            return cached

        date_str = date.strftime("%Y%m%d")
        periods, per_stock = self._fina_per_stock(date)
        ttm_attr_pure, _ = self._compute_ttm(per_stock, 2, date_str, len(periods))  # 纯财报归母 TTM(不经 express)
        ttm_deduct, _ = self.get_ts_code_to_ttm_deducted_profit(date)

        bases = ("attr_ttm", "attr_dynamic", "deduct_ttm", "deduct_dynamic")  # 与 industry_ranking.PROFIT_BASES 同步
        roes: dict[str, dict[str, tuple[float, float]]] = {basis: {} for basis in bases}
        stats = {
            "periods": len(periods),
            "stocks_with_roe": 0,
            "stocks_missing": 0,
            "stocks_ttm_full": 0,
            "stocks_ttm_fallback": 0,
        }

        def _e_waa_of(record: tuple[str, float, float, float, float] | None) -> float | None:
            """反推该期官方加权平均净资产(元) = 归母×100/roe_waa; 数据不齐/结果异常返回 None"""
            if record is None:
                return None
            attr_profit, roe_waa = record[2], record[4]
            if attr_profit is None or roe_waa is None or roe_waa == 0.0:
                return None
            equity = attr_profit * 100.0 / roe_waa
            return equity if equity > 0 else None  # 归母与 roe_waa 同号时恒正, ≤0 视为异常丢弃

        for ts_code, by_period in per_stock.items():
            latest = self._fina_latest_period(by_period, date_str)
            if latest is None:
                stats["stocks_missing"] += 1
                continue
            record_p = by_period[latest]
            e_p = _e_waa_of(record_p)
            if e_p is None:
                stats["stocks_missing"] += 1  # roe_waa 缺失/反推异常: 四口径全无(宁缺勿猜)
                continue
            stats["stocks_with_roe"] += 1

            # 动态两口径: 分子 = 最新期累计 × 4/k, 分母 = 官方加权分母 E_waa(P)
            quarters = self._period_quarters(latest)
            attr_p = record_p[2]  # 归母累计(与披露 roe_waa 同口径, _e_waa_of 已保证非空)
            roes["attr_dynamic"][ts_code] = (attr_p * (4.0 / quarters), e_p)
            deduct_p = record_p[1]
            if deduct_p is not None:
                roes["deduct_dynamic"][ts_code] = (deduct_p * (4.0 / quarters), e_p)

            # TTM 分母: 分段推导(三期披露值齐全时), 否则兜底最新期加权平均
            prev_year = str(int(latest[:4]) - 1)
            e_a = _e_waa_of(by_period.get(f"{prev_year}1231"))
            e_s = _e_waa_of(by_period.get(f"{prev_year}{latest[4:]}"))
            if e_a is not None and e_s is not None and e_a + (e_p - e_s) / 2.0 > 0:
                e_ttm = e_a + (e_p - e_s) / 2.0
                stats["stocks_ttm_full"] += 1
            else:
                e_ttm = e_p  # 新股/缺去年年报或去年同季 roe_waa/推导非正: 兜底
                stats["stocks_ttm_fallback"] += 1

            attr_ttm = ttm_attr_pure.get(ts_code)
            if attr_ttm is not None:
                roes["attr_ttm"][ts_code] = (attr_ttm, e_ttm)
            deduct_ttm = ttm_deduct.get(ts_code)
            if deduct_ttm is not None:
                roes["deduct_ttm"][ts_code] = (deduct_ttm, e_ttm)

        self._roe_cache[date] = (roes, stats)
        return roes, stats

    def get_ts_code_to_ttm_deducted_profit(self, date: datetime) -> tuple[dict[str, float], dict[str, int]]:
        """获取各股票截至 date 的**扣非净利润** TTM(元)——PE 扣非-TTM 口径(Web"净利润口径"下拉切换)

        数据为 fina_indicator_vip 原生 profit_dedt 字段(归属母公司扣非净利润、累计值、单位元),
        其余(PIT/窗口/TTM 规则/统计结构)与归母口径完全一致, 见 _compute_ttm(value_idx=1);
        结果按计算日缓存(_ttm_cache)
        """
        cached = self._ttm_cache.get(date)
        if cached is not None:
            return cached

        date_str = date.strftime("%Y%m%d")
        periods, per_stock = self._fina_per_stock(date)
        result = self._compute_ttm(per_stock, 1, date_str, len(periods))
        self._ttm_cache[date] = result
        return result

    def get_ts_code_to_bps(self, date: datetime) -> tuple[dict[str, float], dict[str, int]]:
        """获取各股票截至 date 的每股净资产(元): (bps_map, stats)——**旧 PB 口径, 保留对照**

        bps_map: ts_code -> 最新报告期 bps(每股净资产, 元); stats: {"periods",
        "stocks_with_bps", "stocks_missing"}。
        **当前 PB 分母已改用 balancesheet_vip 权威归母净资产绝对额(get_ts_code_to_equity)**,
        本方法及其"bps × 当日总股本"折算保留作对照口径(该折算在报告期后送转/增发/回购时
        引入近似, 见 known_issues 第 37 条); 数据仍随 fina 批拉同请求返回、调用零新增请求。
        与 PE 同源同批拉取(fina_indicator_vip 的 bps 字段)、同一 PIT 规则
        (ann_date <= date 的最大报告期, 见 _fina_latest_period); **bps 是报告期末时点值,
        不是累计值**——无需 TTM 滚动、无"不足四期年化"兜底(新股仅一期也直接用其最新期)
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
            bps = by_period[latest_period][3]
            if bps is None:
                stats["stocks_missing"] += 1
                continue
            bps_map[ts_code] = bps
            stats["stocks_with_bps"] += 1

        self._bps_cache[date] = (bps_map, stats)
        return bps_map, stats

    def get_ts_code_to_equity(self, date: datetime) -> tuple[dict[str, float], dict[str, int]]:
        """获取各股票截至 date 的归母普通股股东权益(元): (equity_map, stats)——**PB 当前口径**

        权威来源: balancesheet_vip(归母权益−其他权益工具−优先股 行内合成, **绝对额元、
        报告期末时点值**, 见 _fetch_bs_period)——与 fina 的 bps 分子同口径(实测华能国际等
        永续债大户对账吻合到 0.001%)、与 Tushare daily_basic.pb 及数据商惯例一致; 不经
        "每股×股本"折算, 报告期后送转/增发/回购的股本变动与 CDR 股本口径错配(九号公司
        旧口径 PB 低估 10 倍)不再引入近似(旧口径偏差见 known_issues 第 37 条;
        get_ts_code_to_bps 保留对照)。
        PIT 与 PE 同规则: 每股最新期 = ann_date <= date 的最大报告期(ann_date 缺失按
        法定披露截止日推定, 实测零缺失); 净资产为时点值, 无滚动、无年化兜底(新股仅一期
        也直接用其最新期)。结果按计算日缓存, 报告期数据按 period 缓存跨天复用。
        stats: {"periods", "stocks_with_equity", "stocks_missing"}; 聚合公式见
        industry_ranking.daily_valuation_metric(kind="pb") 与 docs/financial_indicators.md
        """
        cached = self._equity_cache.get(date)
        if cached is not None:
            return cached

        date_str = date.strftime("%Y%m%d")
        periods, per_stock = self._bs_per_stock(date)

        equity_map: dict[str, float] = {}
        stats = {"periods": len(periods), "stocks_with_equity": 0, "stocks_missing": 0}
        for ts_code, by_period in per_stock.items():
            latest_period = self._fina_latest_period(by_period, date_str)
            if latest_period is None:
                stats["stocks_missing"] += 1
                continue
            equity = by_period[latest_period][1]
            if equity is None:
                stats["stocks_missing"] += 1
                continue
            equity_map[ts_code] = equity
            stats["stocks_with_equity"] += 1

        self._equity_cache[date] = (equity_map, stats)
        return equity_map, stats

    def get_index_weight_snapshots(
        self, index_code: str, start_str: str, end_str: str
    ) -> list[tuple[str, set[str]]]:
        """拉取指数月度成分快照(样本空间功能): 返回 [(快照日, {con_code})] 按快照日升序

        index_weight 为**月度数据**(每月末一期, 官方建议按月查询)——按**自然月**逐月拉取并
        做 (index_code, 月) 内存缓存(同窗口二次调用零请求, 供编排与子表共用); **忽略权重字段
        只用样本清单**(行业加权用本模块自有市值权重, 与中证官方权重无关); con_code 与申万/
        行情代码同格式可直接交集; 历史区间回看天然 PIT 正确(快照即当时成分)
        """
        snapshots: dict[str, set[str]] = {}
        # 窗口覆盖的自然月(含 start 所在月, 不早于 start)
        cur = datetime.strptime(start_str[:6] + "01", "%Y%m%d")
        end_month = end_str[:6]
        while cur.strftime("%Y%m") <= end_month:
            month_key = cur.strftime("%Y%m")
            month_start = month_key + "01"
            month_end = month_key + "31"  # 超出部分接口按窗口自动裁剪(31 日为窗口上界)
            cached = self._index_weight_month_cache.get((index_code, month_key))
            if cached is None:
                per_month: dict[str, set[str]] = {}
                offset = 0
                while True:
                    self._acquire_rate_slot("index_weight")
                    df = self.pro.index_weight(
                        index_code=index_code, start_date=month_start, end_date=month_end,
                        offset=offset, limit=9999,
                    )
                    if df is None or df.empty:
                        break
                    for row in df.itertuples(index=False):
                        trade_date = str(getattr(row, "trade_date", "") or "")
                        con_code = str(getattr(row, "con_code", "") or "")
                        if trade_date and con_code:
                            per_month.setdefault(trade_date, set()).add(con_code)
                    if len(df) < 9999:
                        break
                    offset += len(df)
                cached = per_month
                self._index_weight_month_cache[(index_code, month_key)] = cached
            snapshots.update(cached)
            # 下一自然月
            cur = datetime(month=1 if cur.month == 12 else cur.month + 1, year=cur.year + (1 if cur.month == 12 else 0), day=1)
        return sorted(
            (d, s) for d, s in snapshots.items() if start_str <= d <= end_str
        )

    @property
    def dividend_history(self) -> DividendHistory:
        """分红事件持久缓存单例(惰性创建): 首刷/增量经 ensure_refresh(榜单池宇宙), 读取经 events()

        缓存文件 data/dividend_history.json 跨进程复用; 刷新走 dividend 接口独立节流锁,
        与行情/财务三池可并行(见 run_daily_ranking 的分红外预热线程)
        """
        if self._dividend_history is None:
            self._dividend_history = DividendHistory(self)
        return self._dividend_history

    def get_ts_code_to_dividend_dps(
        self, date: datetime
    ) -> tuple[dict[str, float], dict[str, float], dict[str, int]]:
        """获取各股票股息率分子 DPS(元/股, 总额法÷当前总股本) 双口径: (est_map, static_map, stats)

        委托 dividend_data.compute_dividend_dps(规则栈/单位/兜底见其 docstring 与
        docs/financial_indicators.md 第 7 节): est=TTM估算股息率(Web 默认, 进行中财年宣告优先/
        外推补位)、static=静态股息率(最近完整分红年度); 键缺失=无数据, 0.0=齐备零分红(是数值)。
        结果按计算日缓存; 依赖分红缓存已刷新(run_daily_ranking 的分红外预热线程保证)与
        fina 窗口/归母TTM 已预热(与 PE 同批, 零新增请求)
        """
        return compute_dividend_dps(self, date)

    @property
    def share_change_history(self) -> ShareChangeHistory:
        """股本台阶事件持久缓存单例(惰性创建): 首刷/增量经 ensure_refresh, 读取经 events()

        缓存文件 data/share_change_events.json 跨进程复用; 快照拉取走 daily_basic 独立节流锁
        (与市值同接口、请求排队不冲突), 与行情/财务/分红可并行(见 run_daily_ranking 的
        台阶外预热线程); 供"TTM估算股息+注销率"的注销分量(share_change_data 台阶法)
        """
        if self._share_change_history is None:
            self._share_change_history = ShareChangeHistory(self)
        return self._share_change_history

    @property
    def repurchase_history(self) -> RepurchaseHistory:
        """回购公告持久缓存单例(惰性创建): repurchase 按月全市场拉取(接口不支持按股过滤)

        供注销台阶的 vol 交叉验证(对赌 1 元/0 元注销识别, share_change_data.
        _find_zero_price_buyback); 刷新走 repurchase 独立节流锁, 与台阶刷新同线程串行
        (预热线程); 未就绪时交叉验证跳过、不降级 est_bb
        """
        if self._repurchase_history is None:
            self._repurchase_history = RepurchaseHistory(self)
        return self._repurchase_history

    def get_ts_code_to_ttm_window(
        self, date: datetime
    ) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
        """获取各股票归母TTM净利润的覆盖窗口(YYYYMMDD): ({ts_code: (左开端, 右闭端)}, stats)

        **与 get_ts_code_to_ttm_attr_profit 完全同期同源**(归母双源合并视图、含业绩快报提前):
        标准式窗口 = (去年同季期末, 最新报告期期末]——长度恒 12 个月、逐股 PIT、随报告期披露
        整体右移(与 TTM 利润同呼吸); 年化兜底(次新股, 去年年报/去年同季利润缺失)左端为空串
        = 不限(上市以来)。供注销分量的统计窗口("TTM估算股息+注销率", 窗口一致性是设计核心:
        注销与估算所用的 TTM 利润对应同一收益产生期)。stats: {"standard", "annualized"}
        """
        cached = self._ttm_window_cache.get(date)
        if cached is not None:
            return cached
        date_str = date.strftime("%Y%m%d")
        merged, _ = self._attr_merged_view(date)
        windows: dict[str, tuple[str, str]] = {}
        stats = {"standard": 0, "annualized": 0}
        for ts_code, rows in merged.items():
            latest = self._fina_latest_period(rows, date_str)
            if latest is None:
                continue
            prev_year = str(int(latest[:4]) - 1)
            prev_annual = rows.get(f"{prev_year}1231")
            prev_same = rows.get(f"{prev_year}{latest[4:]}")
            if (
                prev_annual is not None and prev_annual[1] is not None
                and prev_same is not None and prev_same[1] is not None
            ):
                windows[ts_code] = (f"{prev_year}{latest[4:]}", latest)
                stats["standard"] += 1
            else:
                windows[ts_code] = ("", latest)
                stats["annualized"] += 1
        result = (windows, stats)
        self._ttm_window_cache[date] = result
        return result

    def get_ts_code_to_buyback_amount(
        self, date: datetime
    ) -> tuple[dict[str, float], dict[str, int]]:
        """获取各股票 TTM 窗口内的回购注销金额(万元): (amount_map, stats)——est_bb 口径分子

        委托 share_change_data.compute_buyback_amount(台阶法/窗口/对冲剔除见其 docstring 与
        docs/financial_indicators.md 第 7.5 节): 窗口 = 该股归母 TTM 覆盖时段(零新增请求),
        金额 = Σ(负台阶量 × 台阶日收盘); 含 0.0(窗口内无注销是数值), 无 TTM 窗口(无财报)
        的股票不产出。结果按计算日缓存; 依赖台阶缓存已刷新(编排的台阶预热线程保证)
        """
        cached = self._bb_amount_cache.get(date)
        if cached is not None:
            return cached
        result = compute_buyback_amount(self, date)
        self._bb_amount_cache[date] = result
        return result

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
