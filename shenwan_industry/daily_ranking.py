"""
单日行业涨幅榜入口脚本

行业树与成分数据在 industry_tree.py, 行情数据在 market_data.py, 排行榜算法在
industry_ranking.py (含 run_daily_ranking 编排), 本脚本负责组装、打印榜单并输出耗时分析。
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import tushare as ts
from tushare.pro.client import DataApi

try:
    from .industry_tree import ShenWanIndustryTree
    from .market_data import MarketDataProvider
    from .industry_ranking import run_daily_ranking, print_timing, DEFAULT_PROFIT_BASIS
except ImportError:
    from industry_tree import ShenWanIndustryTree
    from market_data import MarketDataProvider
    from industry_ranking import run_daily_ranking, print_timing, DEFAULT_PROFIT_BASIS

# token 配置在仓库根公共模块（与 holders 共享同一份 .quant-learning/settings.json）
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from config_store import config_path, get_token


if __name__ == "__main__":
    """代码示例: 指定一个日期, 计算所有申万行业的自由流通市值加权涨幅和等权涨幅"""
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
        (l1_rank_list_ewtr, l2_rank_list_ewtr, l3_rank_list_ewtr), \
        (l1_rank_list_fw, l2_rank_list_fw, l3_rank_list_fw), \
        (l1_rank_list_fr, l2_rank_list_fr, l3_rank_list_fr), \
        (l1_rank_list_tw, l2_rank_list_tw, l3_rank_list_tw), \
        (l1_rank_list_twr, l2_rank_list_twr, l3_rank_list_twr), timings, valuation = run_daily_ranking(
            tree, provider, rank_date
        )
    # CLI 只打印默认口径(归母-TTM)的 PE/净利润同比列与默认口径(TTM估算值)的股息率列;
    # 其余口径已在 run_daily_ranking 内一次算出(valuation 键 pe_{basis}/growth_{basis}/
    # roe_waa_{basis}/div_yield), 供 Web 下拉切换
    pe_data = valuation[f"pe_{DEFAULT_PROFIT_BASIS}"]
    pb_data = valuation["pb"]
    growth_data = valuation[f"growth_{DEFAULT_PROFIT_BASIS}"]
    roe_data = valuation[f"roe_waa_{DEFAULT_PROFIT_BASIS}"]
    div_data = valuation.get("div_yield") or {}
    div_levels = (div_data.get("value") or {}).get("est", {})  # 默认口径 = TTM估算值
    div_stats = div_data.get("stats") or {}
    pe_free = pe_data["free"]
    pe_total = pe_data["total"]
    pe_stats = pe_data["stats"]
    pb_free = pb_data["free"]
    pb_total = pb_data["total"]
    pb_stats = pb_data["stats"]
    growth_levels = growth_data["value"]
    growth_stats = growth_data["stats"]
    roe_levels = roe_data["value"]
    roe_stats = roe_data["stats"]

    print_timing(
        [
            ("数据准备", [("行业树+成分加载", prep_secs)]),
            ("行情数据", [("行情获取 daily", timings["daily_fetch"])]),
            ("市值数据", [("市值获取 daily_basic", timings["mv_fetch"])]),
            ("财务指标", [("PE/PB 数据获取 fina_indicator_vip+balancesheet_vip+express_vip(三池并行)", timings.get("fina_fetch", 0.0)),
                          ("PE(归母-TTM)聚合计算", timings.get("pe_compute", 0.0)),
                          ("PE(归母-动态)聚合计算", timings.get("pe_dynamic_compute", 0.0)),
                          ("PE(扣非-TTM)聚合计算", timings.get("pe_deduct_compute", 0.0)),
                          ("PE(扣非-动态)聚合计算", timings.get("pe_deduct_dynamic_compute", 0.0)),
                          ("PB 聚合计算", timings.get("pb_compute", 0.0)),
                          ("净利润同比(归母-TTM)聚合计算", timings.get("growth_compute", 0.0)),
                          ("净利润同比(归母-动态)聚合计算", timings.get("growth_dynamic_compute", 0.0)),
                          ("净利润同比(扣非-TTM)聚合计算", timings.get("growth_deduct_compute", 0.0)),
                          ("净利润同比(扣非-动态)聚合计算", timings.get("growth_deduct_dynamic_compute", 0.0)),
                          ("ROE(加权平均, 四口径一次)聚合计算", timings.get("roe_compute", 0.0)),
                          ("股息率(双口径一次)聚合计算", timings.get("div_yield_compute", 0.0))]),
            ("排行计算", [
                ("等权计算(两种口径)", timings["equal_compute"] + timings.get("equal_tr_compute", 0.0)),
                ("停牌市值回退", timings["float_fallback"] + timings.get("total_fallback", 0.0) + timings.get("total_tr_fallback", 0.0)),
                ("加权聚合", max(
                    timings["float_compute"] - timings["float_fallback"], 0.0
                ) + max(timings.get("total_compute", 0.0) - timings.get("total_fallback", 0.0), 0.0)
                + max(timings.get("total_tr_compute", 0.0) - timings.get("total_tr_fallback", 0.0), 0.0)),
            ]),
        ],
        provider.snapshot_api_calls(),
    )

    if pe_stats:
        print(
            f"\nPE(归母-TTM)统计: 报告期 {pe_stats.get('periods', 0)} 期, "
            f"标准式 {pe_stats.get('stocks_standard', 0)} 只, "
            f"不足四期年化 {pe_stats.get('stocks_annualized', 0)} 只, "
            f"无财报 {pe_stats.get('stocks_missing', 0)} 只, "
            f"快报参与 {pe_stats.get('stocks_express', 0)} 只"
        )
    if pb_stats:
        print(
            f"PB 统计: 报告期 {pb_stats.get('periods', 0)} 期, "
            f"有净资产 {pb_stats.get('stocks_with_equity', 0)} 只, "
            f"无净资产 {pb_stats.get('stocks_missing', 0)} 只"
        )
    if roe_stats:
        print(
            f"ROE(加权平均, 归母-TTM)统计: 报告期 {roe_stats.get('periods', 0)} 期, "
            f"有披露值 {roe_stats.get('stocks_with_roe', 0)} 只, "
            f"无数据 {roe_stats.get('stocks_missing', 0)} 只, "
            f"TTM分母三期齐全 {roe_stats.get('stocks_ttm_full', 0)} 只, "
            f"TTM分母兜底 {roe_stats.get('stocks_ttm_fallback', 0)} 只, "
            f"池内无数据 {roe_stats.get('pool_no_value', 0)} 只"
        )
    if growth_stats:
        print(
            f"净利润同比(归母-TTM)统计: 参与 {growth_stats.get('stocks_pair', 0)} 只, "
            f"扭亏 {growth_stats.get('stocks_turnaround', 0)} 只, 转亏 {growth_stats.get('stocks_turnloss', 0)} 只, "
            f"持续亏损 {growth_stats.get('stocks_continued_loss', 0)} 只, "
            f"无基期 {growth_stats.get('stocks_no_base', 0)} 只, "
            f"池内无数据 {growth_stats.get('pool_no_value', 0)} 只"
        )
    if div_stats:
        print(
            f"股息率(TTM估算值)统计: 缓存 {div_stats.get('stocks_total', 0)} 只, "
            f"静态有值 {div_stats.get('stocks_static', 0)} 只(其中零分红 {div_stats.get('stocks_static_zero', 0)} 只、"
            f"7/31 推定 {div_stats.get('stocks_static_fallback', 0)} 只), "
            f"估算有值 {div_stats.get('stocks_est', 0)} 只(其中零分红 {div_stats.get('stocks_est_zero', 0)} 只、"
            f"实绩接管 {div_stats.get('stocks_est_realized', 0)} 只、payout 封顶 {div_stats.get('stocks_est_payout_capped', 0)} 只、"
            f"0利润估算 {div_stats.get('stocks_est_zero_profit', 0)} 只), "
            f"无锚 {div_stats.get('stocks_no_anchor', 0)} 只, 无锚年利润 {div_stats.get('stocks_no_profit', 0)} 只, "
            f"池内无数据 {div_stats.get('pool_no_value', 0)} 只, 无市值/股本 {div_stats.get('pool_no_mv', 0)} 只"
        )

    rank_results = [(), (l1_rank_list_ew, l1_rank_list_ewtr, l1_rank_list_fw, l1_rank_list_fr, l1_rank_list_tw, l1_rank_list_twr), (l2_rank_list_ew, l2_rank_list_ewtr, l2_rank_list_fw, l2_rank_list_fr, l2_rank_list_tw, l2_rank_list_twr), (l3_rank_list_ew, l3_rank_list_ewtr, l3_rank_list_fw, l3_rank_list_fr, l3_rank_list_tw, l3_rank_list_twr)]

    industry_levels = [3, 2, 1]
    for industry_level in industry_levels:
        rank_list_equal_weight, rank_list_equal_weight_tr, rank_list, rank_list_fr, rank_list_tw, rank_list_twr = rank_results[industry_level]
        pe_free_for_level = pe_free.get(str(industry_level), {})
        pe_total_for_level = pe_total.get(str(industry_level), {})
        pb_free_for_level = pb_free.get(str(industry_level), {})
        pb_total_for_level = pb_total.get(str(industry_level), {})
        roe_for_level = roe_levels.get(str(industry_level), {})
        growth_for_level = growth_levels.get(str(industry_level), {})
        div_for_level = div_levels.get(str(industry_level), {})
        print(f"\n\n{rank_date.strftime('%Y-%m-%d')} 申万{industry_level}级行业涨幅榜")
        print(f"总市值加权涨幅(官方价格)|总市值·分红再投资涨幅|自由流通市值加权涨幅(官方价格)|自由流通·分红再投资涨幅|等权涨幅(官方价格)|等权·分红再投资涨幅|PE(自由流通)|PE(总市值)|PB(自由流通)|PB(总市值)|ROE|股息率(TTM估算)|净利润同比|行业名称|成分股数量 成分股列表")
        for index_ts_code, index_pct_chg, stock_count in rank_list:
            index_pct_chg_ew = -100
            for i in rank_list_equal_weight:
                if i[0] == index_ts_code:
                    index_pct_chg_ew = i[1]
            if index_pct_chg_ew == -100:
                raise ValueError(f"没有获取到等权重涨幅数据: index_code={index_ts_code}")
            index_pct_chg_ewtr = next((x[1] for x in rank_list_equal_weight_tr if x[0] == index_ts_code), -100)
            if index_pct_chg_ewtr == -100:
                raise ValueError(f"没有获取到等权·分红再投资涨幅数据: index_code={index_ts_code}")
            index_pct_chg_fr = next((x[1] for x in rank_list_fr if x[0] == index_ts_code), -100)
            if index_pct_chg_fr == -100:
                raise ValueError(f"没有获取到自由流通·分红再投资涨幅数据: index_code={index_ts_code}")
            index_pct_chg_tw = next((x[1] for x in rank_list_tw if x[0] == index_ts_code), -100)
            if index_pct_chg_tw == -100:
                raise ValueError(f"没有获取到总市值加权涨幅数据: index_code={index_ts_code}")
            index_pct_chg_twr = next((x[1] for x in rank_list_twr if x[0] == index_ts_code), -100)
            if index_pct_chg_twr == -100:
                raise ValueError(f"没有获取到总市值·分红再投资涨幅数据: index_code={index_ts_code}")

            # 指标列: 键缺失 -> "—"(无数据/计算失败); PE/PB 值为 None -> "亏损"/"资不抵债";
            # 净利润同比值为 数值%/类别文本("扭亏"/"转亏"/"持续亏损")
            def _fmt_metric(metric_for_level: dict[str, float | None], code: str, none_label: str) -> str:
                if code not in metric_for_level:
                    return "—"
                value = metric_for_level[code]
                if value is None:
                    return none_label
                return f"{value:.2f}"

            def _fmt_roe(metric_for_level: dict[str, float | None], code: str) -> str:
                # ROE: 正数两位小数不带+/不着色, 负值显示"亏损"(排序仍按真实负值), 键缺失"—"
                if code not in metric_for_level:
                    return "—"
                value = metric_for_level[code]
                if value is None:
                    return "—"
                return "亏损" if value < 0 else f"{value:.2f}"

            def _fmt_growth(metric_for_level: dict[str, float | str], code: str) -> str:
                # 净利润同比: 数值分档(≥100% 用 +x.xx倍、否则带符号两位小数%) | 类别文本 | "—"
                if code not in metric_for_level:
                    return "—"
                value = metric_for_level[code]
                if isinstance(value, str):
                    return value
                if value >= 100:
                    return f"+{value / 100:.2f}倍"
                return f"{'+' if value >= 0 else ''}{value:.2f}%"

            def _fmt_div(metric_for_level: dict[str, float], code: str) -> str:
                # 股息率: 两位小数%不带+号, 键缺失"—"; 值 0.00% = 齐备零分红(是事实数值)
                if code not in metric_for_level:
                    return "—"
                return f"{metric_for_level[code]:.2f}%"

            print(f"{'+' if index_pct_chg_tw >= 0 else ''}{index_pct_chg_tw:.2f}%|" +
                  f"{'+' if index_pct_chg_twr >= 0 else ''}{index_pct_chg_twr:.2f}%|" +
                  f"{'+' if index_pct_chg >= 0 else ''}{index_pct_chg:.2f}%|" +
                  f"{'+' if index_pct_chg_fr >= 0 else ''}{index_pct_chg_fr:.2f}%|" +
                  f"{'+' if index_pct_chg_ew >= 0 else ''}{index_pct_chg_ew:.2f}%|" +
                  f"{'+' if index_pct_chg_ewtr >= 0 else ''}{index_pct_chg_ewtr:.2f}%|" +
                  f"{_fmt_metric(pe_free_for_level, index_ts_code, '亏损')}|" +
                  f"{_fmt_metric(pe_total_for_level, index_ts_code, '亏损')}|" +
                  f"{_fmt_metric(pb_free_for_level, index_ts_code, '资不抵债')}|" +
                  f"{_fmt_metric(pb_total_for_level, index_ts_code, '资不抵债')}|" +
                  f"{_fmt_roe(roe_for_level, index_ts_code)}|" +
                  f"{_fmt_div(div_for_level, index_ts_code)}|" +
                  f"{_fmt_growth(growth_for_level, index_ts_code)}|" +
                  f"{tree.index_code_to_node[index_ts_code].industry_name_long}|{stock_count}",
                  [f"{tree.stock_basic[s]['name']}({s})" for s in tree.index_code_to_node[index_ts_code].constituent_stocks])
