import argparse
import time

from tushare_client import (
    KEY_WORD_RATIO,
    RAW_CACHE,
    get_combined_stocks,
    get_stock_close_price,
    init_tushare,
    query_single_stock,
    run_parallel_queries,
    save_raw_cache,
)

# ===================== 核心配置 =====================
INDEX_CODES = ["000906.SH", "000852.SH"]  # 样本池: 中证800 + 中证1000
# INDEX_CODES = ["000906.SH"]  # 样本池: 中证800
# INDEX_CODES = ['399300.SZ']  # 样本池: 沪深300

INDEX_DATE = "20260331"  # 样本池成分股日期
REPORT_PERIOD = "20260331"  # 报告期（缓存唯一标识）
REPORT_TRADE_DATE = "20260331"  # 报告期最后一个交易日

# 席位关键词-折算比例（公共配置见 tushare_client.KEY_WORD_RATIO；
# 如需本脚本单独调整，可在此处重新定义 KEY_WORD_RATIO 覆盖）
# ====================================================


def query_top10():
    """主查询函数"""
    stock_map = get_combined_stocks(INDEX_CODES, INDEX_DATE)
    if not stock_map:
        return

    total = len(stock_map)
    keyword_str = ", ".join(KEY_WORD_RATIO.keys())
    index_str = ", ".join(INDEX_CODES)
    print(f"开始查询指数【{index_str}】共 {total} 只股票，筛选{REPORT_PERIOD}报告期包含「{keyword_str}」的持股...\n")

    match_results = run_parallel_queries(
        stock_map,
        lambda code, name: query_single_stock(code, name, REPORT_PERIOD, KEY_WORD_RATIO),
    )

    # 股价计算
    if match_results:
        match_stock_codes = [item["ts_code"] for item in match_results]
        price_map = get_stock_close_price(match_stock_codes, REPORT_TRADE_DATE)

        for item in match_results:
            close = price_map.get(item["ts_code"], 0)
            hold_amount_val = item["hold_amount"]
            ratio = item["ratio"]

            original_val = round(hold_amount_val * close / 100000000, 2) if close > 0 else 0
            adjust_val = round(original_val * ratio, 2)

            item["original_value"] = original_val
            item["adjust_value"] = adjust_val

    # 输出结果
    print("\n" + "=" * 150)
    print(f"【{REPORT_PERIOD}】报告期查询完成！共找到 {len(match_results)} 个匹配席位")
    print("=" * 150)

    if not match_results:
        print(f"未查询到包含「{keyword_str}」的股东数据")
        save_raw_cache(RAW_CACHE)
        return

    total_adjust_value = round(sum(item["adjust_value"] for item in match_results), 2)

    print(f"{'股票代码':<7} {'股票名称':<8} {'持股数量(股)':<15} {'持股比例(%)':<6} "
          f"{'原始持仓(亿)':<8} {'折算持仓(亿)':<8} {'股东名称':<32}")
    print("-" * 150)
    for item in match_results:
        print(f"{item['ts_code']:<10} "
              f"{item['stock_name']:<8} "
              f"{item['hold_amount']:<20} "
              f"{item['hold_ratio']:<10} "
              f"{item['original_value']:<12} "
              f"{item['adjust_value']:<12} "
              f"{item['holder_name']:<32}")
    print("-" * 150)
    print(f"【折算后总持仓(亿)】{total_adjust_value}")

    # 最终：将所有缓存的原始数据持久化到文件
    save_raw_cache(RAW_CACHE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="单报告期十大股东席位关键词持仓市值统计")
    parser.add_argument("--token", default=None,
                        help="Tushare token（已保存配置时可省略；传入后自动保存供未来使用）")
    args = parser.parse_args()
    init_tushare(args.token)

    start_time = time.time()
    query_top10()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
