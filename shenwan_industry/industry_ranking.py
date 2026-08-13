"""
申万行业排行榜: 单日榜 + 区间榜

- daily_rank_equal_weight / daily_rank_float_weight: 单日榜 (逻辑与原 classification.py 一致, 未改动)
- rank_range: 区间累计涨幅榜, 支持 timings 参数记录各阶段耗时
- wrap_api_counter / print_timing: 入口脚本用的接口调用计数与耗时输出工具

区间榜网络策略: 区间内每个交易日拉一次 daily(trade_date), 用每日官方涨跌幅
(close/pre_close, 除权除息日即除权参考价口径) 连乘得到个股区间收益;
停牌日无行自动按 0% 累计, 不再逐股回退查收益; 权重取区间起始日流通市值
(daily_basic 一次 + 仅起始日停牌的少量回退)。
"""

import logging
import math
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd

try:
    from .industry_tree import ShenWanIndustryTree
except ImportError:  # 直接运行本文件时
    from industry_tree import ShenWanIndustryTree

logger = logging.getLogger("shenwan_industry.industry_ranking")

# 榜单项: (行业 index_code, 涨跌幅%, 成分股数量)
RankList = list[tuple[str, float, int]]

# 进度回调: (0~100 的百分比, 阶段说明)
ProgressCallback = Callable[[float, str], None]

# 并发与限流: 本账号 5000 积分实测单接口 500 次/分钟(60 秒滚动窗口)。
# 逐日行情并发拉取时按固定速率平摊请求, 避免瞬时爆发与官方窗口微小不对齐触发 429。
MAX_DAILY_FETCH_WORKERS = 8   # 并发线程数
MAX_DAILY_FETCH_RATE = 7.5    # 请求开始速率上限(次/秒), 约 450 次/分钟, 留 10% 余量
DAILY_FETCH_RETRY = 3         # 单日失败重试次数(网络抖动/瞬时 429)


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


def print_timing(
    groups: list[tuple[str, list[tuple[str, float]]]],
    api_calls: dict[str, int] | None = None,
) -> None:
    """
    控制台输出耗时分析(按主次分组)

    groups: [(组名, [(阶段名, 秒数), ...]), ...], 每组显示小计与占比, 可选附 API 调用次数
    """
    total = sum(secs for _, items in groups for _, secs in items)
    print("\n===== 耗时分析 =====")
    for group_name, items in groups:
        group_total = sum(secs for _, secs in items)
        group_pct = group_total / total * 100 if total > 0 else 0.0
        print(f"[{group_name}] 小计 {group_total:6.2f}s ({group_pct:5.1f}%)")
        for name, secs in items:
            pct = secs / total * 100 if total > 0 else 0.0
            print(f"  {name:<28s} {secs:8.2f}s  {pct:5.1f}%")
    print(f"总计 {total:8.2f}s")
    if api_calls:
        detail = ", ".join(f"{k} {v}次" for k, v in api_calls.items() if v)
        print(f"  API 调用: {detail}")
