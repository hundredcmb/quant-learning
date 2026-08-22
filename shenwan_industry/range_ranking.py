"""
区间累计涨幅榜入口脚本

行业树与成分数据在 industry_tree.py, 行情数据在 market_data.py, 排行榜算法在
industry_ranking.py, 本脚本负责组装、打印区间榜单并输出耗时分析。
"""

import logging
import time
from datetime import datetime

import tushare as ts

try:
    from .config_store import config_path, get_token
    from .industry_tree import ShenWanIndustryTree
    from .market_data import MarketDataProvider
    from .industry_ranking import rank_range, rank_range_chain, print_timing
except ImportError:
    from config_store import config_path, get_token
    from industry_tree import ShenWanIndustryTree
    from market_data import MarketDataProvider
    from industry_ranking import rank_range, rank_range_chain, print_timing


if __name__ == "__main__":
    """区间榜示例: 计算一个交易日区间的申万行业区间累计涨幅"""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    RANGE_START = datetime(2024, 9, 24)
    RANGE_END = datetime(2024, 12, 31)
    RANGE_CHAIN = True  # 官方逐日链式对照; False 只打印静态版区间榜

    token: str = get_token()
    if not token:
        raise ValueError(
            "未配置 Tushare token，请先运行 Web 服务（python -m shenwan_industry.web.server）"
            "并在页面右上角填写保存 token；或直接编辑本地配置文件: " + str(config_path())
        )

    pro = ts.pro_api(token=token)
    provider = MarketDataProvider(pro)  # 构造时已包装 API 调用计数

    tree = ShenWanIndustryTree(tushare_pro=provider.pro)

    t0 = time.perf_counter()
    tree.build_industries()
    tree.build_constituent_stocks_by_tushare()
    prep_secs = time.perf_counter() - t0

    timings: dict[str, float] = {}
    (l1_ew, l2_ew, l3_ew), (l1_fw, l2_fw, l3_fw), (l1_tw, l2_tw, l3_tw) = rank_range(
        tree, provider, RANGE_START, RANGE_END, timings=timings
    )

    other_secs = (
        timings.get("trade_cal", 0.0)
        + timings.get("participate", 0.0)
        + timings.get("compute", 0.0)
    )
    print(
        f"\n区间 {RANGE_START.strftime('%Y-%m-%d')} ~ {RANGE_END.strftime('%Y-%m-%d')} "
        f"共 {int(timings.get('trading_days', 0))} 个交易日"
    )
    print_timing(
        [
            ("数据准备", [("行业树+成分加载", prep_secs)]),
            ("行情数据", [
                (f"逐日行情拉取({int(timings.get('trading_days', 0))}次 daily, 并发+限流)", timings.get("daily_fetch", 0.0)),
                ("收益累计", timings.get("accumulate", 0.0)),
            ]),
            ("市值权重", [
                ("市值拉取 daily_basic", timings.get("mv_fetch", 0.0)),
                ("停牌市值回退", timings.get("mv_fallback", 0.0)),
            ]),
            ("其他计算", [("日历+筛选+聚合", other_secs)]),
        ],
        provider.snapshot_api_calls(),
    )

    for level, ew, fw, tw in ((3, l3_ew, l3_fw, l3_tw), (2, l2_ew, l2_fw, l2_tw), (1, l1_ew, l1_fw, l1_tw)):
        print(f"\n\n{RANGE_START.strftime('%Y-%m-%d')} ~ {RANGE_END.strftime('%Y-%m-%d')} 申万{level}级行业区间涨幅榜")
        print("总市值加权涨幅|自由流通市值加权涨幅|等权涨幅|行业名称|成分股数量")
        for index_ts_code, fw_pct, count in fw:
            ew_pct = next((x[1] for x in ew if x[0] == index_ts_code), None)
            if ew_pct is None:
                raise ValueError(f"没有获取到等权重区间涨幅数据: index_code={index_ts_code}")
            tw_pct = next((x[1] for x in tw if x[0] == index_ts_code), None)
            if tw_pct is None:
                raise ValueError(f"没有获取到总市值加权区间涨幅数据: index_code={index_ts_code}")
            print(
                f"{'+' if tw_pct >= 0 else ''}{tw_pct:.2f}%|"
                f"{'+' if fw_pct >= 0 else ''}{fw_pct:.2f}%|"
                f"{'+' if ew_pct >= 0 else ''}{ew_pct:.2f}%|"
                f"{tree.index_code_to_node[index_ts_code].industry_name_long}|{count}"
            )

    if RANGE_CHAIN:
        chain_timings: dict[str, float] = {}
        (l1_ew_p, l2_ew_p, l3_ew_p), (l1_ew_r, l2_ew_r, l3_ew_r), \
            (l1_fw_p, l2_fw_p, l3_fw_p), (l1_fw_r, l2_fw_r, l3_fw_r), \
            (l1_tw_p, l2_tw_p, l3_tw_p), (l1_tw_r, l2_tw_r, l3_tw_r) = rank_range_chain(
                tree, provider, RANGE_START, RANGE_END, timings=chain_timings
            )
        print_timing(
            [
                ("链式数据", [
                    ("预取(行情+市值+除息并行)", chain_timings.get("prefetch", 0.0)),
                    ("停牌/缺失股点查", chain_timings.get("mv_resolve", 0.0)),
                ]),
                ("链式计算", [
                    ("每日 6 序列聚合", chain_timings.get("compute", 0.0)),
                    ("收益累计", chain_timings.get("accumulate", 0.0)),
                ]),
            ],
            provider.snapshot_api_calls(),
        )

        for level, ew_p, ew_r, fw_p, fw_r, tw_p, tw_r in (
            (3, l3_ew_p, l3_ew_r, l3_fw_p, l3_fw_r, l3_tw_p, l3_tw_r),
            (2, l2_ew_p, l2_ew_r, l2_fw_p, l2_fw_r, l2_tw_p, l2_tw_r),
            (1, l1_ew_p, l1_ew_r, l1_fw_p, l1_fw_r, l1_tw_p, l1_tw_r),
        ):
            print(f"\n\n{RANGE_START.strftime('%Y-%m-%d')} ~ {RANGE_END.strftime('%Y-%m-%d')} 申万{level}级行业区间涨幅榜(官方逐日链)")
            print("总市值加权涨幅(官方价格)|总市值·分红再投资涨幅|自由流通市值加权涨幅(官方价格)|自由流通·分红再投资涨幅|等权涨幅(官方价格)|等权·分红再投资涨幅|行业名称|成分股数量")
            for index_ts_code, fw_pct, count in fw_p:
                ew_pct = next((x[1] for x in ew_p if x[0] == index_ts_code), None)
                if ew_pct is None:
                    raise ValueError(f"没有获取到等权重链式区间涨幅数据: index_code={index_ts_code}")
                ew_r_pct = next((x[1] for x in ew_r if x[0] == index_ts_code), None)
                if ew_r_pct is None:
                    raise ValueError(f"没有获取到等权重链式全收益区间涨幅数据: index_code={index_ts_code}")
                fw_r_pct = next((x[1] for x in fw_r if x[0] == index_ts_code), None)
                if fw_r_pct is None:
                    raise ValueError(f"没有获取到自由流通链式全收益区间涨幅数据: index_code={index_ts_code}")
                tw_pct = next((x[1] for x in tw_p if x[0] == index_ts_code), None)
                if tw_pct is None:
                    raise ValueError(f"没有获取到总市值链式区间涨幅数据: index_code={index_ts_code}")
                tw_r_pct = next((x[1] for x in tw_r if x[0] == index_ts_code), None)
                if tw_r_pct is None:
                    raise ValueError(f"没有获取到总市值链式全收益区间涨幅数据: index_code={index_ts_code}")
                print(
                    f"{'+' if tw_pct >= 0 else ''}{tw_pct:.2f}%|"
                    f"{'+' if tw_r_pct >= 0 else ''}{tw_r_pct:.2f}%|"
                    f"{'+' if fw_pct >= 0 else ''}{fw_pct:.2f}%|"
                    f"{'+' if fw_r_pct >= 0 else ''}{fw_r_pct:.2f}%|"
                    f"{'+' if ew_pct >= 0 else ''}{ew_pct:.2f}%|"
                    f"{'+' if ew_r_pct >= 0 else ''}{ew_r_pct:.2f}%|"
                    f"{tree.index_code_to_node[index_ts_code].industry_name_long}|{count}"
                )
