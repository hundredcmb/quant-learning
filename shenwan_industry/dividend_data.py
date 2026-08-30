"""
分红数据层与股息率计算 (DividendHistory + compute_dividend_dps)

- 持久缓存: dividend 接口按 ts_code 拉每股**全历史**分红事件(实施+预案+停止实施), 落盘
  data/dividend_history.json(首刷全市场约 5400 请求、7.5次/秒档约 12 分钟, 一次性); 增量 =
  last_refresh 之后的 ann_date(按**日历日**逐日——公告日常有周六, 如神华 20250830 预案)
  + ex_date(按交易日)双通道探测, 受影响股票整股重拉, 并顺带补拉缓存未覆盖的新成分股票
  (新上市股票首份分红公告前不在任何探测通道内)。force_full/--full = 忽略现有缓存全量
  重拉(只补缺失会把 last_refresh 推到今天而跳过增量窗口)。双通道必要性: 部分实施行的
  ann_date 被数据商回填为预案日(茅台 FY2024 实施行 ann_date=20250403=预案日), 仅扫
  ann_date 会漏新落地实施; 仅扫 ex_date 会漏纯预案公告; 探测与单股拉取均 offset/limit
  分页循环(实测单日峰值约 3000 行单页即回, 分页为极端披露日防御)
- 股息率(列名"股息率")双口径, 规则栈与接口实测见 docs/financial_indicators.md 第 7 节:
  * **财年归属**: 每个分红事件按 end_date(分红年度)年份前缀归属财年——1231=年度、0630=中期、
    0930=三季度、0331=一季度、非报告期日期=特别分红(计入财年总额、不触发锚切换);
    同财年多事件直接加总, 对"一次转多次"与"多次不均衡分"天然免疫
  * **总额法**: 每股口径 = Σ(每股派现 × 该事件基准股本 base_share) ÷ 当前总股本——与官方
    dv_ratio 稀释口径一致, 正确处理财年内送转/股本变动(同财年每股基数不可直接相加);
    base_share 缺失的事件按"基准股本=当前总股本"退化(等价于每股直接相加; 实施行 base_share
    覆盖实测 100%, 预案行覆盖在首刷时验证)
  * **事件级级联: 实施 > 预案 > 无**(预案修订实测 3.2%、中位 0.44%; 停止实施行作废其之前
    的预案, 之后重报的预案有效); **只认实施与预案, 不碰"股东大会通过"行**(其 ann_date 实测
    被回填为预案日且携带股东大会后才通过的修订金额——中移动 FY2022 预案 2.21/决案行 1.9796
    同盖 20230324, 按它重放历史会用未来信息, PIT 脏)
  * **静态口径** = 最近完整分红年度(锚)的级联总额; 锚 = 年度事件(end_date=*1231)**有实施或
    有预案**的最近财年(预案先行, 每股锚切换提前到 3~4 月; 分批除息实测为零)
  * **TTM估算值口径(Web 默认)** = **进行中财年 N 的估算, 随每期利润报告刷新**(目标财年 =
    年度分红尚未宣告的最近财年; 年报分红一宣告目标即滚到 N+1、payout 锚即切到 N, 全年无空档;
    利润源与 PIT 同 PE——归母TTM 含业绩快报双源合并, ann_date ≤ T): 基准 = payout(锚) ×
    归母TTM ÷ 当前总股本, payout(锚) = 锚财年分红总额 ÷ 锚财年归母净利润(年报值, 与分红
    预案同日披露), **payout 超 95% 封顶**(锚年利润塌方/分红刚性/特别分红的异常锚防荒谬外推);
    **归母TTM ≤ 0(某季度大亏)按 0 利润估算 → 0.00% 参与合成而非"—"**;
    **锚年亏损但仍在分红按 payout=0 估算 → 0.00%**(实测城建发展/华发股份 FY2025 亏损仍派现);
    N 的中期实绩/宣告(级联部分值)**超过估算时用实绩**(部分实绩防低估),
    否则维持估算——"宣告优先、外推补位"
  * **完整性三态**: 年度事件有行(实施/预案/**含 0 金额预案行**=显式"不分配", 4 月年报季确立)
    → 齐备; 无任何行且过 7/31(Y+1, 年报法定披露截止 4/30 + 缓冲; 实测 FY2021~24 预案晚于
    5/31 的为 0、实施公告晚于 7/31 的仅约 0.3% 支付者且 ≤3 周自愈, 集中在北交所/深B 的
    "无预案行"缺口板块——该板块占已实施事件 7.3%) → 推定该财年年度分红为零; 其余未知
    (锚留上一完整财年, 不提前归零)。齐备且总额=0 显示 **0.00%**(事实), 未知显示 **"—"**
    (未知≠零, 二者严格区分)
- PIT: 事件按 实施 ex_date ≤ T / 预案 ann_date ≤ T 过滤; 利润按 ann_date ≤ T(PE 同机制)
- 单位: 每股 DPS(元/股); 个股股息率% = DPS/close×100(子表), 行业整体法 = Σ(DPS×总股本)
  ÷ Σ(总市值)×100 = Σ(分红总额)÷Σ(总市值)×100(主表, 总额法下与子表每股口径自洽);
  DPS=0 是数值(参与合成), 无数据为键缺失
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger("shenwan_industry.dividend_data")

# 持久缓存文件(与 SW2021.json 同目录; 随仓库提交, 与 holders 缓存同约定: 结构稳定、只读复用)
CACHE_PATH = Path(__file__).resolve().parent / "data" / "dividend_history.json"
CACHE_VERSION = 3
# 事件保留下限: 锚定/完整性判定最多回看 2 个财年, 2020 起保留留足余量并控制文件体积
RETENTION_FLOOR = "20200101"
# 缓存只保留级联实际使用的三态("股东大会通过"行 PIT 脏绝不使用、"预披露"仅预告不参与),
# 控制缓存体积(实测约占全阶段行数 ~30%)
KEEP_PROCS = ("实施", "预案", "停止实施")
# 完整性推定截止线: Y+1 年 7 月 31 日(年报法定披露截止 4/30 + 数据商缓冲)——实测
# FY2021~2024 年度预案无一行晚于 5/31(最晚 5/11), 实施公告晚于 7/31 的约 0.3% 支付者
# (8/1~8/22 落地, ≤3 周自愈); 年度分红另有"显式不分配"以 0 金额预案行在 4 月确立的多数路径
FALLBACK_MONTH_DAY = "0731"
# 估算分红率上限: payout(锚)>该值时按该值封顶——锚财年利润塌方/分红刚性/特别分红会造出
# payout>100% 的异常锚(实测五粮液 FY2025 payout 223.5%: 分红 200 亿/归母 89.5 亿), 直接外推
# 会得出比公司实付还高的荒谬估算; 封顶后估算回到"利润的 95% 折算"的保守上界
EST_PAYOUT_CAP = 0.95
# 首刷/增量重拉的并发线程数(请求开始时刻由节流器统一平摊, 与 fina 池同模式)
FILL_WORKERS = 8
# dividend 接口单页行数(单股全历史与按日探测共用, offset/limit 分页循环直到不足一页):
# 实测单日公告峰值约 3000 行(全阶段口径, 缓存三态约占 30%)单页即回, 分页仅防御极端披露日
DIV_FETCH_BATCH = 9999
# dividend 接口单股拉取字段: base_share 为实施/预案行基准股本(万股, 总额法分子必需;
# 非默认显示字段须显式请求)
DIV_FIELDS = "end_date,ann_date,div_proc,ex_date,cash_div,cash_div_tax,base_share"


def _amt_of(row) -> float | None:
    """每股分红金额(税前优先): cash_div_tax 非空取之(实测 688597 型主字段 0/税前字段有值),
    否则 cash_div; 两字段皆空返回 None(0 金额行两字段为 0.0, 正确保留"显式不分配"语义)"""
    tax = getattr(row, "cash_div_tax", None)
    if tax is not None and not pd.isna(tax):
        return float(tax)
    cash = getattr(row, "cash_div", None)
    if cash is not None and not pd.isna(cash):
        return float(cash)
    return None


def _normalize_rows(df) -> list[dict]:
    """dividend 响应规范化为精简事件列表(仅保留 RETENTION_FLOOR 之后的财年)"""
    events: list[dict] = []
    if df is None or df.empty:
        return events
    for r in df.itertuples(index=False):
        end_date = str(getattr(r, "end_date", "") or "")
        if not end_date or end_date < RETENTION_FLOOR:
            continue
        proc = str(getattr(r, "div_proc", "") or "")
        if proc not in KEEP_PROCS:
            continue
        base_share = getattr(r, "base_share", None)
        events.append(
            {
                "end_date": end_date,
                "ann_date": str(getattr(r, "ann_date", "") or "") or None,
                "div_proc": proc,
                "ex_date": str(getattr(r, "ex_date", "") or "") or None,
                "amt": _amt_of(r),
                "base_share": None if base_share is None or pd.isna(base_share) else float(base_share),
            }
        )
    return events


def _pitt_filter(events: list[dict], date_str: str) -> list[dict]:
    """PIT 过滤: 实施行按 ex_date ≤ T(行进入数据表的时间=实施公告日≤除息日, 恒 PIT 安全),
    预案/停止实施行按 ann_date ≤ T(缺失公告日的行不可用时点、丢弃)"""
    kept: list[dict] = []
    for e in events:
        if e["div_proc"] == "实施":
            if e["ex_date"] and e["ex_date"] <= date_str:
                kept.append(e)
        elif e["ann_date"] and e["ann_date"] <= date_str:
            kept.append(e)
    return kept


class DividendHistory:
    """每股分红事件持久缓存: 文件加载 + 首刷/增量刷新 + 每股事件读取

    进程内单实例(经 MarketDataProvider.dividend_history 惰性创建); ensure_refresh 幂等,
    内部加锁防重入(预热线程与主线程并发调用时只刷一次)
    """

    def __init__(self, provider) -> None:
        self._provider = provider  # MarketDataProvider: 用其 pro/节流器/交易日历
        self._lock = threading.Lock()
        self._loaded = False
        self._stocks: dict[str, dict] = {}  # ts_code -> {"updated": YYYYMMDD, "events": [...]}
        self.last_refresh: str | None = None  # 最近一次(首刷或增量)完成日 YYYYMMDD

    # ---------- 文件与刷新 ----------

    def _load(self) -> None:
        if self._loaded:
            return
        if CACHE_PATH.exists():
            try:
                raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                if int(raw.get("version", 0)) == CACHE_VERSION:
                    self._stocks = raw.get("stocks", {})
                    self.last_refresh = raw.get("last_refresh")
                else:
                    logger.warning(
                        f"分红缓存版本不匹配(文件 v{raw.get('version')} != v{CACHE_VERSION}), 全量重建"
                    )
            except Exception as err:  # noqa: BLE001 - 缓存损坏视为无缓存重建
                logger.warning(f"分红缓存文件损坏, 将全量重建: {err!r}")
                self._stocks = {}
                self.last_refresh = None
        self._loaded = True

    def _save(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": CACHE_VERSION, "last_refresh": self.last_refresh, "stocks": self._stocks}
        CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _fetch_one(self, ts_code: str) -> list[dict]:
        """单股全历史拉取(offset/limit 分页循环, 单股通常 1 页): dividend(ts_code=) 返回
        该股全部阶段全历史"""
        events: list[dict] = []
        offset = 0
        while True:
            self._provider._acquire_rate_slot("dividend")
            df = self._provider.pro.dividend(
                ts_code=ts_code, fields=DIV_FIELDS, offset=offset, limit=DIV_FETCH_BATCH
            )
            events.extend(_normalize_rows(df))
            if df is None or len(df) < DIV_FETCH_BATCH:
                return events
            offset += len(df)

    def _pull_stocks(self, ts_codes: list[str], today_str: str, cancel_check=None) -> int:
        """线程池并发整股重拉(请求速率由节流器平摊), 返回更新只数; 单股失败即抛不静默丢股"""
        done = 0
        stocks = self._stocks

        def _pull_one(code: str) -> None:
            if cancel_check is not None:
                cancel_check()
            events = self._fetch_one(code)
            old = stocks.get(code, {}).get("events") or []
            # 全历史接口响应即权威全集; 响应为空保留旧值(防御接口瞬时异常清空历史)
            stocks[code] = {"updated": today_str, "events": events if events else old}

        with ThreadPoolExecutor(max_workers=FILL_WORKERS) as executor:
            futures = {executor.submit(_pull_one, c): c for c in ts_codes}
            for future in as_completed(futures):
                future.result()
                done += 1
        return done

    def ensure_refresh(
        self, universe: set[str], force_full: bool = False, cancel_check=None
    ) -> str:
        """确保缓存新鲜, 幂等加锁: 全量重建 / 增量(双通道探测 + 新成分补拉)

        universe: 需要覆盖的股票集合(榜单股票池 = 树全部成分, 首刷逐股拉取的对象);
        探测发现的集合外股票忽略(不进榜单不浪费缓存)。cancel_check: 协作式取消回调
        (首刷约 12 分钟, 逐股/逐日检查点抛出)。返回动作说明("full-rebuild N只"/
        "incremental ..."/"up-to-date")。
        - **全量重建**(force_full / 无缓存 / last_refresh 缺失无法定增量窗口):
          **重拉 universe 全部股票**(已有数据整股覆盖, --full 语义=忽略现有缓存;
          若只补缺失会把 last_refresh 推到 today 而**跳过增量窗口**, 旧股票新事件漏拉)
        - **增量**: (last_refresh, today] 窗口——ann_date 按日历日逐日(公告可有周六),
          ex_date 按交易日逐日(除权除息日必为交易日); 探测受影响股票 ∪ **缓存未覆盖的
          新成分**(新上市股票首份分红公告前不在任何探测通道内, 主动补齐覆盖)
        异常语义: 失败向上抛(由调用方告警降级), 此时内存中已更新的股票保留、last_refresh
        不推进(下次运行重试同窗口)
        """
        with self._lock:
            self._load()
            universe = {c for c in universe if c}
            today_str = datetime.now().strftime("%Y%m%d")
            if force_full or not self._stocks or not self.last_refresh:
                self._stocks = {}  # 忽略现有缓存(手动 --full/结构升级/窗口起点缺失)
                if universe:
                    self._pull_stocks(sorted(universe), today_str, cancel_check)
                self.last_refresh = today_str
                self._save()
                return f"full-rebuild {len(universe)}只"
            if self.last_refresh >= today_str:
                return "up-to-date"
            start_day = (
                datetime.strptime(self.last_refresh, "%Y%m%d") + timedelta(days=1)
            ).strftime("%Y%m%d")
            ann_days: list[str] = []
            d = datetime.strptime(start_day, "%Y%m%d")
            while d.strftime("%Y%m%d") <= today_str:
                ann_days.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
            ex_days = list(self._provider.get_trading_days(start_day, today_str))
            affected: set[str] = set()
            for probe_key, days in (("ann_date", ann_days), ("ex_date", ex_days)):
                for day in days:
                    if cancel_check is not None:
                        cancel_check()
                    offset = 0
                    while True:
                        self._provider._acquire_rate_slot("dividend")
                        df = self._provider.pro.dividend(
                            **{probe_key: day, "offset": offset, "limit": DIV_FETCH_BATCH}
                        )
                        if df is not None and not df.empty:
                            affected.update(df["ts_code"].astype(str))
                        if df is None or len(df) < DIV_FETCH_BATCH:
                            break
                        offset += len(df)
            new_codes = universe - set(self._stocks)
            hit = sorted((affected & universe) | new_codes)
            if hit:
                self._pull_stocks(hit, today_str, cancel_check)
            self.last_refresh = today_str
            self._save()
            return (
                f"incremental 窗口{start_day}~{today_str} 探测{len(ann_days) + len(ex_days)}次 "
                f"重拉{len(hit)}只(其中新成分{len(new_codes)}只)"
            )

    def events(self, ts_code: str) -> list[dict]:
        """读取单股事件列表(未加载时惰性加载)"""
        self._load()
        return self._stocks.get(ts_code, {}).get("events", [])

    def codes(self) -> list[str]:
        """缓存覆盖的全部股票代码"""
        self._load()
        return sorted(self._stocks)

    def summary(self) -> dict:
        """缓存体检统计(供 dividend_cache.py CLI)"""
        self._load()
        with_events = sum(1 for s in self._stocks.values() if s.get("events"))
        total_events = sum(len(s.get("events", [])) for s in self._stocks.values())
        years: dict[str, int] = {}
        no_base = 0
        for s in self._stocks.values():
            for e in s.get("events", []):
                y = e["end_date"][:4]
                years[y] = years.get(y, 0) + 1
                if e.get("base_share") is None:
                    no_base += 1
        return {
            "stocks": len(self._stocks),
            "stocks_with_events": with_events,
            "stocks_empty": len(self._stocks) - with_events,
            "events_total": total_events,
            "events_no_base_share": no_base,
            "events_by_year": dict(sorted(years.items())),
            "last_refresh": self.last_refresh,
        }


# ---------- 事件分析: 财年归组 / 锚 / 完整性 / 级联(纯函数, 便于独立验证) ----------


def _group_by_fy(events: list[dict]) -> dict[int, dict[str, dict]]:
    """PIT 后事件按财年归组: 财年 -> end_date(事件键) -> 事件槽{impl, plan, stopped}

    槽内保留各阶段的最后版本: 实施=按 ex_date 最大(分批除息实测为零, 防御)、
    预案=按 ann_date 最大(同日多行为股本调整行, 差 ~0.1%)、停止实施=ann_date 最大
    """
    fy: dict[int, dict[str, dict]] = {}
    for e in events:
        year = int(e["end_date"][:4])
        slot = fy.setdefault(year, {}).setdefault(
            e["end_date"], {"impl": None, "plan": None, "stopped": None}
        )
        cand = {
            "amt": e["amt"],
            "ann": e["ann_date"] or "",
            "ex": e["ex_date"] or "",
            "base": e.get("base_share"),
        }
        if e["div_proc"] == "实施":
            if slot["impl"] is None or cand["ex"] > slot["impl"]["ex"]:
                slot["impl"] = cand
        elif e["div_proc"] == "预案":
            if slot["plan"] is None or cand["ann"] >= slot["plan"]["ann"]:
                slot["plan"] = cand
        elif e["div_proc"] == "停止实施":
            if slot["stopped"] is None or cand["ann"] > slot["stopped"]:
                slot["stopped"] = cand["ann"]
    return fy


def _slot_pick(slot: dict) -> tuple[float, float | None] | None:
    """级联选中行(实施 > 预案 > 无): 返回 (每股金额, 基准股本万股); None = 无可用行

    实施即权威(金额缺失视为 0, 不回退预案); 停止实施行作废公告日 ≤ 其公告日的预案
    (之后重报的预案有效)
    """
    impl = slot["impl"]
    if impl is not None:
        return (impl["amt"] if impl["amt"] is not None else 0.0), impl["base"]
    plan = slot["plan"]
    if plan is not None and plan["amt"] is not None:
        stopped = slot["stopped"]
        if stopped is None or plan["ann"] > stopped:
            return float(plan["amt"]), plan["base"]
    return None


def _slot_has_row(slot: dict) -> bool:
    """事件槽是否有任何行(实施/预案/停止实施; 0 金额预案行=显式不分配也算有行)"""
    return slot["impl"] is not None or slot["plan"] is not None or slot["stopped"] is not None


def _analyze(events: list[dict], date_str: str, share_now_wan: float | None) -> dict:
    """锚/完整性/目标财年分析 + 相关财年分红总额, 纯函数

    锚走查(自当前财年降序): 首个"年度事件有行"的财年即锚(预案先行); 无行财年在
    T ≥ 7/31(Y+1) 时推定年度分红为零(齐备, 走查同样终止)。走查终止于第一个齐备财年,
    其上一自然财年即估算目标(年度分红尚未宣告)。
    share_now_wan: 当前总股本(万股)。有值时财年总额 = Σ(级联每股金额×基准股本, base
    缺失按当前总股本退化), 单位万元; **None 时退化为 Σ 级联每股金额直接相加**(纯每股
    口径——有 base 的事件"每股×万股"与每股金额量纲混合无意义, 只在忽略 base 时自洽),
    单位元/股, 调用方直接作 DPS 使用。

    返回键:
      anchor / anchor_ready / anchor_via_fallback
      static_total_wan   锚财年分红总额(万元; share 缺失时为每股退化额); 无锚 None
      target             估算目标财年
      target_total_wan   目标财年当前级联总额(万元, 年度未宣告时=中期等部分值;
                         share 缺失时为每股退化额)
    """
    pit_events = _pitt_filter(events, date_str)
    fy = _group_by_fy(pit_events)
    current_year = int(date_str[:4])

    anchor: int | None = None
    anchor_via_fallback = False
    for y in range(current_year, int(RETENTION_FLOOR[:4]) - 1, -1):
        slots = fy.get(y) or {}
        has_row = any(
            end_date.endswith("1231") and _slot_has_row(slot) for end_date, slot in slots.items()
        )
        if has_row:
            anchor = y
            break
        # 无行且过 7/31(Y+1): 推定该财年年度分红为零(齐备, 走查终止)
        if y <= current_year - 1 and date_str >= f"{y + 1}{FALLBACK_MONTH_DAY}":
            anchor = y
            anchor_via_fallback = True
            break

    def _fy_total_wan(year: int) -> float:
        total = 0.0
        for slot in (fy.get(year) or {}).values():
            picked = _slot_pick(slot)
            if picked is None:
                continue
            amt, base = picked
            if share_now_wan:
                total += amt * (base if base else share_now_wan)
            else:
                total += amt
        return total

    target = anchor + 1 if (anchor is not None and anchor < current_year) else current_year
    return {
        "anchor": anchor,
        "anchor_ready": anchor is not None,
        "anchor_via_fallback": anchor_via_fallback,
        "static_total_wan": _fy_total_wan(anchor) if anchor is not None else None,
        "target": target,
        "target_total_wan": _fy_total_wan(target),
    }


def compute_dividend_dps(
    provider, date: datetime
) -> tuple[dict[str, float], dict[str, float], dict[str, int]]:
    """计算每股股息率分子 DPS(元/股, 总额法÷当前总股本) 双口径: (est_map, static_map, stats)

    est_map: "TTM估算值"口径(Web 默认)——进行中财年的宣告优先/外推补位值
    static_map: "静态"口径——最近完整分红年度的级联总额
    键缺失 = 无数据(未知/无锚且无实绩, 前端显示"—"); 值可为 0.0(齐备且零分红, 是数值参与合成)

    估算 = payout(锚财年) × 归母TTM ÷ 当前总股本: payout = 锚财年分红总额 ÷ 锚财年归母
    净利润(年报值, 与分红预案同日披露, PIT 同批可见), **payout 超 EST_PAYOUT_CAP(95%) 时封顶**
    (锚年利润塌方/分红刚性/特别分红的异常锚防荒谬外推); 归母TTM 复用 PE 归母-TTM
    (get_ts_code_to_ttm_attr_profit, 含业绩快报双源合并——估算利润源与 PE 完全同源同 PIT)。
    锚财年总额=0(停发)时估算恒 0(不猜复分红, 复分红由预案级联接管); **归母TTM ≤ 0(某季度
    大亏)按 0 利润估算 → 0.00% 参与合成而非"—"**(分红率稳定假设下亏损期分红为零的正确推论);
    **锚年亏损但仍在分红(profit ≤ 0 且总额 > 0)按 payout=0 处理 → 0.00% 参与合成**(2026-08-30
    定稿, 实测城建发展/华发股份 FY2025 亏损仍派现触发; 实绩 target_dps > 0 时仍由宣告值接管);
    TTM 缺失(无财报新股)/锚年利润缺失或未披露时估算无定义(交由实绩/无数据兜底)。
    目标财年实绩超过估算时用实绩(部分实绩防低估), 否则维持估算。
    总额法分子: 事件级 每股派现×base_share(缺失按当前股本退化); **当前股本缺失的股票
    退化为每股金额直接相加(忽略 base_share——'每股×万股'与每股口径混合无意义, 且 payout
    无定义跳过估算、由实绩兜底)**。结果按计算日缓存。
    """
    cached = provider._div_dps_cache.get(date)
    if cached is not None:
        return cached

    date_str = date.strftime("%Y%m%d")
    history = provider.dividend_history
    # 当前总股本(万股, daily_basic 同请求缓存; 停牌股由市值回退路径顺带补齐; 缺失按每股退化)
    share_map = provider.get_ts_code_to_total_share(date)

    est_map: dict[str, float] = {}
    static_map: dict[str, float] = {}
    stats = {
        "stocks_total": 0,
        "stocks_static": 0,
        "stocks_static_zero": 0,
        "stocks_static_fallback": 0,  # 锚经 7/31 推定(无年度行)的只数
        "stocks_est": 0,
        "stocks_est_zero": 0,
        "stocks_est_realized": 0,  # 实绩接管(部分实绩>估算)只数
        "stocks_est_payout_capped": 0,  # payout 超上限被封顶的只数
        "stocks_est_zero_profit": 0,  # TTM≤0 按 0 利润估算(0.00% 参与合成)的只数
        "stocks_est_zero_payout": 0,  # 锚年亏损但仍在分红、按 payout=0 估算(0.00% 参与合成)的只数
        "stocks_no_anchor": 0,
        "stocks_no_profit": 0,  # payout 无法计算(锚年利润缺失/≤0 且总额>0)
        "stocks_no_share": 0,  # 当前股本缺失, 总额法按每股退化
    }

    ttm_attr, _ = provider.get_ts_code_to_ttm_attr_profit(date)
    _, fina_per_stock = provider._fina_per_stock(date)

    for ts_code in history.codes():
        stats["stocks_total"] += 1
        share_wan = share_map.get(ts_code)
        if share_wan is None or share_wan <= 0:
            share_wan = None
            stats["stocks_no_share"] += 1

        info = _analyze(history.events(ts_code), date_str, share_wan)

        # 静态口径: 锚财年分红总额 ÷ 当前总股本(share 缺失时 _analyze 已按每股退化)
        static_dps: float | None = None
        if info["anchor_ready"]:
            if info["anchor_via_fallback"]:
                stats["stocks_static_fallback"] += 1
            static_dps = _round6(max(info["static_total_wan"] / share_wan, 0.0)) if share_wan else _round6(max(info["static_total_wan"], 0.0))
            static_map[ts_code] = static_dps
            stats["stocks_static"] += 1
            if static_dps == 0.0:
                stats["stocks_static_zero"] += 1
        else:
            stats["stocks_no_anchor"] += 1

        # 估算: payout(锚) × 归母TTM ÷ 当前总股本; payout 超过 EST_PAYOUT_CAP 时封顶
        estimate: float | None = None
        anchor = info["anchor"]
        if anchor is not None:
            if static_dps == 0.0:
                estimate = 0.0  # 停发锚: 估算恒 0(不猜复分红, 复分红由预案级联接管)
            elif share_wan is not None:
                # payout 需要真实总额(万元)÷当前股本折算——share 缺失时 _analyze 产出为
                # 每股退化额, payout 无定义, 跳过估算(estimate=None 落实绩 target_dps 兜底)
                annual_period = f"{anchor}1231"
                record = fina_per_stock.get(ts_code, {}).get(annual_period)
                profit = record[2] if record is not None else None
                visible = False
                if record is not None:
                    ann = record[0] or provider._fina_ann_date_floor(annual_period)
                    visible = ann <= date_str
                ttm = ttm_attr.get(ts_code)
                if profit is not None and visible and profit > 0 and ttm is not None and info["static_total_wan"] > 0:
                    if ttm <= 0:
                        # 大亏/零利润(TTM≤0): 按 0 利润估算 → 0.00% 参与合成而非"—"
                        #"分红率稳定"假设下的正确推论=亏损期分红为零; 负值守卫由此取代
                        estimate = 0.0
                        stats["stocks_est_zero_profit"] += 1
                    else:
                        payout = info["static_total_wan"] / (profit / 1e4)  # 万元/万元无量纲
                        if payout > EST_PAYOUT_CAP:
                            payout = EST_PAYOUT_CAP
                            stats["stocks_est_payout_capped"] += 1
                        estimate = _round6(payout * (ttm / 1e4) / share_wan)
                        if estimate < 0:
                            estimate = None
                elif (
                    profit is not None and visible and profit <= 0
                    and info["static_total_wan"] > 0
                ):
                    # 锚年亏损但仍在分红(实测城建发展/华发股份 FY2025: 亏损仍派现): payout 分母
                    # ≤0 无定义 → **按 payout=0 处理**(估算 0.00% 参与合成而非"—", 2026-08-30
                    # 定稿; 与 TTM≤0 的 0 利润分支同语义——锚年亏损即视作当年无分红能力外推);
                    # 实绩接管自动生效(target_dps > 0 时仍由宣告值替换); 利润缺失/未披露仍无数据"—"
                    estimate = 0.0
                    stats["stocks_est_zero_payout"] += 1
                else:
                    stats["stocks_no_profit"] += 1

        # 默认口径: 估算目标财年的"宣告优先、外推补位"
        dps: float | None
        target_dps = (
            _round6(max(info["target_total_wan"] / share_wan, 0.0))
            if (share_wan and info["target_total_wan"])
            else (_round6(max(info["target_total_wan"], 0.0)) if info["target_total_wan"] else 0.0)
        )
        if estimate is None:
            dps = target_dps if target_dps > 0 else None
        elif target_dps > estimate:
            dps = target_dps
            stats["stocks_est_realized"] += 1
        else:
            dps = estimate
        if dps is not None:
            est_map[ts_code] = dps
            stats["stocks_est"] += 1
            if dps == 0.0:
                stats["stocks_est_zero"] += 1

    result = (est_map, static_map, stats)
    provider._div_dps_cache[date] = result
    return result


def _round6(v: float) -> float:
    return round(v, 6)
