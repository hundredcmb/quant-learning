import os
import sys
import time
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFont

from tushare_client import (
    KEY_WORD_RATIO,
    OUTPUT_DIR,
    RAW_CACHE,
    get_adj_factors,
    get_combined_stocks,
    get_stock_close_price,
    query_single_stock,
    run_parallel_queries,
    save_raw_cache,
)

# Windows 控制台编码兼容：避免 GBK 下 emoji 打印崩溃
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

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

# 席位关键词-折算比例（公共配置见 tushare_client.KEY_WORD_RATIO；
# 如需本脚本单独调整，可在此处重新定义 KEY_WORD_RATIO 覆盖）

# 输出图片文件名（输出目录见 tushare_client.OUTPUT_DIR；合并版与普通版文件名区分）
OUTPUT_IMAGE_FILE = os.path.join(OUTPUT_DIR, f"股票组合收益统计_{REPORT_TRADE_DATE}_to_{NEW_TRADE_DATE}_合并版.png")
OUTPUT_SUMMARY_IMAGE_FILE = os.path.join(OUTPUT_DIR, f"股票组合收益统计_{REPORT_TRADE_DATE}_to_{NEW_TRADE_DATE}_合并版汇总.png")
# ====================================================


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
    按股票代码合并股东数据
    相同代码合并到一行，股东名称用分号隔开，持股数量/比例/持仓市值累加
    按日1折算持仓从大到小排序，方便查看持仓最重的股票
    """
    stock_groups = defaultdict(list)
    for item in match_results:
        stock_groups[item["ts_code"]].append(item)

    merged_results = []
    for ts_code, items in stock_groups.items():
        stock_name = items[0]["stock_name"]
        holder_names = [item["holder_name"] for item in items]
        merged_holder_names = "; ".join(holder_names)

        merged_item = {
            "ts_code": ts_code,
            "stock_name": stock_name,
            "holder_name": merged_holder_names,
            "hold_amount": sum(item["hold_amount"] for item in items),
            "hold_ratio": round(sum(item["hold_ratio"] for item in items), 2),
            "original_value": round(sum(item["original_value"] for item in items), 2),
            "adjust_value": round(sum(item["adjust_value"] for item in items), 2),
            "adjust_value_new": round(sum(item["adjust_value_new"] for item in items), 2),
            "has_corporate_action": any(item.get("has_corporate_action") for item in items),
        }
        merged_results.append(merged_item)

    merged_results.sort(key=lambda x: (-x["adjust_value"], x["ts_code"]))
    return merged_results


def generate_table_image(match_results, total_adjust_value, total_adjust_value_new, total_diff, return_rate):
    """生成与命令行一致的股票组合收益UI表格图片（按代码合并），标题含关键词+折算比例"""
    # 基础样式配置
    PADDING = 10
    ROW_HEIGHT = 40
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16
    NAME_FONT_SIZE = 12  # 股东名称列字号（略小，配合换行）
    NAME_MAX_LINES = 2  # 股东名称列最多显示行数
    NAME_LINE_HEIGHT = 17  # 股东名称列行高
    # 适配当前数据的列宽
    COL_WIDTHS = [100, 80, 140, 100, 120, 120, 120, 440]
    COL_NAMES = [
        "股票代码", "股票名称", "持股数量(股)", "持股比例(%)",
        "日1原始持仓(亿)", "日1折算持仓(亿)", "日2折算持仓(亿)", "股东名称"
    ]

    # 拼接关键词+折算比例文本
    ratio_text = ", ".join([f"{k}({v})" for k, v in KEY_WORD_RATIO.items()])

    # 计算画布尺寸（标题+关键词说明+表头+数据行+分隔线+4行汇总信息）
    has_any_adj = any(item.get("has_corporate_action") for item in match_results)
    total_rows = len(match_results) + 6 + (1 if has_any_adj else 0)
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
    title_main = f"{REPORT_TRADE_DATE} 到 {NEW_TRADE_DATE} 的股票组合收益统计（按代码合并，按日1折算持仓降序）"
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
            item['ts_code'],
            item['stock_name'] + ("＊" if item.get("has_corporate_action") else ""),
            f"{item['hold_amount']:,}",  # 千分位格式化
            f"{item['hold_ratio']:.2f}",
            f"{item['original_value']:.2f}",
            f"{item['adjust_value']:.2f}",
            f"{item['adjust_value_new']:.2f}",
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

    # 绘制总计信息（与控制台完全一致）
    draw.text((PADDING, y), f"【{REPORT_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【{NEW_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value_new:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【公允价值变动(亿)】{total_diff:.2f}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【收益率】{return_rate:.2f}%", font=font, fill="#e74c3c")
    if has_any_adj:
        # 注释说明放在最下面
        y += ROW_HEIGHT
        draw.text((PADDING, y), "＊ 表示期间发生分红/送转，日2折算持仓已按复权因子修正", font=font, fill="#8e44ad")

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
    img_height = 6 * ROW_HEIGHT + 2 * PADDING + 20  # 预留关键词说明换行空间

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
    title_main = f"{REPORT_TRADE_DATE} 到 {NEW_TRADE_DATE} 的股票组合收益统计（按代码合并）"
    draw.text((x, y), title_main, font=header_font, fill="#2c3e50")
    y += ROW_HEIGHT

    # 第二行：关键词+折算比例说明（与完整图片完全一致）
    title_sub = f"筛选关键词及折算比例：{ratio_text}"
    # 关键词说明过长时按像素宽度换行（最多两行）
    sub_lines = _wrap_text(title_sub, draw, font, img_width - 2 * PADDING, 2)
    for line_index, line in enumerate(sub_lines):
        draw.text((x, y + line_index * 20), line, font=font, fill="#8e44ad")
    y += len(sub_lines) * 20

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
    """主查询函数：单报告期筛选 + 两交易日公允价值变动与收益率（按股票代码合并）"""
    stock_map = get_combined_stocks(INDEX_CODES, INDEX_DATE)
    if not stock_map:
        return

    total = len(stock_map)
    keyword_str = ", ".join(KEY_WORD_RATIO.keys())
    index_str = ", ".join(INDEX_CODES)
    print(f"开始查询指数【{index_str}】共 {total} 只股票，筛选{REPORT_PERIOD}报告期包含「{keyword_str}」的持股...\n")

    match_results = run_parallel_queries(
        stock_map,
        lambda code, name: query_single_stock(code, name, REPORT_PERIOD, KEY_WORD_RATIO),
    )

    # 股价计算（原交易日 + 新交易日）
    if match_results:
        match_stock_codes = [item["ts_code"] for item in match_results]
        # 1. 查询原交易日收盘价
        price_map = get_stock_close_price(match_stock_codes, REPORT_TRADE_DATE)
        # 2. 查询新交易日收盘价
        price_map_new = get_stock_close_price(match_stock_codes, NEW_TRADE_DATE)

        # 3. 查询两个交易日全市场复权因子（用于把日2价格修正到日1同一复权系数水平）
        print(f"拉取 {REPORT_TRADE_DATE} / {NEW_TRADE_DATE} 全市场股票复权因子...")
        adj_map1 = get_adj_factors(REPORT_TRADE_DATE)
        adj_map2 = get_adj_factors(NEW_TRADE_DATE)
        if not adj_map1 or not adj_map2:
            print("⚠️ 复权因子获取失败（可能非交易日/接口权限不足），本次不进行价格修正")

        for item in match_results:
            hold_amount_val = item["hold_amount"]
            ratio = item["ratio"]

            # 原交易日计算逻辑
            close = price_map.get(item["ts_code"], 0)
            original_val = round(hold_amount_val * close / 100000000, 2) if close > 0 else 0
            adjust_val = round(original_val * ratio, 2)

            # 新交易日计算逻辑
            # 复权修正：日2价格换算到日1相同复权系数水平（送转/分红时股数已变，价格需同口径）
            factor1 = adj_map1.get(item["ts_code"], 0)
            factor2 = adj_map2.get(item["ts_code"], 0)
            adj_ratio = factor2 / factor1 if factor1 > 0 and factor2 > 0 else 1.0
            close_new_raw = price_map_new.get(item["ts_code"], 0)
            close_new = close_new_raw * adj_ratio if close_new_raw > 0 else 0
            original_val_new = round(hold_amount_val * close_new / 100000000, 2) if close_new > 0 else 0
            adjust_val_new = round(original_val_new * ratio, 2)

            # 存入数据
            item["original_value"] = original_val    # 日1原始持仓
            item["adjust_value"] = adjust_val        # 日1折算持仓
            item["adjust_value_new"] = adjust_val_new  # 日2折算持仓
            item["has_corporate_action"] = abs(adj_ratio - 1.0) > 1e-9

    # 输出结果
    print("\n" + "=" * 200)
    print(f"【{REPORT_PERIOD}】报告期查询完成！共找到 {len(match_results)} 个匹配席位")

    # 按股票代码合并股东
    merged_results = merge_holders_by_stock(match_results)
    print(f"按股票代码合并后：共 {len(merged_results)} 只股票")
    print("=" * 200)

    if not merged_results:
        print(f"未查询到包含「{keyword_str}」的股东数据")
        save_raw_cache(RAW_CACHE)
        return

    # 汇总计算
    total_adjust_value = round(sum(item["adjust_value"] for item in merged_results), 2)
    total_adjust_value_new = round(sum(item["adjust_value_new"] for item in merged_results), 2)
    total_diff = round(total_adjust_value_new - total_adjust_value, 2)
    if total_adjust_value > 0:
        return_rate = round(total_diff / total_adjust_value * 100, 2)
    else:
        return_rate = 0.00

    print(f"{'股票代码':<7} {'股票名称':<8} {'持股数量(股)':<13} {'持股比例(%)':<5} "
          f"{'日1原始持仓(亿)':<9} {'日1折算持仓(亿)':<9} {'日2折算持仓(亿)':<9} {'股东名称':<32}")
    print("-" * 200)
    for item in merged_results:
        name_display = item['stock_name'] + ("＊" if item.get("has_corporate_action") else "")
        print(f"{item['ts_code']:<10} "
              f"{name_display:<8} "
              f"{item['hold_amount']:<18} "
              f"{item['hold_ratio']:<10} "
              f"{item['original_value']:<14} "
              f"{item['adjust_value']:<14} "
              f"{item['adjust_value_new']:<14} "
              f"{item['holder_name']:<32}")
    print("-" * 200)
    print(f"【{REPORT_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value}")
    print(f"【{NEW_TRADE_DATE}折算后总持仓(亿)】{total_adjust_value_new}")
    print(f"【公允价值变动(亿)】{total_diff}")
    print(f"【收益率】{return_rate}%")
    if any(item.get("has_corporate_action") for item in merged_results):
        print("＊ 表示期间发生分红/送转，日2折算持仓已按复权因子修正")

    # 生成两张图片：完整表格版 + 汇总版
    generate_table_image(merged_results, total_adjust_value, total_adjust_value_new, total_diff, return_rate)
    generate_summary_image(total_adjust_value, total_adjust_value_new, total_diff, return_rate)

    # 最终：将所有缓存的原始数据持久化到文件
    save_raw_cache(RAW_CACHE)


if __name__ == "__main__":
    start_time = time.time()
    query_top10()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
