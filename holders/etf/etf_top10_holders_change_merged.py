import os
import sys
import time
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFont

from etf_client import (
    KEY_WORD_RATIO,
    OUTPUT_DIR,
    format_specific_ratio_summary,
    get_combined_etfs,
    get_daily_prices,
    get_etf_holders,
)

# Windows 控制台编码兼容：避免 GBK 下 emoji 打印崩溃
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ===================== 核心配置 =====================
# 双报告期配置（对比期：前一期 -> 后一期，均为半年报 0630 / 年报 1231）
REPORT_PERIOD1 = "20251231"  # 报告期1（基准期）
REPORT_TRADE_DATE1 = "20251231"  # 报告期1用于市值计算的交易日
REPORT_PERIOD2 = "20260331"  # 报告期2（对比期）
REPORT_TRADE_DATE2 = "20260331"  # 报告期2用于市值计算的交易日

# 输出图片文件名（输出目录见 etf_client.OUTPUT_DIR）
OUTPUT_TABLE_IMAGE_FILE = os.path.join(OUTPUT_DIR, "ETF持股变动表格.png")
MAX_TABLE_ROWS = 500  # 图片最多展示行数（避免匹配过多时图片过大），完整数据见控制台
# ====================================================


def query_single_etf(ts_code: str, etf_name: str):
    """
    单只 ETF 双报告期处理 + 份额变动百分比 + 排序权重
    排序规则：新增0 > 增持1 > 不变2 > 减持3 > 退出4
    """
    raw_holders1 = get_etf_holders(ts_code, REPORT_PERIOD1)
    raw_holders2 = get_etf_holders(ts_code, REPORT_PERIOD2)
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
                    "hold_amount": row["hold_amount"],
                    "hold_ratio": row["hold_ratio"],
                    "ratio": match_ratio,
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
            "etf_name": etf_name,
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


def merge_holders_by_stock(match_results):
    """
    按代码合并持有人数据
    相同代码合并到一行，持有人名称用分号隔开，份额和金额累加
    变动类型基于合并后的总份额计算整体变动百分比
    """
    stock_groups = defaultdict(list)
    for item in match_results:
        stock_groups[item["ts_code"]].append(item)

    merged_results = []
    for ts_code, items in stock_groups.items():
        etf_name = items[0]["etf_name"]
        holder_names = [item["holder_name"] for item in items]
        merged_holder_names = "; ".join(holder_names)

        total_hold1 = sum(item["hold1_amount"] for item in items)
        total_hold2 = sum(item["hold2_amount"] for item in items)
        total_adjust1 = sum(item["adjust_value1"] for item in items)
        total_adjust2 = sum(item["adjust_value2"] for item in items)

        if total_hold1 == 0:
            merged_change_type = "新增"
            min_sort_rank = 0
        elif total_hold2 == 0:
            merged_change_type = "退出"
            min_sort_rank = 4
        else:
            total_pct = (total_hold2 - total_hold1) / total_hold1 * 100
            if abs(total_pct) < 0.01:
                merged_change_type = "不变"
                min_sort_rank = 2
            else:
                pct_round = round(total_pct, 2)
                if total_hold2 > total_hold1:
                    merged_change_type = f"增持(+{pct_round}%)"
                    min_sort_rank = 1
                elif total_hold2 < total_hold1:
                    merged_change_type = f"减持({pct_round}%)"
                    min_sort_rank = 3
                else:
                    merged_change_type = "不变"
                    min_sort_rank = 2

        merged_item = {
            "ts_code": ts_code,
            "etf_name": etf_name,
            "holder_name": merged_holder_names,
            "change_type": merged_change_type,
            "sort_rank": min_sort_rank,
            "hold1_amount": total_hold1,
            "hold2_amount": total_hold2,
            "adjust_value1": round(total_adjust1, 2),
            "adjust_value2": round(total_adjust2, 2),
            "ratio_source": "标的覆盖" if any(item.get("ratio_source") == "标的覆盖" for item in items) else "关键词默认",
        }
        merged_results.append(merged_item)

    merged_results.sort(key=lambda x: x["sort_rank"])
    return merged_results


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


