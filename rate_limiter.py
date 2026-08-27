"""仓库级公共接口节流器与 Tushare 积分档探测——与申万模块限流策略同源。

节流器部分：Tushare 对每个接口独立限额，因此按「接口名」各自维护发射节奏：
同一接口的所有调用点（批拉/点查/翻页/重试）共享一把槽锁串行平摊；
不同接口的限额互相独立，可完全并行、互不等待。叠加调用侧的线程池
即为单账号最大吞吐。

积分档探测部分：`probe_credit_tier()` 用固定历史数据真实调用门槛接口，
把账号归入三档之一（below_2000 / from_2000_to_5000 / at_least_5000），
holders 各客户端据此设定节流速率并拦截无权限账号。两者进程内生效——
同时运行多个脚本时各进程单独计数、额度会相互叠加，这与申万模块约定一致。
"""

from __future__ import annotations

import threading
import time

# 积分档位常量（probe_credit_tier 的返回值）
TIER_BELOW_2000 = "below_2000"          # 不足 2000
TIER_2000_TO_5000 = "from_2000_to_5000"  # 2000 ~ 4999
TIER_AT_LEAST_5000 = "at_least_5000"     # 5000 及以上

# 各档位对应的接口速率（官方约 200/500 次/分钟，均留 10% 余量）
TIER_RATES = {
    TIER_BELOW_2000: 3.0,
    TIER_2000_TO_5000: 3.0,
    TIER_AT_LEAST_5000: 7.5,
}

# 权限/积分类报错的常见字样（命中即说明是账号权限问题，区别于网络抖动）
_PERMISSION_ERROR_KEYWORDS = ("积分", "权限", "抱歉")

# 两个门槛接口的固定探测数据：历史必然存在，仅用于验证权限
_TOP10_HOLDERS_PROBE = ("600036.SH", "20230630")     # 招商银行 2023 中报（2000 积分门槛）
_FUND_DAILY_PROBE = ("510300.SH", "20240102")        # 沪深300ETF 2024 首个交易日（5000 积分门槛）


class InterfaceRateLimiter:
    """按接口独立的固定速率节流器（请求开始时刻等间隔平摊）。

    - acquire(api_name)：阻塞至该接口的下个可用发射时刻
    - halve(api_name)：触发官方频次限制后的自适应兜底，该接口速率减半、
      本次运行不再回升（[下一请求时刻] 与 [间隔] 都存在各自槽位内）
    """

    def __init__(self, requests_per_second: float):
        self._default_interval = 1.0 / requests_per_second
        # api_name -> [threading.Lock, next_start(perf_counter 秒), interval(秒)]
        self._slots: dict[str, list] = {}
        self._table_lock = threading.Lock()

    def _slot(self, api_name: str) -> list:
        with self._table_lock:
            slot = self._slots.get(api_name)
            if slot is None:
                slot = [threading.Lock(), [0.0], [self._default_interval]]
                self._slots[api_name] = slot
            return slot

    def acquire(self, api_name: str) -> None:
        lock, next_start, interval = self._slot(api_name)
        with lock:
            wait = next_start[0] - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            next_start[0] = time.perf_counter() + interval[0]

    def set_rate(self, requests_per_second: float) -> None:
        """统一调整所有接口（含尚未创建的槽位）的发射间隔。

        用于积分档探测完成后按档位整体切换速率；已存在的槽位同步更新，
        下一请求时刻保持不变。
        """
        self._default_interval = 1.0 / requests_per_second
        for _, _, interval in self._slots.values():
            interval[0] = self._default_interval

    def halve(self, api_name: str) -> None:
        lock, _, interval = self._slot(api_name)
        with lock:
            interval[0] *= 2


# 触发官方频次限制时报错信息的常见字样（用于撞限自适应判别）
RATE_LIMIT_ERROR_KEYWORDS = ("每分钟", "超限", "访问频率", "频繁")


def is_rate_limit_error(message: str) -> bool:
    """判断 Tushare 报错信息是否为触发频次限制（区别于权限/积分类错误）"""
    return any(k in message for k in RATE_LIMIT_ERROR_KEYWORDS)


def is_permission_error(message: str) -> bool:
    """判断 Tushare 报错信息是否为积分/权限类错误"""
    return any(k in message for k in _PERMISSION_ERROR_KEYWORDS)


def probe_credit_tier(pro, min_tier_points: int = 2000,
                      limiter: "InterfaceRateLimiter | None" = None) -> str | None:
    """真实调用门槛接口，把账号积分别归入三档：below_2000 / from_2000_to_5000 / at_least_5000。

    - min_tier_points=2000（股票域）：先探 top10_holders(2000 门槛)，通过再探 fund_daily(5000 门槛)
    - min_tier_points=5000（ETF 域）：直接探 fund_daily(5000 门槛)即可定性
    - 探测命中权限/积分类报错返回对应低档结论；网络抖动等其它异常无法定级时
      返回 None 并打印提示，调用方按保守策略自行决定放行与否
    - 传入 limiter 时每个探针先取对应接口的发射槽（与业务请求共享同一节流节奏）

    探针均为单行小请求。
    """
    def _passes_through(api_name, fn):
        try:
            if limiter is not None:
                limiter.acquire(api_name)
            fn(pro)
            return True
        except Exception as e:
            if is_permission_error(str(e)):
                return False
            print(f"⚠️ 积分探测请求失败（可能为网络波动），本次跳过档位检查：{e}")
            return None

    if min_tier_points >= 5000:
        ok = _passes_through("fund_daily", lambda p: p.fund_daily(
            ts_code=_FUND_DAILY_PROBE[0], trade_date=_FUND_DAILY_PROBE[1], fields="ts_code"))
        return None if ok is None else (TIER_AT_LEAST_5000 if ok else TIER_BELOW_2000)

    ok_2000 = _passes_through("top10_holders", lambda p: p.top10_holders(
        ts_code=_TOP10_HOLDERS_PROBE[0], period=_TOP10_HOLDERS_PROBE[1], fields="ts_code"))
    if ok_2000 is None:
        return None   # 无法定级，交由调用方保守处理
    if not ok_2000:
        return TIER_BELOW_2000

    ok_5000 = _passes_through("fund_daily", lambda p: p.fund_daily(
        ts_code=_FUND_DAILY_PROBE[0], trade_date=_FUND_DAILY_PROBE[1], fields="ts_code"))
    if ok_5000 is None:
        return None
    return TIER_AT_LEAST_5000 if ok_5000 else TIER_2000_TO_5000
