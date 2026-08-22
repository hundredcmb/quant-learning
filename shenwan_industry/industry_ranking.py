"""
申万行业排行榜: 单日榜 + 区间榜

- daily_rank_equal_weight / daily_rank_float_weight: 单日榜 (逻辑与原 classification.py 一致, 未改动)
- run_daily_ranking: 单日榜编排 (拉行情/市值 -> 等权 -> 加权), CLI 与 Web 共用
- rank_range: 区间累计涨幅榜, 支持 timings 参数记录各阶段耗时
- print_timing: 入口脚本用的耗时输出工具 (API 调用计数由 MarketDataProvider 提供)

区间榜网络策略: 区间内每个交易日拉一次 daily(trade_date), 用每日官方涨跌幅
(close/pre_close, 除权除息日即除权参考价口径) 连乘得到个股区间收益;
停牌日无行自动按 0% 累计, 不再逐股回退查收益; 权重取区间起始日自由流通市值
(daily_basic 一次 + 仅起始日停牌的少量回退)。
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
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
    div_kind: str = "price",
) -> tuple[RankList, RankList, RankList]:
    """获取指定日期的行业涨幅(等权)排名

    div_kind: "price"=官方价格式(除息计入下跌, 默认; 除息股涨幅改用实际市值比),
    "reinvest"=分红再投资/全收益式(除息中性, 原行为, 用 close/pre_close)
    """
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

    stock_pool: set[str] = set(ts_code_to_pct_chg) | set(tree.all_member_codes)
    tree.filter_stock_pool(stock_pool, date, date, cancel_check=cancel_check)

    # 官方价格式(div_kind=="price")的除息日处理: 除息股涨幅改用
    # "今日实际总市值/昨日实际总市值−1"(纯派现等价于 close_t/close_{t-1}−1,
    # 捆绑送转+派现时送转部分价值中性, 与市值加权的官方价格式同一口径; 用总市值比避免
    # 解禁/股本变动对 free_share 的干扰); 其余股票仍用 close/pre_close。reinvest 式(原行为)不覆盖。
    ex_div_override: dict[str, float] = {}
    if div_kind == "price":
        ex_div_stocks = market_data.get_ex_div_cash(date)
        if ex_div_stocks:
            prev_days = market_data.get_trading_days(
                (date - timedelta(days=12)).strftime("%Y%m%d"), date_str
            )
            prev_td = [d for d in prev_days if d < date_str]
            if prev_td:
                today_mv_map = market_data.get_ts_code_to_total_mv(date)
                y_mv_map = market_data.get_ts_code_to_total_mv(
                    datetime.strptime(prev_td[-1], "%Y%m%d")
                )
                for ts_code in ex_div_stocks:
                    if ts_code not in stock_pool:
                        continue
                    y_mv = y_mv_map.get(ts_code)
                    t_mv = today_mv_map.get(ts_code)
                    if (
                        y_mv is not None
                        and not pd.isna(y_mv)
                        and y_mv > 0
                        and t_mv is not None
                        and not pd.isna(t_mv)
                    ):
                        ex_div_override[ts_code] = (t_mv / y_mv - 1.0) * 100

    for idx, ts_code in enumerate(stock_pool):
        if cancel_check is not None and idx % 500 == 0:
            cancel_check()
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code, date)
        if not l3_node or not l2_node or not l1_node:
            continue

        pct_chg = ts_code_to_pct_chg.get(ts_code, 0.0)  # 有交易数据则用实际涨幅, 停牌则按0%
        if pct_chg is None:
            continue  # 数据异常(涨跌幅非有限值), 不计入
        pct_chg = ex_div_override.get(ts_code, pct_chg)  # 官方价格式除息股用实际市值比
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
    mv_kind: str = "free",
    div_kind: str = "price",
) -> tuple[RankList, RankList, RankList]:
    """获取指定日期的行业涨幅(市值加权)排名

    mv_kind: "free"=自由流通市值加权, "total"=总市值加权
    div_kind: "price"=官方价格式(除息计入下跌, 默认; 除息日 M_pre 用昨日实际市值=官方 LV_{t-1}^{Adj},
    自由流通用昨日 free_mv、总市值用昨日 total_mv); "reinvest"=分红再投资/全收益式(除息中性, 原行为)
    """
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")

    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    if mv_kind == "total":
        ts_code_to_mv: dict[str, float] = market_data.get_ts_code_to_total_mv(date)
        resolve_mv = market_data.resolve_total_mv
        mv_label = "总市值"
    else:
        ts_code_to_mv = market_data.get_ts_code_to_free_mv(date)
        resolve_mv = market_data.resolve_free_mv
        mv_label = "自由流通市值"
    if not ts_code_to_mv:
        raise ValueError(f"没有获取到 {date_str} 交易日的{mv_label}数据")

    ts_code_to_pct_chg: dict[str, float] = market_data.get_ts_code_to_pct_chg(date)
    if not ts_code_to_pct_chg:
        raise ValueError(f"没有获取到 {date_str} 交易日的行情数据")

    # 行业index_code -> (行业index_code, 上涨百分比, 成分股数量)
    l1_chg_map: dict[str, tuple[str, float, int]] = {}
    l2_chg_map: dict[str, tuple[str, float, int]] = {}
    l3_chg_map: dict[str, tuple[str, float, int]] = {}

    # 行业index_code -> (当日收盘新增权重市值总和, 当日开盘前的权重市值总和)
    l1_mv_map: dict[str, tuple[float, float]] = {}
    l2_mv_map: dict[str, tuple[float, float]] = {}
    l3_mv_map: dict[str, tuple[float, float]] = {}

    for node_l1 in tree.level_to_nodes[1]:
        l1_chg_map[node_l1.index_code] = (node_l1.index_code, 0, 0)
        l1_mv_map[node_l1.index_code] = (0, 0)
    for node_l2 in tree.level_to_nodes[2]:
        l2_chg_map[node_l2.index_code] = (node_l2.index_code, 0, 0)
        l2_mv_map[node_l2.index_code] = (0, 0)
    for node_l3 in tree.level_to_nodes[3]:
        l3_chg_map[node_l3.index_code] = (node_l3.index_code, 0, 0)
        l3_mv_map[node_l3.index_code] = (0, 0)

    stock_pool: set[str] = set(ts_code_to_pct_chg) | set(tree.all_member_codes)
    tree.filter_stock_pool(stock_pool, date, date, cancel_check=cancel_check)

    # 新策略: 先并发补齐缺失市值(线程池, 见 market_data.resolve_missing_mv), 避免循环内逐股串行点查
    missing_codes = [c for c in stock_pool if pd.isna(ts_code_to_mv.get(c))]
    if missing_codes:
        _t0 = time.perf_counter()
        market_data.resolve_missing_mv(missing_codes, date, cancel_check)
        if timings is not None:
            timings["mv_resolve"] = timings.get("mv_resolve", 0.0) + (time.perf_counter() - _t0)

    # 整个上市期都没有市值数据(或 legacy 模式超 730 天)的股票: 跳过加权、仅参与等权榜(类同区间榜)
    no_weight_stocks: list[str] = []

    # 官方价格式(div_kind=="price")的除息日处理: 除息股 M_pre 用昨日实际市值
    # (自由流通=close_{t-1}×free_share_{t-1}、总市值口径同 total_mv_{t-1} = 官方 LV_{t-1}^{Adj});
    # 其余事件(送转/配股/解禁/普通)仍用 pre_close×q_t。reinvest 式(原行为)不覆盖。
    # 已按官方公式数值校验过"两日股本不同"的捆绑送转+派现情形。
    ex_div_override: dict[str, float] = {}
    if div_kind == "price":
        ex_div_stocks = market_data.get_ex_div_cash(date)
        if ex_div_stocks:
            prev_days = market_data.get_trading_days(
                (date - timedelta(days=12)).strftime("%Y%m%d"), date_str
            )
            prev_td = [d for d in prev_days if d < date_str]
            if prev_td:
                y_mv_getter = (
                    market_data.get_ts_code_to_free_mv
                    if mv_kind == "free"
                    else market_data.get_ts_code_to_total_mv
                )
                y_mv_map = y_mv_getter(datetime.strptime(prev_td[-1], "%Y%m%d"))
                for ts_code in ex_div_stocks:
                    if ts_code not in stock_pool:
                        continue
                    y_mv = y_mv_map.get(ts_code)
                    if y_mv is not None and not pd.isna(y_mv):
                        ex_div_override[ts_code] = y_mv

    for idx, ts_code in enumerate(stock_pool):
        if cancel_check is not None and idx % 500 == 0:
            cancel_check()
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code, date)
        if not l3_node or not l2_node or not l1_node:
            continue

        data_list = [
            (l3_node, l3_chg_map, l3_mv_map),
            (l2_node, l2_chg_map, l2_mv_map),
            (l1_node, l1_chg_map, l1_mv_map),
        ]

        pct_chg = ts_code_to_pct_chg.get(ts_code, 0.0)  # 有交易数据则用实际涨幅, 停牌则按0%
        if pct_chg is None:
            continue  # 数据异常(涨跌幅非有限值), 不计入

        # 处理当日停牌的情况: 需要获取停牌前的权重市值(最多支持连续停牌 2 年); 每股只解析一次, 供 L3/L2/L1 共用
        weight_mv = ts_code_to_mv.get(ts_code)
        if weight_mv is None or pd.isna(weight_mv):
            if timings is not None:
                _t0 = time.perf_counter()
            weight_mv = resolve_mv(ts_code, date, cancel_check)
            if timings is not None:
                timings["mv_fallback"] = timings.get("mv_fallback", 0.0) + (
                    time.perf_counter() - _t0
                )
            if weight_mv is None:
                no_weight_stocks.append(ts_code)
                continue
            ts_code_to_mv[ts_code] = weight_mv

        # 官方价格式除息股的昨日实际市值(其余股票为空)
        y_mv = ex_div_override.get(ts_code)

        for l_node, l_chg_map, l_mv_map in data_list:
            l_index_code, l_pct_chg, l_count = l_chg_map.get(l_node.index_code)
            l_mv1, l_mv2 = l_mv_map.get(l_node.index_code)
            l_count_new = l_count + 1

            if y_mv is not None:
                # 除息日官方价格式: 新增市值 = 今日实际市值 − 昨日实际市值; 开盘前 = 昨日实际
                l_mv1_new = (weight_mv - y_mv) + l_mv1
                l_mv2_new = y_mv + l_mv2
            else:
                # 当日收盘新增权重市值 = 收盘市值 - 开盘前市值(pre_close×q_t)
                l_mv1_new = weight_mv * pct_chg / (pct_chg + 100) + l_mv1
                # 当日开盘前的权重市值
                l_mv2_new = weight_mv / (pct_chg / 100 + 1) + l_mv2

            l_pct_chg_new = l_mv1_new / l_mv2_new * 100
            l_chg_map[l_node.index_code] = (l_index_code, l_pct_chg_new, l_count_new)
            l_mv_map[l_node.index_code] = (l_mv1_new, l_mv2_new)

    if no_weight_stocks:
        samples = ", ".join(no_weight_stocks[:3])
        logger.warning(
            f"{date_str} 无法获取 {len(no_weight_stocks)} 只成分股的{mv_label}"
            f"(如 {samples}{'...' if len(no_weight_stocks) > 3 else ''}, 多为超长停牌/数据缺失), 仅参与等权榜"
        )

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
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    dict[str, float],
]:
    """单日榜编排: 拉行情/市值 -> 等权 -> 加权, 返回
    (等权·官方价格式, 等权·分红再投资式, 自由流通·官方价格式, 自由流通·分红再投资式,
    总市值·官方价格式, 总市值·分红再投资式, timings)

    等权/自由流通市值加权/总市值加权各提供两种口径: "官方价格式"(默认, 除息计入下跌, 与申万官方
    价格指数一致)与"分红再投资/全收益式"(除息中性, 原行为)。
    供入口脚本 daily_ranking.py 与 Web service._run_daily 共用, 避免两套编排漂移。
    timings key: daily_fetch / mv_fetch / equal_compute / equal_tr_compute / float_compute /
    float_fallback / float_resolve / float_tr_compute / float_tr_fallback / float_tr_resolve /
    total_compute / total_fallback / total_resolve / total_tr_compute / total_tr_fallback /
    total_tr_resolve
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
    market_data.get_ts_code_to_free_mv(date)  # 同一次请求同时缓存自由流通市值/总市值
    timings["mv_fetch"] = time.perf_counter() - t0

    _notify(68.0, "计算等权涨幅(官方价格式)", "计算排行榜")
    t0 = time.perf_counter()
    ew = daily_rank_equal_weight(tree, market_data, date, cancel_check, div_kind="price")
    timings["equal_compute"] = time.perf_counter() - t0

    _notify(72.0, "计算等权涨幅(分红再投资式)", "计算排行榜")
    t0 = time.perf_counter()
    ew_reinvest = daily_rank_equal_weight(tree, market_data, date, cancel_check, div_kind="reinvest")
    timings["equal_tr_compute"] = time.perf_counter() - t0

    _notify(78.0, "计算自由流通市值加权涨幅(官方价格式)", "计算排行榜")
    fw_timings: dict[str, float] = {}
    t0 = time.perf_counter()
    fw = daily_rank_float_weight(
        tree,
        market_data,
        date,
        timings=fw_timings,
        cancel_check=cancel_check,
        mv_kind="free",
        div_kind="price",
    )
    timings["float_compute"] = time.perf_counter() - t0
    timings["float_fallback"] = fw_timings.get("mv_fallback", 0.0)
    timings["float_resolve"] = fw_timings.get("mv_resolve", 0.0)

    _notify(84.0, "计算自由流通市值加权涨幅(分红再投资式)", "计算排行榜")
    fr_timings: dict[str, float] = {}
    t0 = time.perf_counter()
    fw_reinvest = daily_rank_float_weight(
        tree,
        market_data,
        date,
        timings=fr_timings,
        cancel_check=cancel_check,
        mv_kind="free",
        div_kind="reinvest",
    )
    timings["float_tr_compute"] = time.perf_counter() - t0
    timings["float_tr_fallback"] = fr_timings.get("mv_fallback", 0.0)
    timings["float_tr_resolve"] = fr_timings.get("mv_resolve", 0.0)

    _notify(89.0, "计算总市值加权涨幅(官方价格式)", "计算排行榜")
    tw_timings: dict[str, float] = {}
    t0 = time.perf_counter()
    tw = daily_rank_float_weight(
        tree,
        market_data,
        date,
        timings=tw_timings,
        cancel_check=cancel_check,
        mv_kind="total",
        div_kind="price",
    )
    timings["total_compute"] = time.perf_counter() - t0
    timings["total_fallback"] = tw_timings.get("mv_fallback", 0.0)
    timings["total_resolve"] = tw_timings.get("mv_resolve", 0.0)

    _notify(93.0, "计算总市值加权涨幅(分红再投资式)", "计算排行榜")
    tw_tr_timings: dict[str, float] = {}
    t0 = time.perf_counter()
    tw_reinvest = daily_rank_float_weight(
        tree,
        market_data,
        date,
        timings=tw_tr_timings,
        cancel_check=cancel_check,
        mv_kind="total",
        div_kind="reinvest",
    )
    timings["total_tr_compute"] = time.perf_counter() - t0
    timings["total_tr_fallback"] = tw_tr_timings.get("mv_fallback", 0.0)
    timings["total_tr_resolve"] = tw_tr_timings.get("mv_resolve", 0.0)

    return ew, ew_reinvest, fw, fw_reinvest, tw, tw_reinvest, timings


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
    区间累计涨幅榜, 返回 (等权(l1,l2,l3), 自由流通市值加权(l1,l2,l3), 总市值加权(l1,l2,l3))

    口径:
    - 参与股票 = 区间起始日已在成分(in_date <= 起点) 且 区间末仍在(delist_date >= 终点);
      中段才纳入/起始日尚未上市(list_date >= 起点)/区间末前已退市均剔除;
      同类剔除告警按类型汇总为一行(数量 + 少量样例), 避免大量日志刷屏
    - 个股区间收益 = 区间内所有有行情日的每日官方涨跌幅连乘(除权除息自动修正),
      隐含基准 = 区间内首个有行情日的 pre_close(即区间前一交易日收盘/停牌前收盘), **包含起始日当天涨跌**
    - 权重 = 区间**首日盘前市值**(pre_close×q = 首日上一交易日调整后市值, 与单日榜 reinvest 式 M_pre 一致;
      首日停牌的按 730 天回退; 仍取不到则仅参与等权榜并告警)
    - timings: 可选 dict, 记录各阶段耗时
      (trade_cal/participate/daily_fetch/accumulate/mv_fetch/mv_fallback/compute/trading_days)
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
    participating: set[str] = set(tree.all_member_codes)
    excluded = tree.filter_stock_pool(
        participating, start_date, end_date, cancel_check=cancel_check
    )
    excluded_before_listing: list[str] = excluded["not_listed"]  # 起始日尚未上市
    excluded_not_member: list[str] = excluded["not_member"]      # 起始日无归属区间(未来纳入/历史已退出)
    excluded_left_mid: list[str] = excluded["left_mid_range"]    # 起始日在成分但区间末前已调出
    # 剔除告警按类型汇总为一行, 避免大量同类日志刷屏
    if excluded_before_listing:
        samples = ", ".join(
            f"{c}(list_date={tree.stock_basic.get(c, {}).get('list_date')})"
            for c in excluded_before_listing[:3]
        )
        logger.warning(
            f"区间榜剔除起始日未上市或新上市未满6交易日股票 {len(excluded_before_listing)} 只"
            f"(如 {samples}{'...' if len(excluded_before_listing) > 3 else ''})"
        )
    if excluded_not_member:
        samples = ", ".join(
            f"{c}(in_date={tree.ts_code_to_in_date.get(c, '?')})"
            for c in excluded_not_member[:3]
        )
        logger.warning(
            f"区间榜剔除起始日无归属区间股票 {len(excluded_not_member)} 只"
            f"(如 {samples}{'...' if len(excluded_not_member) > 3 else ''})"
        )
    if excluded_left_mid:
        out_samples = []
        for c in excluded_left_mid[:3]:
            rec = tree._get_interval_on(c, start_str)
            out_samples.append(f"{c}(out_date={rec[2] if rec else '?'})")
        logger.warning(
            f"区间榜剔除区间末前已调出股票 {len(excluded_left_mid)} 只"
            f"(如 {', '.join(out_samples)}{'...' if len(excluded_left_mid) > 3 else ''})"
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

    # 3) 权重: 区间首日**开盘前市值** M_pre = pre_close_{t0}×q_{t0}
    #    (= 首日上一交易日调整后市值 = 单日榜 reinvest 式 M_pre, 均按除权参考价口径),
    #    由当日收盘市值折算: M_pre = 收盘市值×(pre_close/close), 零额外请求; 停牌股无首日行情,
    #    后续回退市值本身即盘前市值(最新可用日收盘×股本), 不折算
    if timings is not None:
        _t0 = time.perf_counter()
    weight_date_str = trading_days[0]
    weight_date = datetime.strptime(weight_date_str, "%Y%m%d")
    ts_code_to_free_mv: dict[str, float] = market_data.get_ts_code_to_free_mv(weight_date)
    ts_code_to_total_mv: dict[str, float] = market_data.get_ts_code_to_total_mv(weight_date)
    if timings is not None:
        timings["mv_fetch"] = time.perf_counter() - _t0
    first_td_data = batch_data.get(weight_date_str, {})

    def _to_pre_mv(ts_code: str, mv: float) -> float:
        rec = first_td_data.get(ts_code)
        if rec is None:
            return mv
        close, pre_close = rec
        if (
            close is None
            or pre_close is None
            or close == 0
            or pd.isna(close)
            or pd.isna(pre_close)
        ):
            return mv
        return mv * (pre_close / close)

    ts_code_to_free_mv = {c: _to_pre_mv(c, mv) for c, mv in ts_code_to_free_mv.items()}
    ts_code_to_total_mv = {c: _to_pre_mv(c, mv) for c, mv in ts_code_to_total_mv.items()}
    # 新策略: 先并发补齐缺失市值(线程池, 见 market_data.resolve_missing_mv), 减少逐股点查
    _missing_mv = [c for c in stock_ret if pd.isna(ts_code_to_free_mv.get(c))]
    if _missing_mv:
        _b0 = time.perf_counter()
        market_data.resolve_missing_mv(_missing_mv, weight_date, cancel_check)
        if timings is not None:
            timings["mv_resolve"] = timings.get("mv_resolve", 0.0) + (time.perf_counter() - _b0)
    _notify(86.0, "市值拉取完成")
    _check_cancel()
    if timings is not None:
        _t0 = time.perf_counter()
    no_free_mv_stocks: list[str] = []
    for idx, ts_code in enumerate(stock_ret):
        if idx % 500 == 0:
            _check_cancel()
        if ts_code_to_free_mv.get(ts_code) is None or pd.isna(ts_code_to_free_mv.get(ts_code)):
            free_mv = market_data.resolve_free_mv(ts_code, weight_date, cancel_check)
            if free_mv is None:
                no_free_mv_stocks.append(ts_code)
                continue
            ts_code_to_free_mv[ts_code] = free_mv
        # 总市值: 自由流通市值回退已顺带填充缓存, 缺失时单独回退(同一次请求拿两个字段)
        if ts_code_to_total_mv.get(ts_code) is None or pd.isna(ts_code_to_total_mv.get(ts_code)):
            total_mv = market_data.resolve_total_mv(ts_code, weight_date, cancel_check)
            if total_mv is not None:
                ts_code_to_total_mv[ts_code] = total_mv
    if no_free_mv_stocks:
        samples = ", ".join(no_free_mv_stocks[:3])
        logger.warning(
            f"区间榜无法获取 {len(no_free_mv_stocks)} 只股票起始日自由流通市值"
            f"(如 {samples}{'...' if len(no_free_mv_stocks) > 3 else ''}), 仅参与等权榜"
        )
    if timings is not None:
        timings["mv_fallback"] = time.perf_counter() - _t0
    _notify(89.0, "停牌市值回退完成")
    _check_cancel()

    # 4) 聚合三级行业: 等权 = 起始成分简单平均; 加权 = 起始自由流通市值/总市值加权
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
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code, start_date)
        if not l1_node or not l2_node or not l3_node:
            continue

        for l_node, ew_map in ((l3_node, l3_ew), (l2_node, l2_ew), (l1_node, l1_ew)):
            entry = ew_map[l_node.index_code]
            entry[0] += 1
            entry[1] += ret

        free_mv = ts_code_to_free_mv.get(ts_code)
        if free_mv is None or pd.isna(free_mv):
            continue  # 无起始市值, 仅参与等权榜(已告警)
        for l_node, fw_map in ((l3_node, l3_fw), (l2_node, l2_fw), (l1_node, l1_fw)):
            entry = fw_map[l_node.index_code]
            entry[0] += free_mv
            entry[1] += free_mv * ret
            entry[2] += 1

        total_mv = ts_code_to_total_mv.get(ts_code)
        if total_mv is None or pd.isna(total_mv):
            continue  # 无起始总市值, 仅参与等权/自由流通市值加权榜
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
        detail["ts_code_to_free_mv"] = ts_code_to_free_mv
        detail["ts_code_to_total_mv"] = ts_code_to_total_mv

    return (
        (l1_ew_list, l2_ew_list, l3_ew_list),
        (l1_fw_list, l2_fw_list, l3_fw_list),
        (l1_tw_list, l2_tw_list, l3_tw_list),
    )


