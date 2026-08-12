"""
申万行业排行榜: 单日榜 + 区间榜

- daily_rank_equal_weight / daily_rank_float_weight: 单日榜 (自 classification.py 迁移, 逻辑不变)
- rank_range: 区间累计涨幅榜

区间榜网络策略: 区间内每个交易日拉一次 daily(trade_date), 用每日官方涨跌幅
(close/pre_close, 除权除息日即除权参考价口径) 连乘得到个股区间收益;
停牌日无行自动按 0% 累计, 不再逐股回退查收益; 权重取区间起始日流通市值
(daily_basic 一次 + 仅起始日停牌的少量回退)。
"""

import logging
import math
import warnings
from datetime import datetime, timedelta

import pandas as pd

try:
    from .tree import ShenWanIndustryTree
except ImportError:  # 直接运行本文件时
    from tree import ShenWanIndustryTree

logger = logging.getLogger("shenwan_industry.ranking")

# 榜单项: (行业 index_code, 涨跌幅%, 成分股数量)
RankList = list[tuple[str, float, int]]


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
    for _ix, row in df.iterrows():
        d_str = row['trade_date']
        if datetime.strptime(d_str, "%Y%m%d") <= date:
            cand = row['circ_mv']
            if pd.isna(cand):
                continue  # 该日市值缺失, 继续往前找最近的有效值
            return float(cand)
    return None


