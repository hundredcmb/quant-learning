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

from ..config_store import config_path, get_token, set_token
from ..industry_ranking import (
    run_daily_ranking,
    rank_range,
    rank_range_chain,
)
from ..industry_tree import ShenWanIndustryTree
from ..market_data import MarketDataProvider


logger = logging.getLogger("shenwan_industry.web.service")
_NO_INDUSTRY_STOCKS: set[str] = set()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SW2021_PATH = _REPO_ROOT / "shenwan_industry" / "data" / "SW2021.json"
# 官方指数日线可用性缓存（随仓库提交，写入后最近一个周六 00:00 过期，过期后下次访问自动重探测，约合每周刷新一次）
_SW_DAILY_AVAILABLE_PATH = _REPO_ROOT / "shenwan_industry" / "data" / "sw_index_daily_available.json"


def _sw_daily_available_expire_time(write_date: datetime) -> datetime:
    """缓存有效期截止：写入日期之后最近的一个周六 00:00（写入当天为周六则顺延一周）"""
    days_ahead = 5 - write_date.weekday()  # 周一=0 ... 周六=5
    if days_ahead <= 0:
        days_ahead += 7
    return datetime(write_date.year, write_date.month, write_date.day) + timedelta(days=days_ahead)

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


_sw_daily_available: set[str] | None = None
_sw_daily_available_lock = threading.Lock()


def _load_sw_daily_available_cached() -> set[str] | None:
    """读磁盘缓存（在写入后最近一个周六 00:00 前有效），缺失或过期返回 None"""
    try:
        data = json.loads(_SW_DAILY_AVAILABLE_PATH.read_text(encoding="utf-8"))
        timestamp = datetime.strptime(data["timestamp"], "%Y-%m-%d")
        if datetime.now() < _sw_daily_available_expire_time(timestamp):
            return set(data["codes"])
    except Exception:
        pass
    return None


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


def _probe_sw_daily_available() -> set[str] | None:
    """探测官方指数日线覆盖：sw_daily(trade_date=最新交易日) 一次拉全市场；
    空结果回退前一个交易日再试，全部失败返回 None"""
    try:
        pro = ts.pro_api(token=_get_token())
        for date_str in _latest_trade_dates():
            df = pro.sw_daily(trade_date=date_str)
            if df is not None and len(df) > 0:
                return set(df["ts_code"].astype(str).tolist())
    except Exception as err:  # noqa: BLE001 - 网络/token 异常
        logger.warning("探测官方指数日线可用性失败: %s", err)
        return None
    return None


