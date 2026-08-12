"""十大股东脚本公共模块：Tushare 客户端、原始数据缓存与限流。

四个 holders 脚本共享的数据获取逻辑统一放在这里：
- Tushare token 从 vnpy 全局配置动态获取（SETTINGS["datafeed.password"]）
- 原始数据缓存：holders/tushare_top10_holders_raw.json（随仓库提交，请勿删除）
- 限流控制、指数成分股、收盘价查询、并发查询
- 单报告期关键词筛选（query_single_stock）

各脚本只保留自己的业务配置（指数/报告期/关键词/输出文件名）与业务逻辑。
"""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import tushare as ts
from tushare.pro.client import DataApi
from vnpy.trader.setting import SETTINGS

# Windows 控制台编码兼容：避免 GBK 下 emoji 打印崩溃
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ===================== 公共配置 =====================
# 缓存文件保持在脚本目录 holders/ 下，随仓库提交（全量重拉受限流影响很慢）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "tushare_top10_holders_raw.json")

# 图片等运行产物统一输出到仓库根目录 output/（已在 .gitignore 中忽略）
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

MAX_WORKERS = 5  # 并发数, 越大越快越容易被限流, 上限20
MAX_REQUESTS_PER_MINUTE = 180  # 每分钟最大请求数(推荐设为tushare官方限制数减20)

# 席位关键词-折算比例（四个脚本共用）
# 启用或停用关键词通过注释切换；如需某个脚本单独使用不同关键词，
# 可在该脚本 import 后重新定义 KEY_WORD_RATIO 覆盖
KEY_WORD_RATIO = {
    # =========T0国家队=========
    # "中央汇金投资": 0.0,
    # "中央汇金资产": 1.0,
    # "中国证券金融": 1.0,
    # "中证金融资产": 1.0,
    # "中国国新": 1.0,
    # "中国诚通": 1.0,
    # "中国信达资产": 1.0,
    # "中国东方资产": 1.0,
    # "中国长城资产": 1.0,

    # =========T0社保基金=========
    # "社保基金": 1.0,
    # "社会保障基金": 1.0,

    # =========T1平安险资=========
    # "恒毅持盈": 1.0,
    # "平安资管": 1.0,
    # "平安人寿保险": 1.0,
    # "平安养老保险": 1.0,
    # "中国平安保险(集团)股份有限公司-": 0.0,

    # =========T1国寿险资=========
    # "国丰兴华": 0.5,
    # "中国人寿保险股份": 1.0,
    # "中国人寿保险(集团)公司-": 1.0,

    # =========T2新华险资=========
    "国丰兴华": 0.5,
    "新华资管": 1.0,
    "新华养老": 1.0,
    "新华人寿保险股份有限公司-分红": 1.0,
    "新华人寿保险股份有限公司-传统": 1.0,
    "新华人寿保险股份有限公司-自有资金": 1.0,

    # =========T2太保险资=========
    # "太保致远": 1.0,
    # "太平洋人寿保险": 1.0,
    # "太平洋财产保险": 1.0,

    # =========T2人保险资=========
    # "启元惠众": 1.0,
    # "人民财产保险": 1.0,
    # "人民人寿保险": 1.0,
}
# ====================================================

# ===================== 初始化Tushare接口 =====================
token: str = SETTINGS["datafeed.password"]
if not token:
    raise ValueError("请先在 vnpy 的 datafeed.password 配置中设置你的 tushare token")

pro: DataApi = ts.pro_api(token=token)
request_timestamps: list[float] = []
rate_limit_lock = threading.Lock()
write_cache_lock = threading.Lock()
# ====================================================


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
RAW_CACHE: dict = load_raw_cache()


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


def get_index_stocks(index_code: str, index_date: str) -> dict:
    """获取单指数成分股"""
    try:
        df = pro.index_weight(
            index_code=index_code,
            start_date=index_date,
            end_date=index_date
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


def get_combined_stocks(index_codes: list, index_date: str) -> dict:
    """合并指数成分股"""
    combined_map = {}
    for index_code in index_codes:
        index_stocks = get_index_stocks(index_code, index_date)
        combined_map.update(index_stocks)
    return combined_map


def get_stock_close_price(stock_codes: list, trade_date: str) -> dict:
    """批量查询指定交易日的收盘价"""
    if not stock_codes:
        return {}
    try:
        df = pro.daily(
            ts_code=",".join(stock_codes),
            trade_date=trade_date
        )
        return df.set_index("ts_code")["close"].to_dict()
    except Exception as e:
        print(f"⚠️  查询{trade_date}股价失败: {str(e)}")
        os._exit(-1)


def get_adj_factors(trade_date: str) -> dict:
    """批量查询指定交易日全市场股票复权因子：{ts_code: adj_factor}

    - 按 trade_date 一次请求拉全市场（adj_factor），不建缓存
    - 复权因子随送转/分红变化，用于把不同交易日价格修正到同一系数水平
    - 非交易日/无数据返回空 dict；请求失败时退出（与股价查询一致）
    """
    try:
        df = pro.adj_factor(trade_date=trade_date)
        if df is None or df.empty:
            return {}
        return dict(zip(df["ts_code"], df["adj_factor"]))
    except Exception as e:
        print(f"⚠️  查询{trade_date}复权因子失败: {str(e)}")
        os._exit(-1)


def query_single_stock(
    ts_code: str,
    stock_name: str,
    period: str,
    key_word_ratio: dict,
) -> list:
    """
    单个股票单报告期业务处理：从【原始缓存/接口】获取数据 → 筛选关键词
    职责：纯业务处理，不关心数据来自缓存还是接口
    """
    raw_holders = get_stock_top10_raw(ts_code, period)
    match_list = []

    for row in raw_holders:
        holder_name = row["holder_name"]
        match_ratio = None
        # 业务筛选逻辑
        for keyword, ratio in key_word_ratio.items():
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


def run_parallel_queries(stock_map: dict, query_fn, max_workers: int = MAX_WORKERS) -> list:
    """线程池并发查询：返回所有非空结果的合并列表"""
    stock_list = list(stock_map.items())
    total = len(stock_list)
    match_results = []
    completed_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        task_futures = [executor.submit(query_fn, code, name) for code, name in stock_list]

        for future in as_completed(task_futures):
            completed_count += 1
            if completed_count % 50 == 0:
                print(f"查询进度：{completed_count}/{total}")

            stock_result = future.result()
            if stock_result:
                match_results.extend(stock_result)

    return match_results
