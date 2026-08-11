import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from etf_client import (
    KEY_WORD_RATIO,
    OUTPUT_DIR,
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

            # 计算份额变动百分比 保留2位小数
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


def generate_table_image(match_results, total_adj1, total_adj2, report1, report2):
    """生成与命令行一致的 ETF 持股变动表格图片，标题含关键词+折算比例"""
    # 基础样式配置
    PADDING = 10
    ROW_HEIGHT = 30
    FONT_SIZE = 14
    HEADER_FONT_SIZE = 16
    COL_WIDTHS = [100, 120, 140, 150, 120, 150, 120, 360]
    COL_NAMES = ["代码", "ETF名称", "变动类型", "期1份额(份)", "期1市值(亿)", "期2份额(份)", "期2市值(亿)", "持有人名称"]

    # 拼接关键词+折算比例文本
    ratio_text = ", ".join([f"{k}({v})" for k, v in KEY_WORD_RATIO.items()])

    rows_to_show = match_results[:MAX_TABLE_ROWS]
    truncated = len(match_results) > MAX_TABLE_ROWS
    total_rows = len(rows_to_show) + (6 if truncated else 5)
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
        font = ImageFont.load_default()
        header_font = ImageFont.load_default()

    # 第一行大标题
    x, y = PADDING, PADDING
    title_main = f"{report1} → {report2} ETF 持股变动统计表"
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

    # 分隔线
    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 8

    # 数据行
    for item in rows_to_show:
        x = PADDING
        row_data = [
            item["ts_code"], item["etf_name"], item["change_type"],
            str(item["hold1_amount"]), str(item["adjust_value1"]),
            str(item["hold2_amount"]), str(item["adjust_value2"]),
            item["holder_name"],
        ]
        for i, data in enumerate(row_data):
            draw.text((x + 5, y + 5), str(data), font=font, fill="#2c3e50")
            x += COL_WIDTHS[i]
        y += ROW_HEIGHT

    # 底部分隔线
    draw.line([(PADDING, y), (img_width - PADDING, y)], fill="#95a5a6", width=1)
    y += 15
    if truncated:
        draw.text((PADDING, y), f"……共 {len(match_results)} 条，仅展示前 {MAX_TABLE_ROWS} 条，完整数据见控制台……",
                  font=font, fill="#8e44ad")
        y += ROW_HEIGHT

    # 总计信息
    total_change = round(total_adj2 - total_adj1, 2)
    draw.text((PADDING, y), f"【{report1}折算后总市值(亿)】{total_adj1}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【{report2}折算后总市值(亿)】{total_adj2}", font=font, fill="#e74c3c")
    y += ROW_HEIGHT
    draw.text((PADDING, y), f"【持仓变动(亿)】{total_change}", font=font, fill="#e74c3c")

    # 保存图片
    img.save(OUTPUT_TABLE_IMAGE_FILE)
    print(f"\n✅ UI表格图片生成完成：{OUTPUT_TABLE_IMAGE_FILE}")


def query_top10_change():
    """主查询函数（双报告期+变动百分比+固定顺序排序）"""
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

    # 按指定顺序排序：新增0 → 增持1 → 不变2 → 减持3 → 退出4
    match_results.sort(key=lambda x: x["sort_rank"])

    # 拉两个交易日全市场收盘价（各 1 次请求）
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
    print(f"【{REPORT_PERIOD1} → {REPORT_PERIOD2}】份额变动查询完成！共找到 {len(match_results)} 条匹配数据")
    print("=" * 220)

    if not match_results:
        print(f"未查询到包含「{keyword_str}」的持有人数据（可调整 etf_client.KEY_WORD_RATIO）")
        return

    total_adj1 = round(sum(item["adjust_value1"] for item in match_results), 2)
    total_adj2 = round(sum(item["adjust_value2"] for item in match_results), 2)

    print(f"{'代码':<10} {'ETF名称':<12} {'变动类型':<12} {'期1份额(份)':<15} {'期1市值(亿)':<10} "
          f"{'期2份额(份)':<15} {'期2市值(亿)':<10} {'持有人名称':<32}")
    print("-" * 220)

    for item in match_results:
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

    generate_table_image(match_results, total_adj1, total_adj2, REPORT_PERIOD1, REPORT_PERIOD2)


if __name__ == "__main__":
    start_time = time.time()
    query_top10_change()
    print(f"\n总耗时：{round(time.time() - start_time, 2)} 秒")
