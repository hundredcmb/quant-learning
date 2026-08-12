import os
import sys
import time
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFont

from tushare_client import (
    KEY_WORD_RATIO,
    OUTPUT_DIR,
    RAW_CACHE,
    get_combined_stocks,
    get_stock_close_price,
    get_stock_top10_raw,
    run_parallel_queries,
    save_raw_cache,
)

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

# 席位关键词-折算比例（公共配置见 tushare_client.KEY_WORD_RATIO；
# 如需本脚本单独调整，可在此处重新定义 KEY_WORD_RATIO 覆盖）

# 输出图片文件名（输出目录见 tushare_client.OUTPUT_DIR）
OUTPUT_TABLE_IMAGE_FILE = os.path.join(OUTPUT_DIR, "持股变动表格.png")
# ====================================================


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


def merge_holders_by_stock(match_results):
    """
    按股票代码合并股东数据
    相同股票代码合并到一行，股东名称用分号隔开，持股数量和金额累加
    变动类型基于合并后的总持股量计算整体变动百分比
    """
    stock_groups = defaultdict(list)

    # 按股票代码分组
    for item in match_results:
        stock_groups[item["ts_code"]].append(item)

    merged_results = []

    for ts_code, items in stock_groups.items():
        # 取第一个item的基本信息
        stock_name = items[0]["stock_name"]

        # 合并股东名称
        holder_names = [item["holder_name"] for item in items]
        merged_holder_names = "; ".join(holder_names)

        # 累加持股数量和折算金额
        total_hold1 = sum(item["hold1_amount"] for item in items)
        total_hold2 = sum(item["hold2_amount"] for item in items)
        total_adjust1 = sum(item["adjust_value1"] for item in items)
        total_adjust2 = sum(item["adjust_value2"] for item in items)

        # 计算合并后的总变动百分比
        if total_hold1 == 0:
            # 全部都是新增
            merged_change_type = "新增"
            min_sort_rank = 0
        elif total_hold2 == 0:
            # 全部都是退出
            merged_change_type = "退出"
            min_sort_rank = 4
        else:
            # 计算总变动百分比
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
            "stock_name": stock_name,
            "holder_name": merged_holder_names,
            "change_type": merged_change_type,
            "sort_rank": min_sort_rank,
            "hold1_amount": total_hold1,
            "hold2_amount": total_hold2,
            "adjust_value1": round(total_adjust1, 2),
            "adjust_value2": round(total_adjust2, 2)
        }

        merged_results.append(merged_item)

    # 按原排序规则排序
    merged_results.sort(key=lambda x: x["sort_rank"])

    return merged_results


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
    """生成与命令行一致的持股变动UI表格图片，标题含关键词+折算比例"""
    # 基础样式配置
    PADDING = 10
    ROW_HEIGHT = 40
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16
    NAME_FONT_SIZE = 12  # 股东名称列字号（略小，配合换行）
    NAME_MAX_LINES = 2  # 股东名称列最多显示行数
    NAME_LINE_HEIGHT = 17  # 股东名称列行高
    COL_WIDTHS = [100, 80, 120, 160, 90, 160, 90, 440]
    COL_NAMES = ["股票代码", "股票名称", "变动类型", "期1持股(股)", "期1折算(亿)", "期2持股(股)", "期2折算(亿)",
                 "股东名称"]

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
        # 兜底默认字体
        font = ImageFont.load_default()
        header_font = ImageFont.load_default()
        name_font = ImageFont.load_default()

    # 第一行大标题
    x, y = PADDING, PADDING
    title_main = f"{report1} → {report2} 持股变动统计表"
    draw.text((x, y), title_main, font=header_font, fill="#2c3e50")
    y += ROW_HEIGHT

    # 第二行：关键词+折算比例说明
    title_sub = f"筛选关键词及折算比例：{ratio_text}"
    # 关键词说明过长时按像素宽度换行（最多两行，两行共 40px 与 ROW_HEIGHT 一致）
    sub_lines = _wrap_text(title_sub, draw, font, img_width - 2 * PADDING, 2)
    for line_index, line in enumerate(sub_lines):
        draw.text((x, y + line_index * 20), line, font=font, fill="#8e44ad")
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
            if i == len(COL_NAMES) - 1:
                # 股东名称列：小号字体 + 换行，最多 NAME_MAX_LINES 行，垂直居中
                name_lines = _wrap_text(data, draw, name_font, COL_WIDTHS[i] - 10, NAME_MAX_LINES)
                lines_height = len(name_lines) * NAME_LINE_HEIGHT
                start_y = y + (ROW_HEIGHT - lines_height) // 2
                for line_index, line in enumerate(name_lines):
                    draw.text((x + 5, start_y + line_index * NAME_LINE_HEIGHT), line,
                              font=name_font, fill="#2c3e50")
                x += COL_WIDTHS[i]
                continue
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


def query_top10_change():
    """主查询函数（双报告期+变动百分比+固定顺序排序+按股票合并+总变动计算）"""
    stock_map = get_combined_stocks(INDEX_CODES, INDEX_DATE)
    if not stock_map:
        return

    total = len(stock_map)
    keyword_str = ", ".join(KEY_WORD_RATIO.keys())
    index_str = ", ".join(INDEX_CODES)
    print(f"开始查询指数【{index_str}】共 {total} 只股票")
    print(f"对比报告期：{REPORT_PERIOD1} → {REPORT_PERIOD2}")
    print(f"筛选包含「{keyword_str}」的持股变动...\n")

    match_results = run_parallel_queries(stock_map, query_single_stock)

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
    print(f"【{REPORT_PERIOD1} → {REPORT_PERIOD2}】持股变动查询完成！共找到 {len(match_results)} 条原始匹配数据")

    # 按股票代码合并股东（含总变动计算）
    merged_results = merge_holders_by_stock(match_results)
    print(f"按股票代码合并后：共 {len(merged_results)} 只股票")
    print("=" * 210)

    if not merged_results:
        print(f"未查询到包含「{keyword_str}」的股东数据")
        save_raw_cache(RAW_CACHE)
        return

    total_adj1 = round(sum(item["adjust_value1"] for item in merged_results), 2)
    total_adj2 = round(sum(item["adjust_value2"] for item in merged_results), 2)

    print(f"{'股票代码':<7} {'股票名称':<8} {'变动类型':<11}"
          f"{'期1持股(股)':<12} {'期1折算(亿)':<6} "
          f"{'期2持股(股)':<12} {'期2折算(亿)':<8} "
          f"{'股东名称':<32}")
    print("-" * 210)

    for item in merged_results:
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

    # 调用图片生成函数（使用合并后的数据）
    generate_table_image(merged_results, total_adj1, total_adj2, REPORT_PERIOD1, REPORT_PERIOD2)

    save_raw_cache(RAW_CACHE)


if __name__ == "__main__":
    start_time = time.time()
    query_top10_change()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
