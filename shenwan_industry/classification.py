"""
单日行业涨幅榜入口脚本

行业树与成分数据在 tree.py, 排行榜逻辑在 ranking.py, 本脚本只负责组装与打印。
"""

from datetime import datetime

import tushare as ts
from tushare.pro.client import DataApi
from vnpy.trader.setting import SETTINGS

try:
    from .tree import ShenWanIndustryTree
    from .ranking import daily_rank_equal_weight, daily_rank_float_weight
except ImportError:
    from tree import ShenWanIndustryTree
    from ranking import daily_rank_equal_weight, daily_rank_float_weight


if __name__ == "__main__":
    """代码示例: 指定一个日期, 计算所有申万行业的流通市值加权涨幅和等权涨幅"""
    token: str = SETTINGS["datafeed.password"]
    if not token:
        raise ValueError("请先在 vnpy 的 datafeed.password 配置中设置你的 tushare token")

    pro: DataApi = ts.pro_api(token=token)

    tree = ShenWanIndustryTree(tushare_pro=pro)
    tree.build_industries()
    tree.build_constituent_stocks_by_tushare()

    rank_date = datetime(2025, 4, 7)

    l1_rank_list_fw, l2_rank_list_fw, l3_rank_list_fw = daily_rank_float_weight(tree, rank_date)
    l1_rank_list_ew, l2_rank_list_ew, l3_rank_list_ew = daily_rank_equal_weight(tree, rank_date)
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