def get_sw_daily_available() -> set[str] | None:
    """有官方指数日线数据的行业指数代码集合（L1 全覆盖恒含，L2/L3 以探测为准）。

    - 磁盘缓存在写入后最近一个周六 00:00 前直接复用（约合每周刷新；sw_index_daily_available.json，随仓库提交，离线可用）
    - 否则 sw_daily(trade_date=最新交易日) 全市场一次拉取，与 SW2021.json 的 L2/L3 求交集
    - 探测失败返回 None：调用方回退为"仅 L1 可点击"，不缓存、下次再试
    """
    global _sw_daily_available
    with _sw_daily_available_lock:
        if _sw_daily_available is not None:
            return _sw_daily_available
        cached = _load_sw_daily_available_cached()
        if cached is not None:
            _sw_daily_available = cached
            return _sw_daily_available

        probed = _probe_sw_daily_available()
        if probed is None:
            return None
        available = (probed & _L2_L3_INDEXES) | set(_L1_INDEXES)
        _sw_daily_available = available
        try:
            _SW_DAILY_AVAILABLE_PATH.write_text(
                json.dumps(
                    {"timestamp": date.today().strftime("%Y-%m-%d"), "codes": sorted(available)},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except Exception as err:  # noqa: BLE001 - 缓存写失败不影响本次使用
            logger.warning("写入指数可用性缓存失败: %s", err)
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

    ew, ew_reinvest, fw, fw_reinvest, tw, tw_reinvest, timings = run_daily_ranking(
        tree,
        provider,
        rank_date,
        progress_callback=lambda pct, message, phase: progress(pct, message, phase),
        cancel_check=cancel_check,
    )

    progress(95.0, "整理结果", "整理结果")
    pct_map = provider.get_ts_code_to_pct_chg(rank_date)
    close_map = provider.get_ts_code_to_close(rank_date)
    free_map = provider.get_ts_code_to_free_mv(rank_date)
    total_map = provider.get_ts_code_to_total_mv(rank_date)
    amount_map = provider.get_ts_code_to_amount(rank_date)

    result = {
        "mode": "daily",
        "date": date_str,
        "levels": _build_levels(tree, ew, ew_reinvest, fw, fw_reinvest, tw, tw_reinvest),
    }
    context = {
        "mode": "daily",
        "date": rank_date,
        "tree": tree,
        "pct_chg": pct_map,
        "close": close_map,
        "free_mv": free_map,
        "total_mv": total_map,
        "amount": amount_map,
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

    if chain_mode:
        # 官方逐日链式: 6 条序列(等权/自由流通/总市值 × 官方价格式/全收益式)逐日再平衡累计
        ew_p, ew_r, fw_p, fw_r, tw_p, tw_r = rank_range_chain(
            tree,
            provider,
            start_date,
            end_date,
            timings=timings,
            progress_callback=lambda pct, message: progress(pct, message, "计算区间涨幅"),
            detail=detail,
            cancel_check=cancel_check,
        )
        levels = _build_levels(tree, ew_p, ew_r, fw_p, fw_r, tw_p, tw_r)
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
        # 静态版目前仅全收益式, 价格式列与全收益式同值(链式才有真正的官方价格式差异)
        levels = _build_levels(tree, ew, ew, fw, fw, tw, tw)
    progress(99.0, "整理结果", "整理结果")
    # 成分股子表展示用的末日自由流通市值/总市值/成交额（区间权重锚定首日盘前，市值列需另行补拉末日）
    end_free_mv = provider.get_ts_code_to_free_mv(end_date)
    end_total_mv = provider.get_ts_code_to_total_mv(end_date)
    end_amount = provider.get_ts_code_to_amount(end_date)
    result = {
        "mode": "range",
        "start_date": start_date.strftime("%Y%m%d"),
        "end_date": end_date.strftime("%Y%m%d"),
        "chain": chain_mode,
        "trading_days": timings.get("trading_days"),
        "levels": levels,
    }
    context = {
        "mode": "range",
        "start_date": start_date,
        "end_date": end_date,
        "tree": tree,
        "stock_ret": detail["stock_ret"],
        "last_close": detail["last_close"],
        "ts_code_to_free_mv": detail["ts_code_to_free_mv"],
        "ts_code_to_total_mv": detail["ts_code_to_total_mv"],
        "end_free_mv": end_free_mv,
        "end_total_mv": end_total_mv,
        "end_amount": end_amount,
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
            rows.append(
                {
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

        rows.append(
            {
                "ts_code": ts_code,
                "name": tree.stock_basic.get(ts_code, {}).get("name", ""),
                "pct_chg": pct_chg,
                "close": close_map.get(ts_code),
                "free_mv": free_map.get(ts_code),
                "total_mv": total_map.get(ts_code),
                "amount": amount_map.get(ts_code),
            }
        )
    return rows


def _range_constituents(context: dict[str, Any], level: int, index_code: str, weight: str) -> list[dict[str, Any]]:
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

        rows.append(
            {
                "ts_code": ts_code,
                "name": tree.stock_basic.get(ts_code, {}).get("name", ""),
                "pct_chg": pct_chg,
                "close": last_close.get(ts_code),
                "free_mv": end_free_mv.get(ts_code),
                "total_mv": end_total_mv.get(ts_code),
                "amount": end_amount.get(ts_code),
            }
        )
    return rows


def service_is_ready() -> bool:
    return _CONTEXT.is_ready()
