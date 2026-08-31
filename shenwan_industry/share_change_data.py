"""
股本台阶数据层与回购注销金额计算 (ShareChangeHistory + compute_buyback_amount)

- 台阶法(实测见 docs/financial_indicators.md 第 7.5 节): **总股本逐日 diff 找负台阶 = 回购注销**。
  Tushare 无回购注销专用接口(repurchase 仅覆盖回购买入、不分注销/库存股), 股本登记数据是
  注销的唯一权威事实源——只统计负台阶(注销), 正台阶(送转/增发/转债转股/激励行权)只识别不
  归因、不干扰负台阶(实测美的 2024~2026: 首尾净变化 +5.9 亿股完全掩盖 -1.77 亿注销, 逐台阶
  是唯一正确姿势); 回购进库存股不减股本 → 本口径天然只算"已注销"(真回报股东), 回购未注销
  的库存不计入
- 持久缓存: data/share_change_events.json = 最新一份全市场总股本快照 + 台阶事件列表。
  快照/事件来自 daily_basic 按 trade_date 全市场拉取——**经 provider.daily_basic_rows
  三级查找(内存 → SQLite data/market.db → 网络)与市值拉取共享同一份行数据**, 两子系统
  零重复请求、历史交易日零网络; 每个交易日与前一日快照 diff, 只落盘变化行(全市场日均有
  变化的股票约几百只, 18 个月事件数万条、文件几 MB)。增量 = (snapshot_date, today]
  逐交易日补拉链式 diff; 首刷回填 RETENTION_DAYS 窗口内全部交易日(约 370 个请求,
  3 次/秒档约 2 分钟, 一次性)
- 事件结构: {ts_code: [{"d": 交易日, "p": 前股本, "n": 新股本(万股), "x": 当日收盘(元)}]}
  (按日期升序)。x 缺失(数据行 NaN)的事件保留但金额计零(防御, 实测未见)
- 注销金额(TTM 窗口口径, 供"TTM估算股息+注销率"):
  * **窗口与 TTM 净利润严格一致**(market_data.get_ts_code_to_ttm_window): 标准式 =
    (去年同季期末, 最新报告期期末], 逐股 PIT(含业绩快报提前)、长度恒 12 个月、随报告期披露
    整体右移——期内新注销最迟约 4 个月后随窗口右移计入, 与 TTM 利润"披露后才进"同一逻辑;
    次新股(4/k 年化兜底)左端不限(= 上市以来, 注销金额≈0 可忽略)
  * 金额 = Σ(负台阶量 × 台阶日收盘), 单位万元(万股×元); 价格口径实测误差: 台阶日收盘
    茅台 -6.9% / 回购期 VWAP +2.8%(台阶日收盘偏低是"注销登记日常处回购期末"的随机时点
    效应, 全市场批量取台阶日收盘零额外请求, 单股精确核对用 share_change_cache --check)
  * **对称小台阶对剔除**: -X 后 30 自然日内出现 +X(相对差 <0.1%)视为过户回补噪声成对剔除
    (实测美的 20260612 -109.6 万股 / 20260622 +109.6 万股); 小负台阶(激励股折扣价回购注销,
    市价高估约 3 倍)统一按市价估算、金额占比实测 ~3%, 文档注明不单独修正
  * est_bb 合成语义见 industry_ranking.daily_dividend_yield: 股息分子 + 注销金额同分母相除,
    注销缺失(无 TTM 窗口=无财报)的股票不产出 est_bb(unknown≠zero, 与股息率三态一致)
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger("shenwan_industry.share_change_data")

# 持久缓存文件(与 dividend_history.json 同目录同约定: 结构稳定、随仓库提交)
CACHE_PATH = Path(__file__).resolve().parent / "data" / "share_change_events.json"
CACHE_VERSION = 1
# 快照/事件保留下限(自然日): TTM 窗口左端最早 = D-16 个月(披露滞后最多 4 个月 + 12 个月窗口),
# 18.5 个月留裕量; 更早事件(超长停披股的标准式窗口)被裁、该股按窗口内可得事件计算
RETENTION_DAYS = 560
# 对称对冲窗口(自然日)与相对容差: -X 后该窗口内出现 +X(相对差 ≤ 容差)即视为数据源口径
# 跳变/过户回补成对剔除——实测四例全覆盖: 美的 20260612/-109.63 ↔ 0622/+109.63(同日级)；
# 百济神州 20250807/-11,060.4 ↔ 0915/+11,142.1(39 天, 差 0.74%)、20251107/-10,242.3 ↔
# 1205/+10,242.3(28 天, 精确, 两笔合计虚增 546 亿、repurchase 零公告)；九号 20260420/
# -65,039.1 ↔ 0612/+65,811.8(53 天, 回补混入其他增股差 1.19%)。真注销(-X)与同量增发
# (+X±2%)在 90 天内巧合的概率极低(激励行权量与注销量普遍差一个量级), 误伤可忽略
HEDGE_WINDOW_DAYS = 90
HEDGE_REL_TOL = 2e-2
# 单笔注销量/台阶前股本上限(2026-08-30 定稿 6%): **仅兜底"无价格证据"的台阶**——repurchase
# 交叉验证优先(见下), vol 匹配且隐含均价正常的台阶豁免本守卫(实测科捷智能 7.1%/1.5 亿与
# 西山科技 6.3%/1.97 亿 vol 匹配、均价在市价区间=正常回购, 小市值大比例回购真实存在);
# 无匹配(分批注销/月度累计/公告窗口外)的大占比台阶无价格旁证, >6% 剔除——已知命中: 九号
# 两笔 90% 与百济两笔 7.2%/6.6%(数据脏跳变, 对冲 90 天为第一道防线、守卫双保险)
MAX_CANCEL_RATIO = 0.06
# repurchase 交叉验证(2026-08-30 定稿): 完成公告 vol 与台阶量精确匹配(±REP_MATCH_TOL,
# 公告日 ∈ [台阶日-400 天, 台阶日+7 天])时, **隐含均价 = amount/vol 是真实回购价格**:
# * 均价 ≤ REP_ZERO_PRICE(0.1 元/股) = **对赌/激励类 0/1 元注销**(公司没花钱: 交易对方按
#   业绩承诺被名义对价回购注销、或激励股未达标 0 元收回)→ 剔除, 与占比无关——实测天山生物
#   23.4%/amount=0、创新新材 8.6%/1 元、富乐德 8.3%/0、润泽科技 5.06%/42 亿/1 元、宝鼎
#   科技 5.03%/1 元, 以及建发/中铁/中软等国企激励股 0 元注销 160 笔;
# * 均价在 [台阶日收盘×REP_PRICE_BAND_LO, ×REP_PRICE_BAND_HI] = **正常市价回购** → 金额
#   直接用公告 amount(比台阶日收盘更准: 茅台精确 30.0 亿 vs 市价口径 27.9 亿; ST能特 5.0
#   亿 vs 6.3 亿)且**豁免数量级守卫**; 带外的价格视为 amount 字段异常, 回退市价+守卫路径
# * vol 不匹配(九安 2024 轮 2,922.6 万股分两批注销、美的月度累计口径)→ 不受影响, 走
#   市价+守卫; 回购公告缓存未就绪时整体跳过交叉验证(增强而非前置, 不降级 est_bb)
REP_MATCH_TOL = 2e-2
REP_ZERO_PRICE = 0.1  # 隐含均价(元/股)低于此值判定对赌/激励类(0/1 元注销的均价 ~0.000x)
REP_PRICE_BAND_LO = 0.1  # 正常回购价格带下限(×台阶日收盘)
REP_PRICE_BAND_HI = 3.0  # 正常回购价格带上限(×台阶日收盘; 实测带内: 蓝丰 0.51/茅台 1.07/科捷 0.68)


class ShareChangeHistory:
    """全市场总股本台阶事件持久缓存: 文件加载 + 首刷/增量刷新 + 每股事件读取

    进程内单实例(经 MarketDataProvider.share_change_history 惰性创建); ensure_refresh 幂等,
    内部加锁防重入(预热线程与主线程并发调用时只刷一次)。失败向上抛且不落盘(下次运行重试
    同窗口), 内存进度丢弃安全——事件由快照链式 diff 派生, 重拉幂等
    """

    def __init__(self, provider) -> None:
        self._provider = provider  # MarketDataProvider: 用其 pro/节流器/交易日历
        self._lock = threading.Lock()
        self._loaded = False
        self._events: dict[str, list[dict]] = {}  # ts_code -> [{d,p,n,x}](升序)
        self.snapshot_date: str | None = None  # 快照对应的交易日 YYYYMMDD
        self._snapshot: dict[str, float] = {}  # ts_code -> 总股本(万股)
        self._backfill_start: str | None = None  # 事件覆盖起点(首刷窗口首个交易日, 事件表实际覆盖下界)

    # ---------- 文件与拉取 ----------

    def _load(self) -> None:
        if self._loaded:
            return
        if CACHE_PATH.exists():
            try:
                raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                if int(raw.get("version", 0)) == CACHE_VERSION:
                    self._events = raw.get("events", {})
                    self.snapshot_date = raw.get("snapshot_date")
                    self._snapshot = raw.get("snapshot", {})
                    self._backfill_start = raw.get("backfill_start")
                else:
                    logger.warning(
                        f"股本台阶缓存版本不匹配(文件 v{raw.get('version')} != v{CACHE_VERSION}), 全量重建"
                    )
            except Exception as err:  # noqa: BLE001 - 缓存损坏视为无缓存重建
                logger.warning(f"股本台阶缓存文件损坏, 将全量重建: {err!r}")
        self._loaded = True

    def _save(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "snapshot_date": self.snapshot_date,
            "backfill_start": self._backfill_start,
            "snapshot": self._snapshot,
            "events": self._events,
        }
        CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _fetch_day_with_close(self, day_str: str) -> tuple[dict[str, float], dict[str, float | None]]:
        """首刷/增量共用的当日快照(总股本 + 收盘价; 收盘供事件定价, NaN 记 None)

        经 provider.daily_basic_rows 取全市场原始行(内存 → SQLite data/market.db → 网络
        三级查找), 与市值拉取共享同一份数据零重复请求; 空结果(未出数/节假日)返回空 dict,
        由调用方按原语义处理
        """
        day_dt = datetime.strptime(day_str, "%Y%m%d")
        rows = self._provider.daily_basic_rows(day_dt)
        shares: dict[str, float] = {}
        closes: dict[str, float | None] = {}
        for ts_code, r in rows.items():
            total_share = r.get("total_share")
            if total_share is not None and total_share > 0:
                shares[ts_code] = total_share
            closes[ts_code] = r.get("close")
        return shares, closes

    def ensure_refresh(self, cancel_check=None) -> str:
        """确保台阶事件覆盖到今天, 幂等加锁: 首刷(回填 RETENTION_DAYS) / 增量(逐交易日链式 diff)

        - **首刷**(无缓存/快照日缺失): 回填 [today-RETENTION_DAYS, today] 全部交易日, 链式
          diff 前一日快照落事件, 末日本身也入快照(供后续增量衔接)
        - **增量**: (snapshot_date, today] 逐交易日 diff; 逐日链式保证跳过中间任何天数都不断链
        返回动作说明; 失败向上抛(由调用方降级), 不落盘不推进 snapshot_date
        """
        with self._lock:
            self._load()
            today_str = datetime.now().strftime("%Y%m%d")
            if self.snapshot_date and self.snapshot_date >= today_str:
                return "up-to-date"
            if self.snapshot_date:
                start_str = self.snapshot_date
                base_snapshot = dict(self._snapshot)
                action = "incremental"
            else:
                start_str = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y%m%d")
                base_snapshot = {}
                action = "full-backfill"
                self._backfill_start_pending = True
            days = self._provider.get_trading_days(start_str, today_str)
            if action == "incremental":
                # 增量从快照日**之后**的交易日开始(快照日本身已 diff 过, 重跑防重复事件)
                days = [d for d in days if d > self.snapshot_date]
            if not days:
                return "up-to-date"
            if getattr(self, "_backfill_start_pending", False):
                self._backfill_start = days[0]
                self._backfill_start_pending = False
            new_events = 0
            for day in days:
                if cancel_check is not None:
                    cancel_check()
                if not base_snapshot:
                    shares, closes = self._fetch_day_with_close(day)
                    # 窗口首日只建基准快照(无前日可 diff)
                    base_snapshot = shares
                    continue
                shares, closes = self._fetch_day_with_close(day)
                for ts_code, new_share in shares.items():
                    prev = base_snapshot.get(ts_code)
                    if prev is not None and prev != new_share:
                        self._events.setdefault(ts_code, []).append(
                            {
                                "d": day,
                                "p": prev,
                                "n": new_share,
                                "x": closes.get(ts_code),
                            }
                        )
                        new_events += 1
                base_snapshot = shares
            # 裁剪过期事件(保留窗口外的老事件无收益, 控制体积)
            floor = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y%m%d")
            removed = 0
            for ts_code, evs in self._events.items():
                keep = [e for e in evs if e["d"] >= floor]
                removed += len(evs) - len(keep)
                self._events[ts_code] = keep
            self.snapshot_date = days[-1]
            self._snapshot = base_snapshot
            self._save()
            return f"{action} {days[0]}~{days[-1]} 共{len(days)}个交易日 新增事件{new_events}条 裁剪过期{removed}条"

    # ---------- 读取 ----------

    @property
    def coverage_start(self) -> str | None:
        """事件表实际覆盖的最早交易日(首刷窗口起点; None=未首刷)——供历史回看边界告警"""
        self._load()
        return self._backfill_start

    def is_ready(self) -> bool:
        """缓存是否已完成至少一次成功刷新(snapshot_date 非空)

        空缓存(未首刷)与"窗口内无台阶事件"严格区分: 未就绪时 compute_buyback_amount 直接
        抛错走 est_bb 口径降级(前端"—"), 绝不把"没数据"当"注销为零"——与股息率三态的
        unknown≠zero 哲学一致
        """
        self._load()
        return self.snapshot_date is not None

    def events(self, ts_code: str) -> list[dict]:
        """读取单股台阶事件列表(升序; 未加载时惰性加载)"""
        self._load()
        return self._events.get(ts_code, [])

    def codes(self) -> list[str]:
        """有台阶事件的全部股票代码"""
        self._load()
        return sorted(self._events)

    def summary(self) -> dict:
        """缓存体检统计(供 share_change_cache.py CLI)"""
        self._load()
        total = sum(len(v) for v in self._events.values())
        negative = sum(1 for v in self._events.values() for e in v if e["p"] > e["n"])
        years: dict[str, int] = {}
        for v in self._events.values():
            for e in v:
                y = e["d"][:4]
                years[y] = years.get(y, 0) + 1
        return {
            "stocks_with_events": len(self._events),
            "events_total": total,
            "events_negative": negative,
            "events_by_year": dict(sorted(years.items())),
            "snapshot_date": self.snapshot_date,
            "snapshot_stocks": len(self._snapshot),
            "backfill_start": self._backfill_start,
        }


class RepurchaseHistory:
    """全市场回购公告持久缓存(按日历月全市场拉取): 供注销台阶的 vol 交叉验证

    repurchase 接口不支持按 ts_code 过滤, 按月拉全市场(单月 200~700 行单页即回)本地筛;
    只保留 proc ∈ {完成, 实施}(vol 有值的阶段, 预案行 vol 恒空不参与匹配), 记录
    {ann, end, proc, vol, amount}。首刷回填 24 个月(约 24 请求, 回购期可早于注销一年
    以上——实测九安 2024-07 完成/2025-03 注销、时代电气疑为 2023 年回购的注销); 增量 =
    未拉过的月份 + **当月总是重拉**(月内公告随时新增, 重拉幂等去重)。进程内单实例经
    MarketDataProvider.repurchase_history 惰性创建, ensure_refresh 幂等加锁; 失败向上抛
    且不落盘。**交叉验证是增强而非前置**: 缓存未就绪时 compute_buyback_amount 跳过验证
    (不剔除, 维持台阶口径), 不降级 est_bb
    """

    CACHE_PATH = Path(__file__).resolve().parent / "data" / "repurchase_records.json"
    CACHE_VERSION = 1
    RETENTION_DAYS = 730  # 公告保留 24 个月(anna_date 裁剪), 覆盖"早回购晚注销"的匹配窗口
    KEEP_PROCS = ("完成", "实施")

    def __init__(self, provider) -> None:
        self._provider = provider
        self._lock = threading.Lock()
        self._loaded = False
        self._records: dict[str, list[dict]] = {}  # ts_code -> [{ann,end,proc,vol,amount}]
        self._months_done: list[str] = []  # 已拉取的日历月 YYYYMM(含当月重拉)

    def _load(self) -> None:
        if self._loaded:
            return
        if self.CACHE_PATH.exists():
            try:
                raw = json.loads(self.CACHE_PATH.read_text(encoding="utf-8"))
                if int(raw.get("version", 0)) == self.CACHE_VERSION:
                    self._records = raw.get("records", {})
                    self._months_done = list(raw.get("months_done", []))
                else:
                    logger.warning(
                        f"回购公告缓存版本不匹配(文件 v{raw.get('version')} != v{self.CACHE_VERSION}), 全量重建"
                    )
            except Exception as err:  # noqa: BLE001 - 缓存损坏视为无缓存重建
                logger.warning(f"回购公告缓存文件损坏, 将全量重建: {err!r}")
        self._loaded = True

    def _save(self) -> None:
        self.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.CACHE_VERSION,
            "months_done": sorted(self._months_done),
            "records": self._records,
        }
        self.CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _fetch_month(self, month: str) -> None:
        """拉取单月全市场回购公告并合并去重(单页 2000 上限内, 不分页防御极端披露月)"""
        self._provider._acquire_rate_slot("repurchase")
        df = self._provider.pro.repurchase(
            start_date=f"{month}01",
            end_date=f"{month}31",
            fields="ts_code,ann_date,end_date,proc,vol,amount",
        )
        if df is not None and not df.empty:
            for r in df.itertuples(index=False):
                proc = str(getattr(r, "proc", "") or "")
                if proc not in self.KEEP_PROCS:
                    continue
                vol = getattr(r, "vol", None)
                amount = getattr(r, "amount", None)
                rec = {
                    "ann": str(getattr(r, "ann_date", "") or ""),
                    "end": str(getattr(r, "end_date", "") or "") or None,
                    "proc": proc,
                    "vol": None if vol is None or pd.isna(vol) else float(vol),
                    "amount": None if amount is None or pd.isna(amount) else float(amount),
                }
                bucket = self._records.setdefault(str(r.ts_code), [])
                if rec not in bucket:
                    bucket.append(rec)
        if month not in self._months_done:
            self._months_done.append(month)

    def ensure_refresh(self, cancel_check=None) -> str:
        """确保公告覆盖到当前月, 幂等加锁: 首刷回填 24 个月 / 增量补未拉月份 + 当月重拉"""
        with self._lock:
            self._load()
            today = datetime.now()
            cur_month = today.strftime("%Y%m")
            start = (today - timedelta(days=self.RETENTION_DAYS)).strftime("%Y%m")
            months: list[str] = []
            y, m = int(start[:4]), int(start[4:])
            while f"{y:04d}{m:02d}" <= cur_month:
                months.append(f"{y:04d}{m:02d}")
                m += 1
                if m > 12:
                    y, m = y + 1, 1
            todo = [x for x in months if x not in self._months_done]
            if cur_month not in todo and cur_month not in self._months_done:
                todo.append(cur_month)
            # 当月总是重拉(月内公告随时新增); 已拉过的历史月不重拉
            todo.append(cur_month)
            todo = list(dict.fromkeys(todo))
            for month in todo:
                if cancel_check is not None:
                    cancel_check()
                self._fetch_month(month)
            # 裁剪过期公告(按 ann_date)
            floor = (today - timedelta(days=self.RETENTION_DAYS)).strftime("%Y%m%d")
            removed = 0
            for ts_code, recs in self._records.items():
                keep = [r for r in recs if not (r["ann"] and r["ann"] < floor)]
                removed += len(recs) - len(keep)
                self._records[ts_code] = keep
            self._save()
            return f"{len(todo)}个月重拉 新增月份{len([x for x in todo if x != cur_month])} 裁剪过期{removed}条"

    def is_ready(self) -> bool:
        self._load()
        return bool(self._months_done)

    def records(self, ts_code: str) -> list[dict]:
        self._load()
        return self._records.get(ts_code, [])

    def summary(self) -> dict:
        self._load()
        return {
            "stocks": len(self._records),
            "records_total": sum(len(v) for v in self._records.values()),
            "months_done": len(self._months_done),
            "earliest_month": min(self._months_done) if self._months_done else None,
        }


def _match_buyback(records: list[dict] | None, delta_wan: float, step_date: str) -> dict | None:
    """在回购公告中找与台阶量精确匹配(±REP_MATCH_TOL)的记录(价格判定由调用方负责)

    匹配条件: proc 完成/实施(缓存已过滤)、vol 与台阶量相对差 ≤ 容差、公告日 ∈
    [台阶日-400 天, 台阶日+7 天](公告可晚于股本登记日 1~2 天; 回购完成到注销的合理时滞,
    九安实测 8 个月、时代电气疑为 16 个月)。多条命中取公告日最新; 无匹配返回 None
    (分批注销/月度累计口径不匹配单台阶 → None, 走市价+守卫路径)
    """
    if not records:
        return None
    target = delta_wan * 1e4
    best: dict | None = None
    for rec in records:
        vol = rec.get("vol")
        if not vol or vol <= 0:
            continue
        if abs(vol - target) / target > REP_MATCH_TOL:
            continue
        ann = rec.get("ann") or ""
        if ann and not (_shift_date(step_date, -400) <= ann <= _shift_date(step_date, 7)):
            continue
        if best is None or ann > (best.get("ann") or ""):
            best = rec
    return best


def _shift_date(day: str, days: int) -> str:
    return (datetime.strptime(day, "%Y%m%d") + timedelta(days=days)).strftime("%Y%m%d")


# ---------- 注销金额计算(纯函数部分, 便于独立验证) ----------


def _hedge_filter(events: list[dict]) -> list[dict]:
    """对称对冲剔除: 升序事件里 -X 注销后 HEDGE_WINDOW_DAYS 日内出现 +X(相对差 < 容差)的
    过户回补对成对剔除; 返回剩余事件(保持原序)"""
    used = [False] * len(events)
    for i, ev in enumerate(events):
        delta_i = ev["p"] - ev["n"]  # 正 = 注销(股本减少)
        if delta_i <= 0 or used[i]:
            continue
        try:
            d_i = datetime.strptime(ev["d"], "%Y%m%d")
        except ValueError:
            continue
        for j in range(i + 1, len(events)):
            if used[j]:
                continue
            try:
                gap = (datetime.strptime(events[j]["d"], "%Y%m%d") - d_i).days
            except ValueError:
                continue
            if gap > HEDGE_WINDOW_DAYS:
                break
            delta_j = events[j]["p"] - events[j]["n"]  # 负 = 增股
            if delta_j < 0 and abs(delta_j + delta_i) <= max(1e-4, HEDGE_REL_TOL * delta_i):
                used[i] = used[j] = True
                break
    return [ev for idx, ev in enumerate(events) if not used[idx]]


def _negative_amount_wan(
    events: list[dict], start_excl: str, end_incl: str, buyback_records: list[dict] | None = None
) -> tuple[float, int, int, int, int]:
    """窗口 (start_excl, end_incl] 内负台阶的注销金额(万元)、计入笔数、守卫剔除数、
    对赌剔除数、公告金额笔数——**价格证据 > 对冲启发式 > 数量级守卫** 三层决策

    start_excl 为空串 = 左端不限(次新股年化兜底, = 上市以来); YYYYMMDD 字典序即日期序。
    三遍式(升序事件):
    1. **匹配分类**: 负台阶与完成公告 vol 精确匹配(±REP_MATCH_TOL) → ①隐含均价 ≤
       REP_ZERO_PRICE = 对赌/激励 0/1 元注销 → 剔除(与占比无关); ②均价 ∈ [close×带] =
       正常市价回购 → **铁证: 不参与对冲、豁免数量级守卫、金额直接用公告 amount**(真实
       回购成本比台阶日收盘准——实测对冲启发式曾误杀此列: 蓝丰生化 20250919 真注销
       1,971.63 万股后 20251208 增发 2,003 万股, 量差 1.59% 落进对冲容差; 价格证据优先后
       不再误伤); ③带外价格 = amount 字段可疑, 视同无匹配
    2. **对称对冲(仅无价格证据的负台阶)**: -X 后 90 自然日内出现 +X(相对差 ≤2%)视为数据
       口径跳变/回补噪声成对剔除(实测百济两笔 546 亿/九号 53 天回补/美的 ±109.63)
    3. **计入**: 对冲幸存的负台阶中占比 > MAX_CANCEL_RATIO(6%)剔除(脏跳变兜底), 其余
       按 Δ×台阶日收盘计; 窗口过滤与收盘缺失防御同旧版
    """
    evs = sorted(events, key=lambda e: e["d"])
    n = len(evs)
    used = [False] * n
    # 第一遍: 匹配分类(vam=0/1 元注销剔除, bb=公告金额铁证)
    verdict: dict[int, str] = {}
    matched_amt_wan: dict[int, float] = {}
    for i, ev in enumerate(evs):
        delta_wan = ev["p"] - ev["n"]
        if delta_wan <= 0:
            continue
        close = ev.get("x")
        if close is None or not math.isfinite(close) or close <= 0:
            close = None
        if buyback_records is None:
            continue
        matched = _match_buyback(buyback_records, delta_wan, ev["d"])
        if matched is None:
            continue
        vol, amt = matched["vol"], matched.get("amount") or 0.0
        price = amt / vol if vol else 0.0
        if price <= REP_ZERO_PRICE:
            verdict[i] = "vam"
        elif close is not None and REP_PRICE_BAND_LO * close <= price <= REP_PRICE_BAND_HI * close:
            verdict[i] = "bb"
            matched_amt_wan[i] = amt / 1e4
        # 带外: 视同无匹配, 走对冲+守卫
    # 第二遍: 对冲(仅无价格证据的负台阶; 有铁证的绝不参与——真注销后的真增发量级巧合不误伤)
    for i, ev in enumerate(evs):
        delta_i = ev["p"] - ev["n"]
        if delta_i <= 0 or used[i] or i in verdict:
            continue
        try:
            d_i = datetime.strptime(ev["d"], "%Y%m%d")
        except ValueError:
            continue
        for j in range(i + 1, n):
            if used[j]:
                continue
            try:
                gap = (datetime.strptime(evs[j]["d"], "%Y%m%d") - d_i).days
            except ValueError:
                continue
            if gap > HEDGE_WINDOW_DAYS:
                break
            delta_j = evs[j]["p"] - evs[j]["n"]
            if delta_j < 0 and abs(delta_j + delta_i) <= max(1e-4, HEDGE_REL_TOL * delta_i):
                used[i] = used[j] = True
                break
    # 第三遍: 窗口过滤 + 计入/剔除计数
    amount = 0.0
    count = 0
    large_skipped = 0
    vam_skipped = 0
    from_buyback = 0
    for i, ev in enumerate(evs):
        delta_wan = ev["p"] - ev["n"]
        if delta_wan <= 0 or used[i]:
            continue
        if ev["d"] > end_incl:
            continue
        if start_excl and ev["d"] <= start_excl:
            continue
        v = verdict.get(i)
        if v == "vam":
            vam_skipped += 1
            continue  # 对赌/激励 0/1 元注销: 公司没花钱, 不是股东回报
        if v == "bb":
            amount += matched_amt_wan[i]
            count += 1
            from_buyback += 1
            continue  # 公告金额铁证, 豁免守卫
        if delta_wan / ev["p"] > MAX_CANCEL_RATIO:
            large_skipped += 1
            continue
        close = ev.get("x")
        if close is None or not math.isfinite(close) or close <= 0:
            continue  # 收盘缺失的事件量保留语义但金额无法计(实测未见, 防御)
        amount += delta_wan * close
        count += 1
    return amount, count, large_skipped, vam_skipped, from_buyback


def compute_buyback_amount(provider, date: datetime) -> tuple[dict[str, float], dict[str, int]]:
    """计算各股 TTM 窗口内的回购注销金额(万元): (amount_map, stats)

    窗口 = 该股归母 TTM 净利润的覆盖时段(market_data.get_ts_code_to_ttm_window, PIT 含业绩
    快报提前、与"TTM估算股息率"的利润源完全同期)——标准式 (去年同季期末, 最新报告期期末],
    年化兜底左端不限。amount_map 含 0.0(窗口内无注销是数值, est_bb=股息率本身); 无 TTM
    窗口(无财报)的股票不产出(交由 est_bb 的"仅对 est 有值股票产出"规则显示"—")。
    结果由 provider.get_ts_code_to_buyback_amount 按计算日缓存
    """
    history = provider.share_change_history
    if not history.is_ready():
        raise RuntimeError("股本台阶缓存未就绪(首刷未完成), est_bb 口径本次降级")
    stats = {
        "bb_window_stocks": 0,  # 有 TTM 窗口的股票(= 有财报)
        "bb_stocks": 0,  # 窗口内注销金额 > 0 的只数
        "bb_amount_wan_total": 0.0,  # 全市场窗口内注销金额合计(万元)
        "bb_events": 0,  # 计入金额的负台阶事件数(对冲后)
        "bb_no_window": 0,  # 有台阶事件但无 TTM 窗口(无财报)无法计入的只数
        "bb_coverage_limited": 0,  # 查询日过早: 窗口左端早于事件覆盖起点, 注销分量可能低估
        "bb_large_skipped": 0,  # 数量级守卫剔除的负台阶笔数(仅无价格证据的台阶, >MAX_CANCEL_RATIO)
        "bb_vam_skipped": 0,  # 交叉验证剔除笔数(对赌/激励 0/1 元注销, vol 匹配且隐含均价≈0)
        "bb_amount_from_buyback": 0,  # 金额改用公告 amount 的笔数(vol 匹配且价格正常, 豁免守卫)
        "bb_vam_ready": 0,  # repurchase 缓存是否就绪(0=未就绪跳过交叉验证, 1=已验证)
    }
    # 交叉验证是增强而非前置: repurchase 缓存未就绪时跳过(不剔除, 维持台阶口径)
    rep_history = provider.repurchase_history
    rep_ready = rep_history.is_ready()
    stats["bb_vam_ready"] = 1 if rep_ready else 0
    coverage = history.coverage_start
    if coverage:
        typical_left = (date - timedelta(days=490)).strftime("%Y%m%d")  # D-16个月(窗口左端最早常态)
        if typical_left < coverage:
            stats["bb_coverage_limited"] = 1
            logger.warning(
                f"{date:%Y%m%d} 查询日较早: 典型 TTM 窗口左端 {typical_left} 早于台阶事件覆盖起点 "
                f"{coverage}(保留 {RETENTION_DAYS} 天), 注销分量可能低估(对查询日 ≥ 今天−约2.4个月完整)"
            )
    windows, _window_stats = provider.get_ts_code_to_ttm_window(date)
    stats["bb_window_stocks"] = len(windows)
    amount_map: dict[str, float] = {}
    covered = set(windows)
    for ts_code in history.codes():
        if ts_code not in covered:
            stats["bb_no_window"] += 1
    for ts_code, (start_excl, end_incl) in windows.items():
        amount, count, large_skipped, vam_skipped, from_buyback = _negative_amount_wan(
            history.events(ts_code), start_excl, end_incl,
            buyback_records=rep_history.records(ts_code) if rep_ready else None,
        )
        amount_map[ts_code] = amount
        stats["bb_events"] += count
        stats["bb_large_skipped"] += large_skipped
        stats["bb_vam_skipped"] += vam_skipped
        stats["bb_amount_from_buyback"] += from_buyback
        if amount > 0:
            stats["bb_stocks"] += 1
            stats["bb_amount_wan_total"] += amount
    return amount_map, stats