def daily_rank_equal_weight(
    tree: ShenWanIndustryTree,
    date: datetime,
) -> tuple[RankList, RankList, RankList]:
    """获取指定日期的行业涨幅(等权)排名"""
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")

    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    ts_code_to_pct_chg: dict[str, float] = tree.get_ts_code_to_pct_chg(date)
    if not ts_code_to_pct_chg:
        raise ValueError(f"没有获取到 {date_str} 交易日的行情数据")

    # 行业index_code -> (行业index_code, 上涨百分比, 成分股数量)
    l1_chg_map: dict[str, tuple[str, float, int]] = {}
    l2_chg_map: dict[str, tuple[str, float, int]] = {}
    l3_chg_map: dict[str, tuple[str, float, int]] = {}

    for node_l1 in tree.level_to_nodes[1]:
        l1_chg_map[node_l1.index_code] = (node_l1.index_code, 0, 0)
    for node_l2 in tree.level_to_nodes[2]:
        l2_chg_map[node_l2.index_code] = (node_l2.index_code, 0, 0)
    for node_l3 in tree.level_to_nodes[3]:
        l3_chg_map[node_l3.index_code] = (node_l3.index_code, 0, 0)

    stock_pool: set[str] = set(ts_code_to_pct_chg) | set(tree.constituent_stock_to_l3_node)
    tree.filter_stock_pool(date, stock_pool)

    for ts_code in stock_pool:
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code)
        if not l3_node or not l2_node or not l1_node:
            continue

        pct_chg = ts_code_to_pct_chg.get(ts_code, 0.0)  # 有交易数据则用实际涨幅, 停牌则按0%
        if pct_chg is None:
            continue  # 数据异常(涨跌幅非有限值), 不计入
        for l_node, l_chg_map in [(l3_node, l3_chg_map), (l2_node, l2_chg_map), (l1_node, l1_chg_map)]:
            l_index_code, l_pct_chg, l_count = l_chg_map.get(l_node.index_code)
            l_count_new = l_count + 1
            l_pct_chg_new = (l_pct_chg * l_count + pct_chg) / l_count_new
            l_chg_map[l_node.index_code] = (l_index_code, l_pct_chg_new, l_count_new)

    # 对行业涨幅由大到小排序
    l1_rank_list = sorted(
        [item for item in l1_chg_map.values() if item[2] > 0],
        key=lambda x: x[1],
        reverse=True,
    )
    l2_rank_list = sorted(
        [item for item in l2_chg_map.values() if item[2] > 0],
        key=lambda x: x[1],
        reverse=True,
    )
    l3_rank_list = sorted(
        [item for item in l3_chg_map.values() if item[2] > 0],
        key=lambda x: x[1],
        reverse=True,
    )

    return l1_rank_list, l2_rank_list, l3_rank_list


def _resolve_circ_mv(
    tree: ShenWanIndustryTree,
    ts_code: str,
    date: datetime,
    date_str: str,
) -> float | None:
    """停牌股回退: 查 730 天内最近一个有效流通市值, 查不到返回 None"""
    df = tree.pro.daily_basic(
        ts_code=ts_code,
        fields='trade_date,circ_mv',
        start_date=(date - timedelta(days=730)).strftime("%Y%m%d"),
        end_date=date_str,
    )
    # 响应的数据默认按日期降序
    for row in df.itertuples(index=False):
        d_str = row.trade_date
        if datetime.strptime(d_str, "%Y%m%d") <= date:
            cand = row.circ_mv
            if pd.isna(cand):
                continue  # 该日市值缺失, 继续往前找最近的有效值
            return float(cand)
    return None


