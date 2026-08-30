"""
申万行业排行榜: 单日榜 + 区间榜

- daily_rank_equal_weight / daily_rank_float_weight: 单日榜 (逻辑与原 classification.py 一致, 未改动)
- run_daily_ranking: 单日榜编排 (拉行情/市值 -> 等权 -> 加权), CLI 与 Web 共用
- daily_roe: 单日榜行业 ROE(加权平均算法, 四口径整体法 Σ分子/Σ分母, 见 docs/financial_indicators.md 第 6 节)
- daily_dividend_yield: 单日榜行业股息率(总额法 DPS 双口径整体法 Σ分红总额/Σ总市值, 见第 7 节)
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
from typing import Any, Callable

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

# 单日榜净利润口径四档(basis id -> (profit_kind, dynamic)): 归母/扣非 × TTM/动态, PE 与净利润同比
# 两列共用同一选择。四口径**一次全部算出**(共享同一批报告期财务数据与市值缓存, 动态口径仅本地
# 重算零新增请求, 见 market_data.get_ts_code_to_dynamic_profit / get_ts_code_to_dynamic_growth_pair);
# 首项 attr_ttm 为默认口径(CLI 打印该口径)。valuation 键 = "pe_"+basis / "growth_"+basis,
# web/service 与前端按 basis id 组装字段——修改此处需同步两侧与文档
PROFIT_BASES: dict[str, tuple[str, bool]] = {
    "attr_ttm": ("attr", False),
    "attr_dynamic": ("attr", True),
    "deduct_ttm": ("deduct", False),
    "deduct_dynamic": ("deduct", True),
}
# 默认净利润口径(CLI 打印、Web 下拉首项)
DEFAULT_PROFIT_BASIS = "attr_ttm"

# 样本空间档位(Web"样本空间"下拉, 三档嵌套 full ⊃ csi1800 ⊃ csi800 一次全算、前端切换显示):
# key -> 底层指数代码元组; full=全A(现有申万全池, 无需拉取)。样本清单来自 index_weight 月度
# 快照(忽略权重、只用 con_code 清单——行业加权用本模块自有市值权重); csi1800=中证800+中证1000
# 并集(页面文案用业内常称"中证1800")
SAMPLE_SPACES: dict[str, tuple[str, ...] | None] = {
    "full": None,
    "csi800": ("000906.SH",),
    "csi1800": ("000906.SH", "000852.SH"),
}
# 默认样本空间(CLI 单档=旧行为)
DEFAULT_SAMPLE_SPACE = "full"


def resolve_sample_segments(
    market_data: MarketDataProvider,
    sample_spaces: list[str],
    start_str: str,
    end_str: str,
    cancel_check: CancelCheck | None = None,
) -> dict[str, list[tuple[str, set[str]]]]:
    """各样本档的月度快照段: {key: [(快照日, 样本集)] 按快照日升序}(full 不拉取、不进返回)

    拉取窗口 [start−45天, end]——覆盖"首日之前最近的月末快照"(首日样本 = 生效日 ≤ 首日的
    最近一段); 区间内每个月末快照日切换样本池(中途调样生效, 与链式逐日过滤天然兼容)。
    每指数 1 次请求(带翻页), csi1800 为两指数逐快照日并集
    """
    lookback_start = (datetime.strptime(start_str, "%Y%m%d") - timedelta(days=45)).strftime("%Y%m%d")
    segments: dict[str, list[tuple[str, set[str]]]] = {}
    for key in sample_spaces:
        index_codes = SAMPLE_SPACES.get(key)
        if not index_codes:
            continue  # full 档无样本过滤
        if cancel_check is not None:
            cancel_check()
        merged: dict[str, set[str]] = {}
        for index_code in index_codes:
            for snap_date, members in market_data.get_index_weight_snapshots(
                index_code, lookback_start, end_str
            ):
                merged.setdefault(snap_date, set()).update(members)
        segments[key] = sorted(merged.items())
    return segments


def sample_pool_at(segments: list[tuple[str, set[str]]], date_str: str) -> set[str]:
    """当日生效的样本集: 生效日(快照日) ≤ date_str 的最近一段; 无任何可用快照返回空集"""
    pool: set[str] | None = None
    for snap_date, members in segments:
        if snap_date > date_str:
            break
        pool = members
    return pool if pool is not None else set()


def _valuation_compute_key(prefix: str, profit_kind: str, dynamic: bool) -> str:
    """估值/增长各口径的 timings 计时键: pe/growth + [deduct_] + [dynamic_] + compute"""
    return (
        prefix
        + ("deduct_" if profit_kind == "deduct" else "")
        + ("dynamic_" if dynamic else "")
        + "compute"
    )


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
    sample_pools: dict[str, set[str] | None] | None = None,
) -> tuple[RankList, RankList, RankList] | dict[str, tuple[RankList, RankList, RankList]]:
    """获取指定日期的行业涨幅(等权)排名

    div_kind: "price"=官方价格式(除息计入下跌, 默认; 除息股涨幅改用实际市值比),
    "reinvest"=分红再投资/全收益式(除息中性, 原行为, 用 close/pre_close)
    sample_pools: {key: 当日样本集|None}(None 值=不过滤全池)——多样本空间**单循环多桶**:
    每股归属/涨幅只算一次、同时累计到各样本桶(嵌套样本集仅增加集合判定, 纳秒级, 三档
    总耗时≈单档+10%), 返回 {key: (l1, l2, l3)}; 缺省 None 走单池路径返回 (l1, l2, l3)(旧行为)。
    样本集 = index_weight 月度快照 ∩ 当日申万归属(池外股不计入该桶; 停牌按 0%、数据异常
    跳过等规则各桶同享——同一股的处置与样本档无关)
    """
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")

    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    ts_code_to_pct_chg: dict[str, float] = market_data.get_ts_code_to_pct_chg(date)
    if not ts_code_to_pct_chg:
        raise ValueError(f"没有获取到 {date_str} 交易日的行情数据")

    # 聚合容器: key(None=单池) -> (L1/L2/L3 增量平均 map)
    multi = sample_pools is not None
    pool_keys: list = list(sample_pools) if multi else [None]
    chg_maps: dict = {}
    for key in pool_keys:
        chg_maps[key] = tuple(
            {node.index_code: (node.index_code, 0, 0) for node in tree.level_to_nodes[level]}
            for level in (1, 2, 3)
        )

    stock_pool: set[str] = set(ts_code_to_pct_chg) | set(tree.all_member_codes)
    tree.filter_stock_pool(
        stock_pool,
        date,
        date,
        cancel_check=cancel_check,
        restructure_excluded=market_data.get_restructure_excluded(date),
    )

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
        for key in pool_keys:
            sample = sample_pools.get(key) if multi else None
            if sample is not None and ts_code not in sample:
                continue
            l1_map, l2_map, l3_map = chg_maps[key]
            for l_node, l_chg_map in [(l3_node, l3_map), (l2_node, l2_map), (l1_node, l1_map)]:
                l_index_code, l_pct_chg, l_count = l_chg_map[l_node.index_code]
                l_count_new = l_count + 1
                l_pct_chg_new = (l_pct_chg * l_count + pct_chg) / l_count_new
                l_chg_map[l_node.index_code] = (l_index_code, l_pct_chg_new, l_count_new)

    def _sorted_ranks(chg_maps_one) -> tuple[RankList, RankList, RankList]:
        return tuple(
            sorted(
                (item for item in maps.values() if item[2] > 0),
                key=lambda x: x[1],
                reverse=True,
            )
            for maps in chg_maps_one
        )

    if multi:
        return {key: _sorted_ranks(chg_maps[key]) for key in pool_keys}
    return _sorted_ranks(chg_maps[None])


def daily_rank_float_weight(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    timings: dict[str, float] | None = None,
    cancel_check: CancelCheck | None = None,
    mv_kind: str = "free",
    div_kind: str = "price",
    sample_pools: dict[str, set[str] | None] | None = None,
) -> tuple[RankList, RankList, RankList] | dict[str, tuple[RankList, RankList, RankList]]:
    """获取指定日期的行业涨幅(市值加权)排名

    mv_kind: "free"=自由流通市值加权, "total"=总市值加权
    div_kind: "price"=官方价格式(除息计入下跌, 默认; 除息日 M_pre 用昨日实际市值=官方 LV_{t-1}^{Adj},
    自由流通用昨日 free_mv、总市值用昨日 total_mv); "reinvest"=分红再投资/全收益式(除息中性, 原行为)
    sample_pools: {key: 当日样本集|None}——多样本空间**单循环多桶**(见 daily_rank_equal_weight),
    返回 {key: (l1, l2, l3)}; 缺省 None 走单池路径返回 (l1, l2, l3)(旧行为)。停牌回退/缺失股
    点查与样本档无关(按全池参与股解析一次, 各桶共享该股的权重市值)
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

    # 聚合容器: key(None=单池) -> (L3/L2/L1 的 (涨幅map, mv map))——顺序与下方循环
    # zip((l3_node, l2_node, l1_node), ...) 对齐
    multi = sample_pools is not None
    pool_keys: list = list(sample_pools) if multi else [None]
    agg_maps: dict = {}
    for key in pool_keys:
        agg_maps[key] = tuple(
            (
                {node.index_code: (node.index_code, 0, 0) for node in tree.level_to_nodes[level]},
                {node.index_code: (0, 0) for node in tree.level_to_nodes[level]},
            )
            for level in (3, 2, 1)
        )

    stock_pool: set[str] = set(ts_code_to_pct_chg) | set(tree.all_member_codes)
    tree.filter_stock_pool(
        stock_pool,
        date,
        date,
        cancel_check=cancel_check,
        restructure_excluded=market_data.get_restructure_excluded(date),
    )

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

        for key in pool_keys:
            sample = sample_pools.get(key) if multi else None
            if sample is not None and ts_code not in sample:
                continue
            for l_node, (l_chg_map, l_mv_map) in zip(
                (l3_node, l2_node, l1_node), agg_maps[key]
            ):
                l_index_code, l_pct_chg, l_count = l_chg_map[l_node.index_code]
                l_mv1, l_mv2 = l_mv_map[l_node.index_code]
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

    def _sorted_ranks(agg_maps_one) -> tuple[RankList, RankList, RankList]:
        # agg_maps_one 为 (L3, L2, L1) 顺序, 输出反转为 (l1, l2, l3)
        return tuple(
            sorted(
                (item for item in chg_map.values() if item[2] > 0),
                key=lambda x: x[1],
                reverse=True,
            )
            for chg_map, _mv_map in reversed(agg_maps_one)
        )

    if multi:
        return {key: _sorted_ranks(agg_maps[key]) for key in pool_keys}
    return _sorted_ranks(agg_maps[None])


