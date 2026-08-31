"""
行业指数估值走势序列（PE/PB 历史序列，K 线副图指标数据层）

- **口径（固定一种，2026-08-31 定稿）**：PE = 自由流通市值加权 + 归母TTM
  （`PE_free = Σ free_mv ÷ Σ(归母TTM利润 × free_mv/total_mv)`）、PB = 自由流通市值加权
  （分母 = 归母普通股股东权益，balancesheet_vip 权威绝对额），样本 = 全池（全A）。
  直接复用 `industry_ranking.daily_valuation_metric`（单日榜 PE/PB 同一实现），任取一日
  序列值必须与该日单日榜 `pe_attr_ttm.free` / `pb.free` **分毫不差**（锚点）。
- **窗口**：滚动自然日窗口 `VALUATION_WINDOW_DAYS`（2026-08-31 定稿 365 天 ≈ 1 年，约 240+ 交易日；
  原开发期 92 天/3 个月），右端 = **今日之前最近交易日**（不含今日——当日行情盘后才完整，
  避免半日数据被缓存固化）。
- **持久缓存** `data/valuation_history.json`：`{index_code: {"pe": {日期: 值|null},
  "pb": {...}, "updated": ...}}`。值 `null` = 已计算且行业亏损/资不抵债（走势图断线），
  **键缺失 = 未计算**（二者严格区分，与单日榜 None/键缺失语义一致）；`version` 为算法版本号，
  口径/公式任何变更必须 +1 作废旧缓存整文件重算。
- **后台计算 `ValuationSeriesManager`**：全局互斥串行计算（跨指数并发只会对同一批日期重复
  发 daily/daily_basic 请求；串行化后第二个指数几乎全命中 MarketDataProvider 内存缓存、
  秒级完成，故取"全局串行"而非"按指数并行"）；同一指数计算中重复 start 幂等返回。
  逐日落盘（崩溃/中断已算日期不丢），窗口外旧日期保留在缓存中无害（前端按 K 线日期对齐裁剪）。
- 无渐进式推送（首查分钟级等待由前端进度轮询覆盖，2026-08-31 定稿不做）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

try:
    from .industry_ranking import daily_valuation_metric
    from .industry_tree import ShenWanIndustryTree
    from .market_data import MarketDataProvider
except ImportError:  # 直接以脚本方式运行时的兜底
    from industry_ranking import daily_valuation_metric
    from industry_tree import ShenWanIndustryTree
    from market_data import MarketDataProvider

logger = logging.getLogger("shenwan_industry.valuation_series")

# 持久缓存文件（与 dividend_history.json 同目录；本地运行产物已 gitignore 不随仓库提交、
# 勿删——删了只是重算, 不影响正确性）
CACHE_PATH = Path(__file__).resolve().parent / "data" / "valuation_history.json"
# 算法版本号：PE/PB 公式、口径、窗口右端规则等任何影响数值的变更必须 +1（旧文件整文件作废重算）
CACHE_VERSION = 1
# 滚动窗口自然日数（2026-08-31 定稿 1 年；延长只需改此常量，缓存按缺失日期增量补算不作废）
VALUATION_WINDOW_DAYS = 365

# 上下文获取函数类型: build=True 时阻塞构建(可能分钟级), False 时就绪则返回、未就绪返回 None
ContextFn = Callable[[bool], tuple[ShenWanIndustryTree, MarketDataProvider] | None]


def _level_key_of(tree: ShenWanIndustryTree, index_code: str) -> str:
    """指数代码 -> 层级键("1"|"2"|"3")；不在行业树则抛 ValueError"""
    node = tree.index_code_to_node.get(index_code)
    if node is None:
        raise ValueError(f"不是申万行业指数代码: {index_code}")
    return {"L1": "1", "L2": "2", "L3": "3"}[node.level]


def _window_dates(provider: MarketDataProvider, window_days: int) -> list[str]:
    """窗口内交易日列表(YYYYMMDD 升序)：右端 = 今日之前最近交易日，左端 = 右端前 window_days 自然日"""
    today_str = date.today().strftime("%Y%m%d")
    probe_start = (date.today() - timedelta(days=window_days + 15)).strftime("%Y%m%d")
    days_before_today = [d for d in provider.get_trading_days(probe_start, today_str) if d < today_str]
    if not days_before_today:
        raise ValueError(f"近段无交易日(探测区间 {probe_start}~{today_str})")
    end = days_before_today[-1]
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=window_days)).strftime("%Y%m%d")
    return [d for d in days_before_today if d >= start]


class ValuationSeriesManager:
    """估值序列后台计算与持久缓存管理器（进程内单实例，由 web/service 持有）

    状态机（按指数独立）：need_query（无缓存/有缺失/上次出错）→ computing（后台线程）→
    ready（窗口内交易日全部有键）/ error（异常信息透出，可重试）。
    status() 永不触发构建/计算：上下文未就绪时保守返回 need_query（点击查询后任务侧
    ensure 构建，若实际已齐备则秒级空跑完成，自愈不错报）。
    """

    def __init__(self, context_fn: ContextFn, window_days: int = VALUATION_WINDOW_DAYS) -> None:
        self._context_fn = context_fn
        self._window_days = window_days
        self._lock = threading.Lock()  # 保护 _states/_cache/_epochs/_loaded（含落盘临界区）
        self._states: dict[str, dict[str, Any]] = {}  # index_code -> {state, progress, message}
        self._cache: dict[str, dict[str, Any]] = {}  # index_code -> {"pe": {...}, "pb": {...}, ...}
        self._epochs: dict[str, str] = {}  # trade_date -> 披露纪元指纹(PE 归母合并纪元|bs 纪元)——过期自愈见 _detect_stale
        self._loaded = False
        self._compute_lock = threading.Lock()  # 全局串行计算(见模块 docstring)

    # ---------- 持久缓存 ----------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if CACHE_PATH.exists():
            try:
                raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                if int(raw.get("version", 0)) == CACHE_VERSION:
                    self._cache = raw.get("series", {})
                    self._epochs = raw.get("epochs", {})
                else:
                    logger.warning(
                        f"估值序列缓存版本不匹配(文件 v{raw.get('version')} != v{CACHE_VERSION}), 全量重算"
                    )
            except Exception as err:  # noqa: BLE001 - 缓存损坏视为无缓存重建
                logger.warning(f"估值序列缓存文件损坏, 将全量重建: {err!r}")
        self._loaded = True

    def _save_locked(self) -> None:
        """落盘（调用方须已持 _lock）；失败仅告警不中断计算"""
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": CACHE_VERSION,
                "series": self._cache,
                "epochs": self._epochs,  # 披露纪元指纹(全市场一份, 与指数无关)
            }
            CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as err:  # noqa: BLE001 - 磁盘异常不中断计算
            logger.warning(f"估值序列缓存落盘失败: {err!r}")

    def _entry(self, index_code: str) -> dict[str, Any]:
        entry = self._cache.get(index_code)
        if entry is None:
            entry = {"pe": {}, "pb": {}, "updated": None}
            self._cache[index_code] = entry
        return entry

    def _has_date(self, index_code: str, day_str: str) -> bool:
        entry = self._cache.get(index_code)
        if not entry:
            return False
        # pe/pb 同日同批写入, 校验双键齐备
        return day_str in entry.get("pe", {}) and day_str in entry.get("pb", {})

    # ---------- 状态查询 ----------

    def status(self, index_code: str) -> dict[str, Any]:
        """查询指数序列状态；不触发构建/计算。返回
        {state: need_query|computing|ready|error, progress, message, series?}"""
        index_code = index_code.strip()
        with self._lock:
            self._ensure_loaded()
            state = dict(self._states.get(index_code) or {})
        if state.get("state") == "computing":
            return state
        if state.get("state") == "error":
            return state

        # 就绪判定: 上下文已就绪时按窗口缺失日期精确判定; 未就绪保守 need_query(自愈见 docstring)
        context = self._context_fn(build=False)
        if context is not None:
            tree, provider = context
            try:
                level_key = _level_key_of(tree, index_code)
                dates = _window_dates(provider, self._window_days)
                with self._lock:
                    missing = [d for d in dates if not self._has_date(index_code, d)]
                    if not missing:
                        entry = self._cache.get(index_code) or {}
                        return {
                            "state": "ready",
                            "progress": 100.0,
                            "message": "",
                            "window": {"start": dates[0], "end": dates[-1], "level": level_key},
                            "series": {"pe": entry.get("pe", {}), "pb": entry.get("pb", {})},
                    }
            except ValueError:
                raise
            except Exception as err:  # noqa: BLE001 - 日历异常等保守降级
                logger.warning(f"估值序列就绪判定失败({index_code}), 按 need_query 处理: {err!r}")
        return {"state": "need_query", "progress": 0.0, "message": ""}

    # ---------- 后台计算 ----------

    def start(self, index_code: str) -> dict[str, Any]:
        """启动(或并入)该指数的后台计算；同指数计算中幂等返回 computing"""
        index_code = index_code.strip()
        with self._lock:
            state = self._states.get(index_code)
            if state and state.get("state") == "computing":
                return {"state": "computing", "progress": state.get("progress", 0.0), "message": state.get("message", "")}
            # 先占位再起线程, 防两次 POST 竞态双起
            self._states[index_code] = {"state": "computing", "progress": 0.0, "message": "排队中"}
        threading.Thread(
            target=self._run_job, args=(index_code,), daemon=True, name=f"valuation-{index_code}"
        ).start()
        return {"state": "computing", "progress": 0.0, "message": "排队中"}

    def _set_state(self, index_code: str, **fields: Any) -> None:
        with self._lock:
            state = self._states.setdefault(index_code, {})
            state.update(fields)

    def _run_job(self, index_code: str) -> None:
        try:
            with self._compute_lock:
                self._run_job_locked(index_code)
        except Exception as err:  # noqa: BLE001 - 任何异常转 error 态透出前端
            logger.exception(f"估值序列计算失败({index_code})")
            self._set_state(
                index_code, state="error", progress=0.0, message=f"计算失败: {err}"
            )

    def _run_job_locked(self, index_code: str) -> None:
        self._set_state(index_code, state="computing", progress=0.0, message="准备行业数据")
        tree, provider = self._context_fn(build=True)  # 可能触发建树(首次分钟级)
        level_key = _level_key_of(tree, index_code)
        with self._lock:
            self._ensure_loaded()
        dates = _window_dates(provider, self._window_days)
        missing = [d for d in dates if not self._has_date(index_code, d)]
        if missing:
            # 预热树侧交易日窗口(新股 6 交易日门槛判定用): filter_stock_pool 逐日判定会对
            # "近 24 历日上市"的新股查 [最早新股日, 当日] 跨度——回放逐日跨度不同、树的跨度
            # 缓存逐日 miss, 打点实测 92 天窗口发出 66 次 trade_cal(~12s, 占首查 30%)。与
            # 区间链式榜同款预热: 一次宽跨度(窗口首日−24 天覆盖)后逐日判定全部切片命中零
            # 请求(2026-08-31; 树侧 _trading_days_window 是独立于 provider.get_trading_days
            # 的日历获取路径, 不走 SQLite, 必须单独预热)
            tree._trading_days_window(
                (datetime.strptime(missing[0], "%Y%m%d") - timedelta(days=24)).strftime("%Y%m%d"),
                missing[-1],
            )
        # 过期检测(披露纪元指纹自愈, 2026-08-31): 已算日期重算当前指纹并与存储值比对——
        # 不一致 = 上游财报数据变了(ann_date 回填/新增修订行, 该日 PIT 视图翻转), 当日值
        # 视同缺失重算; **无指纹的历史日期保留**(升级前入库, 数值经锚点验证仍有效), 不做
        # 一刀切重算。指纹全市场一份(与指数无关), 每日增量时顺带全窗口校验(毫秒级/日)
        stale: list[str] = []
        if self._epochs:
            for d in dates:
                if (
                    d not in missing
                    and d in self._epochs
                    and self._day_fingerprint(provider, d) != self._epochs[d]
                ):
                    stale.append(d)
        if stale:
            logger.info(
                f"估值序列({index_code}) 检测到 {len(stale)} 个交易日的上游财报数据已更新, 重算: "
                f"{', '.join(stale[:5])}{'...' if len(stale) > 5 else ''}"
            )
        todo = sorted(set(missing) | set(stale))
        total = len(todo)
        if not total:
            self._set_state(
                index_code,
                state="ready",
                progress=100.0,
                message="",
            )
            return

        done = 0
        skipped: list[str] = []
        t0 = time.perf_counter()
        for day_str in todo:
            result = self._compute_one(tree, provider, index_code, level_key, day_str)
            if result is None:
                skipped.append(day_str)  # 当日行情未发布等, 不落键下次再试
            else:
                pe_value, pb_value = result
                with self._lock:
                    entry = self._entry(index_code)
                    entry["pe"][day_str] = pe_value
                    entry["pb"][day_str] = pb_value
                    entry["updated"] = datetime.now().strftime("%Y%m%d %H:%M:%S")
                    self._epochs[day_str] = self._day_fingerprint(provider, day_str)
                    self._save_locked()  # 逐日落盘, 中断不丢已算日期
            done += 1
            self._set_state(
                index_code,
                state="computing",
                progress=done / total * 100.0,
                message=f"计算估值 {done}/{total}",
            )

        elapsed = time.perf_counter() - t0
        if skipped:
            logger.warning(
                f"估值序列({index_code}) 有 {len(skipped)} 个交易日行情缺失未计入, 样例: "
                f"{', '.join(skipped[:5])}"
            )
        # 有行情缺失日时窗口不完整, 仍标 ready(缺失日非计算错误, 数据可用; 下次查询自动补)
        self._set_state(index_code, state="ready", progress=100.0, message="")
        logger.info(
            f"估值序列({index_code}) 本次计算 {total - len(skipped)}/{total} 个交易日, "
            f"耗时 {elapsed:.1f}s"
        )

    def _day_fingerprint(self, provider: MarketDataProvider, day_str: str) -> str:
        """某交易日的披露纪元指纹: "PE归母合并纪元|bs纪元"(过期自愈用, 见 _run_job_locked)

        与 market_data 视图键同一原料(窗口各期披露边界: fina 去重 ann ∪ express 全版本 ann
        ∪ 法定截止日 / bs 同)但不构建视图——纯边界二分, 单日毫秒级; 首次调用会触发易变期
        财报拉取与边界收集(与回放计算共用同一批 period 缓存, 不多花请求)。express 拉取失败
        时 PE 侧纪元退化为纯财报口径并加哨兵前缀(与 _attr_view_with_key 同语义: 失败态指纹
        与成功态绝不相同, 快报恢复后当日自动判过期重算)
        """
        day = datetime.strptime(day_str, "%Y%m%d")
        periods = tuple(provider._fina_period_window(day))
        fina_sets = [
            provider._pool_ann_boundaries(p, provider._fetch_fina_period, provider._fina_ann_boundaries)
            for p in periods
        ]
        try:
            express_sets = [
                provider._pool_ann_boundaries(p, provider._fetch_express_period, provider._express_ann_boundaries, express_style=True)
                for p in periods
            ]
            pe_epoch = provider._epoch_of(day_str, fina_sets + express_sets)
        except Exception:
            pe_epoch = "noexpress:" + provider._epoch_of(day_str, fina_sets)
        bs_epoch = provider._epoch_of(
            day_str,
            [provider._pool_ann_boundaries(p, provider._fetch_bs_period, provider._bs_ann_boundaries) for p in periods],
        )
        return f"{pe_epoch}|{bs_epoch}"

    @staticmethod
    def _compute_one(
        tree: ShenWanIndustryTree,
        provider: MarketDataProvider,
        index_code: str,
        level_key: str,
        day_str: str,
    ) -> tuple[float | None, float | None] | None:
        """计算单日 PE(free, 归母TTM)/PB(free)；返回 None = 当日行情未发布(跳过不落键)，
        值 None = 行业亏损/资不抵债（正常落键, 前端断线展示）"""
        day = datetime.strptime(day_str, "%Y%m%d")
        if not provider.get_ts_code_to_pct_chg(day):
            return None
        pe_free, _, _ = daily_valuation_metric(
            tree, provider, day, "pe", profit_kind="attr", dynamic=False
        )
        pb_free, _, _ = daily_valuation_metric(tree, provider, day, "pb")
        pe_map = pe_free[level_key]
        pb_map = pb_free[level_key]
        if index_code not in pe_map and index_code not in pb_map:
            # 行业无参与股票(键缺失)——防御分支, 正常行业不会触发; 视为无数据不落键
            return None
        return pe_map.get(index_code), pb_map.get(index_code)

    # ---------- CLI / 体检辅助 ----------

    def run_sync(self, index_code: str, force: bool = False) -> dict[str, Any]:
        """同步计算（CLI 用）：force=True 时先清掉该指数缓存整段重算"""
        index_code = index_code.strip()
        if force:
            with self._lock:
                self._ensure_loaded()
                self._cache.pop(index_code, None)
                self._save_locked()
        self._run_job(index_code)
        with self._lock:
            state = dict(self._states.get(index_code) or {})
            entry = self._cache.get(index_code) or {}
            return {
                "state": state.get("state"),
                "message": state.get("message", ""),
                "pe": entry.get("pe", {}),
                "pb": entry.get("pb", {}),
            }


def _main() -> None:
    """CLI: 计算并打印单个行业指数的估值走势序列（首查/增量/体检用）

    用法: python shenwan_industry/valuation_series.py 801010.SI [--window-days 365] [--force]
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="行业指数估值走势序列(PE/PB)计算入口")
    parser.add_argument("index_code", help="申万行业指数代码, 如 801010.SI")
    parser.add_argument("--window-days", type=int, default=VALUATION_WINDOW_DAYS, help=f"滚动窗口自然日数(默认 {VALUATION_WINDOW_DAYS})")
    parser.add_argument("--force", action="store_true", help="忽略该指数现有缓存整段重算")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        from config_store import config_path, get_token
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from config_store import config_path, get_token

    import tushare as ts

    token = get_token()
    if not token:
        raise ValueError(
            "未配置 Tushare token，请先运行 Web 服务（python -m shenwan_industry.web.server）"
            "并在页面右上角填写保存 token；或直接编辑本地配置文件: " + str(config_path())
        )

    pro = ts.pro_api(token=token)
    provider = MarketDataProvider(pro)
    tree = ShenWanIndustryTree(tushare_pro=provider.pro)
    tree.build_industries()
    tree.build_constituent_stocks_by_tushare()

    context: dict[str, tuple[ShenWanIndustryTree, MarketDataProvider] | None] = {"ctx": None}
    context["ctx"] = (tree, provider)
    manager = ValuationSeriesManager(
        context_fn=lambda build: context["ctx"], window_days=args.window_days
    )
    result = manager.run_sync(args.index_code, force=args.force)

    print(f"指数: {args.index_code}  状态: {result['state']}  {result['message']}")
    pe_map = result["pe"]
    pb_map = result["pb"]
    if not pe_map:
        print("（无已计算日期）")
        return
    print(f"已计算 {len(pe_map)} 个交易日（窗口右端 = 今日之前最近交易日; 值 null = 亏损/资不抵债）")
    print(f"{'日期':<10}{'PE':>10}{'PB':>10}")
    for day_str in sorted(pe_map):
        pe = pe_map[day_str]
        pb = pb_map.get(day_str)
        pe_text = "亏损" if pe is None else f"{pe:.4f}"
        pb_text = "资不抵债" if pb is None else f"{pb:.4f}"
        print(f"{day_str:<10}{pe_text:>10}{pb_text:>10}")


if __name__ == "__main__":
    _main()
