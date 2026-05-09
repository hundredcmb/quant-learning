import time
import tushare as ts
from tushare.pro.client import DataApi
from vnpy.trader.setting import SETTINGS
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================== 核心配置 =====================
#INDEX_CODES = ["000906.SH", "000852.SH"]  # 样本池: 中证800 + 中证1000
#INDEX_CODES = ["000906.SH"]     # 样本池: 中证800
INDEX_CODES = ['399300.SZ'] # 样本池: 沪深300

INDEX_DATE = "20260331"     # 样本池成分股日期
REPORT_PERIOD = "20260331"  # 报告期
PRICE_DATE = "20260331"     # 持仓金额统计日期
MAX_WORKERS = 3             # 并发配置：越大越快越容易被限流，上限20

# 席位关键词-折算比例
KEY_WORD_RATIO = {
    "新华人寿": 1.0,
    "国丰兴华": 0.5
}
# ====================================================

# 初始化Tushare接口
token: str = SETTINGS["datafeed.password"]
if not token:
    raise ValueError("请先在 vnpy 的 datafeed.password 配置中设置你的 tushare token")

pro: DataApi = ts.pro_api(token=token)


def get_index_stocks(index_code: str) -> dict:
    """通用函数：获取单指数成分股代码+名称映射"""
    try:
        df = pro.index_weight(
            index_code=index_code,
            start_date=INDEX_DATE,
            end_date=INDEX_DATE
        )
        if df.empty:
            print(f"未获取到 {index_code} 成分股数据")
            return {}

        stock_basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
        name_map = stock_basic.set_index("ts_code")["name"].to_dict()

        stock_map = {}
        for _, row in df.iterrows():
            con_code = row["con_code"]
            if con_code in name_map:
                stock_map[con_code] = name_map[con_code]
        return stock_map

    except Exception as e:
        print(f"获取 {index_code} 成分股失败：{str(e)}")
        return {}


def get_combined_stocks() -> dict:
    """循环遍历指数列表，合并所有成分股，自动去重"""
    combined_map = {}
    for index_code in INDEX_CODES:
        index_stocks = get_index_stocks(index_code)
        combined_map.update(index_stocks)
    return combined_map


def get_stock_close_price(stock_codes: list) -> dict:
    """批量查询一季度末收盘价"""
    if not stock_codes:
        return {}
    try:
        df = pro.daily(
            ts_code=",".join(stock_codes),
            trade_date=PRICE_DATE
        )
        return df.set_index("ts_code")["close"].to_dict()
    except Exception as e:
        print(f"查询股价失败：{str(e)}")
        return {}


# ===================== 新增：并发查询单元函数 =====================
def query_single_stock(ts_code: str, stock_name: str):
    """
    单个股票的股东数据查询（并发最小单元）
    输入：股票代码、名称
    输出：匹配关键词的结果列表（空则无匹配）
    """
    try:
        df = pro.top10_holders(
            ts_code=ts_code,
            period=REPORT_PERIOD,
            fields="ts_code,holder_name,hold_amount,hold_ratio"
        )
        match_list = []
        if not df.empty:
            for _, row in df.iterrows():
                holder_name = row["holder_name"]
                match_ratio = None
                # 匹配关键词
                for keyword, ratio in KEY_WORD_RATIO.items():
                    if keyword in holder_name:
                        match_ratio = ratio
                        break
                if match_ratio is not None:
                    match_list.append({
                        "ts_code": ts_code,
                        "stock_name": stock_name,
                        "holder_name": holder_name,
                        "hold_amount": int(row["hold_amount"]),
                        "hold_ratio": round(row["hold_ratio"], 2),
                        "ratio": match_ratio
                    })
        return match_list
    except Exception:
        # 保持原代码容错：异常直接返回空列表
        return []


# ================================================================