def daily_valuation_metric(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    kind: str,
    timings: dict[str, float] | None = None,
    cancel_check: CancelCheck | None = None,
    profit_kind: str = "attr",
    dynamic: bool = False,
    sample_set: set[str] | None = None,
) -> tuple[dict[str, dict[str, float | None]], dict[str, dict[str, float | None]], dict[str, int]]:
    """单日榜行业财务指标合成值(PE / PB, 项目自建口径): 返回 (free_map, total_map, stats)

    sample_set: 样本空间过滤(多样本空间档各自调用; None=全池)——在归属解析前跳过非样本股,
    csi 档循环量降至样本规模(嵌套档总成本≈1.5×单档)

    kind: "pe"=净利润(元) / "pb"=归母普通股股东权益(元, balancesheet_vip 权威绝对额)。
    profit_kind(仅 kind="pe" 生效): "attr"=归母净利润(profit_dedt+extra_item 行内合成,
          get_ts_code_to_ttm_attr_profit, 默认) / "deduct"=扣非净利润(get_ts_code_to_ttm_deducted_profit)。
    dynamic(仅 kind="pe" 生效): True=动态口径——净利润取最新报告期累计 × 4/k 年化
          (get_ts_code_to_dynamic_profit, 与 Tushare daily_basic 动态市盈率同法; 最新期为年报时
          与 TTM 退化结果相等), False=TTM 口径(滚动 12 个月)。
          四口径(归母/扣非 × TTM/动态, 见 PROFIT_BASES)同一批报告期数据、同规则换字段,
          共享市值缓存, 逐口径调用零新增请求。
    返回 {"1"|"2"|"3": {index_code: 指标值或 None}}:
      - 值 None = 行业分母合计 <= 0(PE 亏损 / PB 资不抵债), 键缺失 = 无数据
      - 每股指标为合成比值无单位(pe)或倍率(pb)

    公式(单股权重与当日市值加权涨幅同源, 口径见 docs/financial_indicators.md):
        PE_free  = Σ free_mv / Σ (股东值 × free_mv / total_mv)   自由流通市值口径
        PE_total = Σ total_mv / Σ 股东值                          总市值口径
      其中 PE 的"股东值"= 所选口径净利润(元→万元)、PB 的"股东值"= 归母普通股股东权益(元→万元,
      balancesheet_vip 归母权益−其他权益工具[已含优先股], 与 bps 分子同口径; 不经"每股×股本"
      折算——报告期后送转/增发/回购与 CDR 股本口径错配不再引入近似, 旧口径偏差见
      known_issues 第 37 条);
      等价于"以自由流通市值为权重的、个股总市值口径指标的加权调和平均"。
    - 参与范围: 与当日涨幅榜同一股票池(filter_stock_pool); 数据异常股**剔除**并计入 stats:
      财务层统计(periods 等)叠加 pool_no_value(无指标数据) / pool_no_mv(无市值) /
      pool_ratio_invalid(自由流通占比越界>1)
    - 市值复用: 与市值加权涨幅同一套当日缓存(含停牌回退值), 指标权重与指数权重一致
    """
    if kind not in ("pe", "pb"):
        raise ValueError(f"不支持的财务指标类型: {kind}")
    if kind == "pe" and profit_kind not in ("attr", "deduct"):
        raise ValueError(f"不支持的净利润口径: {profit_kind}")
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")
    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    if timings is not None:
        _t0 = time.perf_counter()
    if kind == "pe":
        if dynamic:
            value_map, stats = market_data.get_ts_code_to_dynamic_profit(date, profit_kind=profit_kind)  # 动态净利润(元)
        elif profit_kind == "deduct":
            value_map, stats = market_data.get_ts_code_to_ttm_deducted_profit(date)  # TTM 扣非(元)
        else:
            value_map, stats = market_data.get_ts_code_to_ttm_attr_profit(date)  # TTM 归母(元)
    else:
        value_map, stats = market_data.get_ts_code_to_equity(date)  # 归母普通股股东权益(元)
    if timings is not None:
        timings["fetch"] = time.perf_counter() - _t0

    if timings is not None:
        _t0 = time.perf_counter()
    free_mv_map = market_data.get_ts_code_to_free_mv(date)
    total_mv_map = market_data.get_ts_code_to_total_mv(date)
    pct_map = market_data.get_ts_code_to_pct_chg(date)

    stock_pool: set[str] = set(pct_map) | set(tree.all_member_codes)
    tree.filter_stock_pool(
        stock_pool,
        date,
        date,
        cancel_check=cancel_check,
        restructure_excluded=market_data.get_restructure_excluded(date),
    )

    # 聚合容器: 层级键("1"/"2"/"3") -> index_code -> [Σ自由流通市值, Σ自由流通分摊股东值, Σ总市值, Σ股东值, 数量]
    agg: dict[str, dict[str, list[float]]] = {}
    for level_idx, level_key in ((1, "1"), (2, "2"), (3, "3")):
        agg[level_key] = {
            node.index_code: [0.0, 0.0, 0.0, 0.0, 0.0]
            for node in tree.level_to_nodes[level_idx]
        }
    level_key_map = {"L1": "1", "L2": "2", "L3": "3"}

    def _per_stock_value(ts_code: str) -> float | None:
        """股东值(万元): pe=所选口径净利润(元)/1e4; pb=归母普通股股东权益(元)/1e4"""
        v = value_map.get(ts_code)
        return v / 1e4 if v is not None else None

    pool_no_value = 0
    pool_no_mv = 0
    pool_ratio_invalid = 0
    for idx, ts_code in enumerate(stock_pool):
        if cancel_check is not None and idx % 500 == 0:
            cancel_check()
        if sample_set is not None and ts_code not in sample_set:
            continue
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code, date)
        if not l3_node or not l2_node or not l1_node:
            continue

        value_wan = _per_stock_value(ts_code)
        if value_wan is None:
            pool_no_value += 1
            continue
        free_mv = free_mv_map.get(ts_code)
        total_mv = total_mv_map.get(ts_code)
        if free_mv is None or total_mv is None or pd.isna(free_mv) or pd.isna(total_mv):
            pool_no_mv += 1
            continue
        ratio = float(free_mv) / float(total_mv)  # 同一日同一行口径下 = free_share/total_share
        if not (0.0 < ratio <= 1.0):
            pool_ratio_invalid += 1
            continue

        for l_node in (l3_node, l2_node, l1_node):
            entry = agg[level_key_map[l_node.level]][l_node.index_code]
            entry[0] += free_mv
            entry[1] += value_wan * ratio
            entry[2] += total_mv
            entry[3] += value_wan
            entry[4] += 1

    def _finalize() -> tuple[dict[str, dict[str, float | None]], dict[str, dict[str, float | None]]]:
        metric_free: dict[str, dict[str, float | None]] = {"1": {}, "2": {}, "3": {}}
        metric_total: dict[str, dict[str, float | None]] = {"1": {}, "2": {}, "3": {}}
        for level_key, per_node in agg.items():
            for code, (sum_free_mv, sum_free_value, sum_total_mv, sum_value, count) in per_node.items():
                if count == 0:
                    continue  # 无数据也不记键位, 前端显示 "—"
                if sum_free_value > 0:
                    metric_free[level_key][code] = sum_free_mv / sum_free_value
                else:
                    metric_free[level_key][code] = None  # 行业股东值合计 <= 0: PE=亏损 / PB=资不抵债
                if sum_value > 0:
                    metric_total[level_key][code] = sum_total_mv / sum_value
                else:
                    metric_total[level_key][code] = None
        return metric_free, metric_total

    metric_free, metric_total = _finalize()
    if timings is not None:
        timings["compute"] = time.perf_counter() - _t0

    if kind == "pe":
        label = ("扣非" if profit_kind == "deduct" else "归母") + ("动态" if dynamic else "TTM")
    else:
        label = "净资产"
    if pool_no_value or pool_no_mv or pool_ratio_invalid:
        logger.warning(
            f"{date_str} {label}剔除 {pool_no_value} 只(无{label}数据)、{pool_no_mv} 只(无市值)"
            f"、{pool_ratio_invalid} 只(自由流通占比越界>1), 不计入行业{label}合成"
        )
    pool_stats = {
        "pool_no_value": pool_no_value,
        "pool_no_mv": pool_no_mv,
        "pool_ratio_invalid": pool_ratio_invalid,
    }
    return metric_free, metric_total, {**stats, **pool_stats}