def daily_rank_float_weight(
    tree: ShenWanIndustryTree,
    date: datetime,
    timings: dict[str, float] | None = None,
) -> tuple[RankList, RankList, RankList]:
    """获取指定日期的行业涨幅(流通市值加权)排名"""
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")

    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    ts_code_to_circ_mv: dict[str, float] = tree.get_ts_code_to_circ_mv(date)
    if not ts_code_to_circ_mv:
        raise ValueError(f"没有获取到 {date_str} 交易日的流通市值数据")

    ts_code_to_pct_chg: dict[str, float] = tree.get_ts_code_to_pct_chg(date)
    if not ts_code_to_pct_chg:
        raise ValueError(f"没有获取到 {date_str} 交易日的行情数据")

    # 行业index_code -> (行业index_code, 上涨百分比, 成分股数量)
    l1_chg_map: dict[str, tuple[str, float, int]] = {}
    l2_chg_map: dict[str, tuple[str, float, int]] = {}
    l3_chg_map: dict[str, tuple[str, float, int]] = {}

    # 行业index_code -> (当日收盘新增流通市值总和, 当日开盘前的流通市值总和)
    l1_circ_map: dict[str, tuple[float, float]] = {}
    l2_circ_map: dict[str, tuple[float, float]] = {}
    l3_circ_map: dict[str, tuple[float, float]] = {}

    for node_l1 in tree.level_to_nodes[1]:
        l1_chg_map[node_l1.index_code] = (node_l1.index_code, 0, 0)
        l1_circ_map[node_l1.index_code] = (0, 0)
    for node_l2 in tree.level_to_nodes[2]:
        l2_chg_map[node_l2.index_code] = (node_l2.index_code, 0, 0)
        l2_circ_map[node_l2.index_code] = (0, 0)
    for node_l3 in tree.level_to_nodes[3]:
        l3_chg_map[node_l3.index_code] = (node_l3.index_code, 0, 0)
        l3_circ_map[node_l3.index_code] = (0, 0)

    stock_pool: set[str] = set(ts_code_to_pct_chg) | set(tree.constituent_stock_to_l3_node)
    tree.filter_stock_pool(date, stock_pool)

    for ts_code in stock_pool:
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code)
        if not l3_node or not l2_node or not l1_node:
            continue

        data_list = [
            (l3_node, l3_chg_map, l3_circ_map),
            (l2_node, l2_chg_map, l2_circ_map),
            (l1_node, l1_chg_map, l1_circ_map),
        ]

        pct_chg = ts_code_to_pct_chg.get(ts_code, 0.0)  # 有交易数据则用实际涨幅, 停牌则按0%
        if pct_chg is None:
            continue  # 数据异常(涨跌幅非有限值), 不计入
        for l_node, l_chg_map, l_circ_map in data_list:
            l_index_code, l_pct_chg, l_count = l_chg_map.get(l_node.index_code)
            l_circ1, l_circ2 = l_circ_map.get(l_node.index_code)
            l_count_new = l_count + 1
            l_circ_mv = ts_code_to_circ_mv.get(ts_code)

            # 处理当日停牌的情况: 需要获取停牌前的流通市值(最多支持连续停牌 2 年)
            if l_circ_mv is None or pd.isna(l_circ_mv):
                if timings is not None:
                    _t0 = time.perf_counter()
                l_circ_mv = _resolve_circ_mv(tree, ts_code, date, date_str)
                if timings is not None:
                    timings["circ_fallback"] = timings.get("circ_fallback", 0.0) + (
                        time.perf_counter() - _t0
                    )
                if l_circ_mv is None:
                    raise ValueError(f"没有获取到 {ts_code} 的流通市值数据")
                ts_code_to_circ_mv[ts_code] = l_circ_mv

            # 新增流通市值
            l_circ1_new = l_circ_mv * pct_chg / (pct_chg + 100) + l_circ1

            # 当日开盘前的流通市值
            l_circ2_new = l_circ_mv / (pct_chg / 100 + 1) + l_circ2

            l_pct_chg_new = l_circ1_new / l_circ2_new * 100
            l_chg_map[l_node.index_code] = (l_index_code, l_pct_chg_new, l_count_new)
            l_circ_map[l_node.index_code] = (l_circ1_new, l_circ2_new)

    # 对行业涨幅由大到小排序
    l1_rank_list = sorted(
        [item for item in l1_chg_map.values() if item[2] > 0],
        key=lambda x: x[1],
        reverse=True,
    )
    l2_rank_list = sorted(
        [item for item in l2_chg_map.values() if item[2] > 0],
        key=lambda x: x[1],
        reverse=True,
    )
    l3_rank_list = sorted(
        [item for item in l3_chg_map.values() if item[2] > 0],
        key=lambda x: x[1],
        reverse=True,
    )

    return l1_rank_list, l2_rank_list, l3_rank_list


def _get_trading_days(pro, start_str: str, end_str: str) -> list[str]:
    """获取区间内交易日列表(YYYYMMDD, 升序)"""
    df = pro.trade_cal(
        exchange='SSE',
        start_date=start_str,
        end_date=end_str,
        is_open='1',
        fields='cal_date',
    )
    return sorted(df['cal_date'].astype(str).tolist())


