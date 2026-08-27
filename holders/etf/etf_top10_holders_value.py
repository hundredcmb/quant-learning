import argparse
import time

from etf_client import (
    KEY_WORD_RATIO,
    get_combined_etfs,
    get_daily_prices,
    init_tushare,
    query_single_etf,
)

# ===================== 核心配置 =====================
REPORT_PERIOD = "20251231"  # 报告期（半年报 0630 / 年报 1231）
REPORT_TRADE_DATE = "20251231"  # 用于市值计算的交易日（fund_daily 拉全市场收盘价）
# ====================================================


def query_top10():
    """主查询函数：单报告期关键词筛选 + 份额/市值统计"""
    etf_map = get_combined_etfs()
    if not etf_map:
        print("❌ ETF 基础信息缓存为空，请先运行 import_etf_data.py 导入数据")
        return

    total = len(etf_map)
    keyword_str = ", ".join(KEY_WORD_RATIO.keys())
    print(f"开始分析 {total} 只 ETF，筛选{REPORT_PERIOD}报告期包含「{keyword_str}」的持有人...\n")

    match_results = []
    for code, name in etf_map.items():
        result = query_single_etf(code, name, REPORT_PERIOD, KEY_WORD_RATIO)
        if result:
            match_results.extend(result)

    # 按交易日拉全市场收盘价（1 次请求，无缓存）
    print(f"拉取 {REPORT_TRADE_DATE} 全市场 ETF 收盘价...")
    price_map = get_daily_prices(REPORT_TRADE_DATE)
    has_price = bool(price_map)
    if not has_price:
        print(f"⚠️ {REPORT_TRADE_DATE} 无行情数据（可能非交易日），市值按 0 处理")

    for item in match_results:
        # 折算份额 = 份额 × 席位折算比例（不依赖价格）
        item["adjust_amount"] = round(item["hold_amount"] * item["ratio"], 2)
        # 市值（亿）= 份额 × 收盘价 / 1e8
        close = price_map.get(item["ts_code"], 0)
        original_value = round(item["hold_amount"] * close / 1e8, 2) if close > 0 else 0
        item["original_value"] = original_value
        item["adjust_value"] = round(original_value * item["ratio"], 2)

    # 输出结果
    print("\n" + "=" * 160)
    print(f"【{REPORT_PERIOD}】报告期查询完成！共找到 {len(match_results)} 个匹配持有人")
    print("=" * 160)

    if not match_results:
        print(f"未查询到包含「{keyword_str}」的持有人数据（可调整 etf_client.KEY_WORD_RATIO）")
        return

    header = (f"{'代码':<10} {'ETF名称':<12} {'排名':<5} {'份额(份)':<15} "
              f"{'折算份额(份)':<14} {'比例(%)':<8} {'市值(亿)':<10} {'折算市值(亿)':<10} {'持有人名称':<32}")
    print(header)
    print("-" * len(header))

    total_adjust_amount = 0
    total_adjust_value = 0
    for item in match_results:
        total_adjust_amount += item["adjust_amount"]
        total_adjust_value += item["adjust_value"]
        print(f"{item['ts_code']:<12} "
              f"{item['etf_name']:<12} "
              f"{item['rank']:<7} "
              f"{item['hold_amount']:<16} "
              f"{item['adjust_amount']:<15} "
              f"{item['hold_ratio']:<10.2f} "
              f"{item['original_value']:<12} "
              f"{item['adjust_value']:<13} "
              f"{item['holder_name']:<32}")

    print("-" * len(header))
    print(f"【折算后总份额(份)】{round(total_adjust_amount, 2)}")
    print(f"【折算后总市值(亿)】{round(total_adjust_value, 2)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="单报告期 ETF 十大持有人席位关键词持仓市值统计")
    parser.add_argument("--token", default=None,
                        help="Tushare token（未保存过配置且非交互环境时用此参数指定，传入后自动保存）")
    args = parser.parse_args()
    init_tushare(args.token)

    start_time = time.time()
    query_top10()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