def generate_table_image(match_results, total_adj1, total_adj2, report1, report2):
    """生成与命令行一致的 ETF 持股变动表格图片，标题含关键词+折算比例"""
    PADDING = 10
    ROW_HEIGHT = 40
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16
    NAME_FONT_SIZE = 12  # 持有人名称列字号（略小，配合换行）
    NAME_MAX_LINES = 2  # 持有人名称列最多显示行数
    NAME_LINE_HEIGHT = 17  # 持有人名称列行高
    COL_WIDTHS = [100, 240, 90, 150, 120, 150, 120, 440]
    COL_NAMES = ["代码", "ETF名称", "变动类型", "期1份额(份)", "期1市值(亿)", "期2份额(份)", "期2市值(亿)", "持有人名称"]

    ratio_text = ", ".join([f"{k}({v})" for k, v in KEY_WORD_RATIO.items()])
    rows_to_show = match_results[:MAX_TABLE_ROWS]
    truncated = len(match_results) > MAX_TABLE_ROWS
    ratio_summary_text = format_specific_ratio_summary()
    extra_rows = 6 if truncated else 5
    if ratio_summary_text:
        extra_rows += 1
    total_rows = len(rows_to_show) + extra_rows
    img_width = sum(COL_WIDTHS) + 2 * PADDING
    img_height = total_rows * ROW_HEIGHT + 2 * PADDING

    img = Image.new("RGB", (img_width, img_height), "white")
    draw = ImageDraw.Draw(img)

    try:
        if sys.platform.startswith("win"):
            font = ImageFont.truetype("msyh.ttc", FONT_SIZE)
            header_font = ImageFont.truetype("msyh.ttc", HEADER_FONT_SIZE)
            name_font = ImageFont.truetype("msyh.ttc", NAME_FONT_SIZE)
        elif sys.platform.startswith("darwin"):
            font = ImageFont.truetype("Arial Unicode.ttf", FONT_SIZE)
            header_font = ImageFont.truetype("Arial Unicode.ttf", HEADER_FONT_SIZE)
            name_font = ImageFont.truetype("Arial Unicode.ttf", NAME_FONT_SIZE)
        else:
            font = ImageFont.truetype("DejaVuSans.ttf", FONT_SIZE)
            header_font = ImageFont.truetype("DejaVuSans.ttf", HEADER_FONT_SIZE)
            name_font = ImageFont.truetype("DejaVuSans.ttf", NAME_FONT_SIZE)
    except:
        font = ImageFont.load_default()
        header_font = ImageFont.load_default()
        name_font = ImageFont.load_default()

    x, y = PADDING, PADDING
    title_main = f"{report1} → {report2} ETF 持股变动统计表（按代码合并）"
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

    for row_index, item in enumerate(rows_to_show):
        x = PADDING
        row_data = [
            item["ts_code"], item["etf_name"], item["change_type"],
            str(item["hold1_amount"]), str(item["adjust_value1"]),
            str(item["hold2_amount"]), str(item["adjust_value2"]),
            item["holder_name"],
        ]
        for i, data in enumerate(row_data):
            if i == len(COL_NAMES) - 1:
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

    total_change = round(total_adj2 - total_adj1, 2)
    draw.text((PADDING, y), f"【{report1}折算后总市值(亿)】{total_adj1}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【{report2}折算后总市值(亿)】{total_adj2}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【持仓变动(亿)】{total_change}", font=font, fill="#e74c3c")

    img.save(OUTPUT_TABLE_IMAGE_FILE)
    print(f"\n✅ UI表格图片生成完成：{OUTPUT_TABLE_IMAGE_FILE}")


