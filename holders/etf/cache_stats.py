"""缓存体检：统计 ETF 十大持有人缓存中各报告期的覆盖情况。

只读取本地缓存文件，不访问任何网络接口：
- 持有人缓存 `etf_top10_holders_raw.json`：各报告期（半年报/年报）的 ETF 数量与记录条数
- 基础信息缓存 `etf_basic.json`：已录入的 ETF 总数、从未出现持有人数据的 ETF 数
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
HOLDERS_CACHE_FILE = os.path.join(BASE_DIR, "etf_top10_holders_raw.json")
BASIC_CACHE_FILE = os.path.join(BASE_DIR, "etf_basic.json")


def _load_cache(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"❌ 缓存文件加载失败（{path}）：{e}")
        sys.exit(1)


def collect_stats(holders: dict) -> list[dict]:
    """逐报告期统计：ETF 数 / 记录条数 / 空占位条目数（按报告期升序）"""
    rows = []
    for period in sorted(k for k in holders if isinstance(k, str)):
        codes = holders[period] or {}
        records = sum(len(v) for v in codes.values())
        empties = sum(1 for v in codes.values() if not v)
        rows.append({
            "period": period,
            "etfs": len(codes),
            "records": records,
            "empty_entries": empties,
        })
    return rows


def print_table(rows: list[dict], holders: dict, basic: dict) -> None:
    all_codes: set[str] = set()
    for codes in (holders[p] or {} for p in holders):
        all_codes.update(codes.keys())

    basic_total = len(basic)
    never_holders = sorted(set(basic) - all_codes)

    print(f"\n{'报告期':<12} {'有数据ETF数':<10} {'记录条数':<8} {'空占位条目':<8}")
    print("-" * 42)
    for r in rows:
        empty_note = f"{r['empty_entries']}" + (" ⚠️" if r["empty_entries"] else "")
        print(f"{r['period']:<12} {r['etfs']:<13} {r['records']:<11} {empty_note:<10}")
    print("-" * 42)

    total_records = sum(r["records"] for r in rows)
    print(f"持有人缓存共 {len(rows)} 个报告期、去重后覆盖 {len(all_codes)} 只 ETF、{total_records} 条持有人记录")
    print(f"基础信息缓存已录入 {basic_total} 只 ETF；"
          f"其中 {len(all_codes)} 只有持有人数据，{len(never_holders)} 只至今没有任何持有人录入")

    earliest = min((r["period"] for r in rows), default="")
    latest = max((r["period"] for r in rows), default="")
    if earliest and latest and earliest != latest:
        span_days = (datetime.datetime.strptime(latest, "%Y%m%d")
                     - datetime.datetime.strptime(earliest, "%Y%m%d")).days
        print(f"时间跨度：{earliest} ~ {latest}（约 {span_days // 365} 年多）")

    if len(never_holders) <= 20:
        for code in never_holders:
            name = (basic.get(code) or {}).get("name", "")
            print(f"   未录入持有人：{code}  {name}")
    elif never_holders:
        show = ", ".join(f"{c}{(basic.get(c) or {}).get('name', '')}" for c in never_holders[:10])
        print(f"   未录入较多，仅列前 10 个：{show} …")


def main() -> None:
    parser = argparse.ArgumentParser(description="ETF 十大持有人缓存覆盖统计（只读本地缓存，不联网）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出统计结果")
    args = parser.parse_args()

    holders = _load_cache(HOLDERS_CACHE_FILE)
    basic = _load_cache(BASIC_CACHE_FILE)
    rows = collect_stats(holders)
    if args.json:
        print(json.dumps({
            "holders_cache_file": HOLDERS_CACHE_FILE,
            "basic_cache_file": BASIC_CACHE_FILE,
            "basic_total": len(basic),
            "covered_etfs": len({c for codes in holders.values() for c in (codes or {})}),
            "periods": rows,
        }, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("持有人缓存为空（请先运行 import_etf_data.py 导入 Excel 数据）")
        return
    print_table(rows, holders, basic)


if __name__ == "__main__":
    main()
