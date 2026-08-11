import os
import sys
import time
import json
import threading
import tushare as ts
from tushare.pro.client import DataApi
from vnpy.trader.setting import SETTINGS
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont

# ===================== 核心配置 =====================
INDEX_CODES = ["000906.SH", "000852.SH"]  # 样本池: 中证800 + 中证1000
# INDEX_CODES = ["000906.SH"]  # 样本池: 中证800
# INDEX_CODES = ['399300.SZ']  # 样本池: 沪深300

INDEX_DATE = "20260331"  # 样本池成分股日期
REPORT_PERIOD = "20260331"  # 报告期（缓存唯一标识）
REPORT_TRADE_DATE = "20260331"  # 日1：原报告期交易日
NEW_TRADE_DATE = "20260529"  # 日2：对比的新交易日
# REPORT_TRADE_DATE = "20251231"  # 日1：原报告期交易日
# NEW_TRADE_DATE = "20260331"  # 日2：对比的新交易日
# ============================================================

# 席位关键词-折算比例
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

MAX_WORKERS = 5  # 并发数, 越大越快越容易被限流, 上限20
MAX_REQUESTS_PER_MINUTE = 180  # 每分钟最大请求数(推荐设为tushare官方限制数减20)
# 输出目录：生成的图片统一输出到仓库根目录的 output/（已在 .gitignore 中忽略）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(os.path.dirname(BASE_DIR), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# 缓存文件：存储【Tushare原始接口数据】（保持在脚本目录 holders/ 下，随仓库提交，请勿删除）
CACHE_FILE = os.path.join(BASE_DIR, "tushare_top10_holders_raw.json")
# 输出图片文件名
OUTPUT_IMAGE_FILE = os.path.join(OUTPUT_DIR, f"股票组合收益统计_{REPORT_TRADE_DATE}_to_{NEW_TRADE_DATE}.png")
# 新增：汇总版图片文件名（不带表格数据）
OUTPUT_SUMMARY_IMAGE_FILE = os.path.join(OUTPUT_DIR, f"股票组合收益统计_{REPORT_TRADE_DATE}_to_{NEW_TRADE_DATE}_汇总版.png")
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


def get_stock_close_price(stock_codes: list, trade_date: str) -> dict:
    """【修改】批量查询指定交易日的收盘价"""
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


