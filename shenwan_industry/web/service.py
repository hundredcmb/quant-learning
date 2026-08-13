"""Web 服务与现有申万排行算法之间的适配层。"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import warnings
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any, Iterator

import tushare as ts
from vnpy.trader.setting import SETTINGS

from ..industry_ranking import (
    daily_rank_equal_weight,
    daily_rank_float_weight,
    rank_range,
    wrap_api_counter,
)
from ..industry_tree import ShenWanIndustryTree


logger = logging.getLogger("shenwan_industry.web.service")
_NO_INDUSTRY_STOCKS: set[str] = set()


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


class PreparedContext:
    """缓存行业树和基础 pro 对象，首版仅单 worker 访问。"""

    def __init__(self) -> None:
        self._tree: ShenWanIndustryTree | None = None
        self._base_pro: Any = None
        self._lock = threading.Lock()

    def ensure(self) -> tuple[ShenWanIndustryTree, Any]:
        if self._tree is not None:
            return self._tree, self._base_pro

        with self._lock:
            if self._tree is not None:
                return self._tree, self._base_pro

            base_pro = ts.pro_api(token=_get_token())
            tree = ShenWanIndustryTree(tushare_pro=base_pro)
            tree.build_industries()
            tree.build_constituent_stocks_by_tushare()
            self._tree = tree
            self._base_pro = base_pro
            return tree, base_pro

    def is_ready(self) -> bool:
        return self._tree is not None


_CONTEXT = PreparedContext()


def run_worker(job: Any, progress: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, int]]:
    """JobManager 的 worker 入口。"""
    with _capture_no_industry_warnings():
        if job.mode == "daily":
            result = _run_daily(job, progress)
        elif job.mode == "range":
            result = _run_range(job, progress)
        else:
            raise ValueError(f"不支持的任务类型: {job.mode}")
    _flush_no_industry_warnings()
    return result


def _run_daily(job: Any, progress: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, int]]:
    progress(0.0, "准备行业数据", "准备行业数据")
    tree, base_pro = _CONTEXT.ensure()
    job_pro = ts.pro_api(token=_get_token())
    api_calls = wrap_api_counter(job_pro)
    tree.pro = job_pro

    rank_date = datetime.combine(job.payload["date"], datetime_time.min)
    date_str = rank_date.strftime("%Y%m%d")
    timings: dict[str, float] = {}

    try:
        progress(8.0, "拉取日线行情", "拉取日线行情")
        t0 = time.perf_counter()
        pct_map = tree.get_ts_code_to_pct_chg(rank_date)
        timings["daily_fetch"] = time.perf_counter() - t0
        if not pct_map:
            raise ValueError(f"{date_str} 不是交易日，或未获取到当日行情")

        progress(48.0, "拉取流通市值", "拉取流通市值")
        t0 = time.perf_counter()
        circ_map = tree.get_ts_code_to_circ_mv(rank_date)
        timings["circ_fetch"] = time.perf_counter() - t0

        progress(68.0, "计算等权涨幅", "计算排行榜")
        t0 = time.perf_counter()
        ew = daily_rank_equal_weight(tree, rank_date)
        timings["equal_compute"] = time.perf_counter() - t0

        progress(80.0, "计算流通市值加权涨幅", "计算排行榜")
        fw_timings: dict[str, float] = {}
        t0 = time.perf_counter()
        fw = daily_rank_float_weight(tree, rank_date, timings=fw_timings)
        timings["float_compute"] = time.perf_counter() - t0
        timings["float_fallback"] = fw_timings.get("circ_fallback", 0.0)

        close_map = tree.get_ts_code_to_close(rank_date)
        progress(95.0, "整理结果", "整理结果")

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
        return result, context, timings, api_calls
    finally:
        tree.pro = base_pro


def _run_range(job: Any, progress: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, int]]:
    progress(0.0, "准备行业数据", "准备行业数据")
    tree, base_pro = _CONTEXT.ensure()
    job_pro = ts.pro_api(token=_get_token())
    api_calls = wrap_api_counter(job_pro)
    tree.pro = job_pro

    start_date = datetime.combine(job.payload["start_date"], datetime_time.min)
    end_date = datetime.combine(job.payload["end_date"], datetime_time.min)
    timings: dict[str, Any] = {}
    detail: dict[str, dict[str, float]] = {}

    try:
        ew, fw = rank_range(
            tree,
            start_date,
            end_date,
            timings=timings,
            progress_callback=lambda pct, message: progress(pct, message, "拉取区间数据"),
            detail=detail,
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
        return result, context, timings, api_calls
    finally:
        tree.pro = base_pro


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
    tree.filter_stock_pool(rank_date, stock_pool)

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
