"""
行情/市值 SQLite 持久层补数与体检入口脚本

data/market.db 由 MarketDataProvider **写穿自动生长**(内存 → SQLite → 网络三级查找,
网络拉回非空即入库, 见 market_store), 日常无需运行本脚本; 本脚本只用于三件事:

1. 手动预热大窗口(如给区间榜/估值走势铺数据): --backfill 起止日期(YYYYMMDD),
   只补库内缺失的交易日、已完整的跳过; 一年约 240 交易日 × 2 接口, 按 7.5 次/秒
   限流约 1~2 分钟(两接口各自独立限流, 本脚本串行拉取求稳)
2. 强制重拉指定日期(应对 Tushare 偶发的历史数据修正): --force 日期列表,
   先清库内该日全部痕迹再走网络重拉覆盖
3. 体检: 不带参数即输出覆盖统计与行数对账(fetch_log.row_count vs 表内实数);
   --check --sample N 额外抽 N 个日期与网络实拉结果逐字段比对(每日期 2 次请求)

用法(在仓库根目录):
    .venv\\Scripts\\python.exe shenwan_industry\\market_cache.py                        # 覆盖统计+行数对账
    .venv\\Scripts\\python.exe shenwan_industry\\market_cache.py --backfill 20240101 20241231
    .venv\\Scripts\\python.exe shenwan_industry\\market_cache.py --force 20250407 20250408
    .venv\\Scripts\\python.exe shenwan_industry\\market_cache.py --check --sample 3     # 含网络抽查
"""

import argparse
import logging
import random
import sys
from datetime import datetime
from pathlib import Path

import tushare as ts

try:
    from . import market_data as market_data_module
    from .market_data import MarketDataProvider
    from .market_store import MarketStore
except ImportError:
    import market_data as market_data_module
    from market_data import MarketDataProvider
    from market_store import MarketStore

# token 配置在仓库根公共模块（与 holders 共享同一份 .quant-learning/settings.json）
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from config_store import config_path, get_token  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shenwan_industry.market_cache")


def _build_provider() -> MarketDataProvider:
    token = get_token()
    if not token:
        raise ValueError(
            "未配置 Tushare token，请先运行 Web 服务并在页面右上角填写保存 token；"
            "或直接编辑本地配置文件: " + str(config_path())
        )
    return MarketDataProvider(ts.pro_api(token=token))


def _fetch_day(provider: MarketDataProvider, day: str) -> tuple[int, int]:
    """拉取单日两接口(命中库则零网络), 返回 (daily 行数, daily_basic 行数)"""
    dt = datetime.strptime(day, "%Y%m%d")
    pct_map = provider.get_ts_code_to_pct_chg(dt)
    rows = provider.daily_basic_rows(dt)
    return len(pct_map), len(rows)


def _run_backfill(provider: MarketDataProvider, store: MarketStore, start: str, end: str) -> None:
    days = provider.get_trading_days(start, end)
    if not days:
        logger.info(f"{start}~{end} 无交易日")
        return
    done_daily = set(store.logged_dates("daily"))
    done_basic = set(store.logged_dates("daily_basic"))
    todo = [d for d in days if d not in done_daily or d not in done_basic]
    logger.info(f"回填 {start}~{end}: 交易日 {len(days)} 个, 库内已完整 {len(days) - len(todo)} 个, 待拉取 {len(todo)} 个")
    t0 = datetime.now()
    empty_days = []
    for i, day in enumerate(todo, 1):
        n_daily, n_basic = _fetch_day(provider, day)
        if n_daily == 0 and n_basic == 0:
            empty_days.append(day)  # 未出数(如今日盘前)或接口当日无数据——不落库, 下次自动重试
        if i % 20 == 0 or i == len(todo):
            elapsed = (datetime.now() - t0).total_seconds()
            logger.info(f"进度 {i}/{len(todo)} ({elapsed:.0f}s)")
    if empty_days:
        logger.info(f"以下 {len(empty_days)} 个交易日两接口均 0 行(未出数/无数据, 未落库): {', '.join(empty_days)}")
    logger.info("回填完成")


def _run_force(provider: MarketDataProvider, store: MarketStore, days: list[str]) -> None:
    for day in days:
        datetime.strptime(day, "%Y%m%d")  # 格式校验
    for day in days:
        store.purge(day)
        n_daily, n_basic = _fetch_day(provider, day)
        logger.info(f"已强制重拉 {day}: daily {n_daily} 行, daily_basic {n_basic} 行")


