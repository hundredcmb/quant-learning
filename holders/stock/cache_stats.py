"""缓存体检：统计股票十大股东原始缓存中各报告期的覆盖情况。

只读取本地缓存文件，不访问任何网络接口，用于快速掌握：
- 已缓存的财报日期（报告期）有哪些、时间跨度
- 每个报告期有数据的股票数量、原始记录条数
- 历史遗留的空占位条目（未披露公司的空结果，下次运行会自动重查自愈）
"""
import argparse
import datetime
import json
import os
import sys

# Windows 控制台编码兼容：避免 GBK 下 emoji 打印崩溃
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "tushare_top10_holders_raw.json")


def _load_cache(path: str) -> dict:
    if not os.path.exists(path):
        print(f"❌ 缓存文件不存在：{path}")
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"❌ 缓存文件加载失败：{e}")
        sys.exit(1)


def collect_stats(data: dict) -> list[dict]:
    """逐报告期统计：股票数 / 原始记录条数 / 空占位条目数（按报告期升序）"""
    rows = []
    for period in sorted(k for k in data if isinstance(k, str)):
        codes = data[period] or {}
        records = sum(len(v) for v in codes.values())
        empties = sum(1 for v in codes.values() if not v)
        rows.append({
            "period": period,
            "stocks": len(codes),
            "records": records,
            "empty_entries": empties,
        })
    return rows


def print_table(rows: list[dict], data: dict) -> None:
    """打印逐期统计表与汇总（去重股票总数、时间跨度、空占位告警）"""
    all_codes: set[str] = set()
    for codes in (data[p] or {} for p in data):
        all_codes.update(codes.keys())

    latest = max(rows, key=lambda r: r["period"])
    earliest = min(rows, key=lambda r: r["period"])
    span_days = (datetime.datetime.strptime(latest["period"], "%Y%m%d")
                 - datetime.datetime.strptime(earliest["period"], "%Y%m%d")).days

    print(f"\n{'报告期':<12} {'有数据股票数':<10} {'原始记录条数':<10} {'空占位条目':<8}")
    print("-" * 46)
    for r in rows:
        empty_note = f"{r['empty_entries']}" + (" ⚠️" if r["empty_entries"] else "")
        print(f"{r['period']:<12} {r['stocks']:<14} {r['records']:<13} {empty_note:<10}")
    print("-" * 46)

    total_periods = len(rows)
    total_stocks_slots = sum(r["stocks"] for r in rows)
    total_records = sum(r["records"] for r in rows)
    total_empty = sum(r["empty_entries"] for r in rows)
    print(f"共 {total_periods} 个报告期（{earliest['period']} ~ {latest['period']}，跨度约 {span_days // 365} 年多）")
    print(f"缓存覆盖去重后共 {len(all_codes)} 只股票；累计 {total_stocks_slots} 个「报告期×股票」数据集、"
          f"{total_records} 条股东记录")
    if total_empty:
        print(f"⚠️ 存在 {total_empty} 个空占位条目（历史遗留的未披露查询，读取时会自动重查自愈）")


def main() -> None:
    parser = argparse.ArgumentParser(description="股票十大股东缓存覆盖统计（只读本地缓存，不联网）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出统计结果")
    args = parser.parse_args()

    data = _load_cache(CACHE_FILE)
    rows = collect_stats(data)
    if args.json:
        print(json.dumps({"cache_file": CACHE_FILE, "periods": rows}, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("缓存为空")
        return
    print_table(rows, data)


if __name__ == "__main__":
    main()
