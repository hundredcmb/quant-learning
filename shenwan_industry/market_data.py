"""
申万行业行情数据层 (MarketDataProvider)

- 行情/市值获取: daily / daily_basic, 带按日内存缓存(涨跌幅口径见 shenwan_industry/AGENTS.md 第 3 节)
- 停牌自由流通市值回退: 730 天内最近一个有效值(close/free_share/float_share 同行取值)
- 交易日历: trade_cal
- 区间逐日行情: 并发拉取 + 固定速率限流 + 重试
- API 调用计数: 构造时包装 pro, snapshot_api_calls() 取快照,
  任务前后快照求差即该任务实际调用次数(缓存命中不计)
"""

import math
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd

# 进度回调: (0~100 的百分比, 阶段说明)
ProgressCallback = Callable[[float, str], None]

# 协作式取消检查: 需要取消时抛异常
CancelCheck = Callable[[], None]


def wrap_api_counter(pro) -> dict[str, int]:
    """包装 tushare pro 常用接口以统计调用次数, 返回按接口名计数的 dict"""
    counter: dict[str, int] = {}
    for name in ("stock_basic", "index_member_all", "daily", "daily_basic", "trade_cal"):
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


def _calc_free_mv(
    close,
    free_share,
    float_share,
) -> float | None:
    """自由流通市值(万元) = free_share × close (自由流通股本×收盘价), 三字段须取自同一交易日

    等价于旧式 circ_mv × free_share / float_share (流通市值×自由流通占比, 恒等推导:
    circ_mv=float_share×close 时两式相等); 实测 daily_basic.close 与 daily.close 逐股完全一致,
    且此式即官方《编制说明》附录「自由流通市值 = 自由流通量 × 市价」的直接表述。
    任一字段缺失/非有限值, 或 close/free_share/float_share ≤ 0、比例 >1(自由流通股本超过
    流通股本, 数据异常)时返回 None, 该股不参与加权(等同无市值处理)
    """
    if pd.isna(close) or pd.isna(free_share) or pd.isna(float_share):
        return None
    close_f, free_f, float_f = float(close), float(free_share), float(float_share)
    if not (math.isfinite(close_f) and math.isfinite(free_f) and math.isfinite(float_f)):
        return None
    if close_f <= 0 or float_f <= 0 or free_f <= 0:
        return None
    ratio = free_f / float_f
    if ratio > 1.0:
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
        self.ts_code_to_pct_chg_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股涨跌幅数据
        self.ts_code_to_close_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股收盘价数据
        self.ts_code_to_amount_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股成交额数据(千元)
        self.ts_code_to_free_mv_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股自由流通市值数据
        self.ts_code_to_total_mv_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股总市值数据

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
        与总市值同一次请求拉取并缓存; 股本异常(缺失/非正/比例越界)的股票不记入
        """
        ts_code_to_free_mv: dict[str, float] = self.ts_code_to_free_mv_cache.get(date) or {}
        if ts_code_to_free_mv:
            return ts_code_to_free_mv

        offset = 0
        batch_size = 5999  # 官方单次上限 6000, 留 1 余量; 全市场一次拉完
        date_str = date.strftime("%Y%m%d")
        ts_code_to_total_mv: dict[str, float] = {}
        while True:
            df = self.pro.daily_basic(
                ts_code='',
                trade_date=date_str,
                fields='ts_code,close,total_mv,free_share,float_share',
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

            offset += len(df)
            if batch_size > len(df):
                break

        if ts_code_to_free_mv:
            self.ts_code_to_free_mv_cache[date] = ts_code_to_free_mv
            self.ts_code_to_total_mv_cache[date] = ts_code_to_total_mv

        return ts_code_to_free_mv

    def get_ts_code_to_total_mv(self, date: datetime) -> dict[str, float]:
        """获取A股某日的总市值数据: ts_code -> 总市值(与自由流通市值同一次请求拉取)"""
        self.get_ts_code_to_free_mv(date)
        return self.ts_code_to_total_mv_cache.get(date) or {}

    def _resolve_mvs(
        self,
        ts_code: str,
        date: datetime,
        cancel_check: CancelCheck | None,
    ) -> tuple[float | None, float | None]:
        """停牌股回退: 一次请求查 730 天内最近的有效自由流通市值与总市值, 查不到返回 None

        自由流通市值要求 close/free_share/float_share 取自同一行(同一交易日)计算,
        避免混搭不同日期的股本; 总市值可独立取最近有效值
        """
        if cancel_check is not None:
            cancel_check()
        df = self.pro.daily_basic(
            ts_code=ts_code,
            fields='trade_date,close,total_mv,free_share,float_share',
            start_date=(date - timedelta(days=730)).strftime("%Y%m%d"),
            end_date=date.strftime("%Y%m%d"),
        )
        # 响应的数据默认按日期降序, 自由流通市值与总市值分别取最近的有效值
        free: float | None = None
        total: float | None = None
        for row in df.itertuples(index=False):
            if cancel_check is not None:
                cancel_check()
            d_str = row.trade_date
            if datetime.strptime(d_str, "%Y%m%d") > date:
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
            if free is not None and total is not None:
                break
        return free, total

    def resolve_free_mv(
        self,
        ts_code: str,
        date: datetime,
        cancel_check: CancelCheck | None = None,
    ) -> float | None:
        """停牌股自由流通市值回退: 一次请求同时回退自由流通市值与总市值(总市值写入缓存), 返回自由流通市值"""
        free, total = self._resolve_mvs(ts_code, date, cancel_check)
        if total is not None:
            self.ts_code_to_total_mv_cache.setdefault(date, {})[ts_code] = total
        return free

    def resolve_total_mv(
        self,
        ts_code: str,
        date: datetime,
        cancel_check: CancelCheck | None = None,
    ) -> float | None:
        """停牌股总市值回退: 优先读缓存(自由流通市值回退已顺带填充), 未命中再发请求"""
        cached = self.ts_code_to_total_mv_cache.get(date, {}).get(ts_code)
        if cached is not None:
            return cached
        free, total = self._resolve_mvs(ts_code, date, cancel_check)
        if total is not None:
            self.ts_code_to_total_mv_cache.setdefault(date, {})[ts_code] = total
        return total

    def get_trading_days(self, start_str: str, end_str: str) -> list[str]:
        """获取区间内交易日列表(YYYYMMDD, 升序)"""
        df = self.pro.trade_cal(
            exchange='SSE',
            start_date=start_str,
            end_date=end_str,
            is_open='1',
            fields='cal_date',
        )
        return sorted(df['cal_date'].astype(str).tolist())

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

        - 线程池并发 + 全局请求节流: 请求开始时刻按 MAX_DAILY_FETCH_RATE 平摊,
          60 秒滚动窗口内请求数 ≈ 交易日数, 远低于 500 次/分钟, 且不集中爆发
        - 单日失败自动重试 DAILY_FETCH_RETRY 次, 仍失败则抛错(不静默改变结果)
        """
        results: dict[str, dict[str, tuple[float, float]]] = {}
        lock = threading.Lock()
        next_start = [0.0]
        interval = 1.0 / MAX_DAILY_FETCH_RATE
        total = len(trading_days)
        completed = 0

        def fetch_one(day_str: str) -> tuple[str, dict[str, tuple[float, float]]]:
            if cancel_check is not None:
                cancel_check()
            with lock:
                wait = next_start[0] - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                next_start[0] = time.perf_counter() + interval
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
        return results
