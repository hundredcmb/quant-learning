"""
股本台阶缓存构建与体检入口脚本

首次使用"TTM估算股息+注销率"口径前先运行一次(全市场按交易日回填 18.5 个月快照,
约 370 个请求、3 次/秒档约 2 分钟, 一次性); 此后单日榜/Web 会自动增量刷新(逐交易日
链式 diff), 本脚本用于手动补数/体检/验证, 日常无需运行。

用法(在仓库根目录):
    .venv/bin/python -m shenwan_industry.share_change_cache            # 增量刷新+统计
    .venv/bin/python -m shenwan_industry.share_change_cache --full     # 强制全量重建
    .venv/bin/python -m shenwan_industry.share_change_cache --check 600519.SH 000333.SZ
                                                                        # 抽查个股台阶事件与注销金额
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import tushare as ts

try:
    from .share_change_data import CACHE_PATH, _negative_amount_wan
    from .industry_tree import ShenWanIndustryTree
    from .market_data import MarketDataProvider
except ImportError:
    from share_change_data import CACHE_PATH, _negative_amount_wan
    from industry_tree import ShenWanIndustryTree
    from market_data import MarketDataProvider

# token 配置在仓库根公共模块（与 holders 共享同一份 .quant-learning/settings.json）
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from config_store import config_path, get_token  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shenwan_industry.share_change_cache")


def _build_provider() -> MarketDataProvider:
    token = get_token()
    if not token:
        raise ValueError(
            "未配置 Tushare token，请先运行 Web 服务并在页面右上角填写保存 token；"
            "或直接编辑本地配置文件: " + str(config_path())
        )
    return MarketDataProvider(ts.pro_api(token=token))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="股本台阶缓存构建与体检")
    parser.add_argument("--full", action="store_true", help="强制全量重建(忽略现有缓存回填全窗口)")
    parser.add_argument("--check", nargs="*", default=None, metavar="TS_CODE",
                        help="抽查个股: 打印其台阶事件、TTM 窗口与窗口内注销金额(需先完成刷新)")
    args = parser.parse_args()

    provider = _build_provider()
    history = provider.share_change_history

    if args.full:
        # 强制全量: 丢弃内存进度, 文件不存在即回填全窗口
        history.snapshot_date = None
        history._snapshot = {}
        history._backfill_start = None
        logger.info("强制全量重建(回填 18.5 个月快照, 约 370 个请求)...")
    action = history.ensure_refresh()
    rep = provider.repurchase_history
    rep_action = rep.ensure_refresh()
    logger.info(f"刷新完成: {action}; 回购公告: {rep_action}")

    summary = history.summary()
    rep_summary = rep.summary()
    print("\n===== 股本台阶缓存体检 =====")
    print(f"缓存文件: {CACHE_PATH}")
    print(f"有事件股票: {summary['stocks_with_events']} 只 (负台阶 {summary['events_negative']} / 全部 {summary['events_total']})")
    print(f"事件按年分布: {summary['events_by_year']}")
    print(f"快照日: {summary['snapshot_date']} (覆盖 {summary['snapshot_stocks']} 只)")
    print(f"回购公告缓存: {rep_summary['stocks']} 只 / {rep_summary['records_total']} 条 (已拉 {rep_summary['months_done']} 个月, 最早 {rep_summary['earliest_month']})")

    if args.check:
        today = datetime.now()
        print(f"\n===== 个股抽查(按今日 {today:%Y%m%d} 的 TTM 窗口) =====")
        windows, _ = provider.get_ts_code_to_ttm_window(today)
        for code in args.check:
            print(f"\n{code}:")
            start, end = windows.get(code, ("", ""))
            label = f"({start}, {end}]" if start else f"(上市以来, {end}]"
            print(f"  TTM 窗口: {label}")
            for e in history.events(code)[-12:]:
                arrow = "注销" if e["p"] > e["n"] else ("增股" if e["p"] < e["n"] else "?")
                print(f"  {e['d']} {arrow} {e['p']:,.4f} -> {e['n']:,.4f} 万股 (Δ{e['n'] - e['p']:+,.4f}, close={e['x']})")
            amount, count, large_skipped, vam_skipped, from_buyback = _negative_amount_wan(
                history.events(code), start, end, buyback_records=rep.records(code)
            )
            skipped_note = f", 数量级守卫剔除 {large_skipped} 笔" if large_skipped else ""
            vam_note = f", 对赌匹配剔除 {vam_skipped} 笔(vol 精确匹配且金额≈0)" if vam_skipped else ""
            bb_note = f", {from_buyback} 笔用公告金额" if from_buyback else ""
            print(f"  窗口内注销金额 ≈ {amount:,.1f} 万元 ({count} 笔计入{bb_note}, 对冲剔除后{skipped_note}{vam_note})")
