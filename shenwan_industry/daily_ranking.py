"""
单日行业涨幅榜入口脚本

行业树与成分数据在 industry_tree.py, 行情数据在 market_data.py, 排行榜算法在
industry_ranking.py (含 run_daily_ranking 编排), 本脚本负责组装、打印榜单并输出耗时分析。
"""

import time
from datetime import datetime

import tushare as ts
from tushare.pro.client import DataApi

try:
    from .config_store import config_path, get_token
    from .industry_tree import ShenWanIndustryTree
    from .market_data import MarketDataProvider
    from .industry_ranking import run_daily_ranking, print_timing
except ImportError:
    from config_store import config_path, get_token
    from industry_tree import ShenWanIndustryTree
    from market_data import MarketDataProvider
    from industry_ranking import run_daily_ranking, print_timing


if __name__ == "__main__":
    """代码示例: 指定一个日期, 计算所有申万行业的流通市值加权涨幅和等权涨幅"""
    token: str = get_token()
    if not token:
        raise ValueError(
            "未配置 Tushare token，请先运行 Web 服务（python -m shenwan_industry.web.server）"
            "并在页面右上角填写保存 token；或直接编辑本地配置文件: " + str(config_path())
        )

    pro: DataApi = ts.pro_api(token=token)
    provider = MarketDataProvider(pro)  # 构造时已包装 API 调用计数

    tree = ShenWanIndustryTree(tushare_pro=provider.pro)

    t0 = time.perf_counter()
    tree.build_industries()
    tree.build_constituent_stocks_by_tushare()
    prep_secs = time.perf_counter() - t0

    rank_date = datetime(2025, 4, 7)

    (l1_rank_list_ew, l2_rank_list_ew, l3_rank_list_ew), \
        (l1_rank_list_fw, l2_rank_list_fw, l3_rank_list_fw), timings = run_daily_ranking(
            tree, provider, rank_date
        )

    print_timing(
        [
            ("数据准备", [("行业树+成分加载", prep_secs)]),
            ("行情数据", [("行情获取 daily", timings["daily_fetch"])]),
            ("市值数据", [("市值获取 daily_basic", timings["circ_fetch"])]),
            ("排行计算", [
                ("等权计算", timings["equal_compute"]),
                ("停牌市值回退", timings["float_fallback"]),
                ("加权聚合", max(timings["float_compute"] - timings["float_fallback"], 0.0)),
            ]),
        ],
        provider.snapshot_api_calls(),
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
