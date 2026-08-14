"""Web 服务与现有申万排行算法之间的适配层。"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import warnings
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Iterator

import tushare as ts
from vnpy.trader.setting import SETTINGS

from ..industry_ranking import (
    run_daily_ranking,
    rank_range,
)
from ..industry_tree import ShenWanIndustryTree
from ..market_data import MarketDataProvider


logger = logging.getLogger("shenwan_industry.web.service")
_NO_INDUSTRY_STOCKS: set[str] = set()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SW2021_PATH = _REPO_ROOT / "shenwan_industry" / "SW2021.json"

with _SW2021_PATH.open("r", encoding="utf-8") as _fp:
    _L1_INDEXES = {
        row["index_code"]: row["industry_name"]
        for row in json.load(_fp)
        if row["level"] == "L1"
    }


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
    token = SETTINGS.get("datafeed.password", "")
    if not token:
        raise ValueError("请先在 vnpy 的 datafeed.password 配置中设置你的 tushare token")
    return token


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


def get_index_kline(
    index_code: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """获取申万一级行业官方指数日 K 线。"""
    index_code = index_code.strip()
    if index_code not in _L1_INDEXES:
        raise ValueError(f"不是有效的申万一级行业指数代码: {index_code}")

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
        bars.append(
            {
                "date": str(row.trade_date),
                "open": _safe_float(row.open),
                "high": _safe_float(row.high),
                "low": _safe_float(row.low),
                "close": _safe_float(row.close),
                "vol": _safe_float(getattr(row, "vol", None)),
                "amount": _safe_float(getattr(row, "amount", None)),
            }
        )

    return {
        "index_code": index_code,
        "name": _L1_INDEXES[index_code],
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
    行情/市值按日期内存缓存跨任务复用（单 worker 串行，无需加锁）。
    """

    def __init__(self) -> None:
        self._tree: ShenWanIndustryTree | None = None
        self._provider: MarketDataProvider | None = None
        self._lock = threading.Lock()

    def ensure(self) -> tuple[ShenWanIndustryTree, MarketDataProvider]:
        if self._tree is not None:
            return self._tree, self._provider

        with self._lock:
            if self._tree is not None:
                return self._tree, self._provider

            base_pro = ts.pro_api(token=_get_token())
            provider = MarketDataProvider(base_pro)
            tree = ShenWanIndustryTree(tushare_pro=provider.pro)
            tree.build_industries()
            tree.build_constituent_stocks_by_tushare()
            self._tree = tree
            self._provider = provider
            return tree, provider

    def is_ready(self) -> bool:
        return self._tree is not None