def rank_range_chain(
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
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
]:
    """官方逐日链式区间累计涨幅榜(区间形态的自建指数引擎), 与静态版 rank_range 并存对照

    每日按**当日**成分(逐日归属, 新股纳入/退市规则即单日榜口径)与当日盘前市值权重, 复用
    单日榜同款 6 条序列(等权/自由流通/总市值 × 官方价格式/全收益式), 逐行业连乘
    Π(1+pct/100) 得到区间累计涨幅。
    返回 (等权·价格, 等权·全收益, 自由流通·价格, 自由流通·全收益, 总市值·价格, 总市值·全收益),
    每项为 (L1, L2, L3) 榜单。

    性能约定: 行情从 fetch_daily_batch 一次拉取(并回填 pct/close 缓存), 逐日不再重复请求;
    市值每日一次全市场 daily_basic(同请求缓存 free/total); 除息识别每日一次 dividend(仅价格式需要, 缓存共享);
    **停牌股跨日复用**: 当日不在全市场市值数据中的股票(=停牌)沿用最近一次已知市值(停牌期间必然不变),
    零重复点查, 复牌/新上市当日由全市场数据自动刷新(见 market_data 缓存机制)。
    timings key: trade_cal / prefetch(三池并行总时长) / daily_fetch / mv_prefetch / ex_prefetch / accumulate / mv_resolve / compute / trading_days
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

    # 行情/市值/除息**三池并行**预取(Tushare 每接口限额互相独立, 各自 7.5/s 节流, 接口间不互相等待):
    # 进度按份额加权合并(行情 20% + 市值 18% + 除息 18%, 完成度单调 => 合并进度单调),
    # 各段完成时说明切换为"X完成"; 宽日历(±12/±24 天)随后一次预取供逐日窗口切片
    _notify(6.0, "拉取区间行情/市值/除息数据(并行)")
    prefetch_state = {"daily": 0.0, "mv": 0.0, "ex": 0.0}
    prefetch_lock = threading.Lock()
    prefetch_results: dict[str, dict] = {}
    if timings is not None:
        _t0 = time.perf_counter()

    def _prefetch_percent() -> float:
        # 各段完成度(0~1) × 份额(%) 加权: 行情 20% + 市值 18% + 除息 18% = 最大 62%
        return 6.0 + 20.0 * prefetch_state["daily"] + 18.0 * prefetch_state["mv"] + 18.0 * prefetch_state["ex"]

    def _prefetch_notify(key: str, pct: float, message: str) -> None:
        with prefetch_lock:
            prefetch_state[key] = pct / 100.0
            merged = _prefetch_percent()
        _notify(merged, message)

    def _run_prefetch(key: str, label: str, fn: Callable, timings_key: str) -> None:
        _p0 = time.perf_counter()
        res = fn(
            trading_days,
            progress_callback=lambda pct, message: _prefetch_notify(key, pct, message),
            cancel_check=cancel_check,
        )
        if timings is not None:
            timings[timings_key] = time.perf_counter() - _p0
        with prefetch_lock:
            prefetch_state[key] = 1.0
            merged = _prefetch_percent()
        if res is not None:
            prefetch_results[key] = res
        _notify(merged, f"{label}完成")

    prefetch_futures = []
    with ThreadPoolExecutor(max_workers=3) as prefetch_executor:
        prefetch_futures.append(
            prefetch_executor.submit(
                _run_prefetch, "daily", "区间行情", market_data.fetch_daily_batch, "daily_fetch"
            )
        )
        prefetch_futures.append(
            prefetch_executor.submit(
                _run_prefetch, "mv", "市值", market_data.fetch_mv_batch, "mv_prefetch"
            )
        )
        prefetch_futures.append(
            prefetch_executor.submit(
                _run_prefetch, "ex", "除息识别", market_data.fetch_ex_div_batch, "ex_prefetch"
            )
        )
    for _future in prefetch_futures:
        _future.result()  # 任务异常在此抛出, 不静默吞掉
    if timings is not None:
        timings["prefetch"] = time.perf_counter() - _t0
    market_data.get_trading_days(
        (start_date - timedelta(days=12)).strftime("%Y%m%d"), end_str
    )
    # 树侧窗口日历(新股6交易日门槛用, ±24 天然日覆盖): 逐日 filter_stock_pool 的窗口查询全部切片命中
    tree._trading_days_window(
        (start_date - timedelta(days=24)).strftime("%Y%m%d"), end_str
    )
    _notify(64.0, "区间数据预拉完成")
    _check_cancel()
    batch_data = prefetch_results.get("daily") or {}

    # 个股区间收益(子表展示口径, 与静态版相同的逐日累乘)
    if timings is not None:
        _t0 = time.perf_counter()
    stock_prod: dict[str, float] = {}
    for day_str in trading_days:
        day_data = batch_data.get(day_str) or {}
        for ts_code, (close, pre_close) in day_data.items():
            stock_prod[ts_code] = stock_prod.get(ts_code, 1.0) * (close / pre_close)
    stock_ret: dict[str, float] = {
        ts_code: (prod - 1.0) * 100.0 for ts_code, prod in stock_prod.items() if prod is not None
    }
    if timings is not None:
        timings["accumulate"] = time.perf_counter() - _t0

    # 6 条序列的连乘容器: series -> 层级"1/2/3" -> index_code -> 累计因子
    series_names = ("ew_p", "ew_r", "fw_p", "fw_r", "tw_p", "tw_r")
    chain_prod: dict[str, dict[str, dict[str, float]]] = {
        series: {"1": {}, "2": {}, "3": {}} for series in series_names
    }
    # 末次已知成分股数量(各序列同日一致, 以最后一天的榜单为准)
    last_counts: dict[str, dict[str, int]] = {"1": {}, "2": {}, "3": {}}

    # 停牌市值跨日复用: ts_code -> (自由流通市值, 总市值); 当日活跃时刷新为全市场数据,
    # 当日缺失(=停牌/未上市)时沿用最近一次已知值, 零重复点查; 复牌/新上市日自动刷新
    susp_memo: dict[str, tuple[float | None, float | None]] = {}
    total_days = len(trading_days)

    for day_idx, day_str in enumerate(trading_days):
        _check_cancel()
        day_dt = datetime.strptime(day_str, "%Y%m%d")

        # 市值: 每日一次全市场(daily_basic 同一请求双缓存 free/total)
        if timings is not None:
            _t0 = time.perf_counter()
        free_map = market_data.get_ts_code_to_free_mv(day_dt)
        total_map = market_data.get_ts_code_to_total_mv(day_dt)
        if timings is not None:
            timings["mv_fetch"] = timings.get("mv_fetch", 0.0) + (time.perf_counter() - _t0)

        # 首日一次性解析(停牌/缺失股市值点查, 2~4 秒)单独占用进度段, 避免 UI 停在"数据预拉完成"
        if day_idx == 0:
            _notify(68.0, "解析首日停牌股与缺失股市值(一次性)")

        # 停牌跨日复用(memo): 先刷新当日参与股(逐日过滤口径), 再对缺失市值者复用或首次点查。
        # **仅对当日参与股票生效**——退市已久/尚未上市的历史成分由过滤剔除, 不为它们发起无谓点查;
        # **free/total 两个口径分开判定**——"有行情、total 正常、free 异常被排除"的股票(known_issues 21 条类型)
        # 必须在 free 口径点查一次并写回缓存, 否则 daily_rank(free) 每天重复点查
        if timings is not None:
            _t0 = time.perf_counter()
        participating = set(market_data.get_ts_code_to_pct_chg(day_dt)) | set(tree.all_member_codes)
        tree.filter_stock_pool(participating, day_dt, day_dt, cancel_check=cancel_check)
        for ts_code in participating:
            free_mv = free_map.get(ts_code)
            total_mv = total_map.get(ts_code)
            if free_mv is not None and not pd.isna(free_mv) and total_mv is not None and not pd.isna(total_mv):
                susp_memo[ts_code] = (free_mv, total_mv)
        for ts_code in participating:
            free_ok = free_map.get(ts_code) is not None and not pd.isna(free_map.get(ts_code))
            total_ok = total_map.get(ts_code) is not None and not pd.isna(total_map.get(ts_code))
            if free_ok and total_ok:
                continue  # 两口径当日都有数据(活跃/复牌/新上市), 无需处理
            known = susp_memo.get(ts_code)
            if known is not None:
                known_free, known_total = known
                if not free_ok and known_free is not None:
                    free_map[ts_code] = known_free
                if not total_ok and known_total is not None:
                    total_map[ts_code] = known_total
                continue
            free_mv = free_map.get(ts_code) if free_ok else None
            total_mv = total_map.get(ts_code) if total_ok else None
            if not free_ok:
                free_mv = market_data.resolve_free_mv(ts_code, day_dt, cancel_check)
                if total_ok:
                    # 点查行可能是为找正常 free 而向前的旧行, 恢复以全市场拉取的最新 total 为准
                    total_map[ts_code] = total_mv
            if not total_ok:
                total_mv = market_data.resolve_total_mv(ts_code, day_dt, cancel_check)
            susp_memo[ts_code] = (free_mv, total_mv)
            if not free_ok and free_mv is not None:
                free_map[ts_code] = free_mv
            if not total_ok and total_mv is not None:
                total_map[ts_code] = total_mv
        if timings is not None:
            timings["mv_resolve"] = timings.get("mv_resolve", 0.0) + (time.perf_counter() - _t0)

        # 当日 6 条序列(与单日榜同一套函数/口径)
        if timings is not None:
            _t0 = time.perf_counter()
        series_ranks = {
            "ew_p": daily_rank_equal_weight(tree, market_data, day_dt, cancel_check, div_kind="price"),
            "ew_r": daily_rank_equal_weight(tree, market_data, day_dt, cancel_check, div_kind="reinvest"),
            "fw_p": daily_rank_float_weight(tree, market_data, day_dt, cancel_check=cancel_check, mv_kind="free", div_kind="price"),
            "fw_r": daily_rank_float_weight(tree, market_data, day_dt, cancel_check=cancel_check, mv_kind="free", div_kind="reinvest"),
            "tw_p": daily_rank_float_weight(tree, market_data, day_dt, cancel_check=cancel_check, mv_kind="total", div_kind="price"),
            "tw_r": daily_rank_float_weight(tree, market_data, day_dt, cancel_check=cancel_check, mv_kind="total", div_kind="reinvest"),
        }
        if timings is not None:
            timings["compute"] = timings.get("compute", 0.0) + (time.perf_counter() - _t0)

        for series, (l1, l2, l3) in series_ranks.items():
            for lv, rank_list in (("1", l1), ("2", l2), ("3", l3)):
                target = chain_prod[series][lv]
                count_target = last_counts[lv]
                for code, pct, count in rank_list:
                    target[code] = target.get(code, 1.0) * (1.0 + pct / 100.0)
                    count_target[code] = count

        _notify(72.0 + (day_idx + 1) / total_days * 25.0,
                f"逐日链式计算中 {day_idx + 1}/{total_days} 个交易日")

    # 结果: 连乘因子转累计涨幅, 按涨幅降序
    def _make_rank(series: str, lv: str) -> RankList:
        factored = chain_prod[series][lv]
        return sorted(
            (
                (code, (factor - 1.0) * 100.0, last_counts[lv].get(code, 0))
                for code, factor in factored.items()
            ),
            key=lambda x: x[1],
            reverse=True,
        )

    levels = {series: tuple(_make_rank(series, lv) for lv in ("1", "2", "3")) for series in series_names}

    if detail is not None:
        last_day_str = trading_days[-1]
        last_day_data = batch_data.get(last_day_str, {})
        detail["stock_ret"] = stock_ret
        detail["last_close"] = {
            ts_code: close for ts_code, (close, _pre_close) in last_day_data.items()
        }
        # 子表筛选口径与静态版一致: 首日盘前市值(当日收盘市值×pre_close/close 折算;
        # 停牌股无首日行情, 缓存中的回退值即盘前市值, 不折算)
        first_day_str = trading_days[0]
        first_day_data = batch_data.get(first_day_str, {})

        def _to_pre_mv(ts_code: str, mv: float) -> float:
            rec = first_day_data.get(ts_code)
            if rec is None:
                return mv
            close, pre_close = rec
            if close is None or pre_close is None or close == 0 or pd.isna(close) or pd.isna(pre_close):
                return mv
            return mv * (pre_close / close)

        first_free = market_data.get_ts_code_to_free_mv(datetime.strptime(first_day_str, "%Y%m%d"))
        first_total = market_data.get_ts_code_to_total_mv(datetime.strptime(first_day_str, "%Y%m%d"))
        detail["ts_code_to_free_mv"] = {c: _to_pre_mv(c, mv) for c, mv in first_free.items()}
        detail["ts_code_to_total_mv"] = {c: _to_pre_mv(c, mv) for c, mv in first_total.items()}

    _notify(98.0, "计算完成")
    return (
        levels["ew_p"], levels["ew_r"], levels["fw_p"], levels["fw_r"], levels["tw_p"], levels["tw_r"]
    )
