import argparse
import os
import sys
import time
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFont

from etf_client import (
    KEY_WORD_RATIO,
    OUTPUT_DIR,
    format_specific_ratio_summary,
    get_adj_factors,
    get_combined_etfs,
    get_daily_prices,
    init_tushare,
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
NEW_TRADE_DATE = "20260630"  # 日2：对比的新交易日

# 输出图片文件名（输出目录见 etf_client.OUTPUT_DIR；合并版与普通版文件名区分）
OUTPUT_IMAGE_FILE = os.path.join(OUTPUT_DIR, f"ETF股票组合收益统计_{REPORT_TRADE_DATE}_to_{NEW_TRADE_DATE}_合并版.png")
OUTPUT_SUMMARY_IMAGE_FILE = os.path.join(OUTPUT_DIR, f"ETF股票组合收益统计_{REPORT_TRADE_DATE}_to_{NEW_TRADE_DATE}_合并版汇总.png")
MAX_TABLE_ROWS = 500  # 图片最多展示行数（避免匹配过多时图片过大），完整数据见控制台
# 持有人名称列样式
NAME_FONT_SIZE = 12
NAME_MAX_LINES = 2
NAME_LINE_HEIGHT = 17
# ====================================================


def _get_font(font_size: int, header_font_size: int, name_font_size: int):
    """跨系统字体兼容"""
    try:
        if sys.platform.startswith("win"):
            return (ImageFont.truetype("msyh.ttc", font_size),
                    ImageFont.truetype("msyh.ttc", header_font_size),
                    ImageFont.truetype("msyh.ttc", name_font_size))
        elif sys.platform.startswith("darwin"):
            return (ImageFont.truetype("Arial Unicode.ttf", font_size),
                    ImageFont.truetype("Arial Unicode.ttf", header_font_size),
                    ImageFont.truetype("Arial Unicode.ttf", name_font_size))
        else:
            return (ImageFont.truetype("DejaVuSans.ttf", font_size),
                    ImageFont.truetype("DejaVuSans.ttf", header_font_size),
                    ImageFont.truetype("DejaVuSans.ttf", name_font_size))
    except:
        return ImageFont.load_default(), ImageFont.load_default(), ImageFont.load_default()


def _truncate_text(text, draw, font, max_width):
    """按像素宽度截断文本，超出列宽时以省略号结尾"""
    text = str(text)
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return text + ellipsis


def _wrap_text(text, draw, font, max_width, max_lines):
    """按像素宽度换行，最多 max_lines 行，超出的最后一行以省略号结尾"""
    text = str(text)
    lines = []
    current = ""
    for ch in text:
        if current and draw.textlength(current + ch, font=font) > max_width:
            lines.append(current)
            current = ch
        else:
            current += ch
    if current:
        lines.append(current)

    if len(lines) <= max_lines:
        return lines

    # 超出行数上限：保留前 max_lines-1 行，最后一行截断并加省略号
    result = lines[:max_lines - 1]
    last = lines[max_lines - 1]
    ellipsis = "…"
    while last and draw.textlength(last + ellipsis, font=font) > max_width:
        last = last[:-1]
    result.append(last + ellipsis)
    return result


def merge_holders_by_stock(match_results):
    """
    按 ETF 代码合并持有人数据
    相同代码合并到一行，持有人名称用分号隔开，份额/比例/市值累加
    按日1市值从大到小排序，方便查看持仓最重的 ETF
    """
    etf_groups = defaultdict(list)
    for item in match_results:
        etf_groups[item["ts_code"]].append(item)

    merged_results = []
    for ts_code, items in etf_groups.items():
        etf_name = items[0]["etf_name"]
        holder_names = [item["holder_name"] for item in items]
        merged_holder_names = "; ".join(holder_names)

        merged_item = {
            "ts_code": ts_code,
            "etf_name": etf_name,
            "holder_name": merged_holder_names,
            "hold_amount": sum(item["hold_amount"] for item in items),
            "hold_ratio": round(sum(item["hold_ratio"] for item in items), 2),
            "original_value": round(sum(item["original_value"] for item in items), 2),
            "adjust_value": round(sum(item["adjust_value"] for item in items), 2),
            "adjust_value_new": round(sum(item["adjust_value_new"] for item in items), 2),
            "has_corporate_action": any(item.get("has_corporate_action") for item in items),
            "ratio_source": "标的覆盖" if any(item.get("ratio_source") == "标的覆盖" for item in items) else "关键词默认",
        }
        merged_results.append(merged_item)

    merged_results.sort(key=lambda x: (-x["original_value"], x["ts_code"]))
    return merged_results


def generate_table_image(match_results, total_adjust_value, total_adjust_value_new, total_diff, return_rate):
    """生成与命令行一致的 ETF 组合收益表格图片（按代码合并），标题含关键词+折算比例"""
    PADDING = 10
    ROW_HEIGHT = 40
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16
    COL_WIDTHS = [100, 240, 150, 100, 130, 130, 130, 440]
    COL_NAMES = ["代码", "ETF名称", "份额(份)", "比例(%)", "日1市值(亿)", "日1折算市值(亿)", "日2折算市值(亿)", "持有人名称"]

    ratio_text = ", ".join([f"{k}({v})" for k, v in KEY_WORD_RATIO.items()])
    rows_to_show = match_results[:MAX_TABLE_ROWS]
    truncated = len(match_results) > MAX_TABLE_ROWS
    has_any_adj = any(item.get("has_corporate_action") for item in rows_to_show)
    # 日1原始（折算前）市值小于 1 亿的行折叠为一行汇总（非市值列留空）
    main_rows = [item for item in rows_to_show if item.get("original_value", 0) >= 1.0]
    small_rows = [item for item in rows_to_show if item.get("original_value", 0) < 1.0]
    has_small_rows = bool(small_rows)
    small_count = len(small_rows)
    small_orig_value1 = round(sum(item["original_value"] for item in small_rows), 2)
    small_value1 = round(sum(item["adjust_value"] for item in small_rows), 2)
    small_value2 = round(sum(item["adjust_value_new"] for item in small_rows), 2)
    small_has_adj = any(item.get("has_corporate_action") for item in small_rows)
    display_rows = list(main_rows)
    if has_small_rows:
        display_rows.append(None)  # None 表示小市值汇总行
    extra_rows = 7 if truncated else 6
    if has_any_adj:
        extra_rows += 1
    ratio_summary_text = format_specific_ratio_summary()
    if ratio_summary_text:
        extra_rows += 1
    total_rows = len(display_rows) + extra_rows
    img_width = sum(COL_WIDTHS) + 2 * PADDING
    img_height = total_rows * ROW_HEIGHT + 2 * PADDING + ROW_HEIGHT * 2

    img = Image.new("RGB", (img_width, img_height), "white")
    draw = ImageDraw.Draw(img)
    font, header_font, name_font = _get_font(FONT_SIZE, HEADER_FONT_SIZE, NAME_FONT_SIZE)

    x, y = PADDING, PADDING
    title_main = f"{REPORT_TRADE_DATE} 到 {NEW_TRADE_DATE} 的 ETF 组合收益统计（按代码合并，按日1市值降序）"
    draw.text((x, y), title_main, font=header_font, fill="#2c3e50")
    y += ROW_HEIGHT

    title_sub = f"筛选关键词及折算比例：{ratio_text}"
    # 关键词说明过长时按像素宽度换行（最多两行，两行共 40px 与 ROW_HEIGHT 一致）
    sub_lines = _wrap_text(title_sub, draw, font, img_width - 2 * PADDING, 2)
    for line_index, line in enumerate(sub_lines):
        draw.text((x, y + line_index * 20), line, font=font, fill="#8e44ad")
    y += ROW_HEIGHT

    # 标的特殊设定提示（位于关键词说明下方，最多两行）
    if ratio_summary_text:
        ratio_lines = _wrap_text(ratio_summary_text, draw, font, img_width - 2 * PADDING, 2)
        for line_index, line in enumerate(ratio_lines):
            draw.text((x, y + line_index * 20), line, font=font, fill="#8e44ad")
        y += ROW_HEIGHT  # 提示块占一整行，与下方表头留出间距

    x = PADDING
    for i, name in enumerate(COL_NAMES):
        draw.text((x + 5, y + 5), name, font=header_font, fill="#3498db")
        x += COL_WIDTHS[i]
    y += ROW_HEIGHT

    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 8

    for row_index, item in enumerate(display_rows):
        x = PADDING
        if item is None:
            # 小市值汇总行：非市值列无意义，留空
            row_data = [
                "",
                f"其他小市值（{small_count} 只）" + ("＊" if small_has_adj else ""),
                "", "",
                f"{small_orig_value1:.2f}", f"{small_value1:.2f}", f"{small_value2:.2f}",
                "",
            ]
        else:
            row_data = [
                item["ts_code"],
                item["etf_name"] + ("＊" if item.get("has_corporate_action") else ""),
                f"{item['hold_amount']:,}",
                f"{item['hold_ratio']:.2f}",
                f"{item['original_value']:.2f}",
                f"{item['adjust_value']:.2f}",
                f"{item['adjust_value_new']:.2f}", item["holder_name"],
            ]
        for i, data in enumerate(row_data):
            if i == len(COL_NAMES) - 1:
                if item is None:
                    # 汇总行持有人名称留空
                    x += COL_WIDTHS[i]
                    continue
                # 持有人名称列：小号字体 + 换行，最多 NAME_MAX_LINES 行，垂直居中
                name_lines = _wrap_text(data, draw, name_font, COL_WIDTHS[i] - 10, NAME_MAX_LINES)
                lines_height = len(name_lines) * NAME_LINE_HEIGHT
                start_y = y + (ROW_HEIGHT - lines_height) // 2
                for line_index, line in enumerate(name_lines):
                    draw.text((x + 5, start_y + line_index * NAME_LINE_HEIGHT), line,
                              font=name_font, fill="#2c3e50")
                x += COL_WIDTHS[i]
                continue
            cell_text = str(data)
            if i == 1:
                # ETF名称列：超出列宽时截断
                cell_text = _truncate_text(cell_text, draw, font, COL_WIDTHS[i] - 10)
            draw.text((x + 5, y + 5), cell_text, font=font, fill="#2c3e50")
            x += COL_WIDTHS[i]
        # 行间浅色分隔线（辅助对齐，不抢主内容；第一行与表头间已有分隔线）
        if row_index > 0:
            draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#e8e8e8", width=1)
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
    if has_any_adj:
        # 注释说明放在最下面
        y += ROW_HEIGHT
        draw.text((PADDING, y), "＊ 表示期间发生分红/份额折算，日2市值已按复权因子修正", font=font, fill="#8e44ad")

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
    img_height = 6 * ROW_HEIGHT + 2 * PADDING + 20  # 预留关键词说明换行空间
    ratio_summary_text = format_specific_ratio_summary()
    if ratio_summary_text:
        img_height += 40  # 预留标的特殊设定提示空间

    img = Image.new("RGB", (img_width, img_height), "white")
    draw = ImageDraw.Draw(img)
    font, header_font, _name_font = _get_font(FONT_SIZE, HEADER_FONT_SIZE, NAME_FONT_SIZE)

    x, y = PADDING, PADDING
    title_main = f"{REPORT_TRADE_DATE} 到 {NEW_TRADE_DATE} 的 ETF 组合收益统计（按代码合并）"
    draw.text((x, y), title_main, font=header_font, fill="#2c3e50")
    y += ROW_HEIGHT

    title_sub = f"筛选关键词及折算比例：{ratio_text}"
    # 关键词说明过长时按像素宽度换行（最多两行）
    sub_lines = _wrap_text(title_sub, draw, font, img_width - 2 * PADDING, 2)
    for line_index, line in enumerate(sub_lines):
        draw.text((x, y + line_index * 20), line, font=font, fill="#8e44ad")
    y += len(sub_lines) * 20

    # 标的特殊设定提示（位于关键词说明下方，最多两行）
    if ratio_summary_text:
        ratio_lines = _wrap_text(ratio_summary_text, draw, font, img_width - 2 * PADDING, 2)
        for line_index, line in enumerate(ratio_lines):
            draw.text((x, y + line_index * 20), line, font=font, fill="#8e44ad")
        y += 40  # 提示块固定高度，与下方分隔线留出间距

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
    """主查询函数：单报告期筛选 + 两交易日公允价值变动与收益率（按代码合并）"""
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

    # 拉两个交易日全市场复权因子（用于把日2价格修正到日1同一复权系数水平）
    print(f"拉取 {REPORT_TRADE_DATE} / {NEW_TRADE_DATE} 全市场 ETF 复权因子...")
    adj_map1 = get_adj_factors(REPORT_TRADE_DATE)
    adj_map2 = get_adj_factors(NEW_TRADE_DATE)
    if not adj_map1 or not adj_map2:
        print("⚠️ 复权因子获取失败（可能非交易日/接口权限不足），本次不进行价格修正")

    for item in match_results:
        close = price_map.get(item["ts_code"], 0)
        original_val = round(item["hold_amount"] * close / 1e8, 2) if close > 0 else 0
        adjust_val = round(original_val * item["ratio"], 2)

        # 复权修正：日2价格换算到日1相同复权系数水平（送转/分红时份额已变，价格需同口径）
        factor1 = adj_map1.get(item["ts_code"], 0)
        factor2 = adj_map2.get(item["ts_code"], 0)
        adj_ratio = factor2 / factor1 if factor1 > 0 and factor2 > 0 else 1.0
        close_new_raw = price_map_new.get(item["ts_code"], 0)
        close_new = close_new_raw * adj_ratio if close_new_raw > 0 else 0
        original_val_new = round(item["hold_amount"] * close_new / 1e8, 2) if close_new > 0 else 0
        adjust_val_new = round(original_val_new * item["ratio"], 2)

        item["original_value"] = original_val
        item["adjust_value"] = adjust_val
        item["adjust_value_new"] = adjust_val_new
        item["has_corporate_action"] = abs(adj_ratio - 1.0) > 1e-9

    print("\n" + "=" * 220)
    print(f"【{REPORT_PERIOD}】报告期查询完成！共找到 {len(match_results)} 个匹配持有人")

    merged_results = merge_holders_by_stock(match_results)
    print(f"按代码合并后：共 {len(merged_results)} 只 ETF")
    print("=" * 220)

    if not merged_results:
        print(f"未查询到包含「{keyword_str}」的持有人数据（可调整 etf_client.KEY_WORD_RATIO）")
        return

    total_adjust_value = round(sum(item["adjust_value"] for item in merged_results), 2)
    total_adjust_value_new = round(sum(item["adjust_value_new"] for item in merged_results), 2)
    total_diff = round(total_adjust_value_new - total_adjust_value, 2)
    if total_adjust_value > 0:
        return_rate = round(total_diff / total_adjust_value * 100, 2)
    else:
        return_rate = 0.00

    print(f"{'代码':<10} {'ETF名称':<12} {'份额(份)':<15} {'比例(%)':<8} "
          f"{'日1市值(亿)':<10} {'日1折算市值(亿)':<14} {'日2折算市值(亿)':<10} {'持有人名称':<32}")
    print("-" * 220)
    for item in merged_results:
        name_display = item["etf_name"] + ("＊" if item.get("has_corporate_action") else "")
        print(f"{item['ts_code']:<12} "
              f"{name_display:<12} "
              f"{item['hold_amount']:<16} "
              f"{item['hold_ratio']:<10.2f} "
              f"{item['original_value']:<12} "
              f"{item['adjust_value']:<12} "
              f"{item['adjust_value_new']:<12} "
              f"{item['holder_name']:<32}")
    print("-" * 220)
    print(f"【{REPORT_TRADE_DATE}折算后总市值(亿)】{total_adjust_value}")
    print(f"【{NEW_TRADE_DATE}折算后总市值(亿)】{total_adjust_value_new}")
    print(f"【公允价值变动(亿)】{total_diff}")
    print(f"【收益率】{return_rate}%")
    if any(item.get("has_corporate_action") for item in merged_results):
        print("＊ 表示期间发生分红/份额折算，日2市值已按复权因子修正")

    generate_table_image(merged_results, total_adjust_value, total_adjust_value_new, total_diff, return_rate)
    generate_summary_image(total_adjust_value, total_adjust_value_new, total_diff, return_rate)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="两个交易日之间 ETF 十大持有人持仓收益统计（合并席位版）")
    parser.add_argument("--token", default=None,
                        help="Tushare token（未保存过配置且非交互环境时用此参数指定，传入后自动保存）")
    args = parser.parse_args()
    init_tushare(args.token)

    start_time = time.time()
    query_top10()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
