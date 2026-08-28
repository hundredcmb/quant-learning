"""
分红缓存构建与体检入口脚本

首次使用股息率列前先运行一次(全市场逐股首刷, 约 5400 请求、7.5次/秒档约 12 分钟, 一次性);
此后单日榜/Web 会自动增量刷新(ann_date 日历日 + ex_date 交易日双通道逐日探测), 本脚本
用于手动补数/体检/验证, 日常无需运行。

用法(在仓库根目录):
    .venv\\Scripts\\python.exe -m shenwan_industry.dividend_cache            # 增量刷新+统计
    .venv\\Scripts\\python.exe -m shenwan_industry.dividend_cache --full    # 强制全量重建
    .venv\\Scripts\\python.exe -m shenwan_industry.dividend_cache --check 600036.SH 600519.SH
                                                                             # 抽查个股事件与双口径计算
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import tushare as ts

try:
    from .dividend_data import CACHE_PATH, compute_dividend_dps
    from .industry_tree import ShenWanIndustryTree
    from .market_data import MarketDataProvider
except ImportError:
    from dividend_data import CACHE_PATH, compute_dividend_dps
    from industry_tree import ShenWanIndustryTree
    from market_data import MarketDataProvider

# token 配置在仓库根公共模块（与 holders 共享同一份 .quant-learning/settings.json）
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from config_store import config_path, get_token  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shenwan_industry.dividend_cache")


def _build_tree_and_provider():
    token = get_token()
    if not token:
        raise ValueError(
            "未配置 Tushare token，请先运行 Web 服务并在页面右上角填写保存 token；"
            "或直接编辑本地配置文件: " + str(config_path())
        )
    provider = MarketDataProvider(ts.pro_api(token=token))
    tree = ShenWanIndustryTree(tushare_pro=provider.pro)
    t0 = time.perf_counter()
    tree.build_industries()
    tree.build_constituent_stocks_by_tushare()
    logger.info(f"行业树+成分加载 {time.perf_counter() - t0:.1f}s, 成分池 {len(tree.all_member_codes)} 只")
    return provider, tree


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="分红缓存构建与体检")
    parser.add_argument("--full", action="store_true", help="强制全量重建(忽略现有缓存)")
    parser.add_argument("--check", nargs="*", default=None, metavar="TS_CODE",
                        help="抽查个股: 打印其 PIT 事件、静态/TTM估算 DPS(需先完成刷新)")
    args = parser.parse_args()

    provider, tree = _build_tree_and_provider()
    history = provider.dividend_history

    if args.full or not CACHE_PATH.exists():
        logger.info("开始首刷(全市场逐股, 约 12 分钟)...")
    action = history.ensure_refresh(set(tree.all_member_codes), force_full=args.full)
    logger.info(f"刷新完成: {action}")

    summary = history.summary()
    print("\n===== 分红缓存体检 =====")
    print(f"缓存文件: {CACHE_PATH}")
    print(f"覆盖股票: {summary['stocks']} 只 (有事件 {summary['stocks_with_events']} / 空占位 {summary['stocks_empty']})")
    print(f"事件总数: {summary['events_total']} 条 (无基准股本 {summary['events_no_base_share']} 条)")
    print(f"财年分布: {summary['events_by_year']}")
    print(f"最近刷新: {summary['last_refresh']}")

    if args.check:
        today = datetime.now()
        print(f"\n===== 个股抽查(按今日 {today:%Y%m%d} 的 PIT 视角) =====")
        est_map, static_map, _ = compute_dividend_dps(provider, today)
        close_map = provider.get_ts_code_to_close(today)
        for code in args.check:
            print(f"\n{code}:")
            for e in history.events(code)[-12:]:
                print(f"  {e['end_date']} {e['div_proc']} ann={e['ann_date']} ex={e['ex_date']} amt={e['amt']} base={e['base_share']}")
            s = static_map.get(code)
            d = est_map.get(code)
            close = close_map.get(code)
            s_pct = f"{s / close * 100:.2f}%" if (s is not None and close) else "—"
            d_pct = f"{d / close * 100:.2f}%" if (d is not None and close) else "—"
            print(f"  静态 DPS={s} (股息率 {s_pct}) | TTM估算 DPS={d} (股息率 {d_pct})")
