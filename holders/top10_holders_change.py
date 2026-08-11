import os
import time
import json
import threading
import sys
import tushare as ts
from tushare.pro.client import DataApi
from vnpy.trader.setting import SETTINGS
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont

# ===================== 核心配置 =====================
INDEX_DATE = "20260331"  # 样本池成分股日期
INDEX_CODES = ["000906.SH", "000852.SH"]  # 样本池: 中证800 + 中证1000
# INDEX_CODES = ["000906.SH"]  # 样本池: 中证800
# INDEX_CODES = ['399300.SZ']  # 样本池: 沪深300

# 双报告期配置（对比期：前一期 -> 后一期）
REPORT_PERIOD1 = "20251231"  # 报告期1（基准期）
REPORT_TRADE_DATE1 = "20260331"  # 报告期1用于市值计算的交易日
REPORT_PERIOD2 = "20260331"  # 报告期2（对比期）
REPORT_TRADE_DATE2 = "20260331"  # 报告期2用于市值计算的交易日

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

MAX_WORKERS = 5  # 并发数
MAX_REQUESTS_PER_MINUTE = 180  # 限流
# 输出目录：生成的图片统一输出到仓库根目录的 output/（已在 .gitignore 中忽略）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(os.path.dirname(BASE_DIR), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# 缓存文件：存储【Tushare原始接口数据】（保持在脚本目录 holders/ 下，随仓库提交，请勿删除）
CACHE_FILE = os.path.join(BASE_DIR, "tushare_top10_holders_raw.json")
# 输出图片文件名
OUTPUT_TABLE_IMAGE_FILE = os.path.join(OUTPUT_DIR, "持股变动表格.png")
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
    """批量查询指定交易日收盘价"""
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
    单个股票双报告期处理 + 新增持股变动百分比 + 排序权重
    排序规则：新增0 > 增持1 > 不变2 > 减持3 > 退出4
    """
    raw_holders1 = get_stock_top10_raw(ts_code, REPORT_PERIOD1)
    raw_holders2 = get_stock_top10_raw(ts_code, REPORT_PERIOD2)
    match_results = []

    def filter_keyword_holders(raw_data):
        result = {}
        for row in raw_data:
            holder_name = row["holder_name"]
            match_ratio = None
            for keyword, ratio in KEY_WORD_RATIO.items():
                if keyword in holder_name:
                    match_ratio = ratio
                    break
            if match_ratio is not None:
                result[holder_name] = {
                    "hold_amount": int(row["hold_amount"]),
                    "hold_ratio": round(row["hold_ratio"], 2),
                    "ratio": match_ratio
                }
        return result

    holder1_map = filter_keyword_holders(raw_holders1)
    holder2_map = filter_keyword_holders(raw_holders2)

    all_holders = set(holder1_map.keys()).union(set(holder2_map.keys()))
    for holder_name in all_holders:
        data1 = holder1_map.get(holder_name, None)
        data2 = holder2_map.get(holder_name, None)

        base = {
            "ts_code": ts_code,
            "stock_name": stock_name,
            "holder_name": holder_name,
        }

        # 1. 退出
        if data1 and not data2:
            base.update({
                "change_type": "退出",
                "sort_rank": 4,
                "hold1_amount": data1["hold_amount"],
                "hold1_ratio": data1["hold_ratio"],
                "ratio1": data1["ratio"],
                "hold2_amount": 0,
                "hold2_ratio": 0.0,
                "ratio2": data1["ratio"],
            })
        # 2. 新增
        elif not data1 and data2:
            base.update({
                "change_type": "新增",
                "sort_rank": 0,
                "hold1_amount": 0,
                "hold1_ratio": 0.0,
                "ratio1": data2["ratio"],
                "hold2_amount": data2["hold_amount"],
                "hold2_ratio": data2["hold_ratio"],
                "ratio2": data2["ratio"],
            })
        # 3. 两期都有：计算变动比例 + 拼接百分比 + 排序权重
        else:
            hold1 = data1["hold_amount"]
            hold2 = data2["hold_amount"]

            # 计算持股数量变动百分比 保留2位小数
            if hold1 == 0:
                pct = 0.0
            else:
                pct = (hold2 - hold1) / hold1 * 100

            if abs(pct) < 0.01:
                change = "不变"
                rank = 2
            else:
                pct_round = round(pct, 2)
                if hold2 > hold1:
                    change = f"增持(+{pct_round}%)"
                    rank = 1
                elif hold2 < hold1:
                    change = f"减持({pct_round}%)"
                    rank = 3
                else:
                    change = "不变"
                    rank = 2

            base.update({
                "change_type": change,
                "sort_rank": rank,
                "hold1_amount": hold1,
                "hold1_ratio": data1["hold_ratio"],
                "ratio1": data1["ratio"],
                "hold2_amount": hold2,
                "hold2_ratio": data2["hold_ratio"],
                "ratio2": data2["ratio"],
            })

        match_results.append(base)

    return match_results

# ===================== 新增：生成表格图片函数 =====================
def generate_table_image(match_results, total_adj1, total_adj2, report1, report2):
    """生成与命令行一致的持股变动UI表格图片，标题含关键词+折算比例"""
    # 基础样式配置
    PADDING = 10
    ROW_HEIGHT = 30
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16
    COL_WIDTHS = [100, 80, 120, 160, 90, 160, 90, 400]
    COL_NAMES = ["股票代码", "股票名称", "变动类型", "期1持股(股)", "期1折算(亿)", "期2持股(股)", "期2折算(亿)", "股东名称"]

    # 拼接关键词+折算比例文本
    ratio_text = ", ".join([f"{k}({v})" for k, v in KEY_WORD_RATIO.items()])

    # 计算画布尺寸（多一行标题说明）
    total_rows = len(match_results) + 5
    img_width = sum(COL_WIDTHS) + 2 * PADDING
    img_height = total_rows * ROW_HEIGHT + 2 * PADDING

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
    title_main = f"{report1} → {report2} 持股变动统计表"
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
            item['ts_code'], item['stock_name'], item['change_type'],
            str(item['hold1_amount']), str(item['adjust_value1']),
            str(item['hold2_amount']), str(item['adjust_value2']),
            item['holder_name']
        ]
        for i, data in enumerate(row_data):
            draw.text((x + 5, y + 5), str(data), font=font, fill="#2c3e50")
            x += COL_WIDTHS[i]
        y += ROW_HEIGHT

    # 绘制底部分隔线
    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 15

    # 绘制总计信息
    total_change = round(total_adj2 - total_adj1, 2)
    draw.text((PADDING, y), f"【{report1}公开总持仓(亿)】{total_adj1}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【{report2}公开总持仓(亿)】{total_adj2}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【持仓变动(亿)】{total_change}", font=font, fill="#e74c3c")

    # 保存图片
    img.save(OUTPUT_TABLE_IMAGE_FILE)
    print(f"\n✅ UI表格图片生成完成：{OUTPUT_TABLE_IMAGE_FILE}")
# ================================================================

def query_top10_change():
    """主查询函数（双报告期+变动百分比+固定顺序排序）"""
    stock_map = get_combined_stocks()
    if not stock_map:
        return

    total = len(stock_map)
    keyword_str = ", ".join(KEY_WORD_RATIO.keys())
    index_str = ", ".join(INDEX_CODES)
    print(f"开始查询指数【{index_str}】共 {total} 只股票")
    print(f"对比报告期：{REPORT_PERIOD1} → {REPORT_PERIOD2}")
    print(f"筛选包含「{keyword_str}」的持股变动...\n")

    stock_list = list(stock_map.items())
    match_results = []
    completed_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        task_futures = [executor.submit(query_single_stock, code, name) for code, name in stock_list]

        for future in as_completed(task_futures):
            completed_count += 1
            if completed_count % 50 == 0:
                print(f"查询进度：{completed_count}/{total}")

            stock_result = future.result()
            if stock_result:
                match_results.extend(stock_result)

    # 按指定顺序排序：新增0 → 增持1 → 不变2 → 减持3 → 退出4
    match_results.sort(key=lambda x: x["sort_rank"])

    if match_results:
        match_stock_codes = [item["ts_code"] for item in match_results]
        price_map1 = get_stock_close_price(match_stock_codes, REPORT_TRADE_DATE1)
        price_map2 = get_stock_close_price(match_stock_codes, REPORT_TRADE_DATE2)

        for item in match_results:
            close1 = price_map1.get(item["ts_code"], 0)
            orig1 = round(item["hold1_amount"] * close1 / 100000000, 2) if close1 > 0 else 0
            adj1 = round(orig1 * item["ratio1"], 2)

            close2 = price_map2.get(item["ts_code"], 0)
            orig2 = round(item["hold2_amount"] * close2 / 100000000, 2) if close2 > 0 else 0
            adj2 = round(orig2 * item["ratio2"], 2)

            item.update({
                "original_value1": orig1, "adjust_value1": adj1,
                "original_value2": orig2, "adjust_value2": adj2
            })

    print("\n" + "=" * 210)
    print(f"【{REPORT_PERIOD1} → {REPORT_PERIOD2}】持股变动查询完成！共找到 {len(match_results)} 条匹配数据")
    print("=" * 210)

    if not match_results:
        print(f"未查询到包含「{keyword_str}」的股东数据")
        save_raw_cache(RAW_CACHE)
        return

    total_adj1 = round(sum(item["adjust_value1"] for item in match_results), 2)
    total_adj2 = round(sum(item["adjust_value2"] for item in match_results), 2)

    print(f"{'股票代码':<7} {'股票名称':<8} {'变动类型':<11}"
          f"{'期1持股(股)':<12} {'期1折算(亿)':<6} "
          f"{'期2持股(股)':<12} {'期2折算(亿)':<8} "
          f"{'股东名称':<32}")
    print("-" * 210)

    for item in match_results:
        print(f"{item['ts_code']:<10} "
              f"{item['stock_name']:<8} "
              f"{item['change_type']:<12} "
              f"{item['hold1_amount']:<15} "
              f"{item['adjust_value1']:<11} "
              f"{item['hold2_amount']:<15} "
              f"{item['adjust_value2']:<11} "
              f"{item['holder_name']:<32}")

    print("-" * 210)
    print(f"【{REPORT_PERIOD1} 折算后总持仓(亿)】{total_adj1}")
    print(f"【{REPORT_PERIOD2} 折算后总持仓(亿)】{total_adj2}")
    print(f"【持仓变动(亿)】{round(total_adj2 - total_adj1, 2)}")

    # ===================== 新增：调用图片生成函数 =====================
    generate_table_image(match_results, total_adj1, total_adj2, REPORT_PERIOD1, REPORT_PERIOD2)

    save_raw_cache(RAW_CACHE)


if __name__ == "__main__":
    start_time = time.time()
    query_top10_change()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