def daily_pe(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    timings: dict[str, float] | None = None,
    cancel_check: CancelCheck | None = None,
    profit_kind: str = "attr",
    dynamic: bool = False,
    sample_set: set[str] | None = None,
) -> tuple[dict[str, dict[str, float | None]], dict[str, dict[str, float | None]], dict[str, int]]:
    """单日榜行业 PE: profit_kind="attr"=归母(默认)/"deduct"=扣非 × dynamic=False=TTM(默认)/True=动态,
    四口径(见 PROFIT_BASES)见 daily_valuation_metric; sample_set=样本空间过滤(见其 docstring)"""
    return daily_valuation_metric(tree, market_data, date, "pe", timings, cancel_check, profit_kind, dynamic, sample_set)


def daily_pb(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    timings: dict[str, float] | None = None,
    cancel_check: CancelCheck | None = None,
    sample_set: set[str] | None = None,
) -> tuple[dict[str, dict[str, float | None]], dict[str, dict[str, float | None]], dict[str, int]]:
    """单日榜行业 PB(归母普通股股东权益, balancesheet_vip 权威绝对额): 见 daily_valuation_metric(kind="pb");
    sample_set=样本空间过滤(见其 docstring)"""
    return daily_valuation_metric(tree, market_data, date, "pb", timings, cancel_check, None, False, sample_set)


def classify_profit_growth(now_value: float, last_value: float) -> float | str:
    """净利润同比分类(TTM/动态两口径、个股与行业 Σ 同一规则): 返回 数值%(如 23.45) 或 类别文本

    排序位(低→高): "加大亏损"(两期均≤0 且 当期<基期) < "减少亏损"(两期均≤0 且 当期≥基期)
    < "转亏"(基期>0 当期≤0) < 数值[−100%,∞) < "扭亏"(基期≤0 当期>0);
    数值 = (now/last − 1)×100; now=0 按当期≤0 处理(浮点 TTM 恰为 0 实际遇不到, 规则写死);
    两期均≤0 时按 |当期亏损| 与 |基期亏损| 比较分档(当期更深→加大亏损、持平和收窄→减少亏损)。
    无数据(缺基期等)不入本函数、由键缺失表示(显示"—"、排序恒置底)
    """
    if now_value > 0 and last_value > 0:
        return (now_value / last_value - 1.0) * 100.0
    if now_value > 0:
        return "扭亏"
    if last_value > 0:
        return "转亏"
    return "加大亏损" if now_value < last_value else "减少亏损"


def daily_profit_growth(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    timings: dict[str, float] | None = None,
    cancel_check: CancelCheck | None = None,
    profit_kind: str = "attr",
    dynamic: bool = False,
    sample_set: set[str] | None = None,
) -> tuple[dict[str, dict[str, float | str]], dict[str, dict[str, float | str]], dict[str, int]]:
    """单日榜行业净利润同比(与 PE 分子同源): 返回 (float_levels, total_levels, stats)

    sample_set: 样本空间过滤(多样本空间档各自调用; None=全池, 在归属解析前跳过非样本股)

    profit_kind: "attr"=归母(默认, 含快报双源合并) / "deduct"=扣非(无快报源, 年报季时效
    落后归母一档; 与归母共用已拉报告期数据零新增请求); 类别按各自口径独立判定(归母扭亏而
    扣非仍亏真实存在), 见 get_ts_code_to_ttm_growth_pair / get_ts_code_to_dynamic_growth_pair。
    dynamic: False=TTM 口径(默认, TTM(D)/TTM(D-1年)) / True=动态口径(最新期累计/去年同季累计,
    同相位对比、与"动态值/去年同期动态值"数学等价)。

    **双口径(2026-08-30 改, 原无市值维度单列)**: 随加权方式切换, 与 PE/ROE/回报率同一把尺:
    - total = Σ 当期 / Σ 基期 − 1(全值, 与原实现完全相同——ratio≡1 的退化);
    - float = Σ(当期×ratio) / Σ(基期×ratio) − 1, **ratio = 当日 free_mv/total_mv 固定快照**
      (分子分母同权重, 同比纯粹反映利润变化; 自由流通股东视角的盈利同比, 与 PE(free) 的
      分摊公式同一结构——"PE 亏损 + 同比数值"的呈现违和由此消除, 实测农林牧渔 csi800 档
      free 口径由 -92.5% 数值变为"转亏"与 PE(free) 亏损自洽);
    - 两口径类别各自独立判定(类别可能不同: 全公司数值型而自由流通盘"转亏"——盈利与自由
      流通结构错位的信号); ratio 无效(缺失/越界>1)的股票仅从 float 口径剔除(不连坐 total);
    - **等权模式显示"—"**(与 PE/PB/ROE/回报率一致, 2026-08-30 定稿; 原等权显示全值)。
    levels: {"1"|"2"|"3": {index_code: 数值% | "扭亏" | "转亏" | "加大亏损" | "减少亏损"}},
    键缺失 = 无数据(显示"—")。
    参与股票 = 两期值均有的成分股(both-or-neither, 缺基期的新股不进分子分母); 亏损股不
    剔除、负值参与合计; 类别由 Σ 两期的符号与大小按 classify_profit_growth 判定。
    stats: 数据层六键(stocks_pair/turnaround/turnloss/widen_loss/narrow_loss/no_base, 全市场口径)
    叠加 pool_no_value(池内无增长对的股票数) / pool_ratio_invalid(自由流通占比越界, 仅 float 口径剔除)
    """
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")
    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    if timings is not None:
        _t0 = time.perf_counter()
    if dynamic:
        pair_map, stats = market_data.get_ts_code_to_dynamic_growth_pair(date, profit_kind=profit_kind)
    else:
        pair_map, stats = market_data.get_ts_code_to_ttm_growth_pair(date, profit_kind=profit_kind)
    if timings is not None:
        timings["fetch"] = time.perf_counter() - _t0

    if timings is not None:
        _t0 = time.perf_counter()
    pct_map = market_data.get_ts_code_to_pct_chg(date)
    free_mv_map = market_data.get_ts_code_to_free_mv(date)
    total_mv_map = market_data.get_ts_code_to_total_mv(date)
    stock_pool: set[str] = set(pct_map) | set(tree.all_member_codes)
    tree.filter_stock_pool(
        stock_pool,
        date,
        date,
        cancel_check=cancel_check,
        restructure_excluded=market_data.get_restructure_excluded(date),
    )

    # 聚合容器: 层级键 -> index_code -> [Σ当期, Σ基期, Σ当期×ratio, Σ基期×ratio, total 数量, float 数量]
    agg: dict[str, dict[str, list[float]]] = {}
    for level_idx, level_key in ((1, "1"), (2, "2"), (3, "3")):
        agg[level_key] = {node.index_code: [0.0] * 6 for node in tree.level_to_nodes[level_idx]}
    level_key_map = {"L1": "1", "L2": "2", "L3": "3"}

    pool_no_value = 0
    pool_ratio_invalid = 0
    for idx, ts_code in enumerate(stock_pool):
        if cancel_check is not None and idx % 500 == 0:
            cancel_check()
        if sample_set is not None and ts_code not in sample_set:
            continue
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code, date)
        if not l3_node or not l2_node or not l1_node:
            continue
        pair = pair_map.get(ts_code)
        if pair is None:
            pool_no_value += 1
            continue
        now_value, last_value = pair
        free_mv = free_mv_map.get(ts_code)
        total_mv = total_mv_map.get(ts_code)
        ratio = None
        if (
            free_mv is not None and total_mv is not None
            and not pd.isna(free_mv) and not pd.isna(total_mv) and total_mv > 0
            and 0.0 < free_mv / total_mv <= 1.0
        ):
            ratio = free_mv / total_mv
        else:
            pool_ratio_invalid += 1
        for l_node in (l3_node, l2_node, l1_node):
            entry = agg[level_key_map[l_node.level]][l_node.index_code]
            entry[0] += now_value
            entry[1] += last_value
            entry[4] += 1
            if ratio is not None:
                entry[2] += now_value * ratio
                entry[3] += last_value * ratio
                entry[5] += 1

    total_levels: dict[str, dict[str, float | str]] = {"1": {}, "2": {}, "3": {}}
    float_levels: dict[str, dict[str, float | str]] = {"1": {}, "2": {}, "3": {}}
    for level_key, per_node in agg.items():
        for code, (sum_now, sum_last, sum_now_f, sum_last_f, count, count_f) in per_node.items():
            if count == 0:
                continue  # 无参与股票不记键位, 前端显示 "—"
            total_levels[level_key][code] = classify_profit_growth(sum_now, sum_last)
            if count_f > 0:
                float_levels[level_key][code] = classify_profit_growth(sum_now_f, sum_last_f)
    if timings is not None:
        timings["compute"] = time.perf_counter() - _t0

    if pool_no_value:
        logger.warning(f"{date_str} 净利润同比剔除 {pool_no_value} 只(无两期数据, 多为缺基期的新股), 不计入行业合成")
    if pool_ratio_invalid:
        logger.warning(f"{date_str} 净利润同比 float 口径剔除 {pool_ratio_invalid} 只(自由流通占比缺失/越界)")
    return float_levels, total_levels, {**stats, "pool_no_value": pool_no_value, "pool_ratio_invalid": pool_ratio_invalid}


