"""Web 服务与现有申万排行算法之间的适配层。"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import threading
import time
import warnings
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Iterator

import tushare as ts

from ..industry_ranking import (
    run_daily_ranking,
    rank_range,
    rank_range_chain,
    classify_profit_growth,
    PROFIT_BASES,
    SAMPLE_SPACES,
    resolve_sample_segments,
    sample_pool_at,
)

# Web 固定一次全算的样本档(全A/中证800/中证1800=800+1000, 三档嵌套; 前端下拉即时切换)
WEB_SAMPLE_SPACES = ["full", "csi800", "csi1800"]
from ..industry_tree import ShenWanIndustryTree
from ..market_data import MarketDataProvider

# token 配置在仓库根公共模块（与 holders 共享同一份 .quant-learning/settings.json）
try:
    from config_store import config_path, get_token, set_token
except ImportError:  # 直接以脚本方式运行服务时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from config_store import config_path, get_token, set_token


logger = logging.getLogger("shenwan_industry.web.service")
_NO_INDUSTRY_STOCKS: set[str] = set()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SW2021_PATH = _REPO_ROOT / "shenwan_industry" / "data" / "SW2021.json"

with _SW2021_PATH.open("r", encoding="utf-8") as _fp:
    _sw2021_rows = json.load(_fp)
    # 全层级 index_code -> 行业短名（K 线接口返回 name 用；L1/L2/L3 全覆盖）
    _INDEX_NAMES = {row["index_code"]: row["industry_name"] for row in _sw2021_rows}
    _L1_INDEXES = {row["index_code"]: row["industry_name"] for row in _sw2021_rows if row["level"] == "L1"}
    _L2_L3_INDEXES = {row["index_code"] for row in _sw2021_rows if row["level"] in ("L2", "L3")}


@contextmanager
def _capture_no_industry_warnings() -> Iterator[None]:
    """只汇总“无申万行业归属”告警，其余告警仍按原样输出。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        yield
        for warning in caught:
            message = str(warning.message)
            match = re.search(r"找不到股票 '([^']+)' 对应的 L\d 行业", message)
            if match:
                _NO_INDUSTRY_STOCKS.add(match.group(1))
            else:
                logger.warning("数据处理告警: %s", message)


def _flush_no_industry_warnings() -> None:
    if not _NO_INDUSTRY_STOCKS:
        return
    codes = sorted(_NO_INDUSTRY_STOCKS)
    samples = ", ".join(codes[:5])
    logger.warning(
        "本次任务跳过 %d 只无申万三级行业归属的股票，样例: %s%s",
        len(codes),
        samples,
        "..." if len(codes) > 5 else "",
    )
    _NO_INDUSTRY_STOCKS.clear()