def _fetch_daily_by_date(pro, date_str: str) -> dict[str, tuple[float, float]]:
    """按交易日拉全市场 daily, 返回 ts_code -> (close, pre_close), 跳过异常数据"""
    result: dict[str, tuple[float, float]] = {}
    offset = 0
    batch_size = 5999
    while True:
        df = pro.daily(
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


def _fetch_daily_batch(
    pro,
    trading_days: list[str],
    progress_callback: ProgressCallback | None = None,
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
        with lock:
            wait = next_start[0] - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            next_start[0] = time.perf_counter() + interval
        last_err: Exception | None = None
        for attempt in range(1, DAILY_FETCH_RETRY + 1):
            try:
                return day_str, _fetch_daily_by_date(pro, day_str)
            except Exception as err:
                last_err = err
                time.sleep(0.5 * attempt)
        raise RuntimeError(
            f"拉取 {day_str} 行情连续失败 {DAILY_FETCH_RETRY} 次: {last_err}"
        )

    with ThreadPoolExecutor(max_workers=MAX_DAILY_FETCH_WORKERS) as executor:
        for day_str, data in executor.map(fetch_one, trading_days):
            results[day_str] = data
            completed += 1
            if progress_callback is not None:
                pct = completed / total * 100.0 if total else 100.0
                progress_callback(pct, f"已拉取 {completed}/{total} 个交易日行情")
    return results


def rank_range(
    tree: ShenWanIndustryTree,
    start_date: datetime,
    end_date: datetime,
    timings: dict[str, float] | None = None,
    progress_callback: ProgressCallback | None = None,
    detail: dict[str, dict[str, float]] | None = None,
) -> tuple[tuple[RankList, RankList, RankList], tuple[RankList, RankList, RankList]]:
    """
    区间累计涨幅榜, 返回 (等权(l1,l2,l3), 流通市值加权(l1,l2,l3))

    口径:
    - 参与股票 = 区间起始日已在成分(in_date <= 起点) 且 区间末仍在(delist_date >= 终点);
      中段才纳入/起始日尚未上市(list_date >= 起点)/区间末前已退市均剔除;
      同类剔除告警按类型汇总为一行(数量 + 少量样例), 避免大量日志刷屏
    - 个股区间收益 = 区间内所有有行情日的每日官方涨跌幅连乘(除权除息自动修正),
      隐含基准 = 区间内首个有行情日的 pre_close(即区间前一交易日收盘/停牌前收盘), **包含起始日当天涨跌**
    - 权重 = 区间起始日流通市值(起始日停牌的按 730 天回退; 仍取不到则仅参与等权榜并告警)
    - timings: 可选 dict, 记录各阶段耗时
      (trade_cal/participate/daily_fetch/accumulate/circ_fetch/circ_fallback/compute/trading_days)
    - progress_callback: 可选进度回调 (0~100, 阶段说明), 不影响计算结果
    - detail: 可选 dict, 写入 stock_ret(个股区间收益) 与 last_close(区间末日收盘价), 供子表展示
    """
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")

    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")
    if start_str > end_str:
        raise ValueError(f"区间起点不能晚于终点: {start_str} > {end_str}")

    def _notify(percent: float, message: str) -> None:
        if progress_callback is not None:
            progress_callback(max(0.0, min(100.0, percent)), message)

    _notify(2.0, "拉取交易日历")
    if timings is not None:
        _t0 = time.perf_counter()
    trading_days = _get_trading_days(tree.pro, start_str, end_str)
    if timings is not None:
        timings["trade_cal"] = time.perf_counter() - _t0
        timings["trading_days"] = float(len(trading_days))
    if not trading_days:
        raise ValueError(f"区间内没有交易日: {start_str} ~ {end_str}")
    _notify(6.0, f"区间内共 {len(trading_days)} 个交易日")

    # 1) 参与股票: 起始日已在成分 且 区间末仍在
    if timings is not None:
        _t0 = time.perf_counter()
    participating: set[str] = set()
    excluded_before_listing: list[str] = []  # 起始日尚未上市
    excluded_mid_range: list[str] = []       # 中段才纳入
    for ts_code in tree.constituent_stock_to_l3_node:
        in_date = tree.ts_code_to_in_date.get(ts_code)
        delist_date = tree.ts_code_to_delist_date.get(ts_code)
        list_date = tree.stock_basic.get(ts_code, {}).get('list_date')
        if list_date is not None and not pd.isna(list_date) and str(list_date) >= start_str:
            excluded_before_listing.append(ts_code)
            continue
        if in_date is not None and in_date > start_str:
            excluded_mid_range.append(ts_code)
            continue
        if delist_date is not None and delist_date < end_str:
            continue  # 区间末前已退市, 按区间末成分口径不参与
        participating.add(ts_code)
    # 剔除告警按类型汇总为一行, 避免大量同类日志刷屏
    if excluded_before_listing:
        samples = ", ".join(
            f"{c}(list_date={tree.stock_basic.get(c, {}).get('list_date')})"
            for c in excluded_before_listing[:3]
        )
        logger.warning(
            f"区间榜剔除起始日尚未上市股票 {len(excluded_before_listing)} 只"
            f"(如 {samples}{'...' if len(excluded_before_listing) > 3 else ''})"
        )
    if excluded_mid_range:
        samples = ", ".join(
            f"{c}(in_date={tree.ts_code_to_in_date.get(c)})" for c in excluded_mid_range[:3]
        )
        logger.warning(
            f"区间榜剔除中段纳入股票 {len(excluded_mid_range)} 只"
            f"(如 {samples}{'...' if len(excluded_mid_range) > 3 else ''})"
        )
    if timings is not None:
        timings["participate"] = time.perf_counter() - _t0
    _notify(8.0, "筛选参与股票完成")

    # 2) 逐日拉行情(并发+限流平摊), 再连乘区间累计收益(含起始日当天涨跌,
    #    隐含基准=首个有行情日的 pre_close)
    if timings is not None:
        _t0 = time.perf_counter()
    _notify(9.0, "开始拉取区间行情")
    batch_data = _fetch_daily_batch(
        tree.pro,
        trading_days,
        progress_callback=lambda pct, message: _notify(9.0 + pct * 0.72, message),
    )
    if timings is not None:
        timings["daily_fetch"] = time.perf_counter() - _t0
    _notify(81.0, "区间行情拉取完成")
    if timings is not None:
        _t0 = time.perf_counter()
    stock_prod: dict[str, float] = {}
    for day_str in trading_days:
        day_data = batch_data[day_str]
        if not day_data:
            continue
        for ts_code, (close, pre_close) in day_data.items():
            if ts_code not in participating:
                continue
            stock_prod[ts_code] = stock_prod.get(ts_code, 1.0) * (close / pre_close)
    if timings is not None:
        timings["accumulate"] = time.perf_counter() - _t0
    _notify(84.0, "收益累计完成")

    # 区间累计收益(%): 整段区间无任何行情的股票直接剔除
    stock_ret: dict[str, float] = {}
    for ts_code in participating:
        if stock_prod.get(ts_code) is not None:
            stock_ret[ts_code] = (stock_prod.get(ts_code, 1.0) - 1.0) * 100.0

    # 3) 权重: 区间起始日(=区间内第一个交易日)流通市值, 停牌回退
    if timings is not None:
        _t0 = time.perf_counter()
    weight_date_str = trading_days[0]
    weight_date = datetime.strptime(weight_date_str, "%Y%m%d")
    ts_code_to_circ_mv: dict[str, float] = tree.get_ts_code_to_circ_mv(weight_date)
    if timings is not None:
        timings["circ_fetch"] = time.perf_counter() - _t0
    _notify(86.0, "流通市值拉取完成")
    if timings is not None:
        _t0 = time.perf_counter()
    no_circ_mv_stocks: list[str] = []
    for ts_code in stock_ret:
        if ts_code_to_circ_mv.get(ts_code) is None or pd.isna(ts_code_to_circ_mv.get(ts_code)):
            circ_mv = _resolve_circ_mv(tree, ts_code, weight_date, weight_date_str)
            if circ_mv is None:
                no_circ_mv_stocks.append(ts_code)
                continue
            ts_code_to_circ_mv[ts_code] = circ_mv
    if no_circ_mv_stocks:
        samples = ", ".join(no_circ_mv_stocks[:3])
        logger.warning(
            f"区间榜无法获取 {len(no_circ_mv_stocks)} 只股票起始日流通市值"
            f"(如 {samples}{'...' if len(no_circ_mv_stocks) > 3 else ''}), 仅参与等权榜"
        )
    if timings is not None:
        timings["circ_fallback"] = time.perf_counter() - _t0
    _notify(89.0, "停牌市值回退完成")

    # 4) 聚合三级行业: 等权 = 起始成分简单平均; 加权 = 起始流通市值加权
    _notify(90.0, "聚合行业涨幅")
    if timings is not None:
        _t0 = time.perf_counter()
    l1_ew: dict[str, list] = {}  # index_code -> [count, 收益和]
    l2_ew: dict[str, list] = {}
    l3_ew: dict[str, list] = {}
    l1_fw: dict[str, list] = {}  # index_code -> [市值和, 市值*收益和, count]
    l2_fw: dict[str, list] = {}
    l3_fw: dict[str, list] = {}
    for node_l1 in tree.level_to_nodes[1]:
        l1_ew[node_l1.index_code] = [0, 0.0]
        l1_fw[node_l1.index_code] = [0.0, 0.0, 0]
    for node_l2 in tree.level_to_nodes[2]:
        l2_ew[node_l2.index_code] = [0, 0.0]
        l2_fw[node_l2.index_code] = [0.0, 0.0, 0]
    for node_l3 in tree.level_to_nodes[3]:
        l3_ew[node_l3.index_code] = [0, 0.0]
        l3_fw[node_l3.index_code] = [0.0, 0.0, 0]

    for ts_code, ret in stock_ret.items():
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code)
        if not l1_node or not l2_node or not l3_node:
            continue

        for l_node, ew_map in ((l3_node, l3_ew), (l2_node, l2_ew), (l1_node, l1_ew)):
            entry = ew_map[l_node.index_code]
            entry[0] += 1
            entry[1] += ret

        circ_mv = ts_code_to_circ_mv.get(ts_code)
        if circ_mv is None or pd.isna(circ_mv):
            continue  # 无起始市值, 仅参与等权榜(已告警)
        for l_node, fw_map in ((l3_node, l3_fw), (l2_node, l2_fw), (l1_node, l1_fw)):
            entry = fw_map[l_node.index_code]
            entry[0] += circ_mv
            entry[1] += circ_mv * ret
            entry[2] += 1

    def _finalize(ew_map: dict[str, list], fw_map: dict[str, list]) -> tuple[RankList, RankList]:
        ew_list = sorted(
            (
                (code, entry[1] / entry[0], entry[0])
                for code, entry in ew_map.items()
                if entry[0] > 0
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        fw_list = sorted(
            (
                (code, entry[1] / entry[0], entry[2])
                for code, entry in fw_map.items()
                if entry[0] > 0
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        return ew_list, fw_list

    l1_ew_list, l1_fw_list = _finalize(l1_ew, l1_fw)
    l2_ew_list, l2_fw_list = _finalize(l2_ew, l2_fw)
    l3_ew_list, l3_fw_list = _finalize(l3_ew, l3_fw)
    if timings is not None:
        timings["compute"] = time.perf_counter() - _t0
    _notify(98.0, "计算完成")

    if detail is not None:
        last_day_str = trading_days[-1]
        last_day_data = batch_data.get(last_day_str, {})
        detail["stock_ret"] = stock_ret
        detail["last_close"] = {
            ts_code: close
            for ts_code, (close, _pre_close) in last_day_data.items()
            if ts_code in participating
        }
        detail["ts_code_to_circ_mv"] = ts_code_to_circ_mv

    return (
        (l1_ew_list, l2_ew_list, l3_ew_list),
        (l1_fw_list, l2_fw_list, l3_fw_list),
    )
