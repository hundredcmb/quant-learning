"""
单日行业涨幅榜入口脚本

行业树与成分数据在 industry_tree.py, 排行榜算法在 industry_ranking.py,
本脚本负责组装、打印榜单并输出耗时分析。
"""

import time
from datetime import datetime

import tushare as ts
from tushare.pro.client import DataApi
from vnpy.trader.setting import SETTINGS

try:
    from .industry_tree import ShenWanIndustryTree
    from .industry_ranking import (
        daily_rank_equal_weight,
        daily_rank_float_weight,
        wrap_api_counter,
        print_timing,
    )
except ImportError:
    from industry_tree import ShenWanIndustryTree
    from industry_ranking import (
        daily_rank_equal_weight,
        daily_rank_float_weight,
        wrap_api_counter,
        print_timing,
    )


if __name__ == "__main__":
    """代码示例: 指定一个日期, 计算所有申万行业的流通市值加权涨幅和等权涨幅"""
    token: str = SETTINGS["datafeed.password"]
    if not token:
        raise ValueError("请先在 vnpy 的 datafeed.password 配置中设置你的 tushare token")

    pro: DataApi = ts.pro_api(token=token)
    api_calls = wrap_api_counter(pro)

    tree = ShenWanIndustryTree(tushare_pro=pro)

    t0 = time.perf_counter()
    tree.build_industries()
    tree.build_constituent_stocks_by_tushare()
    prep_secs = time.perf_counter() - t0

    rank_date = datetime(2025, 4, 7)

    t0 = time.perf_counter()
    tree.get_ts_code_to_pct_chg(rank_date)
    pct_fetch_secs = time.perf_counter() - t0

    t0 = time.perf_counter()
    tree.get_ts_code_to_circ_mv(rank_date)
    circ_fetch_secs = time.perf_counter() - t0

    t0 = time.perf_counter()
    l1_rank_list_ew, l2_rank_list_ew, l3_rank_list_ew = daily_rank_equal_weight(tree, rank_date)
    ew_secs = time.perf_counter() - t0

    fw_timings: dict[str, float] = {}
    t0 = time.perf_counter()
    l1_rank_list_fw, l2_rank_list_fw, l3_rank_list_fw = daily_rank_float_weight(
        tree, rank_date, timings=fw_timings
    )
    fw_secs = time.perf_counter() - t0

    fallback_secs = fw_timings.get("circ_fallback", 0.0)
    print_timing(
        [
            ("数据准备", [("行业树+成分加载", prep_secs)]),
            ("行情数据", [("行情获取 daily", pct_fetch_secs)]),
            ("市值数据", [("市值获取 daily_basic", circ_fetch_secs)]),
            ("排行计算", [
                ("等权计算", ew_secs),
                ("停牌市值回退", fallback_secs),
                ("加权聚合", max(fw_secs - fallback_secs, 0.0)),
            ]),
        ],
        api_calls,
    )

    rank_results = [(), (l1_rank_list_ew, l1_rank_list_fw), (l2_rank_list_ew, l2_rank_list_fw), (l3_rank_list_ew, l3_rank_list_fw)]

    industry_levels = [3, 2, 1]
    for industry_level in industry_levels:
        rank_list_equal_weight, rank_list = rank_results[industry_level]
        print(f"\n\n{rank_date.strftime('%Y-%m-%d')} 申万{industry_level}级行业涨幅榜")
        print(f"流通市值加权涨幅|等权涨幅|行业名称|成分股数量 成分股列表")
        for index_ts_code, index_pct_chg, stock_count in rank_list:
            index_pct_chg_ew = -100
            for i in rank_list_equal_weight:
                if i[0] == index_ts_code:
                    index_pct_chg_ew = i[1]
            if index_pct_chg_ew == -100:
                raise ValueError(f"没有获取到等权重涨幅数据: index_code={index_ts_code}")

            print(f"{'+' if index_pct_chg >= 0 else ''}{index_pct_chg:.2f}%|" +
                  f"{'+' if index_pct_chg_ew >= 0 else ''}{index_pct_chg_ew:.2f}%|" +
                  f"{tree.index_code_to_node[index_ts_code].industry_name_long}|{stock_count}",
                  [f"{tree.stock_basic[s]['name']}({s})" for s in tree.index_code_to_node[index_ts_code].constituent_stocks])