def query_xinhua_combined():
    """主查询函数"""
    stock_map = get_combined_stocks()
    if not stock_map:
        return

    total = len(stock_map)
    keyword_str = "、".join(KEY_WORD_RATIO.keys())
    index_str = "、".join(INDEX_CODES)
    print(f"开始查询指数【{index_str}】共 {total} 只股票，筛选2026年一季报包含「{keyword_str}」的持股...\n")

    # ===================== 核心改造：并发查询替换串行循环 =====================
    stock_list = list(stock_map.items())
    match_results = []
    completed_count = 0

    # 线程池并发执行所有股票查询
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 提交所有任务到线程池
        task_futures = [executor.submit(query_single_stock, code, name) for code, name in stock_list]

        # 遍历完成的任务，收集结果
        for future in as_completed(task_futures):
            completed_count += 1
            # 保持原进度打印逻辑
            if completed_count % 100 == 0:
                print(f"查询进度：{completed_count}/{total}")

            # 合并匹配结果
            stock_result = future.result()
            if stock_result:
                match_results.extend(stock_result)
    # ========================================================================

    # 批量查股价 + 计算金额（原逻辑完全不变）
    if match_results:
        match_stock_codes = [item["ts_code"] for item in match_results]
        price_map = get_stock_close_price(match_stock_codes)

        for item in match_results:
            close = price_map.get(item["ts_code"], 0)
            hold_amount_val = item["hold_amount"]
            ratio = item["ratio"]

            original_val = round(hold_amount_val * close / 100000000, 2) if close > 0 else 0
            adjust_val = round(original_val * ratio, 2)

            item["original_value"] = original_val
            item["adjust_value"] = adjust_val

    # ===================== 打印输出（完全不变） =====================
    print("\n" + "=" * 150)
    print(f"指数【{index_str}】查询完成！共找到 {len(match_results)} 个匹配席位")
    print("=" * 150)

    if not match_results:
        print(f"未查询到包含「{keyword_str}」的股东数据")
        return

    total_adjust_value = round(sum(item["adjust_value"] for item in match_results), 2)

    col_formats = {
        "ts_code": "<10",
        "stock_name": "<8",
        "hold_amount": "<20",
        "hold_ratio": "<10",
        "original_value": "<12",
        "adjust_value": "<12",
        "holder_name": "<35"
    }

    print(
        f"{'代码':{col_formats['ts_code']}} "
        f"{'名称':{col_formats['stock_name']}} "
        f"{'股数':{col_formats['hold_amount']}} "
        f"{'比例(%)':{col_formats['hold_ratio']}} "
        f"{'金额(亿)':{col_formats['original_value']}} "
        f"{'折算后(亿)':{col_formats['adjust_value']}} "
        f"{'席位':{col_formats['holder_name']}}"
    )
    print("-" * 150)

    for item in match_results:
        print(
            f"{item['ts_code']:{col_formats['ts_code']}} "
            f"{item['stock_name']:{col_formats['stock_name']}} "
            f"{item['hold_amount']:{col_formats['hold_amount']}} "
            f"{item['hold_ratio']:{col_formats['hold_ratio']}} "
            f"{item['original_value']:{col_formats['original_value']}} "
            f"{item['adjust_value']:{col_formats['adjust_value']}} "
            f"{item['holder_name']:{col_formats['holder_name']}}"
        )

    print("-" * 150)
    total_text = f"【折算后总金额(亿)】{total_adjust_value}"
    print(
        f"{'':{col_formats['ts_code']}} "
        f"{'':{col_formats['stock_name']}} "
        f"{'':{col_formats['hold_amount']}} "
        f"{'':{col_formats['hold_ratio']}} "
        f"{'':{col_formats['original_value']}} "
        f"{total_text:{col_formats['adjust_value']}} "
        f"{'':{col_formats['holder_name']}}"
    )


# 执行查询
if __name__ == "__main__":
    start_time = time.time()
    query_xinhua_combined()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")