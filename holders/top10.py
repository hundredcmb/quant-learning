import os
import time
import json
import threading
import tushare as ts
from tushare.pro.client import DataApi
from vnpy.trader.setting import SETTINGS
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================== 核心配置 =====================
INDEX_CODES = ["000906.SH", "000852.SH"]  # 样本池: 中证800 + 中证1000
# INDEX_CODES = ["000906.SH"]  # 样本池: 中证800
# INDEX_CODES = ['399300.SZ']  # 样本池: 沪深300

INDEX_DATE = "20260331"  # 样本池成分股日期
REPORT_PERIOD = "20260331"  # 报告期（缓存唯一标识）
REPORT_TRADE_DATE = "20260331"  # 报告期最后一个交易日

# 席位关键词-折算比例
KEY_WORD_RATIO = {
    # =========T0国家队=========
    "中央汇金投资": 0.0,
    "中央汇金资产": 1.0,
    "中国证券金融": 1.0,
    "中国国新": 1.0,
    "中国诚通": 1.0,
    "中国信达资产": 1.0,
    "中国东方资产": 1.0,
    "中国长城资产": 1.0,

    # =========T0社保基金=========
    # "社保基金": 1.0,
    # "社会保障基金": 1.0,

    # =========T1平安险资=========
    # "恒毅持盈": 1.0,
    # "平安资管": 1.0,
    # "平安人寿保险": 1.0,
    # "平安养老保险": 1.0,
    # "中国平安保险(集团)股份有限公司-": 1.0,

    # =========T1国寿险资=========
    # "国丰兴华": 0.5,
    # "中国人寿保险股份": 1.0,
    # "中国人寿保险(集团)公司-": 1.0,

    # =========T2新华险资=========
    # "国丰兴华": 0.5,
    # "新华人寿保险": 1.0,

    # =========T2太保险资=========
    # "太保致远": 1.0,
    # "太平洋人寿保险": 1.0,
    # "太平洋财产保险": 1.0,

    # =========T2人保险资=========
    # "启元惠众": 1.0,
    # "人民财产保险": 1.0,
    # "人民人寿保险": 1.0,
}

MAX_WORKERS = 5  # 并发数, 越大越快越容易被限流, 上限20
MAX_REQUESTS_PER_MINUTE = 180  # 每分钟最大请求数(推荐设为tushare官方限制数减20)
# 缓存文件：存储【Tushare原始接口数据】
CACHE_FILE = "tushare_top10_holders_raw.json"
# ====================================================

# 初始化Tushare接口
token: str = SETTINGS["datafeed.password"]
if not token:
    raise ValueError("请先在 vnpy 的 datafeed.password 配置中设置你的 tushare token")

pro: DataApi = ts.pro_api(token=token)
request_timestamps = []
rate_limit_lock = threading.Lock()
write_cache_lock = threading.Lock()


