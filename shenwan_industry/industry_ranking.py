"""
申万行业排行榜: 单日榜 + 区间榜

- daily_rank_equal_weight / daily_rank_float_weight: 单日榜 (逻辑与原 classification.py 一致, 未改动)
- run_daily_ranking: 单日榜编排 (拉行情/市值 -> 等权 -> 加权), CLI 与 Web 共用
- rank_range: 区间累计涨幅榜, 支持 timings 参数记录各阶段耗时
- print_timing: 入口脚本用的耗时输出工具 (API 调用计数由 MarketDataProvider 提供)

区间榜网络策略: 区间内每个交易日拉一次 daily(trade_date), 用每日官方涨跌幅
(close/pre_close, 除权除息日即除权参考价口径) 连乘得到个股区间收益;
停牌日无行自动按 0% 累计, 不再逐股回退查收益; 权重取区间起始日流通市值
(daily_basic 一次 + 仅起始日停牌的少量回退)。
"""

import logging
import time
from datetime import datetime
from typing import Callable

import pandas as pd

try:
    from .industry_tree import ShenWanIndustryTree
    from .market_data import MarketDataProvider
except ImportError:  # 直接运行本文件时
    from industry_tree import ShenWanIndustryTree
    from market_data import MarketDataProvider

logger = logging.getLogger("shenwan_industry.industry_ranking")

# 榜单项: (行业 index_code, 涨跌幅%, 成分股数量)
RankList = list[tuple[str, float, int]]

# 进度回调: (0~100 的百分比, 阶段说明)
ProgressCallback = Callable[[float, str], None]

# 带阶段名的进度回调: (0~100 的百分比, 阶段说明, 阶段名), 阶段名用于 Web 前端展示
DailyProgressCallback = Callable[[float, str, str | None], None]

# 协作式取消检查: 需要取消时抛异常
CancelCheck = Callable[[], None]


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
    market_data: MarketDataProvider,
    date: datetime,
    cancel_check: CancelCheck | None = None,
) -> tuple[RankList, RankList, RankList]:
    """获取指定日期的行业涨幅(等权)排名"""
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")

    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    ts_code_to_pct_chg: dict[str, float] = market_data.get_ts_code_to_pct_chg(date)
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
    tree.filter_stock_pool(stock_pool, date, date, cancel_check=cancel_check)

    for idx, ts_code in enumerate(stock_pool):
        if cancel_check is not None and idx % 500 == 0:
            cancel_check()
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


def daily_rank_float_weight(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    timings: dict[str, float] | None = None,
    cancel_check: CancelCheck | None = None,
    mv_kind: str = "circ",
) -> tuple[RankList, RankList, RankList]:
    """获取指定日期的行业涨幅(市值加权)排名

    mv_kind: "circ"=流通市值加权, "total"=总市值加权
    """
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")

    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    if mv_kind == "total":
        ts_code_to_circ_mv: dict[str, float] = market_data.get_ts_code_to_total_mv(date)
        resolve_mv = market_data.resolve_total_mv
        mv_label = "总市值"
    else:
        ts_code_to_circ_mv = market_data.get_ts_code_to_circ_mv(date)
        resolve_mv = market_data.resolve_circ_mv
        mv_label = "流通市值"
    if not ts_code_to_circ_mv:
        raise ValueError(f"没有获取到 {date_str} 交易日的{mv_label}数据")

    ts_code_to_pct_chg: dict[str, float] = market_data.get_ts_code_to_pct_chg(date)
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
    tree.filter_stock_pool(stock_pool, date, date, cancel_check=cancel_check)

    for idx, ts_code in enumerate(stock_pool):
        if cancel_check is not None and idx % 500 == 0:
            cancel_check()
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

            # 处理当日停牌的情况: 需要获取停牌前的市值(最多支持连续停牌 2 年)
            if l_circ_mv is None or pd.isna(l_circ_mv):
                if timings is not None:
                    _t0 = time.perf_counter()
                l_circ_mv = resolve_mv(ts_code, date, cancel_check)
                if timings is not None:
                    timings["circ_fallback"] = timings.get("circ_fallback", 0.0) + (
                        time.perf_counter() - _t0
                    )
                if l_circ_mv is None:
                    raise ValueError(f"没有获取到 {ts_code} 的{mv_label}数据")
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


