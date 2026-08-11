"""ETF 数据导入脚本：把 Excel 模板中的 ETF 基础信息 + 十大持有人导入本地缓存。

Excel 模板格式（单工作表，8 列）：
    证券代码 | 证券简称 | 基金成立日 | 年度 | 持有人排名 | 持有人名称 | 持有份额 亿 | 持有比例 %

说明：
- ETF 十大持有人与基础信息不从 Tushare 获取，全部以本脚本导入的缓存为准
- 代码兼容规则：基础信息缓存的 key 与持有人缓存记录的 ts_code 统一为**无后缀代码**（如 `159001`）；
  Excel 原始代码（如 `159001.OF`）保存在基础信息 value 的 `import_code` 字段，
  tushare 代码（如 `159001.SZ`）保存在 value 的 `ts_code` 字段（拉取日线数据时更新）
- 持有人缓存默认 `holders/etf_top10_holders_raw.json`，结构与股票缓存
  `holders/tushare_top10_holders_raw.json` 完全一致：{报告期: {代码: [持有人记录]}}
    {
      "20251231": {
        "159001": [
          {"ts_code": "159001", "rank": 1, "holder_name": "...", "hold_amount": 560800, "hold_ratio": 3.8}
        ]
      }
    }
  记录字段与股票一致（ts_code / holder_name / hold_amount / hold_ratio），
  仅额外多一个 rank（Excel 模板中的持有人排名，便于展示与排序）
- ETF 基础信息单独存放 `holders/etf_basic.json`：
    {
      "159001": {
        "name": "易方达保证金A",
        "found_date": "2013-03-29",
        "import_code": "159001.OF",
        "ts_code": ""
      }
    }
  （股票基础信息走 Tushare 接口、无需缓存；ETF 基础信息按约定只从缓存读取，故单独成文件）
- 持有份额在 Excel 中是“亿份”，导入时统一转换为“份”（hold_amount = 亿份 * 1e8），
  与股票缓存语义一致，后续市值计算可直接复用 hold_amount * close / 1e8
- 冲突策略：--on-conflict overwrite（覆盖旧数据）或 keep（保留旧数据），
  默认取脚本顶部 DEFAULT_ON_CONFLICT

用法：
    C:\\veighna_studio\\python.exe holders\\import_etf_data.py "C:\\path\\ETF1.xlsx"
    C:\\veighna_studio\\python.exe holders\\import_etf_data.py "C:\\path\\ETF1.xlsx" --on-conflict overwrite
    不传 Excel 路径时，默认读取下方 DEFAULT_EXCEL_FILE 指定的文件
"""
import argparse
import json
import os
import sys
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 持有人缓存（结构与股票缓存一致）
DEFAULT_CACHE_FILE = os.path.join(BASE_DIR, "etf_top10_holders_raw.json")
# ETF 基础信息缓存（代码、名称、成立日）
DEFAULT_BASIC_CACHE_FILE = os.path.join(BASE_DIR, "etf_basic.json")

# 默认 Excel 模板路径：可在此手动指定，运行时无需再传参
DEFAULT_EXCEL_FILE = os.path.join(BASE_DIR, "etf_data_example.xlsx")

# 默认冲突策略：overwrite=覆盖旧的，keep=保留旧的（可在此手动指定，命令行 --on-conflict 可覆盖）
DEFAULT_ON_CONFLICT = "keep"

# Excel 列名（模板暂定格式）
COL_CODE = "证券代码"
COL_NAME = "证券简称"
COL_FOUND_DATE = "基金成立日"
COL_PERIOD = "年度"
COL_RANK = "持有人排名"
COL_HOLDER = "持有人名称"
COL_AMOUNT_YI = "持有份额 亿"
COL_RATIO = "持有比例 %"


def parse_period(value) -> str | None:
    """报告期转 YYYYMMDD：支持日期、'YYYY-MM-DD'、'YYYYMMDD'、数字"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        return digits
    if len(digits) == 10:  # YYYY-MM-DD 等格式
        return digits[:8]
    return None


def parse_date(value) -> str | None:
    """日期转 YYYY-MM-DD 字符串"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text[:10]