def daily_rank_float_weight(
    tree: ShenWanIndustryTree,
    date: datetime,
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
                l_circ_mv = _resolve_circ_mv(tree, ts_code, date, date_str)
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
        for _ix, row in df.iterrows():
            ts_code = row['ts_code']
            pre_close = row['pre_close']
            close = row['close']
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


def rank_range(
    tree: ShenWanIndustryTree,
    start_date: datetime,
    end_date: datetime,
) -> tuple[tuple[RankList, RankList, RankList], tuple[RankList, RankList, RankList]]:
    """
    区间累计涨幅榜, 返回 (等权(l1,l2,l3), 流通市值加权(l1,l2,l3))

    口径:
    - 参与股票 = 区间起始日已在成分(in_date <= 起点) 且 区间末仍在(delist_date >= 终点);
      中段才纳入的剔除并告警, 区间末前已退市的不参与; 起始日尚未上市(list_date >= 起点)的剔除并告警
    - 个股区间收益 = 区间内所有有行情日的每日官方涨跌幅连乘(除权除息自动修正),
      隐含基准 = 区间内首个有行情日的 pre_close(即区间前一交易日收盘/停牌前收盘), **包含起始日当天涨跌**
    - 权重 = 区间起始日流通市值(起始日停牌的按 730 天回退; 仍取不到则仅参与等权榜并告警)
    """
    if not tree.root.children:
        raise RuntimeError("请先构建行业树结构")

    if not tree.constituent_stock_to_l3_node:
        raise RuntimeError("请先加载行业成分股")

    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")
    if start_str > end_str:
        raise ValueError(f"区间起点不能晚于终点: {start_str} > {end_str}")

    trading_days = _get_trading_days(tree.pro, start_str, end_str)
    if not trading_days:
        raise ValueError(f"区间内没有交易日: {start_str} ~ {end_str}")

    # 1) 参与股票: 起始日已在成分 且 区间末仍在
    participating: set[str] = set()
    for ts_code in tree.constituent_stock_to_l3_node:
        in_date = tree.ts_code_to_in_date.get(ts_code)
        delist_date = tree.ts_code_to_delist_date.get(ts_code)
        list_date = tree.stock_basic.get(ts_code, {}).get('list_date')
        if list_date is not None and not pd.isna(list_date) and str(list_date) >= start_str:
            logger.warning(
                f"区间榜剔除起始日尚未上市股票: {ts_code} list_date={list_date} 晚于区间起点 {start_str}"
            )
            continue
        if in_date is not None and in_date > start_str:
            logger.warning(
                f"区间榜剔除中段纳入股票: {ts_code} in_date={in_date} 晚于区间起点 {start_str}"
            )
            continue
        if delist_date is not None and delist_date < end_str:
            continue  # 区间末前已退市, 按区间末成分口径不参与
        participating.add(ts_code)

    # 2) 逐日拉行情, 连乘区间累计收益(含起始日当天涨跌, 隐含基准=首个有行情日的 pre_close)
    stock_prod: dict[str, float] = {}
    for day_str in trading_days:
        day_data = _fetch_daily_by_date(tree.pro, day_str)
        if not day_data:
            continue
        for ts_code, (close, pre_close) in day_data.items():
            if ts_code not in participating:
                continue
            stock_prod[ts_code] = stock_prod.get(ts_code, 1.0) * (close / pre_close)

    # 区间累计收益(%): 整段区间无任何行情的股票直接剔除
    stock_ret: dict[str, float] = {}
    for ts_code in participating:
        if stock_prod.get(ts_code) is not None:
            stock_ret[ts_code] = (stock_prod.get(ts_code, 1.0) - 1.0) * 100.0

    # 3) 权重: 区间起始日(=区间内第一个交易日)流通市值, 停牌回退
    weight_date_str = trading_days[0]
    weight_date = datetime.strptime(weight_date_str, "%Y%m%d")
    ts_code_to_circ_mv: dict[str, float] = tree.get_ts_code_to_circ_mv(weight_date)
    for ts_code in stock_ret:
        if ts_code_to_circ_mv.get(ts_code) is None or pd.isna(ts_code_to_circ_mv.get(ts_code)):
            circ_mv = _resolve_circ_mv(tree, ts_code, weight_date, weight_date_str)
            if circ_mv is None:
                logger.warning(f"区间榜无法获取 {ts_code} 起始日流通市值, 仅参与等权榜")
                continue
            ts_code_to_circ_mv[ts_code] = circ_mv

    # 4) 聚合三级行业: 等权 = 起始成分简单平均; 加权 = 起始流通市值加权
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

    return (
        (l1_ew_list, l2_ew_list, l3_ew_list),
        (l1_fw_list, l2_fw_list, l3_fw_list),
    )


if __name__ == "__main__":
    """区间榜示例: 计算一个交易日区间的申万行业区间累计涨幅"""
    import tushare as ts
    from vnpy.trader.setting import SETTINGS

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    RANGE_START = datetime(2024, 9, 24)
    RANGE_END = datetime(2024, 12, 31)

    token: str = SETTINGS["datafeed.password"]
    if not token:
        raise ValueError("请先在 vnpy 的 datafeed.password 配置中设置你的 tushare token")

    pro = ts.pro_api(token=token)
    tree = ShenWanIndustryTree(tushare_pro=pro)
    tree.build_industries()
    tree.build_constituent_stocks_by_tushare()

    (l1_ew, l2_ew, l3_ew), (l1_fw, l2_fw, l3_fw) = rank_range(tree, RANGE_START, RANGE_END)

    for level, ew, fw in ((3, l3_ew, l3_fw), (2, l2_ew, l2_fw), (1, l1_ew, l1_fw)):
        print(f"\n\n{RANGE_START.strftime('%Y-%m-%d')} ~ {RANGE_END.strftime('%Y-%m-%d')} 申万{level}级行业区间涨幅榜")
        print("流通市值加权涨幅|等权涨幅|行业名称|成分股数量")
        for index_ts_code, fw_pct, count in fw:
            ew_pct = next((x[1] for x in ew if x[0] == index_ts_code), None)
            if ew_pct is None:
                raise ValueError(f"没有获取到等权重区间涨幅数据: index_code={index_ts_code}")
            print(
                f"{'+' if fw_pct >= 0 else ''}{fw_pct:.2f}%|"
                f"{'+' if ew_pct >= 0 else ''}{ew_pct:.2f}%|"
                f"{tree.index_code_to_node[index_ts_code].industry_name_long}|{count}"
            )