def daily_roe(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    timings: dict[str, float] | None = None,
    cancel_check: CancelCheck | None = None,
    sample_set: set[str] | None = None,
) -> tuple[
    dict[str, dict[str, dict[str, float]]],
    dict[str, dict[str, dict[str, float]]],
    dict[str, int],
]:
    """单日榜行业 ROE(加权平均算法, 四口径一次算出): 返回 (levels_float, levels_total, stats)

    sample_set: 样本空间过滤(多样本空间档各自调用; None=全池, 在归属解析前跳过非样本股)

    levels: {basis: {"1"|"2"|"3": {index_code: ROE%}}}——行业值 = **按当日市值权重的个股 ROE
    加权算术平均** Σ(市值ᵢ×ROEᵢ)÷Σ市值ᵢ×100(指数按什么权重分配成分股、指标就用什么权重:
    自由流通/总市值两套随加权方式切换, 等权模式无市值权重、前端与 PE/PB 一致显示"—";
    个股 ROE 为负[亏损]正常参与加权, 算术平均对负值稳定)。数据(每股分子/分母对)见
    market_data.get_ts_code_to_roes——披露值 roe_waa 锚定、全链不接业绩快报、roe_waa 缺失
    四口径全部降级; 键缺失 = 无参与股票(前端显示"—")。
    stats: 数据层五键(periods/stocks_with_roe/stocks_missing/stocks_ttm_full/stocks_ttm_fallback)
    叠加 pool_no_value(池内无 ROE 数据)/pool_no_mv(无市值, 不能参与加权)
    """
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")
    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    if timings is not None:
        _t0 = time.perf_counter()
    pair_maps, stats = market_data.get_ts_code_to_roes(date)
    if timings is not None:
        timings["fetch"] = time.perf_counter() - _t0

    if timings is not None:
        _t0 = time.perf_counter()
    pct_map = market_data.get_ts_code_to_pct_chg(date)
    free_map = market_data.get_ts_code_to_free_mv(date)  # 万股/万元口径同批缓存
    total_map = market_data.get_ts_code_to_total_mv(date)
    stock_pool: set[str] = set(pct_map) | set(tree.all_member_codes)
    tree.filter_stock_pool(
        stock_pool,
        date,
        date,
        cancel_check=cancel_check,
        restructure_excluded=market_data.get_restructure_excluded(date),
    )

    # 聚合容器: 市值口径 -> basis -> 层级键 -> index_code -> [Σ(市值×ROE), Σ市值, 数量]
    kinds = ("float", "total")
    agg: dict[str, dict[str, dict[str, dict[str, list[float]]]]] = {
        kind: {basis: {} for basis in pair_maps} for kind in kinds
    }
    for level_idx, level_key in ((1, "1"), (2, "2"), (3, "3")):
        for kind in kinds:
            for basis in agg[kind]:
                agg[kind][basis][level_key] = {
                    node.index_code: [0.0, 0.0, 0.0] for node in tree.level_to_nodes[level_idx]
                }
    level_key_map = {"L1": "1", "L2": "2", "L3": "3"}

    def _usable(mv: float | None) -> bool:
        return mv is not None and not pd.isna(mv) and mv > 0

    pool_no_value = 0
    pool_no_mv = 0
    for idx, ts_code in enumerate(stock_pool):
        if cancel_check is not None and idx % 500 == 0:
            cancel_check()
        if sample_set is not None and ts_code not in sample_set:
            continue
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code, date)
        if not l3_node or not l2_node or not l1_node:
            continue
        has_any_basis = False
        for basis, pair_map in pair_maps.items():
            pair = pair_map.get(ts_code)
            if pair is None:
                continue  # 各口径覆盖互不连坐(如扣非缺失仅缺扣非两档)
            has_any_basis = True
            numerator, denominator = pair
            roe_pct = numerator / denominator * 100.0
            for kind, mv_map in (("float", free_map), ("total", total_map)):
                mv = mv_map.get(ts_code)
                if not _usable(mv):
                    continue  # 该市值口径缺失(停牌回退也没有)仅缺该口径
                for l_node in (l3_node, l2_node, l1_node):
                    entry = agg[kind][basis][level_key_map[l_node.level]][l_node.index_code]
                    entry[0] += mv * roe_pct
                    entry[1] += mv
                    entry[2] += 1
        if not has_any_basis:
            pool_no_value += 1
        elif not (_usable(free_map.get(ts_code)) or _usable(total_map.get(ts_code))):
            pool_no_mv += 1  # 有 ROE 但两口径市值均缺失, 无法加权

    levels_by_kind: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for kind in kinds:
        levels_by_kind[kind] = {basis: {"1": {}, "2": {}, "3": {}} for basis in agg[kind]}
        for basis, per_level in agg[kind].items():
            for level_key, per_node in per_level.items():
                for code, (sum_wr, sum_w, count) in per_node.items():
                    if count == 0:
                        continue  # 无参与股票不记键位, 前端显示 "—"
                    levels_by_kind[kind][basis][level_key][code] = sum_wr / sum_w
    if timings is not None:
        timings["compute"] = time.perf_counter() - _t0

    if pool_no_value or pool_no_mv:
        logger.warning(
            f"{date_str} ROE 剔除 {pool_no_value} 只(池内无 ROE 数据, 多为 roe_waa 未披露)、"
            f"{pool_no_mv} 只(两口径市值均缺失), 不计入行业合成"
        )
    return levels_by_kind["float"], levels_by_kind["total"], {**stats, "pool_no_value": pool_no_value, "pool_no_mv": pool_no_mv}