def _diff_api_calls(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """任务前后 API 计数快照求差, 得到本次任务实际调用次数(缓存命中不计)"""
    return {
        name: after.get(name, 0) - before.get(name, 0)
        for name in set(before) | set(after)
        if after.get(name, 0) - before.get(name, 0) > 0
    }


def _get_token() -> str:
    token = get_token()
    if not token:
        raise ValueError(
            f"尚未配置 Tushare token，请先在页面右上角「数据配置」中填写并保存"
            f"（本地配置文件: {config_path()}）"
        )
    return token


def get_token_config() -> dict[str, Any]:
    """返回 token 配置状态（不回显完整 token，仅掩码）。"""
    token = get_token()
    if not token:
        return {"configured": False, "token_mask": None}
    return {"configured": True, "token_mask": _mask_token(token)}


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return f"{token[:2]}***"
    return f"{token[:4]}***{token[-4:]}"


def save_token(token: str) -> None:
    """保存 token 并重置已构建的行业数据上下文，随后后台用新 token 重建（首次查询即可就绪）。"""
    set_token(token)
    _CONTEXT.reset()
    _CONTEXT.build_async()
    reset_sw_daily_available()
    prebuild_sw_daily_available()


def test_token() -> tuple[bool, str]:
    """用 trade_cal 验证已保存的 token 是否可用。"""
    token = get_token()
    if not token:
        return False, "尚未配置 Tushare token"
    try:
        pro = ts.pro_api(token=token)
        pro.trade_cal(
            exchange="SSE",
            start_date="20250101",
            end_date="20250110",
            is_open="1",
            fields="cal_date",
        )
        return True, "token 有效"
    except Exception as err:  # noqa: BLE001 - 接口失败即视为无效
        return False, f"token 无效或接口无权限: {err}"


def get_default_dates() -> dict[str, str]:
    """返回前端默认日期：单日 = 上一交易日，区间 = 上一个完整自然月。"""
    today = date.today()
    first_of_month = today.replace(day=1)
    prev_month_last = first_of_month - timedelta(days=1)
    prev_month_first = prev_month_last.replace(day=1)

    try:
        pro = ts.pro_api(token=_get_token())
        end_date = today - timedelta(days=1)
        start_date = today - timedelta(days=45)
        df = pro.trade_cal(
            exchange="SSE",
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            is_open="1",
            fields="cal_date",
        )
        trading_days = sorted(df["cal_date"].astype(str).tolist())
        previous_trading_day = trading_days[-1] if trading_days else None
    except Exception as err:  # noqa: BLE001 - 网络异常时使用工作日兜底
        logger.warning("获取默认交易日失败，使用工作日兜底: %s", err)
        previous_trading_day = None

    if previous_trading_day is None:
        candidate = today - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        previous_trading_day = candidate
    else:
        previous_trading_day = datetime.strptime(previous_trading_day, "%Y%m%d").date()

    return {
        "daily_date": previous_trading_day.strftime("%Y-%m-%d"),
        "range_start": prev_month_first.strftime("%Y-%m-%d"),
        "range_end": prev_month_last.strftime("%Y-%m-%d"),
    }


_sw_daily_available: set[str] | None = None  # 内存缓存: 官方指数日线可得代码集合(启动后台探测填充)
_sw_daily_cond = threading.Condition()  # 兼作锁与探测完成通知
_sw_daily_probing = False


def _latest_trade_dates(count: int = 3) -> list[str]:
    """最近 N 个交易日（YYYYMMDD，降序）；交易日历失败时退化为最近工作日"""
    try:
        pro = ts.pro_api(token=_get_token())
        today = date.today()
        df = pro.trade_cal(
            exchange="SSE",
            start_date=(today - timedelta(days=45)).strftime("%Y%m%d"),
            end_date=today.strftime("%Y%m%d"),
            is_open="1",
            fields="cal_date",
        )
        days = sorted(df["cal_date"].astype(str).tolist(), reverse=True)
        if days:
            return days[:count]
    except Exception as err:  # noqa: BLE001 - 退化为工作日兜底
        logger.warning("获取交易日历失败，使用工作日兜底: %s", err)
    result: list[str] = []
    candidate = date.today()
    while len(result) < count:
        candidate -= timedelta(days=1)
        if candidate.weekday() < 5:
            result.append(candidate.strftime("%Y%m%d"))
    return result


def _compute_sw_daily_available() -> set[str] | None:
    """探测官方指数日线覆盖：sw_daily(trade_date=最新交易日) 一次拉全市场；
    空结果回退前一个交易日再试，与 SW2021.json 的 L2/L3 求交集（L1 全覆盖恒含），
    全部失败返回 None。每次服务启动时后台默默执行一次（无文件缓存）"""
    try:
        pro = ts.pro_api(token=_get_token())
        for date_str in _latest_trade_dates():
            df = pro.sw_daily(trade_date=date_str)
            if df is not None and len(df) > 0:
                probed = set(df["ts_code"].astype(str).tolist())
                return (probed & _L2_L3_INDEXES) | set(_L1_INDEXES)
    except Exception as err:  # noqa: BLE001 - 网络/token 异常
        logger.warning("探测官方指数日线可用性失败: %s", err)
        return None
    return None


def prebuild_sw_daily_available() -> None:
    """服务启动/保存 token 后**后台默默探测**官方指数日线可用性（不阻塞启动、无感完成）。

    已就绪或探测进行中则跳过；token 未配置也跳过（保存 token 时再触发）。
    前端 /api/index/available 与 K 线校验在探测完成前会等待其就绪（最多 30 秒），
    失败则回退"仅 L1 可点击"。
    """
    global _sw_daily_available, _sw_daily_probing
    if not get_token():
        return
    with _sw_daily_cond:
        if _sw_daily_available is not None or _sw_daily_probing:
            return
        _sw_daily_probing = True

    def _worker() -> None:
        global _sw_daily_available, _sw_daily_probing
        try:
            available = _compute_sw_daily_available()
            with _sw_daily_cond:
                if available is not None:
                    _sw_daily_available = available
        except Exception:  # noqa: BLE001
            logger.exception("后台探测官方指数可用性失败, 前端回退为仅 L1 可点击")
        finally:
            with _sw_daily_cond:
                _sw_daily_probing = False
                _sw_daily_cond.notify_all()

    threading.Thread(target=_worker, daemon=True, name="sw-daily-probe").start()


def reset_sw_daily_available() -> None:
    """保存新 token 后清空内存缓存, 下次触发后按新 token 重新探测"""
    with _sw_daily_cond:
        _sw_daily_available = None


def get_sw_daily_available() -> set[str] | None:
    """有官方指数日线数据的行业指数代码集合（L1 全覆盖恒含，L2/L3 以探测为准）。

    - 服务启动/保存 token 后后台探测一次并内存缓存（无文件、无过期——数据本身长期不变）
    - 调用方（首次 /api/index/available）在探测完成前**等待就绪**（无感，最多 30 秒）
    - 探测失败返回 None：回退"仅 L1 可点击"，不缓存、下次调用重试
    """
    global _sw_daily_available, _sw_daily_probing
    with _sw_daily_cond:
        if _sw_daily_available is not None:
            return _sw_daily_available
        if _sw_daily_probing:
            deadline = time.monotonic() + 30.0
            while _sw_daily_probing and time.monotonic() < deadline:
                _sw_daily_cond.wait(timeout=max(0.1, deadline - time.monotonic()))
            return _sw_daily_available
        # 探测从未被触发（如直接导入调用）: 同步补探测一次
        _sw_daily_probing = True
    available = _compute_sw_daily_available()
    with _sw_daily_cond:
        _sw_daily_probing = False
        _sw_daily_cond.notify_all()
    with _sw_daily_cond:
        if available is not None:
            _sw_daily_available = available
    return available


def get_available_index_codes() -> list[str]:
    """可查看 K 线的行业指数代码列表（L1 + 有官方日线的 L2/L3；探测失败时仅 L1）"""
    available = get_sw_daily_available()
    if available is None:
        return sorted(_L1_INDEXES)
    return sorted(available)


def get_index_kline(
    index_code: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """获取申万行业官方指数日 K 线（L1 全覆盖；L2/L3 需有官方指数日线数据）。"""
    index_code = index_code.strip()
    allowed = get_sw_daily_available()
    if allowed is None:
        allowed = set(_L1_INDEXES)  # 探测失败时回退为仅一级
    if index_code not in allowed:
        raise ValueError(f"不是可查看的申万行业指数代码: {index_code}")

    pro = ts.pro_api(token=_get_token())
    kwargs: dict[str, Any] = {"ts_code": index_code}
    if start_date:
        kwargs["start_date"] = start_date
    if end_date:
        kwargs["end_date"] = end_date

    df = pro.sw_daily(**kwargs)
    if df is None or len(df) == 0:
        raise ValueError(f"没有获取到 {index_code} 的 K 线数据")

    df = df.sort_values("trade_date")
    bars = []
    for row in df.itertuples(index=False):
        close = _safe_float(row.close)
        change = _safe_float(getattr(row, "change", None))
        bars.append(
            {
                "date": str(row.trade_date),
                "open": _safe_float(row.open),
                "high": _safe_float(row.high),
                "low": _safe_float(row.low),
                "close": close,
                # sw_daily 无 pre_close 字段，用 close - change 反推前收（涨幅口径与模块一致）
                "pre_close": close - change if (close is not None and change is not None) else None,
                "vol": _safe_float(getattr(row, "vol", None)),
                "amount": _safe_float(getattr(row, "amount", None)),
            }
        )

    return {
        "index_code": index_code,
        "name": _INDEX_NAMES[index_code],
        "bars": bars,
    }


# 个股 K 线最早日期（2014-01-01 之前的数据不拉取）
STOCK_KLINE_START_DATE = "20140101"


def get_stock_kline(
    ts_code: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """获取个股前复权日 K 线（vol 单位手、amount 单位千元，原样返回由前端适配）。"""
    ts_code = ts_code.strip().upper()
    pro = ts.pro_api(token=_get_token())
    kwargs: dict[str, Any] = {"ts_code": ts_code, "adj": "qfq"}
    if start_date:
        kwargs["start_date"] = start_date
    else:
        kwargs["start_date"] = STOCK_KLINE_START_DATE
    if end_date:
        kwargs["end_date"] = end_date

    df = pro.daily(**kwargs)
    if df is None or len(df) == 0:
        raise ValueError(f"没有获取到 {ts_code} 的 K 线数据")

    df = df.sort_values("trade_date")
    bars = []
    for row in df.itertuples(index=False):
        bars.append(
            {
                "date": str(row.trade_date),
                "open": _safe_float(row.open),
                "high": _safe_float(row.high),
                "low": _safe_float(row.low),
                "close": _safe_float(row.close),
                "pre_close": _safe_float(getattr(row, "pre_close", None)),
                "vol": _safe_float(getattr(row, "vol", None)),
                "amount": _safe_float(getattr(row, "amount", None)),
            }
        )

    return {
        "ts_code": ts_code,
        "name": "",
        "bars": bars,
    }


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


class PreparedContext:
    """缓存行业树与行情数据层，首版仅单 worker 访问。

    MarketDataProvider 内部包装 pro 并累计 API 调用计数，任务前后快照求差即本次任务调用；
    行情/市值按日期内存缓存跨任务复用（单 worker 串行）。
    构建时记录所用 token，token 变更（页面重新保存）后自动重建。
    支持启动后后台预建（build_async）：任一时刻至多一个构建在跑（_building + 条件变量），
    任务侧 ensure 遇到预建进行中会等待其完成、不重复构建；预建失败自动回退到首次查询构建。
    """

    def __init__(self) -> None:
        self._tree: ShenWanIndustryTree | None = None
        self._provider: MarketDataProvider | None = None
        self._token: str | None = None
        self._building = False  # 是否有(后台或任务触发的)构建正在进行
        self._cond = threading.Condition()  # 兼作锁与构建完成通知

    def _do_build(self, token: str) -> tuple[ShenWanIndustryTree, MarketDataProvider]:
        """实际构建（调用方须已置 _building=True 且保证同一时刻只一个构建在跑）"""
        base_pro = ts.pro_api(token=token)
        provider = MarketDataProvider(base_pro)
        tree = ShenWanIndustryTree(tushare_pro=provider.pro)
        tree.build_industries()
        tree.build_constituent_stocks_by_tushare()
        with self._cond:
            self._tree = tree
            self._provider = provider
            self._token = token
        return tree, provider

    def ensure(self) -> tuple[ShenWanIndustryTree, MarketDataProvider]:
        with self._cond:
            token = _get_token()
            if self._tree is not None and token != self._token:
                self._tree = None
                self._provider = None
                self._token = None
            while self._building:
                self._cond.wait()  # 后台/他处构建进行中, 等待其完成
            if self._tree is not None:
                return self._tree, self._provider
            self._building = True
        try:
            return self._do_build(token)
        finally:
            with self._cond:
                self._building = False
                self._cond.notify_all()

    def build_async(self) -> None:
        """后台预建行业树（服务启动/保存 token 后调用）；token 未配置则跳过，
        保持 not-ready，待保存 token 时再触发。构建失败由 worker 捕获记录、不阻塞服务，
        且 ensure 的懒构建会自动兜底重试。
        """
        token = get_token()
        if not token:
            return
        with self._cond:
            if self._tree is not None or self._building:
                return
            self._building = True

        def _worker() -> None:
            try:
                self._do_build(token)
            except Exception:
                logger.exception("后台预建行业树失败, 将回退到首次查询时构建")
            finally:
                with self._cond:
                    self._building = False
                    self._cond.notify_all()

        threading.Thread(target=_worker, daemon=True, name="shenwan-prebuild").start()

    def reset(self) -> None:
        with self._cond:
            self._tree = None
            self._provider = None
            self._token = None

    def is_ready(self) -> bool:
        return self._tree is not None


_CONTEXT = PreparedContext()


def prebuild_context() -> None:
    """服务启动后在后台预建行业树（token 未配置则跳过），首次查询即可就绪。"""
    _CONTEXT.build_async()


def run_worker(
    job: Any,
    progress: Any,
    cancel_check: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, int]]:
    """JobManager 的 worker 入口。"""
    with _capture_no_industry_warnings():
        if job.mode == "daily":
            result = _run_daily(job, progress, cancel_check)
        elif job.mode == "range":
            result = _run_range(job, progress, cancel_check)
        else:
            raise ValueError(f"不支持的任务类型: {job.mode}")
    _flush_no_industry_warnings()
    return result


def _compute_stock_metrics(
    provider: MarketDataProvider,
    rank_date: datetime,
    close_map: dict[str, float],
    total_map: dict[str, float],
) -> dict[str, Any]:
    """成分股子表的个股财务指标(单日榜与区间链式榜共用, rank_date=指标时点日)

    返回 {"stock_pe", "stock_pb", "stock_growth", "stock_roe", "stock_div"} 五键(区间榜口径=
    区间末交易日, 与主表行业指标同一点位): 个股 PE(总市值口径, 四口径 pe_{basis})/PB(归母
    普通股东权益)/净利润同比(四口径, 分类文本)/ROE(四口径作商)/股息率(双口径 DPS÷close);
    全部命中财务缓存零新增请求; 值 None = 亏损/资不抵债, 键缺失 = 无数据(前端显示"—")。
    股息率段失败降级为空(与行业列一致, 不波及其他指标)。
    """
    # 个股估值(总市值口径, 与行业总市值口径公式一致): PE = 总市值(万元)/(净利润(元)/1e4);
    # PB = 总市值(万元)/(归母普通股股东权益(元)/1e4)——分母为 balancesheet_vip 权威绝对额
    # (归母权益−其他权益工具[已含优先股], 不经"每股×股本"折算)。循环按四口径利润键并集驱动,
    # 各口径覆盖互不连坐(归母合成失败仅缺归母列, 不影响该股其他口径/PB 行)
    stock_pe: dict[str, dict[str, float | None]] = {basis: {} for basis in PROFIT_BASES}
    profit_maps: dict[str, dict[str, float]] = {
        "attr_ttm": provider.get_ts_code_to_ttm_attr_profit(rank_date)[0],
        "attr_dynamic": provider.get_ts_code_to_dynamic_profit(rank_date, "attr")[0],
        "deduct_ttm": provider.get_ts_code_to_ttm_deducted_profit(rank_date)[0],
        "deduct_dynamic": provider.get_ts_code_to_dynamic_profit(rank_date, "deduct")[0],
    }
    equity_map, _ = provider.get_ts_code_to_equity(rank_date)
    stock_pb: dict[str, float | None] = {}
    for ts_code in set().union(*(set(m) for m in profit_maps.values())):
        total_mv = total_map.get(ts_code)
        if total_mv is None:
            continue
        for basis, profit_one_map in profit_maps.items():
            profit = profit_one_map.get(ts_code)
            if profit is not None:
                profit_wan = profit / 1e4
                stock_pe[basis][ts_code] = total_mv / profit_wan if profit_wan > 0 else None
        equity = equity_map.get(ts_code)
        if equity is not None:
            equity_wan = equity / 1e4
            stock_pb[ts_code] = total_mv / equity_wan if equity_wan > 0 else None

    # 个股净利润同比(与行业同一分类规则)四口径: 增长对命中缓存零请求(TTM 对与动态对各自
    # 缓存), 动态口径的"去年同季"落在 TTM 同比基期预热窗口内零新增请求(前端随"净利润口径"下拉切换)
    pair_maps: dict[str, dict[str, tuple[float, float]]] = {
        "attr_ttm": provider.get_ts_code_to_ttm_growth_pair(rank_date)[0],
        "attr_dynamic": provider.get_ts_code_to_dynamic_growth_pair(rank_date, "attr")[0],
        "deduct_ttm": provider.get_ts_code_to_ttm_growth_pair(rank_date, profit_kind="deduct")[0],
        "deduct_dynamic": provider.get_ts_code_to_dynamic_growth_pair(rank_date, "deduct")[0],
    }
    stock_growth: dict[str, dict[str, float | str]] = {
        basis: {
            ts_code: classify_profit_growth(now_value, last_value)
            for ts_code, (now_value, last_value) in pair_map.items()
        }
        for basis, pair_map in pair_maps.items()
    }

    # 个股 ROE 四口径(与行业整体法同一分子/分母对, 个股层面直接作商; 披露值 roe_waa 锚定、
    # 不接快报, 命中缓存零请求; 前端随"净利润口径"下拉切换、随"ROE算法"下拉选算法[当前仅加权])
    roe_pair_maps, _ = provider.get_ts_code_to_roes(rank_date)
    stock_roe: dict[str, dict[str, float]] = {
        basis: {
            ts_code: numerator / denominator * 100.0
            for ts_code, (numerator, denominator) in pair_map.items()
        }
        for basis, pair_map in roe_pair_maps.items()
    }

    # 个股股息率/回报率(三口径, 总额法 DPS ÷ 当日收盘价 × 100): DPS 命中分红缓存零请求;
    # est_bb = est + TTM 窗口注销分量折每股(见 share_change_data, 台阶缓存未就绪时该口径
    # 缺失前端显示"—")。键缺失 = 无数据(显示"—"), 值 0.0 = 齐备零分红(是数值);
    # 收盘价缺失/非正的股票无该列值。失败降级为空(分红缓存刷新失败等不波及其他指标)
    stock_div: dict[str, dict[str, float]] = {"est": {}, "static": {}}
    try:
        est_dps, static_dps, _ = provider.get_ts_code_to_dividend_dps(rank_date)

        def _dps_to_yield(dps_map: dict[str, float]) -> dict[str, float]:
            result_map: dict[str, float] = {}
            for ts_code, dps in dps_map.items():
                close = close_map.get(ts_code)
                if close is not None and close > 0:
                    result_map[ts_code] = dps / close * 100.0
            return result_map

        stock_div = {"est": _dps_to_yield(est_dps), "static": _dps_to_yield(static_dps)}
        # est_bb(股息+注销): TTM 窗口台阶法注销金额(万元)÷总股本(万股)=元/股, 与 DPS 相加同除
        # close; 总股本由 total_mv/close 回折(万元÷元=万股, 与 daily_basic 股本一致性足够)。
        # 嵌套 try: 仅该口径降级(台阶缓存未就绪等), est/static 不连坐
        try:
            bb_map, _ = provider.get_ts_code_to_buyback_amount(rank_date)
            est_bb_map: dict[str, float] = {}
            for ts_code, dps in est_dps.items():
                close = close_map.get(ts_code)
                total_mv = total_map.get(ts_code)
                if close is not None and close > 0 and total_mv is not None:
                    share_wan = total_mv / close
                    est_bb_map[ts_code] = (dps + bb_map.get(ts_code, 0.0) / share_wan) / close * 100.0
            stock_div["est_bb"] = est_bb_map
        except Exception as err:  # noqa: BLE001 - est_bb 子表口径降级
            logger.warning(f"个股注销分量(est_bb) 计算失败, 子表该口径全'—': {err!r}")
    except Exception as err:  # noqa: BLE001 - 股息率子表列降级
        logger.warning(f"个股股息率 计算失败, 子表该列全'—': {err!r}")

    return {
        "stock_pe": stock_pe,
        "stock_pb": stock_pb,
        "stock_growth": stock_growth,
        "stock_roe": stock_roe,
        "stock_div": stock_div,
    }


def _run_daily(
    job: Any,
    progress: Any,
    cancel_check: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, int]]:
    progress(0.0, "准备行业数据", "准备行业数据")
    tree, provider = _CONTEXT.ensure()
    before_calls = provider.snapshot_api_calls()

    rank_date = datetime.combine(job.payload["date"], datetime_time.min)
    date_str = rank_date.strftime("%Y%m%d")

    orch = run_daily_ranking(
        tree,
        provider,
        rank_date,
        progress_callback=lambda pct, message, phase: progress(pct, message, phase),
        cancel_check=cancel_check,
        sample_spaces=WEB_SAMPLE_SPACES,
    )
    # 多档: ({key: 六序列}, timings, {key: valuation}); 单档(降级): 旧 8 元组
    if isinstance(orch[0], dict):
        six_by_key, timings, valuation_by_key = orch
    else:
        ew, ew_reinvest, fw, fw_reinvest, tw, tw_reinvest, timings, valuation = orch
        six_by_key = {"full": (ew, ew_reinvest, fw, fw_reinvest, tw, tw_reinvest)}
        valuation_by_key = {"full": valuation}

    progress(95.0, "整理结果", "整理结果")
    pct_map = provider.get_ts_code_to_pct_chg(rank_date)
    close_map = provider.get_ts_code_to_close(rank_date)
    free_map = provider.get_ts_code_to_free_mv(rank_date)
    total_map = provider.get_ts_code_to_total_mv(rank_date)
    amount_map = provider.get_ts_code_to_amount(rank_date)

    # 成分股子表个股指标(公共编排, 与区间链式榜共用; 见 _compute_stock_metrics docstring)
    stock_metrics = _compute_stock_metrics(provider, rank_date, close_map, total_map)

    def _levels_for(key: str) -> dict[str, list[dict[str, Any]]]:
        """组装单个样本档的主表行(六条涨幅 + 全部财务指标字段)"""
        ew, ew_reinvest, fw, fw_reinvest, tw, tw_reinvest = six_by_key[key]
        v = valuation_by_key[key]
        return _build_levels(
            tree,
            ew,
            ew_reinvest,
            fw,
            fw_reinvest,
            tw,
            tw_reinvest,
            pe_maps={
                basis: {
                    "free": v[f"pe_{basis}"]["free"],
                    "total": v[f"pe_{basis}"]["total"],
                }
                for basis in PROFIT_BASES
            },
            pb_free=v["pb"]["free"],
            pb_total=v["pb"]["total"],
            growth_maps={basis: v[f"growth_{basis}"] for basis in PROFIT_BASES},
            roe_maps={basis: v[f"roe_waa_{basis}"] for basis in PROFIT_BASES},
            dividend_levels=v["div_yield"],
        )

    result = {
        "mode": "daily",
        "date": date_str,
        "samples": list(six_by_key),
        "levels": {key: _levels_for(key) for key in six_by_key},
    }
    # 各样本档的当日生效样本(子表过滤用; 编排已拉过月度快照, 此处命中缓存零请求)
    sample_pools_ctx: dict[str, set[str] | None] = {key: None for key in six_by_key}
    try:
        _segs = resolve_sample_segments(provider, list(six_by_key), date_str, date_str)
        sample_pools_ctx = {
            key: (None if SAMPLE_SPACES.get(key) is None else sample_pool_at(_segs[key], date_str))
            for key in six_by_key
        }
    except Exception as err:  # noqa: BLE001 - 子表过滤退化为全池
        logger.warning(f"样本空间子表过滤解析失败, 子表显示全池: {err!r}")

    context = {
        "mode": "daily",
        "date": rank_date,
        "tree": tree,
        "sample_pools": sample_pools_ctx,
        "pct_chg": pct_map,
        "close": close_map,
        "free_mv": free_map,
        "total_mv": total_map,
        "amount": amount_map,
        **stock_metrics,
    }
    api_calls = _diff_api_calls(before_calls, provider.snapshot_api_calls())
    return result, context, timings, api_calls


def _run_range(
    job: Any,
    progress: Any,
    cancel_check: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, int]]:
    progress(0.0, "准备行业数据", "准备行业数据")
    tree, provider = _CONTEXT.ensure()
    before_calls = provider.snapshot_api_calls()

    start_date = datetime.combine(job.payload["start_date"], datetime_time.min)
    end_date = datetime.combine(job.payload["end_date"], datetime_time.min)
    timings: dict[str, Any] = {}
    detail: dict[str, dict[str, float]] = {}
    chain_mode = bool(job.payload.get("chain", True))  # Web 默认官方逐日链式; 静态版仅显式 chain=false 或 CLI

    levels_by_key: dict[str, dict[str, list[dict[str, Any]]]] = {}
    end_day_str = end_date.strftime("%Y%m%d")
    sample_pools_ctx: dict[str, set[str] | None] = {"full": None}
    if chain_mode:
        # 官方逐日链式: 6 条序列(等权/自由流通/总市值 × 官方价格式/全收益式)逐日再平衡累计;
        # 财务指标(PE/PB/ROE/股息率/净利润同比)按**区间末交易日时点值**随链式一次算出
        # (rank_range_chain 内启动预热并与逐日计算并行), 前端区间表同样展示、注明时点口径;
        # 样本空间三档一次全算(涨幅单循环多桶、跨月快照分段换池)
        valuation_out: dict[str, Any] = {}
        orch = rank_range_chain(
            tree,
            provider,
            start_date,
            end_date,
            timings=timings,
            progress_callback=lambda pct, message: progress(pct, message, "计算区间涨幅"),
            detail=detail,
            cancel_check=cancel_check,
            valuation_out=valuation_out,
            sample_spaces=WEB_SAMPLE_SPACES,
        )

        def _levels_for(key: str) -> dict[str, list[dict[str, Any]]]:
            ew_p, ew_r, fw_p, fw_r, tw_p, tw_r = six_by_key[key]
            v = valuation_by_key[key]
            return _build_levels(
                tree, ew_p, ew_r, fw_p, fw_r, tw_p, tw_r,
                pe_maps={
                    basis: {
                        "free": v[f"pe_{basis}"]["free"],
                        "total": v[f"pe_{basis}"]["total"],
                    }
                    for basis in PROFIT_BASES
                },
                pb_free=v["pb"]["free"],
                pb_total=v["pb"]["total"],
                growth_maps={basis: v[f"growth_{basis}"] for basis in PROFIT_BASES},
                roe_maps={basis: v[f"roe_waa_{basis}"] for basis in PROFIT_BASES},
                dividend_levels=v["div_yield"],
            )

        if isinstance(orch, dict):
            # 多档: ({key: 六序列}); valuation_out 为 {key: valuation}(三档完整同构)
            six_by_key = orch
            valuation_by_key = valuation_out
        else:
            ew_p, ew_r, fw_p, fw_r, tw_p, tw_r = orch
            six_by_key = {"full": (ew_p, ew_r, fw_p, fw_r, tw_p, tw_r)}
            valuation_by_key = {"full": valuation_out}
        levels_by_key = {key: _levels_for(key) for key in six_by_key}
        # 子表过滤用的各档末日生效样本(编排已拉过月度快照, 命中缓存零请求)
        try:
            _segs = resolve_sample_segments(
                provider, list(six_by_key), start_date.strftime("%Y%m%d"), end_day_str
            )
            _last_day = max(
                (d for d in provider.get_trading_days(start_date.strftime("%Y%m%d"), end_day_str)),
                default=end_day_str,
            )
            sample_pools_ctx = {
                key: (None if SAMPLE_SPACES.get(key) is None else sample_pool_at(_segs[key], _last_day))
                for key in six_by_key
            }
        except Exception as err:  # noqa: BLE001 - 子表过滤退化为全池
            logger.warning(f"样本空间子表过滤解析失败, 子表显示全池: {err!r}")
    else:
        ew, fw, tw = rank_range(
            tree,
            provider,
            start_date,
            end_date,
            timings=timings,
            progress_callback=lambda pct, message: progress(pct, message, "拉取区间数据"),
            detail=detail,
            cancel_check=cancel_check,
        )
        # 静态版目前仅全收益式, 价格式列与全收益式同值(链式才有真正的官方价格式差异);
        # 样本空间仅链式版支持(静态版为对照模式), 恒为全 A 单档
        levels_by_key = {"full": _build_levels(tree, ew, ew, fw, fw, tw, tw)}
    progress(99.0, "整理结果", "整理结果")
    # 成分股子表展示用的末日自由流通市值/总市值/成交额（区间权重锚定首日盘前，市值列需另行补拉末日）:
    # 链式版统一取**区间末交易日**(end_date 落在非交易日时 daily_basic 无数据, 与主表指标同一
    # 时点); 静态版沿用 end_date 保持原行为
    metric_date = end_date
    if chain_mode:
        trading_days = provider.get_trading_days(start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"))
        if trading_days:
            metric_date = datetime.strptime(trading_days[-1], "%Y%m%d")
    end_free_mv = provider.get_ts_code_to_free_mv(metric_date)
    end_total_mv = provider.get_ts_code_to_total_mv(metric_date)
    end_amount = provider.get_ts_code_to_amount(metric_date)
    # 成分股子表个股指标(区间末交易日时点, 与主表行业指标同一点位; 公共编排与单日榜共用,
    # 财务缓存命中零新增请求; 静态版不算——context 无键时子表该五列显示"—")
    stock_metrics: dict[str, Any] = {}
    if chain_mode:
        stock_metrics = _compute_stock_metrics(
            provider, metric_date, detail.get("last_close") or {}, end_total_mv
        )
    result = {
        "mode": "range",
        "start_date": start_date.strftime("%Y%m%d"),
        "end_date": end_date.strftime("%Y%m%d"),
        "chain": chain_mode,
        "trading_days": timings.get("trading_days"),
        "samples": list(levels_by_key),
        "levels": levels_by_key,
    }
    context = {
        "mode": "range",
        "start_date": start_date,
        "end_date": end_date,
        "tree": tree,
        "sample_pools": sample_pools_ctx,
        "stock_ret": detail["stock_ret"],
        "last_close": detail["last_close"],
        "ts_code_to_free_mv": detail["ts_code_to_free_mv"],
        "ts_code_to_total_mv": detail["ts_code_to_total_mv"],
        "end_free_mv": end_free_mv,
        "end_total_mv": end_total_mv,
        "end_amount": end_amount,
        **stock_metrics,
    }
    api_calls = _diff_api_calls(before_calls, provider.snapshot_api_calls())
    return result, context, timings, api_calls


def _build_levels(
    tree: ShenWanIndustryTree,
    ew: tuple[list, list, list],
    ew_reinvest: tuple[list, list, list],
    fw: tuple[list, list, list],
    fw_reinvest: tuple[list, list, list],
    tw: tuple[list, list, list],
    tw_reinvest: tuple[list, list, list],
    pe_maps: dict[str, dict[str, dict[str, float | None]]] | None = None,
    pb_free: dict[str, dict[str, float | None]] | None = None,
    pb_total: dict[str, dict[str, float | None]] | None = None,
    growth_maps: dict[str, dict[str, dict[str, float | str]]] | None = None,
    roe_maps: dict[str, dict[str, dict[str, float]]] | None = None,
    dividend_levels: dict[str, dict[str, dict[str, float]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    levels: dict[str, list[dict[str, Any]]] = {}
    for level_name, ew_list, ew_tr_list, fw_list, fr_list, tw_list, tfr_list in zip(
        ("1", "2", "3"), ew, ew_reinvest, fw, fw_reinvest, tw, tw_reinvest
    ):
        ew_by_code = {code: (pct, count) for code, pct, count in ew_list}
        ewt_by_code = {code: (pct, count) for code, pct, count in ew_tr_list}
        fr_by_code = {code: (pct, count) for code, pct, count in fr_list}
        tw_by_code = {code: (pct, count) for code, pct, count in tw_list}
        tfr_by_code = {code: (pct, count) for code, pct, count in tfr_list}
        rows: list[dict[str, Any]] = []
        for index_code, fw_pct, fw_count in fw_list:
            ew_item = ew_by_code.get(index_code)
            if ew_item is None:
                raise ValueError(f"没有获取到等权涨幅数据: index_code={index_code}")
            ewt_item = ewt_by_code.get(index_code)
            if ewt_item is None:
                raise ValueError(f"没有获取到等权·分红再投资涨幅数据: index_code={index_code}")
            fr_item = fr_by_code.get(index_code)
            if fr_item is None:
                raise ValueError(f"没有获取到自由流通·分红再投资涨幅数据: index_code={index_code}")
            tw_item = tw_by_code.get(index_code)
            if tw_item is None:
                raise ValueError(f"没有获取到总市值加权涨幅数据: index_code={index_code}")
            tfr_item = tfr_by_code.get(index_code)
            if tfr_item is None:
                raise ValueError(f"没有获取到总市值·分红再投资涨幅数据: index_code={index_code}")
            node = tree.index_code_to_node.get(index_code)
            if node is None:
                continue
            row = {
                "index_code": index_code,
                "industry_name": node.industry_name_long,
                "total_weighted_pct": tw_item[0],
                "total_tr_weighted_pct": tfr_item[0],
                "float_weighted_pct": fw_pct,
                "float_tr_weighted_pct": fr_item[0],
                "equal_weighted_pct": ew_item[0],
                "equal_tr_weighted_pct": ewt_item[0],
                "total_constituent_count": tw_item[1],
                "total_tr_constituent_count": tfr_item[1],
                "float_constituent_count": fw_count,
                "float_tr_constituent_count": fr_item[1],
                "equal_constituent_count": ew_item[1],
                "equal_tr_constituent_count": ewt_item[1],
            }
            # 财务指标列仅单日榜携带: 值 None = PE 亏损 / PB 资不抵债, 键缺失 = 未计算/失败降级(前端显示"—");
            # PE 携带四口径字段 pe_{basis}_float/total(basis ∈ 归母/扣非 × TTM/动态, 见 PROFIT_BASES),
            # 前端"净利润口径"下拉切换显示; 成功时对应 dict 必含 "1"/"2"/"3" 键(非空), 空 dict 视为
            # 计算失败(与区间榜同不携带)
            for basis, basis_maps in (pe_maps or {}).items():
                basis_free = basis_maps.get("free")
                basis_total = basis_maps.get("total")
                if basis_free and basis_total:
                    row[f"pe_{basis}_float"] = basis_free.get(level_name, {}).get(index_code)
                    row[f"pe_{basis}_total"] = basis_total.get(level_name, {}).get(index_code)
            if pb_free and pb_total:
                row["pb_float"] = pb_free.get(level_name, {}).get(index_code)
                row["pb_total"] = pb_total.get(level_name, {}).get(index_code)
            # 净利润同比列仅单日/区间链式榜携带: 数值% | "扭亏"/"转亏"/"加大亏损"/"减少亏损",
            # 四口径 × 双市值口径字段 profit_growth_{basis}_{float|total}(2026-08-30 改双口径:
            # float=当日 ratio 分摊、total=全值, 随加权方式切换, 等权无值前端显示"—"), 另随
            # "净利润口径"下拉切换; 键缺失 = 未计算/失败降级/无参与股票(前端显示"—")
            for basis, basis_kinds in (growth_maps or {}).items():
                for kind in ("float", "total"):
                    kind_levels = (basis_kinds or {}).get(kind)
                    if kind_levels:
                        row[f"profit_growth_{basis}_{kind}"] = kind_levels.get(level_name, {}).get(index_code)
            # ROE 列(仅单日/区间链式榜携带, 市值加权算术平均; "ROE算法"下拉当前仅一档, 字段名带
            # 算法段 roe_waa_ 供将来扩展): **双市值口径字段 roe_waa_{basis}_float/total 随加权
            # 方式切换**(等权无市值权重, 前端显示"—"); 数值%, 键缺失 = 未计算/失败降级/无参与股票
            for basis, basis_value in (roe_maps or {}).items():
                for kind in ("float", "total"):
                    kind_levels = (basis_value or {}).get(kind)
                    if kind_levels:
                        row[f"roe_waa_{basis}_{kind}"] = kind_levels.get(level_name, {}).get(index_code)
            # 股息率列(仅单日/区间链式榜携带, 市值加权平均): **双市值口径字段 div_{basis}_float/
            # total 随加权方式切换**(等权显示"—"), 前端另随"回报率口径"下拉切换 est/static;
            # dividend_levels 结构 = {市值口径: {est/static: levels}}(与 roe_maps 的 {basis: {市值口径}}
            # 不同构——basis 维度在内层); 键缺失 = 未计算/失败降级/无参与股票(前端显示"—")
            for kind in ("float", "total"):
                kind_levels = (dividend_levels or {}).get(kind)
                if not kind_levels:
                    continue
                for basis, basis_value in kind_levels.items():
                    if basis_value:
                        row[f"div_{basis}_{kind}"] = basis_value.get(level_name, {}).get(index_code)
            rows.append(row)
        rows.sort(key=lambda item: item["float_weighted_pct"], reverse=True)
        levels[level_name] = rows
    return levels


def build_constituents(job: Any, level: int, index_code: str, weight: str, sample: str = "full") -> dict[str, Any]:
    """根据已完成任务的上下文生成某个行业的成分股子表(sample=样本空间档, 行集 = 行业∩该档样本)。"""
    if job.context is None:
        raise ValueError("任务上下文不存在")

    tree: ShenWanIndustryTree = job.context["tree"]
    node = tree.index_code_to_node.get(index_code)
    if node is None or node not in tree.level_to_nodes.get(level, []):
        raise ValueError(f"找不到层级 {level} 的行业节点: {index_code}")
    if sample not in SAMPLE_SPACES:
        raise ValueError(f"未知样本空间档: {sample}")
    sample_set = (job.context.get("sample_pools") or {}).get(sample, None)

    if job.context["mode"] == "daily":
        rows = _daily_constituents(job.context, level, index_code, weight, sample_set)
    else:
        rows = _range_constituents(job.context, level, index_code, weight, sample_set)

    rows.sort(key=lambda item: item["pct_chg"] if item["pct_chg"] is not None else -math.inf, reverse=True)
    return {
        "job_id": job.id,
        "level": level,
        "index_code": index_code,
        "industry_name": node.industry_name_long,
        "rows": rows,
    }


def _daily_constituents(
    context: dict[str, Any], level: int, index_code: str, weight: str, sample_set: set[str] | None = None,
) -> list[dict[str, Any]]:
    tree: ShenWanIndustryTree = context["tree"]
    rank_date: datetime = context["date"]
    pct_map: dict[str, float | None] = context["pct_chg"]
    close_map: dict[str, float] = context["close"]
    free_map: dict[str, float] = context["free_mv"]
    total_map: dict[str, float] = context["total_mv"]
    amount_map: dict[str, float] = context["amount"]

    stock_pool = set(pct_map) | set(tree.all_member_codes)
    tree.filter_stock_pool(stock_pool, rank_date, rank_date)

    # 市值加权子表口径: float/float_tr 用自由流通市值、total/total_tr 用总市值, 缺失市值不参与
    mv_map = context["total_mv"] if weight in ("total", "total_tr") else free_map
    weight_filtered = weight in ("float", "float_tr", "total", "total_tr")

    rows: list[dict[str, Any]] = []
    for ts_code in stock_pool:
        if sample_set is not None and ts_code not in sample_set:
            continue  # 样本空间档过滤(行业成分 = 样本 ∩ 当日申万归属)
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code, rank_date)
        if not l1_node or not l2_node or not l3_node:
            continue

        node_for_level = {1: l1_node, 2: l2_node, 3: l3_node}[level]
        if node_for_level.index_code != index_code:
            continue

        if ts_code in pct_map and pct_map[ts_code] is None:
            continue
        pct_chg = pct_map.get(ts_code, 0.0)

        if weight_filtered:
            mv = mv_map.get(ts_code)
            if mv is None or (isinstance(mv, float) and math.isnan(mv)):
                continue

        row = {
            "ts_code": ts_code,
            "name": tree.stock_basic.get(ts_code, {}).get("name", ""),
            "pct_chg": pct_chg,
            "close": close_map.get(ts_code),
            "free_mv": free_map.get(ts_code),
            "total_mv": total_map.get(ts_code),
            "amount": amount_map.get(ts_code),
        }
        # 个股估值列: 单日榜(当日时点)与区间链式榜(区间末交易日时点)均携带, 静态版区间榜
        # 不计算(context 无键, 前端显示"—"); PE 四口径(pe_{basis})与净利润同比四口径
        # (profit_growth_{basis})一次全带, 前端"净利润口径"下拉切换; 键缺失 = 无数据,
        # None = 亏损/资不抵债
        if "stock_pe" in context:
            for basis, basis_pe in context["stock_pe"].items():
                row[f"pe_{basis}"] = basis_pe.get(ts_code)
            row["pb"] = context["stock_pb"].get(ts_code)
            for basis, basis_growth in context["stock_growth"].items():
                row[f"profit_growth_{basis}"] = basis_growth.get(ts_code)
            for basis, basis_value in context["stock_roe"].items():
                row[f"roe_waa_{basis}"] = basis_value.get(ts_code)
            # 个股股息率(双口径, DPS/close): 键缺失 = 无数据, 值 0.0 = 齐备零分红
            for basis, basis_div in context["stock_div"].items():
                row[f"div_{basis}"] = basis_div.get(ts_code)
        rows.append(row)
    return rows


def _range_constituents(
    context: dict[str, Any], level: int, index_code: str, weight: str, sample_set: set[str] | None = None,
) -> list[dict[str, Any]]:
    tree: ShenWanIndustryTree = context["tree"]
    stock_ret: dict[str, float] = context["stock_ret"]
    last_close: dict[str, float] = context["last_close"]
    free_map: dict[str, float] = context["ts_code_to_free_mv"]
    end_free_mv: dict[str, float] = context["end_free_mv"]
    end_total_mv: dict[str, float] = context["end_total_mv"]
    end_amount: dict[str, float] = context["end_amount"]

    # 市值加权子表口径: float/float_tr 用首日盘前自由流通市值、total/total_tr 用首日盘前总市值, 缺失市值不参与
    mv_map = context["ts_code_to_total_mv"] if weight in ("total", "total_tr") else free_map
    weight_filtered = weight in ("float", "float_tr", "total", "total_tr")

    rows: list[dict[str, Any]] = []
    start_date: datetime = context["start_date"]
    for ts_code, pct_chg in stock_ret.items():
        if sample_set is not None and ts_code not in sample_set:
            continue  # 样本空间档过滤(区间末生效样本 ∩ 起始日行业归属)
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code, start_date)
        if not l1_node or not l2_node or not l3_node:
            continue

        node_for_level = {1: l1_node, 2: l2_node, 3: l3_node}[level]
        if node_for_level.index_code != index_code:
            continue

        if weight_filtered:
            mv = mv_map.get(ts_code)
            if mv is None or (isinstance(mv, float) and math.isnan(mv)):
                continue

        # 涨跌幅/收盘/市值/成交额之外, 个股估值列链式版携带(区间末交易日时点, 外层主表已
        # 注明口径、子表不再重复); PE 四口径(pe_{basis})与净利润同比四口径(profit_growth_{basis})
        # 一次全带, 前端"净利润口径"下拉切换; 键缺失 = 无数据(静态版区间榜不计算, 前端显示"—"),
        # None = 亏损/资不抵债
        row = {
            "ts_code": ts_code,
            "name": tree.stock_basic.get(ts_code, {}).get("name", ""),
            "pct_chg": pct_chg,
            "close": last_close.get(ts_code),
            "free_mv": end_free_mv.get(ts_code),
            "total_mv": end_total_mv.get(ts_code),
            "amount": end_amount.get(ts_code),
        }
        if "stock_pe" in context:
            for basis, basis_pe in context["stock_pe"].items():
                row[f"pe_{basis}"] = basis_pe.get(ts_code)
            row["pb"] = context["stock_pb"].get(ts_code)
            for basis, basis_growth in context["stock_growth"].items():
                row[f"profit_growth_{basis}"] = basis_growth.get(ts_code)
            for basis, basis_value in context["stock_roe"].items():
                row[f"roe_waa_{basis}"] = basis_value.get(ts_code)
            # 个股股息率(双口径, DPS/close): 键缺失 = 无数据, 值 0.0 = 齐备零分红
            for basis, basis_div in context["stock_div"].items():
                row[f"div_{basis}"] = basis_div.get(ts_code)
        rows.append(row)
    return rows


def service_is_ready() -> bool:
    return _CONTEXT.is_ready()