def _run_check(provider_factory, store: MarketStore, sample_n: int | None) -> None:
    stats = store.stats()
    print("\n===== 行情 SQLite 持久层体检 =====")
    print(f"库文件: {stats['db_path']} ({stats['db_size_mb']} MB)")
    for api in (
        "daily", "daily_basic", "dividend_ex",
        "fina_indicator_vip", "balancesheet_vip", "express_vip", "index_weight",
    ):
        s = stats[api]
        rng = f"{s['first']}~{s['last']}" if s["first"] else "无"
        label = "个交易日" if api in ("daily", "daily_basic", "dividend_ex") else "个报告期" if api != "index_weight" else "个指数月"
        print(f"{api}: 覆盖 {s['dates']} {label} ({rng})")
        if s.get("row_count_mismatch"):
            for key, logged, actual in s["row_count_mismatch"][:10]:
                print(f"  ⚠ {key} fetch_log 记 {logged} 行, 表内实数 {actual} 行")
            if len(s["row_count_mismatch"]) > 10:
                print(f"  ⚠ ...共 {len(s['row_count_mismatch'])} 个键不一致")
        elif "row_count_mismatch" in s:
            print("  行数对账一致")
    if stats["trade_cal"]["spans"]:
        spans = ", ".join(f"{a}~{b}" for a, b in stats["trade_cal"]["spans"][:8])
        more = f" ...共{len(stats['trade_cal']['spans'])}段" if len(stats["trade_cal"]["spans"]) > 8 else ""
        print(f"trade_cal: 连续覆盖跨度 {spans}{more}")
    else:
        print("trade_cal: 无覆盖跨度")
    for api in ("stock_basic", "index_member_all"):
        at = stats[api]["fetched_at"]
        print(f"{api}: 快照 {'今日(' + at + ')' if at and at[:10] == __import__('datetime').datetime.now().strftime('%Y-%m-%d') else (at + ' (已过期, 下次构建刷新)') if at else '无'}")

    if not sample_n:
        return
    logged = sorted(set(store.logged_dates("daily")) & set(store.logged_dates("daily_basic")))
    logged = [d for d in logged if not store.is_volatile(d)]
    if not logged:
        print("无可抽查日期(两接口交集为空)")
        return
    picks = random.sample(logged, min(sample_n, len(logged)))
    print(f"\n----- 网络抽查 {len(picks)} 个日期(每日期 2 次请求, 与库内逐字段比对) -----")
    # 抽查用独立 provider 并临时禁用持久层, 强制走网络取"真值"
    market_data_module.MARKET_DB_ENABLED = False
    try:
        spot_provider = provider_factory()
        for day in picks:
            dt = datetime.strptime(day, "%Y%m%d")
            spot_provider.get_ts_code_to_pct_chg(dt)
            spot_provider.daily_basic_rows(dt)
            db_daily = store.load_daily(day) or []
            db_basic = store.load_daily_basic(day) or {}
            net_daily = spot_provider.ts_code_to_close_cache.get(dt, {})
            net_pct = spot_provider.ts_code_to_pct_chg_cache.get(dt, {})
            net_amount = spot_provider.ts_code_to_amount_cache.get(dt, {})
            net_rows = spot_provider._daily_basic_rows_cache.get(dt, {})
            diffs: list[str] = []
            db_close = {c: cl for c, cl, _pc, _a in db_daily}
            for code in set(db_close) | set(net_daily):
                if db_close.get(code) != net_daily.get(code):
                    diffs.append(f"daily.close {code}: 库={db_close.get(code)} 网={net_daily.get(code)}")
            db_pct = {c: (cl - pc) / pc * 100 for c, cl, pc, _a in db_daily}
            for code in set(db_pct) | set(net_pct):
                if code not in net_pct or db_pct.get(code) != net_pct.get(code):
                    diffs.append(f"daily.pct {code}: 库={db_pct.get(code)} 网={net_pct.get(code)}")
            db_amount = {c: a for c, _cl, _pc, a in db_daily if a is not None}
            for code in set(db_amount) | set(net_amount):
                if db_amount.get(code) != net_amount.get(code):
                    diffs.append(f"daily.amount {code}: 库={db_amount.get(code)} 网={net_amount.get(code)}")
            for code in set(db_basic) | set(net_rows):
                db_r = db_basic.get(code) or {}
                net_r = net_rows.get(code) or {}
                for field in ("close", "total_mv", "free_share", "float_share", "total_share"):
                    if db_r.get(field) != net_r.get(field):
                        diffs.append(
                            f"daily_basic.{field} {code}: 库={db_r.get(field)} 网={net_r.get(field)}"
                        )
            if diffs:
                print(f"⚠ {day}: {len(diffs)} 处不一致(前 5 条): {'; '.join(diffs[:5])}")
            else:
                print(f"✓ {day}: daily {len(db_daily)} 行 / daily_basic {len(db_basic)} 行逐字段一致")
    finally:
        market_data_module.MARKET_DB_ENABLED = True


def _run_force_fina(provider: MarketDataProvider, periods: list[str]) -> None:
    result = provider.refresh_fina_periods(periods)
    for key, count in result.items():
        print(f"  {key}: {count} 行")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="行情/市值 SQLite 持久层(data/market.db)补数与体检")
    parser.add_argument("--backfill", nargs=2, metavar=("START", "END"),
                        help="预热日期区间(YYYYMMDD): 只补库内缺失的交易日")
    parser.add_argument("--force", nargs="+", metavar="DATE",
                        help="强制重拉指定日期(先清库内痕迹再网络覆盖)")
    parser.add_argument("--force-fina", nargs="+", metavar="PERIOD", dest="force_fina",
                        help="强制重拉指定报告期三池(YYYYMMDD 季末日, 如 20240630; 应对远期追溯修正)")
    parser.add_argument("--check", action="store_true", help="体检(默认无参数也执行)")
    parser.add_argument("--sample", type=int, default=None, metavar="N",
                        help="配合 --check: 随机抽 N 个日期与网络实拉逐字段比对")
    args = parser.parse_args()

    provider = _build_provider()
    store = provider.market_store
    if store is None:
        raise RuntimeError("SQLite 持久层未启用(SW_MARKET_DB=0 或初始化失败), 无法执行本脚本")

    if args.backfill:
        _run_backfill(provider, store, args.backfill[0], args.backfill[1])
    if args.force:
        _run_force(provider, store, args.force)
    if args.force_fina:
        _run_force_fina(provider, args.force_fina)
    _run_check(_build_provider, store, args.sample if args.check else None)