def daily_dividend_yield(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    timings: dict[str, float] | None = None,
    cancel_check: CancelCheck | None = None,
    sample_set: set[str] | None = None,
) -> tuple[dict[str, dict[str, dict[str, dict[str, float]]]], dict[str, int]]:
    """单日榜行业股息率(双口径一次算出): 返回 (levels_by_kind, stats)

    sample_set: 样本空间过滤(多样本空间档各自调用; None=全池, 在归属解析前跳过非样本股)

    levels_by_kind: {"float"|"total": {basis("est"|"static"|"est_bb"): {"1"|"2"|"3": {index_code: 回报率%}}}}
    ——行业值 = **按当日市值权重的加权平均**(指数按什么权重分配成分股、指标就用
    什么权重): total = Σ(DPS×总股本)÷Σ总市值(数学上恒等于个股总市值股息率的市值加权平均,
    即原整体法)、float = Σ(DPS×自由流通股本)÷Σ自由流通市值(自由流通盘的现金回报, 系统性
    高于 total 口径); 等权模式无市值权重、前端与 PE/PB 一致显示"—"。
    **est_bb = est + TTM 窗口注销分量**(折每股后与 DPS 同量纲, 金额层相加再除同一分母;
    窗口/台阶法见 market_data.get_ts_code_to_buyback_amount, 台阶缓存未就绪时该口径整体
    降级不产出)。
    与子表每股口径(DPS/close)自洽(加权平均的权重与分母同源)。
    DPS 见 market_data.get_ts_code_to_dividend_dps(总额法/锚定/完整性三态/兜底规则);
    DPS 键缺失(无数据)的股票剔除并计入 pool_no_value——**DPS=0(齐备零分红)是数值, 正常参与
    加权**(非分红股摊薄行业股息率, 属语义本身); 对应市值口径/收盘价缺失剔除并计入 pool_no_mv
    (float 口径的自由流通股本 = 自由流通市值÷收盘价, 停牌无 close 时仅缺 float 口径)。
    stats: 数据层键(stocks_total/static/static_zero/static_fallback/est/est_zero/est_realized/
    no_anchor/no_profit/no_share) 叠加 pool_no_value / pool_no_mv
    """
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")
    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    date_str = date.strftime("%Y%m%d")

    if timings is not None:
        _t0 = time.perf_counter()
    est_map, static_map, stats = market_data.get_ts_code_to_dividend_dps(date)
    dps_maps: dict[str, dict[str, float]] = {"est": est_map, "static": static_map}
    if timings is not None:
        timings["fetch"] = time.perf_counter() - _t0

    if timings is not None:
        _t0 = time.perf_counter()
    total_mv_map = market_data.get_ts_code_to_total_mv(date)  # 万元
    free_mv_map = market_data.get_ts_code_to_free_mv(date)
    close_map = market_data.get_ts_code_to_close(date)
    share_map = market_data.get_ts_code_to_total_share(date)  # 万股(总股本, total 口径分子)

    # 注销分量 → est_bb("TTM估算股息+注销率"): TTM 窗口台阶法注销金额(万元)÷当前总股本(万股)
    # = 元/股, 与 DPS 同量纲、聚合公式零特例; est 缺失(unknown)的股票不产出 est_bb(unknown≠zero,
    # 与股息率三态一致), 窗口内无注销(金额 0)是数值(= 股息率本身); 台阶缓存未就绪/计算失败 →
    # 本次无该口径(仅 est/static, 前端 est_bb 显示"—"), 不连坐两基础口径
    try:
        bb_map, bb_stats = market_data.get_ts_code_to_buyback_amount(date)
        dps_maps["est_bb"] = {
            ts_code: dps + (bb_map.get(ts_code, 0.0) / share_wan if (share_wan := share_map.get(ts_code)) else 0.0)
            for ts_code, dps in est_map.items()
        }
        stats = {**stats, **bb_stats}
    except Exception as err:  # noqa: BLE001 - est_bb 口径降级不影响 est/static
        logger.warning(f"注销分量(est_bb) 计算失败, 本次无该口径: {err!r}")
    pct_map = market_data.get_ts_code_to_pct_chg(date)
    stock_pool: set[str] = set(pct_map) | set(tree.all_member_codes)
    tree.filter_stock_pool(
        stock_pool,
        date,
        date,
        cancel_check=cancel_check,
        restructure_excluded=market_data.get_restructure_excluded(date),
    )

    # 聚合容器: 市值口径 -> basis -> 层级键 -> index_code -> [Σ(市值×股息率), Σ市值, 数量]
    kinds = ("float", "total")
    agg: dict[str, dict[str, dict[str, dict[str, list[float]]]]] = {
        kind: {basis: {} for basis in dps_maps} for kind in kinds
    }
    for level_idx, level_key in ((1, "1"), (2, "2"), (3, "3")):
        for kind in kinds:
            for basis in agg[kind]:
                agg[kind][basis][level_key] = {
                    node.index_code: [0.0, 0.0, 0.0] for node in tree.level_to_nodes[level_idx]
                }
    level_key_map = {"L1": "1", "L2": "2", "L3": "3"}

    def _usable(mv: float | None) -> bool:
        return mv is not None and not pd.isna(mv) and mv > 0

    pool_no_value = 0
    pool_no_mv = 0
    for idx, ts_code in enumerate(stock_pool):
        if cancel_check is not None and idx % 500 == 0:
            cancel_check()
        if sample_set is not None and ts_code not in sample_set:
            continue
        l1_node, l2_node, l3_node = tree.get_stock_industry_nodes(ts_code, date)
        if not l3_node or not l2_node or not l1_node:
            continue
        close = close_map.get(ts_code)
        free_mv = free_mv_map.get(ts_code)
        total_mv = total_mv_map.get(ts_code)
        share_wan = share_map.get(ts_code)
        float_ok = _usable(free_mv) and close is not None and close > 0  # 自由流通股本=市值÷close
        total_ok = _usable(total_mv) and share_wan is not None and share_wan > 0
        has_any_basis = False
        for basis, dps_map in dps_maps.items():
            dps = dps_map.get(ts_code)
            if dps is None:
                continue  # 各口径覆盖互不连坐
            has_any_basis = True
            for kind in kinds:
                if kind == "float" and not float_ok:
                    continue
                if kind == "total" and not total_ok:
                    continue
                # 市值×个股股息率(= DPS×对应口径股本, 万元), 与分母同一权重源
                if kind == "float":
                    contribution = dps * (free_mv / close)
                    weight = free_mv
                else:
                    contribution = dps * share_wan
                    weight = total_mv
                yield_pct = contribution / weight * 100.0
                for l_node in (l3_node, l2_node, l1_node):
                    entry = agg[kind][basis][level_key_map[l_node.level]][l_node.index_code]
                    entry[0] += weight * yield_pct
                    entry[1] += weight
                    entry[2] += 1
        if not has_any_basis:
            pool_no_value += 1
        elif not (float_ok or total_ok):
            pool_no_mv += 1

    levels_by_kind: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for kind in kinds:
        levels_by_kind[kind] = {basis: {"1": {}, "2": {}, "3": {}} for basis in dps_maps}
        for basis, per_level in agg[kind].items():
            for level_key, per_node in per_level.items():
                for code, (sum_wy, sum_w, count) in per_node.items():
                    if count == 0:
                        continue  # 无参与股票不记键位, 前端显示 "—"
                    levels_by_kind[kind][basis][level_key][code] = sum_wy / sum_w
    if timings is not None:
        timings["compute"] = time.perf_counter() - _t0

    if pool_no_value or pool_no_mv:
        logger.warning(
            f"{date_str} 股息率剔除 {pool_no_value} 只(池内无 DPS 数据) 、{pool_no_mv} 只(两口径市值/股本均缺失), 不计入行业合成"
        )
    return levels_by_kind, {**stats, "pool_no_value": pool_no_value, "pool_no_mv": pool_no_mv}


def start_metric_prefetch(
    market_data: MarketDataProvider,
    date: datetime,
    universe: set[str],
    cancel_check: CancelCheck | None = None,
) -> tuple[threading.Thread, dict[str, float], threading.Thread, list[Exception]]:
    """启动财务/分红/股本台阶后台预热(单日榜与区间链式榜共用, 调用方在编排最开始调用)

    fina 三池(含增长基期 D-1年 串行补拉)、dividend 缓存刷新、股本台阶缓存刷新各一线程、
    接口限流独立, 与行情/市值拉取及涨幅计算全程并行、只写各自缓存; 指标计算阶段
    (compute_fin_metric_suite) join 命中。返回 (fina 线程, fina 墙时 dict[键 "secs"],
    分红线程, 分红异常转存列表, 台阶线程, 台阶异常转存列表)——分红/台阶线程异常不直接抛
    (存列表, 由 suite 的股息率阶段 join 处理: 分红异常重抛走股息率整列降级, 台阶异常仅
    est_bb 口径降级、est/static 不连坐)
    """
    fina_wall: dict[str, float] = {}
    div_exc: list[Exception] = []
    share_exc: list[Exception] = []

    def _warm_fina() -> None:
        _w0 = time.perf_counter()
        market_data.prefetch_fina_indicators(date, growth_base_date=market_data.growth_base_date(date))
        fina_wall["secs"] = time.perf_counter() - _w0

    def _warm_dividend() -> None:
        _w0 = time.perf_counter()
        try:
            action = market_data.dividend_history.ensure_refresh(universe, cancel_check=cancel_check)
            logger.info(f"分红缓存刷新: {action} ({time.perf_counter() - _w0:.1f}s)")
        except Exception as err:  # noqa: BLE001 - 线程异常转存, suite 的股息率阶段重抛
            div_exc.append(err)

    def _warm_share_change() -> None:
        _w0 = time.perf_counter()
        try:
            action = market_data.share_change_history.ensure_refresh(cancel_check=cancel_check)
            # 回购公告缓存(对赌 1 元/0 元注销的 vol 交叉验证)同线程串行刷新: 按月拉取请求量
            # 极小(首刷 24 个月 24 次/增量 1~2 次); 失败独立捕获——交叉验证是增强而非前置,
            # 仅跳过验证(est_bb 维持台阶口径), 不连坐台阶缓存
            try:
                action += "; 回购公告 " + market_data.repurchase_history.ensure_refresh(cancel_check=cancel_check)
            except Exception as rep_err:  # noqa: BLE001 - 交叉验证降级, 台阶不受影响
                logger.warning(f"回购公告缓存刷新失败(跳过对赌交叉验证): {rep_err!r}")
            logger.info(f"股本台阶缓存刷新: {action} ({time.perf_counter() - _w0:.1f}s)")
        except Exception as err:  # noqa: BLE001 - 线程异常转存, suite 的 est_bb 阶段降级
            share_exc.append(err)

    fina_thread = threading.Thread(target=_warm_fina, daemon=True)
    dividend_thread = threading.Thread(target=_warm_dividend, daemon=True)
    share_thread = threading.Thread(target=_warm_share_change, daemon=True)
    fina_thread.start()
    dividend_thread.start()
    share_thread.start()
    return fina_thread, fina_wall, dividend_thread, div_exc, share_thread, share_exc


