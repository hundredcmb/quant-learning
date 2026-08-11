import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from etf_client import (
    KEY_WORD_RATIO,
    OUTPUT_DIR,
    get_combined_etfs,
    get_daily_prices,
    query_single_etf,
)

# Windows 控制台编码兼容：避免 GBK 下 emoji 打印崩溃
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ===================== 核心配置 =====================
REPORT_PERIOD = "20251231"  # 报告期（半年报 0630 / 年报 1231）
REPORT_TRADE_DATE = "20251231"  # 日1：原报告期交易日
NEW_TRADE_DATE = "20260807"  # 日2：对比的新交易日

# 输出图片文件名（输出目录见 etf_client.OUTPUT_DIR）
OUTPUT_IMAGE_FILE = os.path.join(OUTPUT_DIR, f"ETF股票组合收益统计_{REPORT_TRADE_DATE}_to_{NEW_TRADE_DATE}.png")
OUTPUT_SUMMARY_IMAGE_FILE = os.path.join(OUTPUT_DIR, f"ETF股票组合收益统计_{REPORT_TRADE_DATE}_to_{NEW_TRADE_DATE}_汇总版.png")
MAX_TABLE_ROWS = 500  # 图片最多展示行数（避免匹配过多时图片过大），完整数据见控制台
# ====================================================


def _get_font(font_size: int, header_font_size: int):
    """跨系统字体兼容"""
    try:
        if sys.platform.startswith("win"):
            return (ImageFont.truetype("msyh.ttc", font_size),
                    ImageFont.truetype("msyh.ttc", header_font_size))
        elif sys.platform.startswith("darwin"):
            return (ImageFont.truetype("Arial Unicode.ttf", font_size),
                    ImageFont.truetype("Arial Unicode.ttf", header_font_size))
        else:
            return (ImageFont.truetype("DejaVuSans.ttf", font_size),
                    ImageFont.truetype("DejaVuSans.ttf", header_font_size))
    except:
        return ImageFont.load_default(), ImageFont.load_default()


def generate_table_image(match_results, total_adjust_value, total_adjust_value_new, total_diff, return_rate):
    """生成与命令行一致的 ETF 组合收益表格图片，标题含关键词+折算比例"""
    PADDING = 10
    ROW_HEIGHT = 30
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16
    COL_WIDTHS = [100, 120, 150, 100, 130, 130, 360]
    COL_NAMES = ["代码", "ETF名称", "份额(份)", "比例(%)", "日1市值(亿)", "日2市值(亿)", "持有人名称"]

    ratio_text = ", ".join([f"{k}({v})" for k, v in KEY_WORD_RATIO.items()])
    rows_to_show = match_results[:MAX_TABLE_ROWS]
    truncated = len(match_results) > MAX_TABLE_ROWS
    total_rows = len(rows_to_show) + (7 if truncated else 6)
    img_width = sum(COL_WIDTHS) + 2 * PADDING
    img_height = total_rows * ROW_HEIGHT + 2 * PADDING + ROW_HEIGHT * 2

    img = Image.new("RGB", (img_width, img_height), "white")
    draw = ImageDraw.Draw(img)
    font, header_font = _get_font(FONT_SIZE, HEADER_FONT_SIZE)

    x, y = PADDING, PADDING
    title_main = f"{REPORT_TRADE_DATE} 到 {NEW_TRADE_DATE} 的 ETF 组合收益统计"
    draw.text((x, y), title_main, font=header_font, fill="#2c3e50")
    y += ROW_HEIGHT

    title_sub = f"筛选关键词及折算比例：{ratio_text}"
    draw.text((x, y), title_sub, font=font, fill="#8e44ad")
    y += ROW_HEIGHT

    x = PADDING
    for i, name in enumerate(COL_NAMES):
        draw.text((x + 5, y + 5), name, font=header_font, fill="#3498db")
        x += COL_WIDTHS[i]
    y += ROW_HEIGHT

    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 8

    for item in rows_to_show:
        x = PADDING
        row_data = [
            item["ts_code"], item["etf_name"], f"{item['hold_amount']:,}",
            f"{item['hold_ratio']:.2f}", f"{item['adjust_value']:.2f}",
            f"{item['adjust_value_new']:.2f}", item["holder_name"],
        ]
        for i, data in enumerate(row_data):
            draw.text((x + 5, y + 5), str(data), font=font, fill="#2c3e50")
            x += COL_WIDTHS[i]
        y += ROW_HEIGHT

    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 15
    if truncated:
        draw.text((PADDING, y), f"……共 {len(match_results)} 条，仅展示前 {MAX_TABLE_ROWS} 条，完整数据见控制台……",
                  font=font, fill="#8e44ad")
        y += ROW_HEIGHT

    draw.text((PADDING, y), f"【{REPORT_TRADE_DATE}折算后总市值(亿)】{total_adjust_value:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【{NEW_TRADE_DATE}折算后总市值(亿)】{total_adjust_value_new:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【公允价值变动(亿)】{total_diff:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【收益率】{return_rate:.2f}%", font=font, fill="#e74c3c")

    img.save(OUTPUT_IMAGE_FILE)
    print(f"\n✅ 完整表格图片生成完成：{OUTPUT_IMAGE_FILE}")