def run_daily_ranking(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    progress_callback: DailyProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> tuple[
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    dict[str, float],
]:
    """单日榜编排: 拉行情/市值 -> 等权 -> 加权, 返回 (等权榜, 流通市值加权榜, 总市值加权榜, timings)

    供入口脚本 daily_ranking.py 与 Web service._run_daily 共用, 避免两套编排漂移。
    timings key: daily_fetch / circ_fetch / equal_compute / float_compute / float_fallback
    progress_callback: 可选 (0~100, 阶段说明, 阶段名), 阶段名用于 Web 前端展示
    """
    date_str = date.strftime("%Y%m%d")
    timings: dict[str, float] = {}

    def _notify(percent: float, message: str, phase: str | None = None) -> None:
        if progress_callback is not None:
            progress_callback(max(0.0, min(100.0, percent)), message, phase)

    _notify(8.0, "拉取日线行情", "拉取日线行情")
    t0 = time.perf_counter()
    pct_map = market_data.get_ts_code_to_pct_chg(date)
    timings["daily_fetch"] = time.perf_counter() - t0
    if not pct_map:
        raise ValueError(f"{date_str} 不是交易日，或未获取到当日行情")

    _notify(48.0, "拉取市值数据", "拉取市值数据")
    t0 = time.perf_counter()
    market_data.get_ts_code_to_circ_mv(date)  # 同一次请求同时缓存流通/总市值
    timings["circ_fetch"] = time.perf_counter() - t0

    _notify(68.0, "计算等权涨幅", "计算排行榜")
    t0 = time.perf_counter()
    ew = daily_rank_equal_weight(tree, market_data, date, cancel_check)
    timings["equal_compute"] = time.perf_counter() - t0

    _notify(78.0, "计算流通市值加权涨幅", "计算排行榜")
    fw_timings: dict[str, float] = {}
    t0 = time.perf_counter()
    fw = daily_rank_float_weight(
        tree,
        market_data,
        date,
        timings=fw_timings,
        cancel_check=cancel_check,
    )
    timings["float_compute"] = time.perf_counter() - t0
    timings["float_fallback"] = fw_timings.get("circ_fallback", 0.0)

    _notify(89.0, "计算总市值加权涨幅", "计算排行榜")
    tw_timings: dict[str, float] = {}
    t0 = time.perf_counter()
    tw = daily_rank_float_weight(
        tree,
        market_data,
        date,
        timings=tw_timings,
        cancel_check=cancel_check,
        mv_kind="total",
    )
    timings["total_compute"] = time.perf_counter() - t0
    timings["total_fallback"] = tw_timings.get("circ_fallback", 0.0)

    return ew, fw, tw, timings


def rank_range(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    start_date: datetime,
    end_date: datetime,
    timings: dict[str, float] | None = None,
    progress_callback: ProgressCallback | None = None,
    detail: dict[str, dict[str, float]] | None = None,
    cancel_check: CancelCheck | None = None,
) -> tuple[
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
]:
    """
    区间累计涨幅榜, 返回 (等权(l1,l2,l3), 流通市值加权(l1,l2,l3), 总市值加权(l1,l2,l3))

    口径:
    - 参与股票 = 区间起始日已在成分(in_date <= 起点) 且 区间末仍在(delist_date >= 终点);
      中段才纳入/起始日尚未上市(list_date >= 起点)/区间末前已退市均剔除;
      同类剔除告警按类型汇总为一行(数量 + 少量样例), 避免大量日志刷屏
    - 个股区间收益 = 区间内所有有行情日的每日官方涨跌幅连乘(除权除息自动修正),
      隐含基准 = 区间内首个有行情日的 pre_close(即区间前一交易日收盘/停牌前收盘), **包含起始日当天涨跌**
    - 权重 = 区间起始日流通市值/总市值(起始日停牌的按 730 天回退; 仍取不到则仅参与等权榜并告警)
    - timings: 可选 dict, 记录各阶段耗时
      (trade_cal/participate/daily_fetch/accumulate/circ_fetch/circ_fallback/compute/trading_days)
    - progress_callback: 可选进度回调 (0~100, 阶段说明), 不影响计算结果
    - detail: 可选 dict, 写入 stock_ret(个股区间收益) 与 last_close(区间末日收盘价), 供子表展示
    - cancel_check: 可选取消检查回调, 在长循环/网络拉取前调用, 取消时抛出异常
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

    def _check_cancel() -> None:
        if cancel_check is not None:
            cancel_check()

    _check_cancel()
    _notify(2.0, "拉取交易日历")
    if timings is not None:
        _t0 = time.perf_counter()
    trading_days = market_data.get_trading_days(start_str, end_str)
    if timings is not None:
        timings["trade_cal"] = time.perf_counter() - _t0
        timings["trading_days"] = float(len(trading_days))
    if not trading_days:
        raise ValueError(f"区间内没有交易日: {start_str} ~ {end_str}")
    _notify(6.0, f"区间内共 {len(trading_days)} 个交易日")
    _check_cancel()

    # 1) 参与股票: 起始日已在成分 且 区间末仍在 (共用 filter_stock_pool, 锚点=起始日, 末日=终点)
    if timings is not None:
        _t0 = time.perf_counter()
    participating: set[str] = set(tree.constituent_stock_to_l3_node)
    excluded = tree.filter_stock_pool(
        participating, start_date, end_date, cancel_check=cancel_check
    )
    excluded_before_listing: list[str] = excluded["not_listed"]  # 起始日尚未上市
    excluded_mid_range: list[str] = excluded["in_date_later"]    # 中段才纳入
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
    _check_cancel()

    # 2) 逐日拉行情(并发+限流平摊), 再连乘区间累计收益(含起始日当天涨跌,
    #    隐含基准=首个有行情日的 pre_close)
    if timings is not None:
        _t0 = time.perf_counter()
    _notify(9.0, "开始拉取区间行情")
    batch_data = market_data.fetch_daily_batch(
        trading_days,
        progress_callback=lambda pct, message: _notify(9.0 + pct * 0.72, message),
        cancel_check=cancel_check,
    )
    if timings is not None:
        timings["daily_fetch"] = time.perf_counter() - _t0
    _notify(81.0, "区间行情拉取完成")
    _check_cancel()
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
    _check_cancel()

    # 区间累计收益(%): 整段区间无任何行情的股票直接剔除
    stock_ret: dict[str, float] = {}
    for ts_code in participating:
        if stock_prod.get(ts_code) is not None:
            stock_ret[ts_code] = (stock_prod.get(ts_code, 1.0) - 1.0) * 100.0

    # 3) 权重: 区间起始日(=区间内第一个交易日)流通市值/总市值, 停牌回退
    if timings is not None:
        _t0 = time.perf_counter()
    weight_date_str = trading_days[0]
    weight_date = datetime.strptime(weight_date_str, "%Y%m%d")
    ts_code_to_circ_mv: dict[str, float] = market_data.get_ts_code_to_circ_mv(weight_date)
    ts_code_to_total_mv: dict[str, float] = market_data.get_ts_code_to_total_mv(weight_date)
    if timings is not None:
        timings["circ_fetch"] = time.perf_counter() - _t0
    _notify(86.0, "市值拉取完成")
    _check_cancel()
    if timings is not None:
        _t0 = time.perf_counter()
    no_circ_mv_stocks: list[str] = []
    for idx, ts_code in enumerate(stock_ret):
        if idx % 500 == 0:
            _check_cancel()
        if ts_code_to_circ_mv.get(ts_code) is None or pd.isna(ts_code_to_circ_mv.get(ts_code)):
            circ_mv = market_data.resolve_circ_mv(ts_code, weight_date, cancel_check)
            if circ_mv is None:
                no_circ_mv_stocks.append(ts_code)
                continue
            ts_code_to_circ_mv[ts_code] = circ_mv
        # 总市值: 流通市值回退已顺带填充缓存, 缺失时单独回退(同一次请求拿两个字段)
        if ts_code_to_total_mv.get(ts_code) is None or pd.isna(ts_code_to_total_mv.get(ts_code)):
            total_mv = market_data.resolve_total_mv(ts_code, weight_date, cancel_check)
            if total_mv is not None:
                ts_code_to_total_mv[ts_code] = total_mv
    if no_circ_mv_stocks:
        samples = ", ".join(no_circ_mv_stocks[:3])
        logger.warning(
            f"区间榜无法获取 {len(no_circ_mv_stocks)} 只股票起始日流通市值"
            f"(如 {samples}{'...' if len(no_circ_mv_stocks) > 3 else ''}), 仅参与等权榜"
        )
    if timings is not None:
        timings["circ_fallback"] = time.perf_counter() - _t0
    _notify(89.0, "停牌市值回退完成")
    _check_cancel()

    # 4) 聚合三级行业: 等权 = 起始成分简单平均; 加权 = 起始流通市值/总市值加权
    _notify(90.0, "聚合行业涨幅")
    _check_cancel()
    if timings is not None:
        _t0 = time.perf_counter()
    l1_ew: dict[str, list] = {}  # index_code -> [count, 收益和]
    l2_ew: dict[str, list] = {}
    l3_ew: dict[str, list] = {}
    l1_fw: dict[str, list] = {}  # index_code -> [市值和, 市值*收益和, count]
    l2_fw: dict[str, list] = {}
    l3_fw: dict[str, list] = {}
    l1_tw: dict[str, list] = {}  # 总市值加权
    l2_tw: dict[str, list] = {}
    l3_tw: dict[str, list] = {}
    for node_l1 in tree.level_to_nodes[1]:
        l1_ew[node_l1.index_code] = [0, 0.0]
        l1_fw[node_l1.index_code] = [0.0, 0.0, 0]
        l1_tw[node_l1.index_code] = [0.0, 0.0, 0]
    for node_l2 in tree.level_to_nodes[2]:
        l2_ew[node_l2.index_code] = [0, 0.0]
        l2_fw[node_l2.index_code] = [0.0, 0.0, 0]
        l2_tw[node_l2.index_code] = [0.0, 0.0, 0]
    for node_l3 in tree.level_to_nodes[3]:
        l3_ew[node_l3.index_code] = [0, 0.0]
        l3_fw[node_l3.index_code] = [0.0, 0.0, 0]
        l3_tw[node_l3.index_code] = [0.0, 0.0, 0]

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

        total_mv = ts_code_to_total_mv.get(ts_code)
        if total_mv is None or pd.isna(total_mv):
            continue  # 无起始总市值, 仅参与等权/流通市值加权榜
        for l_node, tw_map in ((l3_node, l3_tw), (l2_node, l2_tw), (l1_node, l1_tw)):
            entry = tw_map[l_node.index_code]
            entry[0] += total_mv
            entry[1] += total_mv * ret
            entry[2] += 1

    def _finalize(
        ew_map: dict[str, list],
        fw_map: dict[str, list],
        tw_map: dict[str, list] | None = None,
    ) -> tuple[RankList, RankList, RankList]:
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
        tw_list: RankList = []
        if tw_map is not None:
            tw_list = sorted(
                (
                    (code, entry[1] / entry[0], entry[2])
                    for code, entry in tw_map.items()
                    if entry[0] > 0
                ),
                key=lambda x: x[1],
                reverse=True,
            )
        return ew_list, fw_list, tw_list

    l1_ew_list, l1_fw_list, l1_tw_list = _finalize(l1_ew, l1_fw, l1_tw)
    l2_ew_list, l2_fw_list, l2_tw_list = _finalize(l2_ew, l2_fw, l2_tw)
    l3_ew_list, l3_fw_list, l3_tw_list = _finalize(l3_ew, l3_fw, l3_tw)
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
        detail["ts_code_to_total_mv"] = ts_code_to_total_mv

    return (
        (l1_ew_list, l2_ew_list, l3_ew_list),
        (l1_fw_list, l2_fw_list, l3_fw_list),
        (l1_tw_list, l2_tw_list, l3_tw_list),
    )