def compute_fin_metric_suite(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    timings: dict[str, float] | None = None,
    cancel_check: CancelCheck | None = None,
    fina_thread: threading.Thread | None = None,
    fina_wall: dict[str, float] | None = None,
    dividend_thread: threading.Thread | None = None,
    div_exc: list[Exception] | None = None,
    share_thread: threading.Thread | None = None,
    notify: Callable[[float, str, str | None], None] | None = None,
    progress_base: float = 93.5,
    progress_step: float = 0.3,
    sample_sets: dict[str, set[str] | None] | None = None,
) -> dict[str, Any]:
    """单日时点财务指标全套一次算出(PE 四口径/PB/净利润同比四口径/ROE 四口径/股息率双口径)

    **单日榜(date=查询日)与区间链式榜(date=区间末交易日, "区间末时点值"口径)共用同一编排**,
    避免两套实现漂移: 涨幅=区间累计、指标=区间末收盘快照(与指数公司估值表"最新收盘日"口径
    一致)。预热线程由 start_metric_prefetch 启动、各阶段 join 命中缓存(缺省不 join, 供测试
    直接调用); 各指标失败逐项降级为空数据(前端/CLI 显示"—"), 不影响涨幅榜主结果。
    sample_sets: {key: 当日样本集|None}——多样本空间档逐档调用底层聚合(sample_set 在归属
    解析前跳过非样本股, 嵌套档总成本≈1.5×单档); 缺省 None 走单档(全池)路径。
    返回: 单档 valuation dict(键结构见 run_daily_ranking); sample_sets 提供时返回
    {key: valuation}(各档完整同构, 降级也写入空结构, 调用方可硬下标)。
    末段取消兜底: 股息率为本套最后阶段, 其降级 except 会连 JobCancelled 一并吞掉(任务误报
    success), 函数返回前再查一次 cancel_check 把取消状态补抛(未请求取消时零副作用)。
    notify(percent, message, phase): 进度回调(按指标段通知一次、不逐档重复);
    progress_base/progress_step 控制刻度(单日 93.5/0.3, 区间链式 97.0/0.08——11 个指标段
    不越过"计算完成"刻度)。
    """
    def _notify_stage(mode: float, message: str) -> None:
        if notify is not None:
            notify(max(0.0, min(99.0, mode)), message, "计算财务指标")

    def _join_fina() -> None:
        if fina_thread is not None:
            fina_thread.join()
        if timings is not None and "fina_fetch" not in timings and fina_wall and "secs" in fina_wall:
            timings["fina_fetch"] = fina_wall["secs"]

    multi = sample_sets is not None
    keys: list = list(sample_sets) if multi else [None]
    valuation_by_key: dict = {key: {} for key in keys}

    def _set_for(key) -> set[str] | None:
        return None if key is None else sample_sets.get(key)

    def _run_valuation(
        valuation: dict, sample_set: set[str] | None,
        kind: str, label: str, compute_key: str,
        profit_kind: str = "attr", dynamic: bool = False,
    ) -> None:
        """财务指标阶段(pe/pb): 失败时报错降级, 不影响涨幅榜主结果(口径见 daily_valuation_metric)"""
        v_timings: dict[str, float] = {}
        metric_free: dict[str, dict[str, float | None]] = {}
        metric_total: dict[str, dict[str, float | None]] = {}
        metric_stats: dict[str, int] = {}
        try:
            _join_fina()  # 等待后台预热; 预热失败时 pe/pb 一致降级、不重复拉取
            if kind == "pe":
                metric_free, metric_total, metric_stats = daily_pe(
                    tree, market_data, date, timings=v_timings, cancel_check=cancel_check,
                    profit_kind=profit_kind, dynamic=dynamic, sample_set=sample_set,
                )
            else:
                metric_free, metric_total, metric_stats = daily_pb(
                    tree, market_data, date, timings=v_timings, cancel_check=cancel_check,
                    sample_set=sample_set,
                )
        except Exception as err:
            logger.warning(f"{label} 计算失败, 本次无该列: {err!r}")
        if timings is not None:
            timings[compute_key] = v_timings.get("compute", 0.0)
        valuation.update({"free": metric_free, "total": metric_total, "stats": metric_stats})

    # PE 四口径(PROFIT_BASES)**一次全部算出**: 共享同一批报告期财务数据与市值缓存, 动态口径
    # 仅本地重算零新增请求, Web 端"净利润口径"下拉切换显示; 进度按段通知一次、段内逐档算
    _mode = progress_base
    for basis, (profit_kind, dynamic) in PROFIT_BASES.items():
        basis_label = ("归母" if profit_kind == "attr" else "扣非") + ("动态" if dynamic else "-TTM")
        _notify_stage(_mode, f"计算行业PE({basis_label})")
        for key in keys:
            valuation_by_key[key][f"pe_{basis}"] = {}
            _run_valuation(
                valuation_by_key[key][f"pe_{basis}"], _set_for(key),
                "pe", f"PE({basis_label})", _valuation_compute_key("pe_", profit_kind, dynamic),
                profit_kind=profit_kind, dynamic=dynamic,
            )
        _mode += progress_step
    _notify_stage(_mode, "计算行业PB")
    for key in keys:
        valuation_by_key[key]["pb"] = {}
        _run_valuation(valuation_by_key[key]["pb"], _set_for(key), "pb", "PB", "pb_compute")
    _mode += progress_step

    # 净利润同比(无市值维度)四口径一次算出: 与 PE 共用已拉报告期数据, 动态/扣非口径仅本地
    # 重算零新增请求; 失败时报错降级为空 levels(前端显示"—")
    for basis, (profit_kind, dynamic) in PROFIT_BASES.items():
        basis_label = ("归母" if profit_kind == "attr" else "扣非") + ("动态" if dynamic else "-TTM")
        _notify_stage(_mode, f"计算行业净利润同比({basis_label})")
        for key in keys:
            g_timings: dict[str, float] = {}
            growth_float: dict[str, dict[str, float | str]] = {"1": {}, "2": {}, "3": {}}
            growth_total: dict[str, dict[str, float | str]] = {"1": {}, "2": {}, "3": {}}
            stats_one: dict[str, int] = {}
            try:
                _join_fina()
                growth_float, growth_total, stats_one = daily_profit_growth(
                    tree, market_data, date, timings=g_timings, cancel_check=cancel_check,
                    profit_kind=profit_kind, dynamic=dynamic, sample_set=_set_for(key),
                )
            except Exception as err:
                logger.warning(f"净利润同比({basis}) 计算失败, 本次无该列: {err!r}")
            if timings is not None:
                timings[_valuation_compute_key("growth_", profit_kind, dynamic)] = g_timings.get("compute", 0.0)
            valuation_by_key[key][f"growth_{basis}"] = {
                "float": growth_float, "total": growth_total, "stats": stats_one,
            }
        _mode += progress_step

    # ROE(加权平均算法)四口径一次算出: **按当日市值权重的加权算术平均**(随加权方式切换
    # free/total 两口径、等权显示"—", 与 PE/PB 一致); 披露值 roe_waa 锚定、**全链不接业绩
    # 快报**(快报窗口内时效落后 PE/同比一档)、roe_waa 缺失四口径全部降级; 失败时报错降级
    _notify_stage(_mode, "计算行业ROE(加权平均)")
    for key in keys:
        r_timings: dict[str, float] = {}
        roe_levels: dict[str, dict[str, dict[str, dict[str, float]]]] = {"float": {}, "total": {}}
        roe_stats: dict[str, int] = {}
        try:
            _join_fina()
            roe_float, roe_total, roe_stats = daily_roe(
                tree, market_data, date, timings=r_timings, cancel_check=cancel_check,
                sample_set=_set_for(key),
            )
            roe_levels = {"float": roe_float, "total": roe_total}
        except Exception as err:
            logger.warning(f"ROE(加权平均) 计算失败, 本次无该列: {err!r}")
            roe_levels, roe_stats = {"float": {}, "total": {}}, {}
        if timings is not None:
            timings["roe_compute"] = r_timings.get("compute", 0.0)
        for basis in PROFIT_BASES:
            valuation_by_key[key][f"roe_waa_{basis}"] = {
                "float": roe_levels["float"].get(basis, {}),
                "total": roe_levels["total"].get(basis, {}),
                "stats": roe_stats,
            }
    _mode += progress_step

    # 股息率/回报率(总额法 DPS 三口径一次算出: est=TTM估算股息率/Web 默认 + static=静态股息率 +
    # est_bb=TTM估算股息+注销率[台阶法注销分量]): **按当日市值权重的加权平均**(free/total 两口径
    # 随加权方式切换、等权显示"—"); 失败时报错降级为空 levels; 分红刷新线程异常在此重抛(首刷失败/
    # 网络错误走股息率整列降级; JobCancelled 经下方 return 前的末段兜底补抛); 台阶刷新线程仅 join
    # (异常由 daily_dividend_yield 内的 est_bb try 捕获降级该口径, est/static 不连坐)
    _notify_stage(_mode, "计算行业股息率")
    for key in keys:
        d_timings: dict[str, float] = {}
        div_levels: dict[str, dict[str, dict[str, dict[str, float]]]] = {"float": {}, "total": {}}
        div_stats: dict[str, int] = {}
        try:
            _join_fina()
            if dividend_thread is not None:
                dividend_thread.join()
            if div_exc:
                raise div_exc[0]
            if share_thread is not None:
                share_thread.join()
            div_levels, div_stats = daily_dividend_yield(
                tree, market_data, date, timings=d_timings, cancel_check=cancel_check,
                sample_set=_set_for(key),
            )
        except Exception as err:  # noqa: BLE001 - 股息率列降级不影响涨幅榜主结果
            logger.warning(f"股息率 计算失败, 本次无该列: {err!r}")
            div_levels, div_stats = {"float": {}, "total": {}}, {}
        if timings is not None:
            timings["div_yield_compute"] = d_timings.get("compute", 0.0)
        valuation_by_key[key]["div_yield"] = {
            "float": div_levels.get("float", {}),
            "total": div_levels.get("total", {}),
            "stats": div_stats,
        }

    # 末段取消兜底(股息率为全套最后阶段, 其降级 except 吞掉的取消信号在此补抛给上层)
    if cancel_check is not None:
        cancel_check()

    if multi:
        return valuation_by_key
    return valuation_by_key[None]