def load_cache(cache_file: str) -> dict:
    """加载缓存（普通 dict），文件不存在或损坏时返回 {}"""
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ 缓存加载失败（{e}），按空缓存处理")
        return {}


def save_cache(cache_file: str, data: dict) -> None:
    """保存缓存"""
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_from_excel(excel_path: str) -> tuple[dict, dict]:
    """解析 Excel，返回 (etf_basic, holders)：
    etf_basic: {无后缀代码: {"name", "found_date", "import_code", "ts_code"}}
    holders:   {period: {code: [持有人记录列表]}}
    """
    df = pd.read_excel(excel_path)
    missing_cols = [c for c in (
        COL_CODE, COL_NAME, COL_FOUND_DATE, COL_PERIOD,
        COL_RANK, COL_HOLDER, COL_AMOUNT_YI, COL_RATIO,
    ) if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Excel 缺少列: {missing_cols}，请检查模板格式")

    etf_basic: dict = {}
    holders: dict = {}
    skipped = 0

    for idx, row in df.iterrows():
        code = str(row[COL_CODE]).strip()  # 导入格式代码，如 159001.OF
        code_key = code.split(".")[0] if "." in code else code  # 无后缀代码，如 159001
        period = parse_period(row[COL_PERIOD])
        holder_name = str(row[COL_HOLDER]).strip()

        if not code:
            skipped += 1
            continue

        # ETF 基础信息（同一代码重复出现时以最后一个非空为准）
        # key = 无后缀代码；value 记录导入格式代码（导入时更新）与 tushare 代码（拉取日线时更新）
        name = str(row[COL_NAME]).strip() if pd.notna(row[COL_NAME]) else ""
        found_date = parse_date(row[COL_FOUND_DATE])
        if code_key not in etf_basic:
            etf_basic[code_key] = {
                "name": name,
                "found_date": found_date or "",
                "import_code": code,
                "ts_code": "",
            }
        else:
            if name:
                etf_basic[code_key]["name"] = name
            if found_date:
                etf_basic[code_key]["found_date"] = found_date
            etf_basic[code_key]["import_code"] = code  # 导入时更新导入格式代码

        if not period or not holder_name:
            skipped += 1
            continue

        # 持有份额：亿份 -> 份；持有比例：%
        # 使用 Decimal 精确换算，避免 float 二进制误差导致偶发差 1 份
        amount_yi = row[COL_AMOUNT_YI]
        ratio = row[COL_RATIO]
        try:
            hold_amount = int(
                (Decimal(str(float(amount_yi))) * Decimal("1e8"))
                .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            hold_ratio = float(
                Decimal(str(float(ratio))).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            )
        except (TypeError, ValueError):
            print(f"⚠️ 第 {idx + 2} 行份额/比例非数值，已跳过：{code} {holder_name}")
            skipped += 1
            continue

        # 记录字段与股票缓存一致（ts_code/holder_name/hold_amount/hold_ratio），另含 rank；
        # ts_code 为无后缀代码，与 tushare 代码的映射见 etf_basic.json
        record = {
            "ts_code": code_key,
            "rank": int(row[COL_RANK]),
            "holder_name": holder_name,
            "hold_amount": hold_amount,
            "hold_ratio": hold_ratio,
        }
        holders.setdefault(period, {}).setdefault(code_key, []).append(record)

    # 按排名排序（模板偶有不足 10 行的情况，保持原样）
    for period, code_map in holders.items():
        for code in code_map:
            code_map[code].sort(key=lambda r: r["rank"])

    print(f"解析完成：ETF {len(etf_basic)} 只，持有人记录 "
          f"{sum(len(v) for m in holders.values() for v in m.values())} 条，跳过 {skipped} 行")
    return etf_basic, holders


def merge_basic(cache_basic: dict, new_basic: dict, on_conflict: str) -> tuple[dict, dict]:
    """ETF 基础信息合并进 etf_basic.json"""
    stats = {"added": 0, "overwritten": 0, "kept": 0, "identical": 0}
    for code, info in new_basic.items():
        if code not in cache_basic:
            cache_basic[code] = info
            stats["added"] += 1
        elif cache_basic[code] == info:
            stats["identical"] += 1
        elif on_conflict == "overwrite":
            cache_basic[code] = info
            stats["overwritten"] += 1
        else:
            stats["kept"] += 1
    return cache_basic, stats


def merge_holders(cache_holders: dict, new_holders: dict, on_conflict: str) -> tuple[dict, dict]:
    """十大持有人合并进 etf_top10_holders_raw.json（结构与股票缓存一致）"""
    stats = {"added": 0, "overwritten": 0, "kept": 0, "identical": 0}
    for period, code_map in new_holders.items():
        for code, records in code_map.items():
            old_records = cache_holders.get(period, {}).get(code)
            if old_records is None:
                cache_holders.setdefault(period, {})[code] = records
                stats["added"] += 1
            elif old_records == records:
                stats["identical"] += 1
            elif on_conflict == "overwrite":
                cache_holders[period][code] = records
                stats["overwritten"] += 1
            else:
                stats["kept"] += 1
    return cache_holders, stats


def print_stats(stats: dict, dry_run: bool) -> None:
    """打印合并统计"""
    tag = "（预演，未写文件）" if dry_run else ""
    print(f"\n===== 合并结果{tag} =====")
    for section in ("etf_basic", "holders"):
        s = stats[section]
        label = "ETF 基础信息" if section == "etf_basic" else "持有人记录(报告期×代码)"
        print(f"{label}: 新增 {s['added']} | 覆盖 {s['overwritten']} | "
              f"保留旧 {s['kept']} | 内容相同跳过 {s['identical']}")


def main() -> None:
    # Windows 控制台编码兼容：避免 GBK 下 emoji 打印崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="ETF 基础信息 + 十大持有人导入缓存")
    parser.add_argument("excel", nargs="?", default=None,
                        help="Excel 模板文件路径（默认取脚本顶部 DEFAULT_EXCEL_FILE）")
    parser.add_argument("--cache", default=DEFAULT_CACHE_FILE,
                        help=f"持有人缓存文件路径（默认 {DEFAULT_CACHE_FILE}）")
    parser.add_argument("--basic-cache", default=DEFAULT_BASIC_CACHE_FILE,
                        help=f"ETF 基础信息缓存文件路径（默认 {DEFAULT_BASIC_CACHE_FILE}）")
    parser.add_argument(
        "--on-conflict",
        choices=["overwrite", "keep"],
        default=None,
        help="遇到冲突时：overwrite=覆盖旧的，keep=保留旧的（默认取脚本顶部 DEFAULT_ON_CONFLICT）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只解析和预演，不写文件")
    args = parser.parse_args()

    excel_path = args.excel or DEFAULT_EXCEL_FILE
    if not excel_path:
        print("❌ 未指定 Excel 路径：请在命令行传入，或在脚本顶部设置 DEFAULT_EXCEL_FILE")
        sys.exit(1)
    if not os.path.exists(excel_path):
        print(f"❌ Excel 文件不存在：{excel_path}")
        sys.exit(1)

    on_conflict = args.on_conflict or DEFAULT_ON_CONFLICT
    print(f"冲突策略：{on_conflict}（{'覆盖旧数据' if on_conflict == 'overwrite' else '保留旧数据'}）")

    cache_holders = load_cache(args.cache)
    cache_basic = load_cache(args.basic_cache)
    new_basic, new_holders = build_from_excel(excel_path)

    cache_basic, stats_basic = merge_basic(cache_basic, new_basic, on_conflict)
    cache_holders, stats_holders = merge_holders(cache_holders, new_holders, on_conflict)
    print_stats({"etf_basic": stats_basic, "holders": stats_holders}, args.dry_run)

    if args.dry_run:
        return

    save_cache(args.basic_cache, cache_basic)
    save_cache(args.cache, cache_holders)
    print(f"✅ 已写入持有人缓存：{args.cache}")
    print(f"✅ 已写入基础信息缓存：{args.basic_cache}")


if __name__ == "__main__":
    main()