_CONTEXT = PreparedContext()


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

    ew, fw, timings = run_daily_ranking(
        tree,
        provider,
        rank_date,
        progress_callback=lambda pct, message, phase: progress(pct, message, phase),
        cancel_check=cancel_check,
    )

    progress(95.0, "整理结果", "整理结果")
    pct_map = provider.get_ts_code_to_pct_chg(rank_date)
    close_map = provider.get_ts_code_to_close(rank_date)
    circ_map = provider.get_ts_code_to_circ_mv(rank_date)

    result = {
        "mode": "daily",
        "date": date_str,
        "levels": _build_levels(tree, ew, fw),
    }
    context = {
        "mode": "daily",
        "date": rank_date,
        "tree": tree,
        "pct_chg": pct_map,
        "close": close_map,
        "circ_mv": circ_map,
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

    ew, fw = rank_range(
        tree,
        provider,
        start_date,
        end_date,
        timings=timings,
        progress_callback=lambda pct, message: progress(pct, message, "拉取区间数据"),
        detail=detail,
        cancel_check=cancel_check,
    )
    progress(99.0, "整理结果", "整理结果")
    result = {
        "mode": "range",
        "start_date": start_date.strftime("%Y%m%d"),
        "end_date": end_date.strftime("%Y%m%d"),
        "trading_days": timings.get("trading_days"),
        "levels": _build_levels(tree, ew, fw),
    }
    context = {
        "mode": "range",
        "start_date": start_date,
        "end_date": end_date,
        "tree": tree,
        "stock_ret": detail["stock_ret"],
        "last_close": detail["last_close"],
        "ts_code_to_circ_mv": detail["ts_code_to_circ_mv"],
    }
    api_calls = _diff_api_calls(before_calls, provider.snapshot_api_calls())
    return result, context, timings, api_calls


def _build_levels(
    tree: ShenWanIndustryTree,
    ew: tuple[list, list, list],
    fw: tuple[list, list, list],
) -> dict[str, list[dict[str, Any]]]:
    levels: dict[str, list[dict[str, Any]]] = {}
    for level_name, ew_list, fw_list in zip(("1", "2", "3"), ew, fw):
        ew_by_code = {code: (pct, count) for code, pct, count in ew_list}
        rows: list[dict[str, Any]] = []
        for index_code, fw_pct, fw_count in fw_list:
            ew_item = ew_by_code.get(index_code)
            if ew_item is None:
                raise ValueError(f"没有获取到等权涨幅数据: index_code={index_code}")
            node = tree.index_code_to_node.get(index_code)
            if node is None:
                continue
            rows.append(
                {
                    "index_code": index_code,
                    "industry_name": node.industry_name_long,
                    "float_weighted_pct": fw_pct,
                    "equal_weighted_pct": ew_item[0],
                    "float_constituent_count": fw_count,
                    "equal_constituent_count": ew_item[1],
                }
            )
        rows.sort(key=lambda item: item["float_weighted_pct"], reverse=True)
        levels[level_name] = rows
    return levels


def build_constituents(job: Any, level: int, index_code: str, weight: str) -> dict[str, Any]:
    """根据已完成任务的上下文生成某个行业的成分股子表。"""
    if job.context is None:
        raise ValueError("任务上下文不存在")

    tree: ShenWanIndustryTree = job.context["tree"]
    node = tree.index_code_to_node.get(index_code)
    if node is None or node not in tree.level_to_nodes.get(level, []):
        raise ValueError(f"找不到层级 {level} 的行业节点: {index_code}")

    if job.context["mode"] == "daily":
        rows = _daily_constituents(job.context, level, index_code, weight)
    else:
        rows = _range_constituents(job.context, level, index_code, weight)

    rows.sort(key=lambda item: item["pct_chg"] if item["pct_chg"] is not None else -math.inf, reverse=True)
    return {
        "job_id": job.id,
        "level": level,
        "index_code": index_code,
        "industry_name": node.industry_name_long,
        "rows": rows,
    }


def _daily_constituents(context: dict[str, Any], level: int, index_code: str, weight: str) -> list[dict[str, Any]]:
    tree: ShenWanIndustryTree = context["tree"]
    rank_date: datetime = context["date"]
    pct_map: dict[str, float | None] = context["pct_chg"]
    close_map: dict[str, float] = context["close"]
    circ_map: dict[str, float] = context["circ_mv"]

    stock_pool = set(pct_map) | set(tree.constituent_stock_to_l3_node)
    tree.filter_stock_pool(stock_pool, rank_date, rank_date)

    rows: list[dict[str, Any]] = []
    for ts_code in stock_pool:
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code)
        if not l1_node or not l2_node or not l3_node:
            continue

        node_for_level = {1: l1_node, 2: l2_node, 3: l3_node}[level]
        if node_for_level.index_code != index_code:
            continue

        if ts_code in pct_map and pct_map[ts_code] is None:
            continue
        pct_chg = pct_map.get(ts_code, 0.0)

        if weight == "float":
            circ_mv = circ_map.get(ts_code)
            if circ_mv is None or (isinstance(circ_mv, float) and math.isnan(circ_mv)):
                continue

        rows.append(
            {
                "ts_code": ts_code,
                "name": tree.stock_basic.get(ts_code, {}).get("name", ""),
                "pct_chg": pct_chg,
                "close": close_map.get(ts_code),
            }
        )
    return rows


def _range_constituents(context: dict[str, Any], level: int, index_code: str, weight: str) -> list[dict[str, Any]]:
    tree: ShenWanIndustryTree = context["tree"]
    stock_ret: dict[str, float] = context["stock_ret"]
    last_close: dict[str, float] = context["last_close"]
    circ_map: dict[str, float] = context["ts_code_to_circ_mv"]

    rows: list[dict[str, Any]] = []
    for ts_code, pct_chg in stock_ret.items():
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code)
        if not l1_node or not l2_node or not l3_node:
            continue

        node_for_level = {1: l1_node, 2: l2_node, 3: l3_node}[level]
        if node_for_level.index_code != index_code:
            continue

        if weight == "float":
            circ_mv = circ_map.get(ts_code)
            if circ_mv is None or (isinstance(circ_mv, float) and math.isnan(circ_mv)):
                continue

        rows.append(
            {
                "ts_code": ts_code,
                "name": tree.stock_basic.get(ts_code, {}).get("name", ""),
                "pct_chg": pct_chg,
                "close": last_close.get(ts_code),
            }
        )
    return rows


def service_is_ready() -> bool:
    return _CONTEXT.is_ready()