def generate_summary_image(total_adjust_value, total_adjust_value_new, total_diff, return_rate):
    """生成只显示汇总信息的图片（不带表格数据），样式与完整图片一致"""
    PADDING = 10
    ROW_HEIGHT = 30
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16

    ratio_text = ", ".join([f"{k}({v})" for k, v in KEY_WORD_RATIO.items()])
    img_width = 1200
    img_height = 6 * ROW_HEIGHT + 2 * PADDING

    img = Image.new("RGB", (img_width, img_height), "white")
    draw = ImageDraw.Draw(img)
    font, header_font = _get_font(FONT_SIZE, HEADER_FONT_SIZE)

    x, y = PADDING, PADDING
    title_main = f"{REPORT_TRADE_DATE} 到 {NEW_TRADE_DATE} 的 ETF 组合收益统计"
    draw.text((x, y), title_main, font=header_font, fill="#2c3e50")
    y += ROW_HEIGHT

    title_sub = f"筛选关键词及折算比例：{ratio_text}"
    draw.text((x, y), title_sub, font=font, fill="#8e44ad")
    y += ROW_HEIGHT

    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 15

    draw.text((PADDING, y), f"【{REPORT_TRADE_DATE}折算后总市值(亿)】{total_adjust_value:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【{NEW_TRADE_DATE}折算后总市值(亿)】{total_adjust_value_new:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【公允价值变动(亿)】{total_diff:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【收益率】{return_rate:.2f}%", font=font, fill="#e74c3c")

    img.save(OUTPUT_SUMMARY_IMAGE_FILE)
    print(f"✅ 汇总版图片生成完成：{OUTPUT_SUMMARY_IMAGE_FILE}")


def query_top10():
    """主查询函数：单报告期筛选 + 两交易日公允价值变动与收益率"""
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

    # 拉两个交易日全市场收盘价（各 1 次请求）
    print(f"拉取 {REPORT_TRADE_DATE} / {NEW_TRADE_DATE} 全市场 ETF 收盘价...")
    price_map = get_daily_prices(REPORT_TRADE_DATE)
    price_map_new = get_daily_prices(NEW_TRADE_DATE)
    if not price_map:
        print(f"⚠️ {REPORT_TRADE_DATE} 无行情数据（可能非交易日），日1市值按 0 处理")
    if not price_map_new:
        print(f"⚠️ {NEW_TRADE_DATE} 无行情数据（可能非交易日），日2市值按 0 处理")

    for item in match_results:
        close = price_map.get(item["ts_code"], 0)
        original_val = round(item["hold_amount"] * close / 1e8, 2) if close > 0 else 0
        adjust_val = round(original_val * item["ratio"], 2)

        close_new = price_map_new.get(item["ts_code"], 0)
        original_val_new = round(item["hold_amount"] * close_new / 1e8, 2) if close_new > 0 else 0
        adjust_val_new = round(original_val_new * item["ratio"], 2)

        item["original_value"] = original_val
        item["adjust_value"] = adjust_val
        item["adjust_value_new"] = adjust_val_new

    print("\n" + "=" * 220)
    print(f"【{REPORT_PERIOD}】报告期查询完成！共找到 {len(match_results)} 个匹配持有人")
    print("=" * 220)

    if not match_results:
        print(f"未查询到包含「{keyword_str}」的持有人数据（可调整 etf_client.KEY_WORD_RATIO）")
        return

    total_adjust_value = round(sum(item["adjust_value"] for item in match_results), 2)
    total_adjust_value_new = round(sum(item["adjust_value_new"] for item in match_results), 2)
    total_diff = round(total_adjust_value_new - total_adjust_value, 2)
    if total_adjust_value > 0:
        return_rate = round(total_diff / total_adjust_value * 100, 2)
    else:
        return_rate = 0.00

    print(f"{'代码':<10} {'ETF名称':<12} {'份额(份)':<15} {'比例(%)':<8} "
          f"{'日1市值(亿)':<10} {'日2市值(亿)':<10} {'持有人名称':<32}")
    print("-" * 220)
    for item in match_results:
        print(f"{item['ts_code']:<12} "
              f"{item['etf_name']:<12} "
              f"{item['hold_amount']:<16} "
              f"{item['hold_ratio']:<10.2f} "
              f"{item['adjust_value']:<12} "
              f"{item['adjust_value_new']:<12} "
              f"{item['holder_name']:<32}")
    print("-" * 220)
    print(f"【{REPORT_TRADE_DATE}折算后总市值(亿)】{total_adjust_value}")
    print(f"【{NEW_TRADE_DATE}折算后总市值(亿)】{total_adjust_value_new}")
    print(f"【公允价值变动(亿)】{total_diff}")
    print(f"【收益率】{return_rate}%")

    generate_table_image(match_results, total_adjust_value, total_adjust_value_new, total_diff, return_rate)
    generate_summary_image(total_adjust_value, total_adjust_value_new, total_diff, return_rate)


if __name__ == "__main__":
    start_time = time.time()
    query_top10()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