def load_raw_cache() -> dict:
    """加载缓存：{报告期: {股票代码: [原始接口数据列表]}}"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 原始缓存加载失败：{str(e)}")
            return {}
    return {}


def save_raw_cache(cache_data: dict) -> None:
    """保存原始接口数据到缓存文件"""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 原始缓存保存失败：{str(e)}")


# 全局缓存对象（程序运行时常驻内存）
RAW_CACHE = load_raw_cache()


# =================================================================

def get_stock_top10_raw(ts_code: str, period: str) -> list:
    """
    【核心底层函数】
    获取单股票 原始十大股东数据（优先缓存，无则请求tushare）
    返回：Tushare原始数据列表（空列表=无数据）
    缓存：仅存储接口原始结果，无任何业务处理
    """
    # 1. 优先读缓存
    if period in RAW_CACHE and ts_code in RAW_CACHE[period]:
        return RAW_CACHE[period][ts_code]

    # 2. 无缓存，执行限流 + 接口请求
    rate_limit_control()
    raw_data = []
    try:
        df = pro.top10_holders(
            ts_code=ts_code,
            period=period,
            fields="ts_code,holder_name,hold_amount,hold_ratio"
        )
        # 转换为原始字典列表（标准可序列化格式）
        raw_data = df.to_dict("records") if not df.empty else []
    except Exception as e:
        print(f"⚠️  大股东查询接口请求失败 {ts_code}, 请修改限流或并发参数后重试：{str(e)}")
        with write_cache_lock:
            save_raw_cache(RAW_CACHE)
            os._exit(-1)

    # 3. 写入内存缓存
    if period not in RAW_CACHE:
        RAW_CACHE[period] = {}
    RAW_CACHE[period][ts_code] = raw_data

    return raw_data


def rate_limit_control():
    """限流控制函数"""
    global request_timestamps
    current_time = time.time()

    with rate_limit_lock:
        request_timestamps = [t for t in request_timestamps if current_time - t < 60]

        while len(request_timestamps) >= MAX_REQUESTS_PER_MINUTE:
            wait_seconds = 60 - (current_time - request_timestamps[0]) + 1
            print(f"⚠️  已达每分钟最大请求次数({MAX_REQUESTS_PER_MINUTE})，等待 {wait_seconds:.1f} 秒后继续...")
            time.sleep(wait_seconds)
            current_time = time.time()
            request_timestamps = [t for t in request_timestamps if current_time - t < 60]

        request_timestamps.append(current_time)


def get_index_stocks(index_code: str) -> dict:
    """获取单指数成分股"""
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
        print(f"⚠️  获取 {index_code} 成分股失败：{str(e)}")
        os._exit(-1)


def get_combined_stocks() -> dict:
    """合并指数成分股"""
    combined_map = {}
    for index_code in INDEX_CODES:
        index_stocks = get_index_stocks(index_code)
        combined_map.update(index_stocks)
    return combined_map


def get_stock_close_price(stock_codes: list) -> dict:
    """批量查询收盘价"""
    if not stock_codes:
        return {}
    try:
        df = pro.daily(
            ts_code=",".join(stock_codes),
            trade_date=REPORT_TRADE_DATE
        )
        return df.set_index("ts_code")["close"].to_dict()
    except Exception as e:
        print(f"⚠️  查询股价失败: {str(e)}")
        os._exit(-1)


def query_single_stock(ts_code: str, stock_name: str):
    """
    单个股票业务处理：从【原始缓存/接口】获取数据 → 筛选关键词
    职责：纯业务处理，不关心数据来自缓存还是接口
    """
    # 核心修改：调用底层函数获取原始数据
    raw_holders = get_stock_top10_raw(ts_code, REPORT_PERIOD)
    match_list = []

    for row in raw_holders:
        holder_name = row["holder_name"]
        match_ratio = None
        # 业务筛选逻辑
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


def query_xinhua_combined():
    """主查询函数"""
    stock_map = get_combined_stocks()
    if not stock_map:
        return

    total = len(stock_map)
    keyword_str = ", ".join(KEY_WORD_RATIO.keys())
    index_str = ", ".join(INDEX_CODES)
    print(f"开始查询指数【{index_str}】共 {total} 只股票，筛选{REPORT_PERIOD}报告期包含「{keyword_str}」的持股...\n")

    stock_list = list(stock_map.items())
    match_results = []
    completed_count = 0

    # 线程池并发：底层自动处理缓存，上层无感知
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        task_futures = [executor.submit(query_single_stock, code, name) for code, name in stock_list]

        for future in as_completed(task_futures):
            completed_count += 1
            if completed_count % 50 == 0:
                print(f"查询进度：{completed_count}/{total}")

            stock_result = future.result()
            if stock_result:
                match_results.extend(stock_result)

    # 股价计算（原有逻辑）
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
    cached_count = len(RAW_CACHE.get(REPORT_PERIOD, {}))


if __name__ == "__main__":
    start_time = time.time()
    query_xinhua_combined()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