def run_daily_ranking(
    tree: ShenWanIndustryTree,
    market_data: MarketDataProvider,
    date: datetime,
    progress_callback: DailyProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    sample_spaces: list[str] | None = None,
) -> tuple[
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    tuple[RankList, RankList, RankList],
    dict[str, float],
    dict[str, Any],
]:
    """单日榜编排: 拉行情/市值 -> 等权 -> 加权 -> PE/PB/净利润同比/ROE, 返回
    (等权·官方价格式, 等权·分红再投资式, 自由流通·官方价格式, 自由流通·分红再投资式,
    总市值·官方价格式, 总市值·分红再投资式, timings, valuation)

    等权/自由流通市值加权/总市值加权各提供两种口径: "官方价格式"(默认, 除息计入下跌, 与申万官方
    价格指数一致)与"分红再投资/全收益式"(除息中性, 原行为)。
    valuation: "pe_{basis}"/"pb" 为 {"free": {"1"|"2"|"3": {index_code: 值|None}}, "total": {...},
    "stats": {...}}——PE 的四口径(basis ∈ PROFIT_BASES: 归母/扣非 × TTM/动态)**一次全部算出**
    (共享同一批财务数据与市值缓存, 动态与扣非口径仅本地重算零新增请求), 供 Web"净利润口径"
    下拉切换显示; **"growth_{basis}" 为 {"value": {"1"|"2"|"3": {index_code: 数值%|"扭亏"/"转亏"/
    "加大亏损"/"减少亏损"}}, "stats": {...}}**(净利润同比四口径一次算出, 无市值维度、等权模式也显示、
    随"净利润口径"下拉切换, 键缺失=无数据/降级); **"roe_waa_{basis}" 为 {"value":
    {"1"|"2"|"3": {index_code: ROE%}}, "stats": {...}}**(ROE 加权平均算法四口径一次算出
    (daily_roe), 无市值维度、等权模式也显示、随"净利润口径"下拉切换, 键缺失=无数据/降级/
    无参与股票); **"div_yield" 为 {"value": {"est"|"static": {"1"|"2"|"3": {index_code:
    股息率%}}}, "stats": {...}}**(股息率双口径一次算出(daily_dividend_yield), est=TTM估算股息率
    [Web 默认]/static=静态股息率, 无 free/total 双市值维度、等权模式也显示、随"回报率口径"下拉
    切换, 键缺失=无数据/降级/无参与股票); None=亏损/资不抵债(仅 PE/PB)、键缺失=无数据/降级;
    口径见 daily_valuation_metric / daily_pe / daily_pb / daily_profit_growth / daily_roe /
    daily_dividend_yield;
    财务接口失败时
    任一指标降级为空数据(告警不中断, 涨幅榜不受影响)。
    供入口脚本 daily_ranking.py 与 Web service._run_daily 共用, 避免两套编排漂移。
    timings key: daily_fetch / mv_fetch / equal_compute / equal_tr_compute / float_compute /
    float_fallback / float_resolve / float_tr_compute / float_tr_fallback / float_tr_resolve /
    total_compute / total_fallback / total_resolve / total_tr_compute / total_tr_fallback /
    total_tr_resolve / fina_fetch(fina+balancesheet+express 三池并行总墙时, 含增长基期串行补拉) /
    pe_compute / pe_dynamic_compute / pe_deduct_compute / pe_deduct_dynamic_compute / pb_compute /
    growth_compute / growth_dynamic_compute / growth_deduct_compute / growth_deduct_dynamic_compute /
    roe_compute(ROE 四口径一次) / div_yield_compute(股息率双口径一次)
    progress_callback: 可选 (0~100, 阶段说明, 阶段名), 阶段名用于 Web 前端展示
    """
    date_str = date.strftime("%Y%m%d")
    timings: dict[str, float] = {}

    def _notify(percent: float, message: str, phase: str | None = None) -> None:
        if progress_callback is not None:
            progress_callback(max(0.0, min(100.0, percent)), message, phase)

    # 财务/分红后台预热: 在编排**最开始**启动(只依赖 provider、不依赖行情/市值), 与行情/市值
    # 拉取及后续六条涨幅序列计算**全程并行**——fina_indicator_vip(利润/bps)、balancesheet_vip
    # (归母净资产) 与 express_vip(业绩快报) 三池同时并发(接口限流互相独立, 见
    # MarketDataProvider.prefetch_fina_indicators), 并串行补拉 TTM 增长基期([D-36月, D-12月],
    # 旧报告期行数触顶 9999 自动翻页, 实测含基期共 ~16 次 fina 请求、限速地板 ~2.1s、预热总墙时
    # ~3.1s——早启动才能整体藏进 行情+市值+涨幅计算 ~3.3s 的窗口内) + dividend 缓存刷新
    # (首次运行需全市场逐股首刷 ~12 分钟一次性, 日常增量秒级); PE/PB/增长阶段 join 命中预热
    # 缓存; 线程失败在 join 处抛出、走既有"指标降级告警"路径
    fina_thread, fina_wall, dividend_thread, div_exc, share_thread, _share_exc = start_metric_prefetch(
        market_data, date, set(tree.all_member_codes), cancel_check=cancel_check
    )

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

    # 样本空间(多样本档一次全算, Web 用): 快照拉取失败降级为仅全 A 档(涨幅榜不受影响)
    sample_spaces = list(sample_spaces or [])
    multi_sample = bool(sample_spaces and sample_spaces != ["full"])
    sample_pools: dict[str, set[str] | None] | None = None
    if multi_sample:
        try:
            segments = resolve_sample_segments(market_data, sample_spaces, date_str, date_str, cancel_check)
            sample_pools = {
                key: (None if SAMPLE_SPACES.get(key) is None else sample_pool_at(segments[key], date_str))
                for key in sample_spaces
            }
        except Exception as err:  # noqa: BLE001 - index_weight 失败降级全 A
            logger.warning(f"样本空间快照拉取失败, 本次仅计算全A档: {err!r}")
            multi_sample = False
            sample_spaces = ["full"]

    _notify(68.0, "计算等权涨幅(官方价格式)", "计算排行榜")
    t0 = time.perf_counter()
    ew = daily_rank_equal_weight(tree, market_data, date, cancel_check, div_kind="price", sample_pools=sample_pools)
    timings["equal_compute"] = time.perf_counter() - t0

    _notify(72.0, "计算等权涨幅(分红再投资式)", "计算排行榜")
    t0 = time.perf_counter()
    ew_reinvest = daily_rank_equal_weight(tree, market_data, date, cancel_check, div_kind="reinvest", sample_pools=sample_pools)
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
        sample_pools=sample_pools,
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
        sample_pools=sample_pools,
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
        sample_pools=sample_pools,
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
        sample_pools=sample_pools,
    )
    timings["total_tr_compute"] = time.perf_counter() - t0
    timings["total_tr_fallback"] = tw_tr_timings.get("mv_fallback", 0.0)
    timings["total_tr_resolve"] = tw_tr_timings.get("mv_resolve", 0.0)

    # 财务指标全套(PE 四口径/PB/净利润同比四口径/ROE 四口径/股息率双口径)一次算出: 公共编排
    # 与区间链式榜共用(compute_fin_metric_suite), 键结构/降级/末段取消兜底见其 docstring
    valuation = compute_fin_metric_suite(
        tree,
        market_data,
        date,
        timings=timings,
        cancel_check=cancel_check,
        fina_thread=fina_thread,
        fina_wall=fina_wall,
        dividend_thread=dividend_thread,
        div_exc=div_exc,
        share_thread=share_thread,
        notify=_notify,
        progress_base=93.5,
        progress_step=0.3,
        sample_sets=sample_pools,
    )

    if multi_sample:
        # ({key: (六序列)}, timings, {key: valuation}): 三档完整同构, Web 前端切档即时显示
        return (
            {key: (ew[key], ew_reinvest[key], fw[key], fw_reinvest[key], tw[key], tw_reinvest[key]) for key in sample_spaces},
            timings,
            valuation,
        )

    return (
        ew,
        ew_reinvest,
        fw,
        fw_reinvest,
        tw,
        tw_reinvest,
        timings,
        valuation,
    )


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
    valuation_out: dict[str, Any] | None = None,
    sample_spaces: list[str] | None = None,
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
    valuation_out: 传入非 None dict 时, 追加计算**区间末交易日时点值**的财务指标全套
    (PE 四口径/PB/净利润同比四口径/ROE 四口径/股息率双口径, 与单日榜同一公共编排
    compute_fin_metric_suite)并写入该 dict(键结构与 run_daily_ranking 的 valuation 一致,
    调用方可硬下标; Web 链式区间榜由此携带财务列——涨幅=区间累计、指标=区间末收盘快照,
    与指数公司估值表"最新收盘日"口径一致); None 则完全不算(静态版/对照场景)。

    性能约定: 行情从 fetch_daily_batch 一次拉取(并回填 pct/close 缓存), 逐日不再重复请求;
    市值每日一次全市场 daily_basic(同请求缓存 free/total); 除息识别每日一次 dividend(仅价格式需要, 缓存共享);
    **停牌股跨日复用**: 当日不在全市场市值数据中的股票(=停牌)沿用最近一次已知市值(停牌期间必然不变),
    零重复点查, 复牌/新上市当日由全市场数据自动刷新(见 market_data 缓存机制);
    valuation_out 场景另在编排最开始启动财务/分红预热线程(与三池预取及逐日计算全程并行,
    接口限流独立), 指标阶段 join 命中缓存、末日市值直接复用逐日循环已拉的缓存零新增请求。
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

    # 财务/分红后台预热(仅 valuation_out 场景): 以**区间末交易日**为时点, 与三池预取及逐日
    # 链式计算全程并行(接口限流独立); 指标阶段 join 命中。末日市值由逐日循环的 daily_basic
    # 缓存直接命中, 财务指标零重复拉取
    fina_thread: threading.Thread | None = None
    fina_wall: dict[str, float] | None = None
    dividend_thread: threading.Thread | None = None
    div_exc: list[Exception] | None = None
    if valuation_out is not None:
        last_day_dt = datetime.strptime(trading_days[-1], "%Y%m%d")
        fina_thread, fina_wall, dividend_thread, div_exc, share_thread, _share_exc = start_metric_prefetch(
            market_data, last_day_dt, set(tree.all_member_codes), cancel_check=cancel_check
        )

    # 样本空间(多样本档一次全算, Web 用): 各档月度快照段, 快照拉取失败降级为仅全 A 档
    sample_spaces = list(sample_spaces or [])
    multi_sample = bool(sample_spaces and sample_spaces != ["full"])
    sample_segments: dict[str, list[tuple[str, set[str]]]] = {}
    if multi_sample:
        try:
            sample_segments = resolve_sample_segments(
                market_data, sample_spaces, start_str, end_str, cancel_check
            )
        except Exception as err:  # noqa: BLE001 - index_weight 失败降级全 A
            logger.warning(f"样本空间快照拉取失败, 本次仅计算全A档: {err!r}")
            multi_sample = False
            sample_spaces = ["full"]

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

    # 6 条序列的连乘容器: series -> [样本档key(None=单档)] -> 层级"1/2/3" -> index_code -> 累计因子
    series_names = ("ew_p", "ew_r", "fw_p", "fw_r", "tw_p", "tw_r")
    pool_keys: list = list(sample_spaces) if multi_sample else [None]
    chain_prod: dict[str, dict[str, dict[str, dict[str, float]]]] = {
        series: {key: {"1": {}, "2": {}, "3": {}} for key in pool_keys} for series in series_names
    }
    # 末次已知成分股数量(各序列同日一致, 以最后一天的榜单为准)
    last_counts: dict[str, dict[str, dict[str, int]]] = {
        key: {"1": {}, "2": {}, "3": {}} for key in pool_keys
    }

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

        # 当日各样本档的生效样本(月度快照分段: 跨月在快照日切换, 中途调样生效)
        day_pools: dict[str, set[str] | None] | None = None
        if multi_sample:
            day_pools = {
                key: (
                    None if SAMPLE_SPACES.get(key) is None
                    else sample_pool_at(sample_segments[key], day_str)
                )
                for key in sample_spaces
            }

        # 当日 6 条序列(与单日榜同一套函数/口径; 多样本档单循环多桶)
        if timings is not None:
            _t0 = time.perf_counter()
        day_results: dict[str, tuple | dict] = {
            "ew_p": daily_rank_equal_weight(tree, market_data, day_dt, cancel_check, div_kind="price", sample_pools=day_pools),
            "ew_r": daily_rank_equal_weight(tree, market_data, day_dt, cancel_check, div_kind="reinvest", sample_pools=day_pools),
            "fw_p": daily_rank_float_weight(tree, market_data, day_dt, cancel_check=cancel_check, mv_kind="free", div_kind="price", sample_pools=day_pools),
            "fw_r": daily_rank_float_weight(tree, market_data, day_dt, cancel_check=cancel_check, mv_kind="free", div_kind="reinvest", sample_pools=day_pools),
            "tw_p": daily_rank_float_weight(tree, market_data, day_dt, cancel_check=cancel_check, mv_kind="total", div_kind="price", sample_pools=day_pools),
            "tw_r": daily_rank_float_weight(tree, market_data, day_dt, cancel_check=cancel_check, mv_kind="total", div_kind="reinvest", sample_pools=day_pools),
        }
        if timings is not None:
            timings["compute"] = timings.get("compute", 0.0) + (time.perf_counter() - _t0)

        for series, ranks in day_results.items():
            per_key = ranks if multi_sample else {None: ranks}
            for key, (l1, l2, l3) in per_key.items():
                for lv, rank_list in (("1", l1), ("2", l2), ("3", l3)):
                    target = chain_prod[series][key][lv]
                    count_target = last_counts[key][lv]
                    for code, pct, count in rank_list:
                        target[code] = target.get(code, 1.0) * (1.0 + pct / 100.0)
                        count_target[code] = count

        _notify(72.0 + (day_idx + 1) / total_days * 25.0,
                f"逐日链式计算中 {day_idx + 1}/{total_days} 个交易日")

    # 结果: 连乘因子转累计涨幅, 按涨幅降序
    def _make_rank(series: str, key, lv: str) -> RankList:
        factored = chain_prod[series][key][lv]
        return sorted(
            (
                (code, (factor - 1.0) * 100.0, last_counts[key][lv].get(code, 0))
                for code, factor in factored.items()
            ),
            key=lambda x: x[1],
            reverse=True,
        )

    levels = {
        (series, key): tuple(_make_rank(series, key, lv) for lv in ("1", "2", "3"))
        for series in series_names
        for key in pool_keys
    }

    # 区间末交易日时点财务指标全套(仅 valuation_out 场景): 与单日榜同一公共编排(键结构/降级/
    # 末段取消兜底一致), 进度刻度 97.0~97.9(不越过 98"计算完成"段)
    if valuation_out is not None:
        _notify(97.0, "计算区间末财务指标")
        end_pools = (
            {
                key: (
                    None if SAMPLE_SPACES.get(key) is None
                    else sample_pool_at(sample_segments[key], trading_days[-1])
                )
                for key in sample_spaces
            }
            if multi_sample
            else None
        )
        valuation_out.update(
            compute_fin_metric_suite(
                tree,
                market_data,
                last_day_dt,
                timings=timings,
                cancel_check=cancel_check,
                fina_thread=fina_thread,
                fina_wall=fina_wall,
                dividend_thread=dividend_thread,
                div_exc=div_exc,
                share_thread=share_thread,
                # 链式版 _notify 为 2 参(无阶段名), 套件按单日榜 3 参调用——包一层丢弃阶段名
                notify=lambda pct, message, _phase: _notify(pct, message),
                progress_base=97.0,
                progress_step=0.08,
                sample_sets=end_pools,
            )
        )

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
    if multi_sample:
        return {
            key: (
                levels[("ew_p", key)], levels[("ew_r", key)],
                levels[("fw_p", key)], levels[("fw_r", key)],
                levels[("tw_p", key)], levels[("tw_r", key)],
            )
            for key in pool_keys
        }
    return (
        levels[("ew_p", None)], levels[("ew_r", None)], levels[("fw_p", None)],
        levels[("fw_r", None)], levels[("tw_p", None)], levels[("tw_r", None)]
    )