def generate_table_image(match_results, total_adjust_value, total_adjust_value_new, total_diff, return_rate):
    """生成与命令行一致的股票组合收益UI表格图片，标题含关键词+折算比例"""
    # 基础样式配置
    PADDING = 10
    ROW_HEIGHT = 30
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16
    # 适配当前数据的列宽
    COL_WIDTHS = [100, 80, 140, 100, 120, 120, 120, 400]
    COL_NAMES = [
        "股票代码", "股票名称", "持股数量(股)", "持股比例(%)",
        "日1原始持仓(亿)", "日1折算持仓(亿)", "日2折算持仓(亿)", "股东名称"
    ]

    # 拼接关键词+折算比例文本
    ratio_text = ", ".join([f"{k}({v})" for k, v in KEY_WORD_RATIO.items()])

    # 计算画布尺寸（标题+关键词说明+表头+数据行+分隔线+4行汇总信息）
    total_rows = len(match_results) + 6
    img_width = sum(COL_WIDTHS) + 2 * PADDING
    img_height = total_rows * ROW_HEIGHT + 2 * PADDING + ROW_HEIGHT * 2

    # 创建白色背景画布
    img = Image.new("RGB", (img_width, img_height), "white")
    draw = ImageDraw.Draw(img)

    # 跨系统字体兼容
    try:
        if sys.platform.startswith("win"):
            font = ImageFont.truetype("msyh.ttc", FONT_SIZE)
            header_font = ImageFont.truetype("msyh.ttc", HEADER_FONT_SIZE)
        elif sys.platform.startswith("darwin"):
            font = ImageFont.truetype("Arial Unicode.ttf", FONT_SIZE)
            header_font = ImageFont.truetype("Arial Unicode.ttf", HEADER_FONT_SIZE)
        else:
            font = ImageFont.truetype("DejaVuSans.ttf", FONT_SIZE)
            header_font = ImageFont.truetype("DejaVuSans.ttf", HEADER_FONT_SIZE)
    except:
        # 兜底默认字体
        font = ImageFont.load_default()
        header_font = ImageFont.load_default()

    # 第一行大标题
    x, y = PADDING, PADDING
    title_main = f"{REPORT_TRADE_DATE} 到 {NEW_TRADE_DATE} 的股票组合收益统计"
    draw.text((x, y), title_main, font=header_font, fill="#2c3e50")
    y += ROW_HEIGHT

    # 第二行：关键词+折算比例说明
    title_sub = f"筛选关键词及折算比例：{ratio_text}"
    draw.text((x, y), title_sub, font=font, fill="#8e44ad")
    y += ROW_HEIGHT

    # 绘制表头
    x = PADDING
    for i, name in enumerate(COL_NAMES):
        draw.text((x + 5, y + 5), name, font=header_font, fill="#3498db")
        x += COL_WIDTHS[i]
    y += ROW_HEIGHT

    # 绘制分隔线
    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 8

    # 绘制数据行
    for item in match_results:
        x = PADDING
        row_data = [
            item['ts_code'],
            item['stock_name'],
            f"{item['hold_amount']:,}",  # 千分位格式化
            f"{item['hold_ratio']:.2f}",
            f"{item['original_value']:.2f}",
            f"{item['adjust_value']:.2f}",
            f"{item['adjust_value_new']:.2f}",
            item['holder_name']
        ]
        for i, data in enumerate(row_data):
            draw.text((x + 5, y + 5), str(data), font=font, fill="#2c3e50")
            x += COL_WIDTHS[i]
        y += ROW_HEIGHT

    # 绘制底部分隔线
    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 15

    # 绘制总计信息（与控制台完全一致）
    draw.text((PADDING, y), f"【{REPORT_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【{NEW_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value_new:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【公允价值变动(亿)】{total_diff:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【收益率】{return_rate:.2f}%", font=font, fill="#e74c3c")

    # 保存图片
    img.save(OUTPUT_IMAGE_FILE)
    print(f"\n✅ 完整表格图片生成完成：{OUTPUT_IMAGE_FILE}")


def generate_summary_image(total_adjust_value, total_adjust_value_new, total_diff, return_rate):
    """生成只显示汇总信息的图片（不带表格数据），样式与完整图片完全一致"""
    # 基础样式配置（与完整图片保持完全一致）
    PADDING = 10
    ROW_HEIGHT = 30
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16

    # 拼接关键词+折算比例文本
    ratio_text = ", ".join([f"{k}({v})" for k, v in KEY_WORD_RATIO.items()])

    # 计算画布尺寸（标题+关键词说明+分隔线+4行汇总信息）
    img_width = 1200  # 固定宽度，与完整图片比例协调
    img_height = 6 * ROW_HEIGHT + 2 * PADDING

    # 创建白色背景画布
    img = Image.new("RGB", (img_width, img_height), "white")
    draw = ImageDraw.Draw(img)

    # 跨系统字体兼容（与完整图片保持完全一致）
    try:
        if sys.platform.startswith("win"):
            font = ImageFont.truetype("msyh.ttc", FONT_SIZE)
            header_font = ImageFont.truetype("msyh.ttc", HEADER_FONT_SIZE)
        elif sys.platform.startswith("darwin"):
            font = ImageFont.truetype("Arial Unicode.ttf", FONT_SIZE)
            header_font = ImageFont.truetype("Arial Unicode.ttf", HEADER_FONT_SIZE)
        else:
            font = ImageFont.truetype("DejaVuSans.ttf", FONT_SIZE)
            header_font = ImageFont.truetype("DejaVuSans.ttf", HEADER_FONT_SIZE)
    except:
        # 兜底默认字体
        font = ImageFont.load_default()
        header_font = ImageFont.load_default()

    # 第一行大标题（与完整图片完全一致）
    x, y = PADDING, PADDING
    title_main = f"{REPORT_TRADE_DATE} 到 {NEW_TRADE_DATE} 的股票组合收益统计"
    draw.text((x, y), title_main, font=header_font, fill="#2c3e50")
    y += ROW_HEIGHT

    # 第二行：关键词+折算比例说明（与完整图片完全一致）
    title_sub = f"筛选关键词及折算比例：{ratio_text}"
    draw.text((x, y), title_sub, font=font, fill="#8e44ad")
    y += ROW_HEIGHT

    # 绘制分隔线（与完整图片完全一致）
    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 15

    # 绘制总计信息（与完整图片完全一致）
    draw.text((PADDING, y), f"【{REPORT_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【{NEW_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value_new:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【公允价值变动(亿)】{total_diff:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【收益率】{return_rate:.2f}%", font=font, fill="#e74c3c")

    # 保存汇总版图片
    img.save(OUTPUT_SUMMARY_IMAGE_FILE)
    print(f"✅ 汇总版图片生成完成：{OUTPUT_SUMMARY_IMAGE_FILE}")