def query_top10_change():
    """主查询函数（双报告期+变动百分比+固定顺序排序+按代码合并+总变动计算）"""
    etf_map = get_combined_etfs()
    if not etf_map:
        print("❌ ETF 基础信息缓存为空，请先运行 import_etf_data.py 导入数据")
        return

    total = len(etf_map)
    keyword_str = ", ".join(KEY_WORD_RATIO.keys())
    print(f"开始分析 {total} 只 ETF")
    print(f"对比报告期：{REPORT_PERIOD1} → {REPORT_PERIOD2}")
    print(f"筛选包含「{keyword_str}」的份额变动...\n")

    match_results = []
    for code, name in etf_map.items():
        result = query_single_etf(code, name)
        if result:
            match_results.extend(result)

    print(f"拉取 {REPORT_TRADE_DATE1} / {REPORT_TRADE_DATE2} 全市场 ETF 收盘价...")
    price_map1 = get_daily_prices(REPORT_TRADE_DATE1)
    price_map2 = get_daily_prices(REPORT_TRADE_DATE2)
    if not price_map1:
        print(f"⚠️ {REPORT_TRADE_DATE1} 无行情数据（可能非交易日），期1市值按 0 处理")
    if not price_map2:
        print(f"⚠️ {REPORT_TRADE_DATE2} 无行情数据（可能非交易日），期2市值按 0 处理")

    for item in match_results:
        close1 = price_map1.get(item["ts_code"], 0)
        orig1 = round(item["hold1_amount"] * close1 / 1e8, 2) if close1 > 0 else 0
        adj1 = round(orig1 * item["ratio1"], 2)

        close2 = price_map2.get(item["ts_code"], 0)
        orig2 = round(item["hold2_amount"] * close2 / 1e8, 2) if close2 > 0 else 0
        adj2 = round(orig2 * item["ratio2"], 2)

        item.update({
            "original_value1": orig1, "adjust_value1": adj1,
            "original_value2": orig2, "adjust_value2": adj2,
        })

    print("\n" + "=" * 220)
    print(f"【{REPORT_PERIOD1} → {REPORT_PERIOD2}】份额变动查询完成！共找到 {len(match_results)} 条原始匹配数据")

    merged_results = merge_holders_by_stock(match_results)
    print(f"按代码合并后：共 {len(merged_results)} 只 ETF")
    print("=" * 220)

    if not merged_results:
        print(f"未查询到包含「{keyword_str}」的持有人数据（可调整 etf_client.KEY_WORD_RATIO）")
        return

    total_adj1 = round(sum(item["adjust_value1"] for item in merged_results), 2)
    total_adj2 = round(sum(item["adjust_value2"] for item in merged_results), 2)

    print(f"{'代码':<10} {'ETF名称':<12} {'变动类型':<12} {'期1份额(份)':<15} {'期1市值(亿)':<10} "
          f"{'期2份额(份)':<15} {'期2市值(亿)':<10} {'持有人名称':<32}")
    print("-" * 220)

    for item in merged_results:
        print(f"{item['ts_code']:<12} "
              f"{item['etf_name']:<12} "
              f"{item['change_type']:<14} "
              f"{item['hold1_amount']:<16} "
              f"{item['adjust_value1']:<12} "
              f"{item['hold2_amount']:<16} "
              f"{item['adjust_value2']:<12} "
              f"{item['holder_name']:<32}")

    print("-" * 220)
    print(f"【{REPORT_PERIOD1} 折算后总市值(亿)】{total_adj1}")
    print(f"【{REPORT_PERIOD2} 折算后总市值(亿)】{total_adj2}")
    print(f"【持仓变动(亿)】{round(total_adj2 - total_adj1, 2)}")

    generate_table_image(merged_results, total_adj1, total_adj2, REPORT_PERIOD1, REPORT_PERIOD2)


if __name__ == "__main__":
    start_time = time.time()
    query_top10_change()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
