import time
import threading
import tushare as ts
from tushare.pro.client import DataApi
from vnpy.trader.setting import SETTINGS
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================== 核心配置 =====================
# INDEX_CODES = ["000906.SH", "000852.SH"]  # 样本池: 中证800 + 中证1000
# INDEX_CODES = ["000906.SH"]  # 样本池: 中证800
INDEX_CODES = ['399300.SZ']  # 样本池: 沪深300

INDEX_DATE = "20260331"  # 样本池成分股日期
REPORT_PERIOD = "20260331"  # 报告期
REPORT_TRADE_DATE = "20260331"  # 报告期最后一个交易日

# 席位关键词-折算比例
KEY_WORD_RATIO = {
    "新华人寿": 1.0,
    "国丰兴华": 0.5
}

MAX_WORKERS = 5  # 并发配置：越大越快越容易被限流，上限20
MAX_REQUESTS_PER_MINUTE = 200 # 配置：每分钟最大请求数
# ====================================================

# 初始化Tushare接口
token: str = SETTINGS["datafeed.password"]
if not token:
    raise ValueError("请先在 vnpy 的 datafeed.password 配置中设置你的 tushare token")

pro: DataApi = ts.pro_api(token=token)

# ===================== 限流全局变量（线程安全） =====================
request_timestamps = []  # 记录所有请求的时间戳
rate_limit_lock = threading.Lock()  # 线程锁，防止并发计数错误


def rate_limit_control():
    """限流控制函数：确保每分钟请求数不超过 MAX_REQUESTS_PER_MINUTE"""
    global request_timestamps
    current_time = time.time()

    with rate_limit_lock:
        # 过滤掉1分钟之前的历史请求时间戳
        request_timestamps = [t for t in request_timestamps if current_time - t < 60]

        # 如果达到最大请求数，等待直到窗口重置
        while len(request_timestamps) >= MAX_REQUESTS_PER_MINUTE:
            # 计算需要等待的时间
            wait_seconds = 60 - (current_time - request_timestamps[0]) + 0.1
            print(f"⚠️  已达每分钟最大请求次数({MAX_REQUESTS_PER_MINUTE})，等待 {wait_seconds:.1f} 秒后继续...")
            time.sleep(wait_seconds)

            # 重新刷新时间，过滤过期请求
            current_time = time.time()
            request_timestamps = [t for t in request_timestamps if current_time - t < 60]

        # 记录当前请求的时间戳
        request_timestamps.append(current_time)


# ===================== 原业务函数（仅新增限流调用） =====================
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
            trade_date=REPORT_TRADE_DATE
        )
        return df.set_index("ts_code")["close"].to_dict()
    except Exception as e:
        print(f"错误：查询股价失败, {str(e)}")
        return {}


def query_single_stock(ts_code: str, stock_name: str):
    """
    单个股票的股东数据查询（并发最小单元）
    输入：股票代码、名称
    输出：匹配关键词的结果列表（空则无匹配）
    """
    rate_limit_control()
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
    except Exception as e:
        print(f"查询 {ts_code} 十大股东失败：{str(e)}")
        return []


def query_xinhua_combined():
    """主查询函数"""
    stock_map = get_combined_stocks()
    if not stock_map:
        return

    total = len(stock_map)
    keyword_str = ", ".join(KEY_WORD_RATIO.keys())
    index_str = ", ".join(INDEX_CODES)
    print(f"开始查询指数【{index_str}】共 {total} 只股票，筛选2026年一季报包含「{keyword_str}」的持股...\n")

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
            if completed_count % 50 == 0:
                print(f"查询进度：{completed_count}/{total}")

            # 合并匹配结果
            stock_result = future.result()
            if stock_result:
                match_results.extend(stock_result)
    # ========================================================================

    # 批量查股价 + 计算金额
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

    print("\n" + "=" * 150)
    print(f"指数【{index_str}】查询完成！共找到 {len(match_results)} 个匹配席位")
    print("=" * 150)

    if not match_results:
        print(f"未查询到包含「{keyword_str}」的股东数据")
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


# 执行查询
if __name__ == "__main__":
    start_time = time.time()
    query_xinhua_combined()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")