def query_top10():
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

    # 股价计算（原有逻辑 + 新增新交易日股价）
    if match_results:
        match_stock_codes = [item["ts_code"] for item in match_results]
        # 1. 查询原交易日收盘价
        price_map = get_stock_close_price(match_stock_codes, REPORT_TRADE_DATE)
        # 2. 【新增】查询新交易日收盘价
        price_map_new = get_stock_close_price(match_stock_codes, NEW_TRADE_DATE)

        for item in match_results:
            hold_amount_val = item["hold_amount"]
            ratio = item["ratio"]

            # 原交易日计算逻辑
            close = price_map.get(item["ts_code"], 0)
            original_val = round(hold_amount_val * close / 100000000, 2) if close > 0 else 0
            adjust_val = round(original_val * ratio, 2)

            # 【新增】新交易日计算逻辑
            close_new = price_map_new.get(item["ts_code"], 0)
            original_val_new = round(hold_amount_val * close_new / 100000000, 2) if close_new > 0 else 0
            adjust_val_new = round(original_val_new * ratio, 2)

            # 存入数据
            item["original_value"] = original_val    # 日1原始持仓
            item["adjust_value"] = adjust_val        # 日1折算持仓
            item["adjust_value_new"] = adjust_val_new# 日2折算持仓

    # 输出结果
    print("\n" + "=" * 200)
    print(f"【{REPORT_PERIOD}】报告期查询完成！共找到 {len(match_results)} 个匹配席位")
    print("=" * 200)

    if not match_results:
        print(f"未查询到包含「{keyword_str}」的股东数据")
        save_raw_cache(RAW_CACHE)
        return

    # 汇总计算
    total_adjust_value = round(sum(item["adjust_value"] for item in match_results), 2)
    # 【新增】新交易日总折算持仓
    total_adjust_value_new = round(sum(item["adjust_value_new"] for item in match_results), 2)
    # 【新增】差值计算
    total_diff = round(total_adjust_value_new - total_adjust_value, 2)
    # 计算收益率
    if total_adjust_value > 0:
        return_rate = round(total_diff / total_adjust_value * 100, 2)
    else:
        return_rate = 0.00

    # ====================== 核心修改：新增日1原始持仓列表头 ======================
    print(f"{'股票代码':<7} {'股票名称':<8} {'持股数量(股)':<13} {'持股比例(%)':<5} "
          f"{'日1原始持仓(亿)':<9} {'日1折算持仓(亿)':<9} {'日2折算持仓(亿)':<9} {'股东名称':<32}")
    print("-" * 200)
    for item in match_results:
        print(f"{item['ts_code']:<10} "
              f"{item['stock_name']:<8} "
              f"{item['hold_amount']:<18} "
              f"{item['hold_ratio']:<10} "
              f"{item['original_value']:<14} "
              f"{item['adjust_value']:<14} "
              f"{item['adjust_value_new']:<14} "
              f"{item['holder_name']:<32}")
    # ==========================================================================
    print("-" * 200)
    print(f"【{REPORT_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value}")
    print(f"【{NEW_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value_new}")
    print(f"【公允价值变动(亿)】{total_diff}")
    print(f"【收益率】{return_rate}%")

    # 生成两张图片：完整表格版 + 汇总版
    generate_table_image(match_results, total_adjust_value, total_adjust_value_new, total_diff, return_rate)
    generate_summary_image(total_adjust_value, total_adjust_value_new, total_diff, return_rate)

    # 最终：将所有缓存的原始数据持久化到文件
    save_raw_cache(RAW_CACHE)


if __name__ == "__main__":
    start_time = time.time()
    query_top10()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
