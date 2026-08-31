"""
行情/市值 SQLite 持久层 (MarketStore)

- 目标: daily / daily_basic 逐日全市场行情与市值快照落盘复用。历史交易日数据不可变,
  每次进程重启(或 CLI 逐次运行)重复逐日拉取是纯浪费; SQLite 单文件免服务、Python
  标准库自带零新增依赖, 单年约 240 交易日 × 每接口 1 页的规模对其毫无压力
- **入库时机 = 写穿(write-through)**: MarketDataProvider 三级查找(内存 → 本库 → 网络)
  走到网络并成功拉回**非空**数据的那一刻, 由拉取方顺手写库——单交易日一个事务
  (约 5400 行、毫秒级), 不在查询关键路径上可感知; 不设独立入库流程, 所有现有入口
  (单日榜/区间榜/估值走势/台阶快照/CLI)自动成为写入口, 库随正常使用有机生长、
  只存真正被用到的日期
- 两条防脏数据规则(与估值走势"当日行情缺失跳过不落键"同哲学):
  * **空结果绝不落库**: 某交易日接口返回 0 行(未收盘/未出数)不写 fetch_log,
    读取视为未命中, 下次自动重试
  * **近端易变尾窗口不信任库值**: 距今 MARKET_DB_VOLATILE_DAYS 自然日内的日期
    读取时绕过库直走网络(收盘数据发布后短时间内可能被 Tushare 修正, 如成交额
    补全), 写入照常(upsert 覆盖)——错误数据活不过尾窗口
- **完整日元表 fetch_log**: "该日全市场已拉取完成"的唯一判定依据((trade_date, api)
  主键)。停牌回退的逐股点查只写内存缓存、**不读写本库**——库内行集恒等于某次
  全市场拉取的原始有效行, 不会有"表里有该日部分行"的中间态(第一期范围; 点查
  走库属将来扩展, 见 docs/known_issues.md 第 47 条)
- 表结构(只存原始行; 派生值——涨跌幅/自由流通市值——读取时按 market_data 原公式
  重算, 口径唯一不落库):
      daily(trade_date, ts_code, close, pre_close, amount)
      daily_basic(trade_date, ts_code, close, total_mv, free_share, float_share, total_share)
      fina_raw / bs_raw / express_raw(end_date, ts_code, seq, ann_date, ...)——财报三池
        **原始版本行**(seq=同股行序, 保留重复行与响应顺序, 字段级去重/行内合成/PIT
        过滤全部留在读取时的 market_data 原逻辑, 存储层零口径折叠; 报告期刷新=整期
        DELETE+INSERT, FINA_VOLATILE_MONTHS 内的期每次走网络覆盖)
      dividend_ex(ex_date, ts_code, seq, ...)——dividend(ex_date=D) 当日除息/送转全记录
      trade_cal(cal_date, is_open) + trade_cal_spans(连续覆盖跨度, 区间并集合并)
      index_weight(index_code, trade_date, con_code)——样本空间月度快照(历史月不可变)
      stock_basic / index_member_all——树构建快照(**日级新鲜度**: snapshots.fetched_at
        为当日才命中, 新上市/退市/成分调整次日生效; 刷新=整表 DELETE+INSERT)
      fetch_log(trade_date, api, row_count, fetched_at)——完整日元表, **第一列是通用键**
        (交易日/报告期/index_weight 的 "指数|月份"), (键, api) 唯一判定"已完整拉取"
  amount 列可空: 区间链式预取路径(fields=ts_code,close,pre_close)不拉 amount,
  该路径 upsert 不覆盖已有 amount; get_ts_code_to_amount 补拉全字段后写全列
- 并发: WAL + 每线程独立连接 + busy_timeout(写事务毫秒级; Web 与 CLI 跨进程同时
  写由 SQLite 串行化, 重复写被 upsert 幂等吸收, 最坏=两进程各拉一遍同日, 与改造
  前持平); 连续失败 MARKET_DB_MAX_FAILURES 次熔断——本进程内禁用降级纯网络,
  榜单可用性不受影响(警告日志可见); **批量读库必须在调用线程顺序执行**(
  fetch_daily_batch/fetch_mv_batch 对库命中日期顺序直读、线程池只服务网络日期——
  DB 行物化与网络 I/O 混跑会触发 GIL 押送效应, 见 known_issues 第 47 条⑧)
- 库文件 data/market.db 已 gitignore(可再生的数据不进 git; 丢失重灌成本约
  1 分钟/年, 见 market_cache.py --backfill)
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("shenwan_industry.market_store")

# ============================== 核心配置 ==============================
# 库文件路径(单一常量: 将来若要共享给 holders 等模块, 只改这里挪位置)
DB_PATH = Path(__file__).resolve().parent / "data" / "market.db"
# 近端易变尾窗口(自然日): 距今 N 天内的日期读取时不信任库值、直走网络覆盖写。
# 收盘数据发布后短时间内可能被 Tushare 修正, 5 天 = 今日 + 前 4 个自然日,
# 顺带覆盖跨周末/三天小长假的间隔; 更早的日期视为不可变、纯读库
MARKET_DB_VOLATILE_DAYS = 5
# 财报三池(fina/bs/express)的报告期易变窗口(月): 报告期 end_date 距今不足 N 个月的期
# **每次都走网络重拉**(与改造前行为一致——财报更正/追溯调整集中发生在披露后两个年报
# 周期内, 主窗口 [D-24月, D] 恰好全在易变带内, 热路径请求量不变); 更早的长尾(同比基期
# 24~36 个月、历史复盘窗口)信任库值零网络。更久远的追溯修正不会被自动感知, 须
# market_cache.py --force-fina 手动重拉
FINA_VOLATILE_MONTHS = 24
# 连续失败熔断阈值: SQLite 异常(文件损坏/磁盘满/被占用)自上次成功后累计到此次数,
# 本进程内禁用(降级纯网络, 不影响榜单), 警告日志可见
MARKET_DB_MAX_FAILURES = 3
# 写锁等待(毫秒): 跨进程同时写时排队上限(单日一个事务毫秒级, 30s 足够宽裕)
MARKET_DB_BUSY_TIMEOUT_MS = 30_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    trade_date TEXT NOT NULL,
    ts_code    TEXT NOT NULL,
    close      REAL,
    pre_close  REAL,
    amount     REAL,
    PRIMARY KEY (trade_date, ts_code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily_basic (
    trade_date  TEXT NOT NULL,
    ts_code     TEXT NOT NULL,
    close       REAL,
    total_mv    REAL,
    free_share  REAL,
    float_share REAL,
    total_share REAL,
    PRIMARY KEY (trade_date, ts_code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fetch_log (
    trade_date TEXT NOT NULL,
    api        TEXT NOT NULL,
    row_count  INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, api)
);
CREATE TABLE IF NOT EXISTS fina_raw (
    end_date TEXT NOT NULL,
    ts_code  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    ann_date TEXT NOT NULL DEFAULT '',
    deduct   REAL,
    extra    REAL,
    bps      REAL,
    roe_waa  REAL,
    PRIMARY KEY (end_date, ts_code, seq)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bs_raw (
    end_date    TEXT NOT NULL,
    ts_code     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    ann_date    TEXT NOT NULL DEFAULT '',
    report_type TEXT NOT NULL DEFAULT '',
    eq_exc_min  REAL,
    oth_eqt     REAL,
    PRIMARY KEY (end_date, ts_code, seq)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS express_raw (
    end_date  TEXT NOT NULL,
    ts_code   TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    ann_date  TEXT NOT NULL DEFAULT '',
    n_income  REAL,
    PRIMARY KEY (end_date, ts_code, seq)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dividend_ex (
    ex_date      TEXT NOT NULL,
    ts_code      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    cash_div     REAL,
    cash_div_tax REAL,
    stk_div      REAL,
    stk_bo_rate  REAL,
    stk_co_rate  REAL,
    div_proc     TEXT NOT NULL DEFAULT '',
    div_listdate TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ex_date, ts_code, seq)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS trade_cal (
    cal_date TEXT NOT NULL PRIMARY KEY,
    is_open  INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS trade_cal_spans (
    span_start TEXT NOT NULL,
    span_end   TEXT NOT NULL,
    PRIMARY KEY (span_start, span_end)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS index_weight (
    index_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    con_code   TEXT NOT NULL,
    PRIMARY KEY (index_code, trade_date, con_code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code     TEXT NOT NULL PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    list_date   TEXT NOT NULL DEFAULT '',
    delist_date TEXT,
    list_status TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS index_member_all (
    ts_code  TEXT NOT NULL,
    l3_code  TEXT NOT NULL,
    in_date  TEXT NOT NULL DEFAULT '',
    out_date TEXT,
    is_new   TEXT NOT NULL DEFAULT 'Y',
    PRIMARY KEY (ts_code, l3_code, in_date, out_date, is_new)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS snapshots (
    api        TEXT NOT NULL PRIMARY KEY,
    fetched_at TEXT NOT NULL
);
"""


def _f(v) -> float | None:
    """安全转 float: 缺失/NaN/非有限值 → None(存储为 SQL NULL)"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class MarketStore:
    """daily/daily_basic 全市场快照的 SQLite 读写层(线程安全: 每线程独立连接)"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else DB_PATH
        self._local = threading.local()
        self._failures = 0  # 自上次成功以来的连续失败数
        self._disabled = False
        try:
            self._conn()  # 触发建目录/建库/建表
        except sqlite3.Error as err:
            self._bail(err)

    # ---------- 连接与容错 ----------

    def _conn(self) -> sqlite3.Connection:
        """取当前线程的连接(惰性创建; WAL 模式对跨进程读写并发友好)"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self._db_path, timeout=MARKET_DB_BUSY_TIMEOUT_MS / 1000.0
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(f"PRAGMA busy_timeout={MARKET_DB_BUSY_TIMEOUT_MS}")
            conn.executescript(_SCHEMA)  # 幂等: 每个新连接重跑 CREATE IF NOT EXISTS
            self._local.conn = conn
        return conn

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def disabled(self) -> bool:
        """熔断标志(真=本进程内已禁用, 调用方应直走网络)"""
        return self._disabled

    def _bail(self, err: Exception) -> None:
        self._failures += 1
        if self._failures >= MARKET_DB_MAX_FAILURES:
            self._disabled = True
            logger.warning(
                f"行情 SQLite 持久层连续失败 {self._failures} 次, 本进程内禁用"
                f"(降级纯网络, 不影响结果): {err!r}"
            )
        else:
            logger.warning(
                f"行情 SQLite 持久层访问失败({self._failures}/{MARKET_DB_MAX_FAILURES}): {err!r}"
            )

    # ---------- 易变尾窗口 ----------

    def is_volatile(self, date_str: str) -> bool:
        """距今 MARKET_DB_VOLATILE_DAYS 自然日内的日期视为易变(读取绕库, 见模块 docstring)"""
        cutoff = (datetime.now() - timedelta(days=MARKET_DB_VOLATILE_DAYS)).strftime("%Y%m%d")
        return date_str > cutoff

    # ---------- 读取(供 MarketDataProvider 三级查找) ----------

    def load_daily(self, date_str: str) -> list[tuple[str, float, float, float | None]] | None:
        """读取某日全市场 daily 原始行: [(ts_code, close, pre_close, amount|None)]

        None = 未完整拉取过(无 fetch_log)或已熔断/出错——调用方走网络;
        行集恒为写入时过滤后的有效行(close/pre_close 非 None 且 pre_close>0)
        """
        if self._disabled:
            return None
        try:
            conn = self._conn()
            logged = conn.execute(
                "SELECT 1 FROM fetch_log WHERE trade_date=? AND api='daily'", (date_str,)
            ).fetchone()
            if logged is None:
                return None
            rows = conn.execute(
                "SELECT ts_code, close, pre_close, amount FROM daily WHERE trade_date=?",
                (date_str,),
            ).fetchall()
            self._failures = 0
            return [(r["ts_code"], r["close"], r["pre_close"], r["amount"]) for r in rows]
        except sqlite3.Error as err:
            self._bail(err)
            return None

    def load_daily_basic(self, date_str: str) -> dict[str, dict[str, float | None]] | None:
        """读取某日全市场 daily_basic 原始行: {ts_code: {close,total_mv,free_share,float_share,total_share}}

        None 语义同 load_daily; 值为 None 表示原始字段缺失/NaN(派生口径由读取方处理)
        """
        if self._disabled:
            return None
        try:
            conn = self._conn()
            logged = conn.execute(
                "SELECT 1 FROM fetch_log WHERE trade_date=? AND api='daily_basic'", (date_str,)
            ).fetchone()
            if logged is None:
                return None
            rows = conn.execute(
                "SELECT ts_code, close, total_mv, free_share, float_share, total_share"
                " FROM daily_basic WHERE trade_date=?",
                (date_str,),
            ).fetchall()
            self._failures = 0
            return {
                r["ts_code"]: {
                    "close": r["close"],
                    "total_mv": r["total_mv"],
                    "free_share": r["free_share"],
                    "float_share": r["float_share"],
                    "total_share": r["total_share"],
                }
                for r in rows
            }
        except sqlite3.Error as err:
            self._bail(err)
            return None

    # ---------- 写入(写穿: 网络拉回非空后调用) ----------

    def save_daily(
        self,
        date_str: str,
        rows: list[tuple[str, float, float, float | None]],
        include_amount: bool,
    ) -> bool:
        """写穿某日 daily 行(单事务含 fetch_log); include_amount=False 的调用路径
        (区间链式预取只拉 close/pre_close)不覆盖已有 amount 列

        返回是否写入成功(失败仅告警, 调用方结果不受影响)
        """
        if self._disabled or not rows:
            return False
        try:
            conn = self._conn()
            with conn:
                if include_amount:
                    conn.executemany(
                        "INSERT INTO daily(trade_date, ts_code, close, pre_close, amount)"
                        " VALUES (?,?,?,?,?)"
                        " ON CONFLICT(trade_date, ts_code) DO UPDATE SET"
                        " close=excluded.close, pre_close=excluded.pre_close, amount=excluded.amount",
                        [(date_str, c, cl, pc, amt) for c, cl, pc, amt in rows],
                    )
                else:
                    conn.executemany(
                        "INSERT INTO daily(trade_date, ts_code, close, pre_close) VALUES (?,?,?,?)"
                        " ON CONFLICT(trade_date, ts_code) DO UPDATE SET"
                        " close=excluded.close, pre_close=excluded.pre_close",
                        [(date_str, c, cl, pc) for c, cl, pc, _amt in rows],
                    )
                conn.execute(
                    "INSERT INTO fetch_log(trade_date, api, row_count, fetched_at)"
                    " VALUES (?, 'daily', ?, ?)"
                    " ON CONFLICT(trade_date, api) DO UPDATE SET"
                    " row_count=excluded.row_count, fetched_at=excluded.fetched_at",
                    (
                        date_str,
                        conn.execute(
                            "SELECT COUNT(*) FROM daily WHERE trade_date=?", (date_str,)
                        ).fetchone()[0],
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            self._failures = 0
            return True
        except sqlite3.Error as err:
            self._bail(err)
            return False

    def save_daily_basic(self, date_str: str, rows: dict[str, dict[str, float | None]]) -> bool:
        """写穿某日 daily_basic 原始行(单事务含 fetch_log), 语义同 save_daily"""
        if self._disabled or not rows:
            return False
        try:
            conn = self._conn()
            with conn:
                conn.executemany(
                    "INSERT INTO daily_basic(trade_date, ts_code, close, total_mv,"
                    " free_share, float_share, total_share) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(trade_date, ts_code) DO UPDATE SET"
                    " close=excluded.close, total_mv=excluded.total_mv,"
                    " free_share=excluded.free_share, float_share=excluded.float_share,"
                    " total_share=excluded.total_share",
                    [
                        (
                            date_str,
                            c,
                            _f(r.get("close")),
                            _f(r.get("total_mv")),
                            _f(r.get("free_share")),
                            _f(r.get("float_share")),
                            _f(r.get("total_share")),
                        )
                        for c, r in rows.items()
                    ],
                )
                conn.execute(
                    "INSERT INTO fetch_log(trade_date, api, row_count, fetched_at)"
                    " VALUES (?, 'daily_basic', ?, ?)"
                    " ON CONFLICT(trade_date, api) DO UPDATE SET"
                    " row_count=excluded.row_count, fetched_at=excluded.fetched_at",
                    (
                        date_str,
                        conn.execute(
                            "SELECT COUNT(*) FROM daily_basic WHERE trade_date=?", (date_str,)
                        ).fetchone()[0],
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            self._failures = 0
            return True
        except sqlite3.Error as err:
            self._bail(err)
            return False

    # ---------- 财报三池(报告期键, 原始版本行) ----------

    def is_period_volatile(self, period: str) -> bool:
        """报告期是否在易变窗口内(距今不足 FINA_VOLATILE_MONTHS 个月, 见常量注释)"""
        today = datetime.now()
        year = today.year - FINA_VOLATILE_MONTHS // 12
        cutoff = f"{year}{today.month:02d}{today.day:02d}"
        return period >= cutoff

    def _load_period_rows(self, period: str, api: str, cols: str) -> list[tuple] | None:
        """读取某报告期的原始行(按 ts_code, seq 排序保去重所需行序); None=未完整拉取过"""
        if self._disabled:
            return None
        try:
            conn = self._conn()
            logged = conn.execute(
                "SELECT 1 FROM fetch_log WHERE trade_date=? AND api=?", (period, api)
            ).fetchone()
            if logged is None:
                return None
            rows = conn.execute(
                f"SELECT {cols} FROM {api} WHERE end_date=? ORDER BY ts_code, seq", (period,)
            ).fetchall()
            self._failures = 0
            return [tuple(r) for r in rows]
        except sqlite3.Error as err:
            self._bail(err)
            return None

    def _save_period_rows(self, period: str, api: str, cols: str, rows: list[tuple]) -> bool:
        """整期刷新式写穿(DELETE+INSERT+fetch_log, 单事务); 行数记 upsert 后表内实数

        rows 的每个元组以 ts_code 开头(与各 load_* 返回同构、不含 end_date/seq);
        seq = 同股行序(按出现顺序编号), 读取方 ORDER BY ts_code, seq 还原去重语义
        """
        if self._disabled or not rows:
            return False
        try:
            conn = self._conn()
            col_list = cols.split(", ")
            with conn:
                conn.execute(f"DELETE FROM {api} WHERE end_date=?", (period,))
                seq_counters: dict[str, int] = {}
                payload = []
                for r in rows:
                    ts_code = r[0]
                    seq = seq_counters.get(ts_code, 0)
                    seq_counters[ts_code] = seq + 1
                    payload.append((period, seq, *r))
                conn.executemany(
                    f"INSERT INTO {api}(end_date, seq, {cols})"
                    f" VALUES ({','.join('?' * (len(col_list) + 2))})",
                    payload,
                )
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {api} WHERE end_date=?", (period,)
                ).fetchone()[0]
                self._log_fetch(conn, period, api, count)
            self._failures = 0
            return True
        except sqlite3.Error as err:
            self._bail(err)
            return False

    @staticmethod
    def _log_fetch(conn: sqlite3.Connection, key: str, api: str, row_count: int) -> None:
        """fetch_log upsert(键为交易日/报告期/"指数|月份"通用键)"""
        conn.execute(
            "INSERT INTO fetch_log(trade_date, api, row_count, fetched_at) VALUES (?,?,?,?)"
            " ON CONFLICT(trade_date, api) DO UPDATE SET"
            " row_count=excluded.row_count, fetched_at=excluded.fetched_at",
            (key, api, row_count, datetime.now().isoformat(timespec="seconds")),
        )

    def load_fina_period(self, period: str) -> list[tuple[str, str, float | None, float | None, float | None, float | None]] | None:
        """读某期 fina 原始行: [(ts_code, ann_date, 扣非, 非经常损益, bps, roe_waa)](行序保去重语义)"""
        return self._load_period_rows(period, "fina_raw", "ts_code, ann_date, deduct, extra, bps, roe_waa")

    def save_fina_period(self, period: str, raw_rows: list[tuple]) -> bool:
        """写穿某期 fina 原始行(网络路径收集的原始行, 含重复行)"""
        return self._save_period_rows(period, "fina_raw", "ts_code, ann_date, deduct, extra, bps, roe_waa", raw_rows)

    def load_bs_period(self, period: str) -> list[tuple[str, str, str, float | None, float | None]] | None:
        """读某期 bs 原始行: [(ts_code, ann_date, report_type, 归母权益, 其他权益工具)]"""
        return self._load_period_rows(period, "bs_raw", "ts_code, ann_date, report_type, eq_exc_min, oth_eqt")

    def save_bs_period(self, period: str, raw_rows: list[tuple]) -> bool:
        return self._save_period_rows(period, "bs_raw", "ts_code, ann_date, report_type, eq_exc_min, oth_eqt", raw_rows)

    def load_express_period(self, period: str) -> list[tuple[str, str, float | None]] | None:
        """读某期 express 原始行: [(ts_code, ann_date, 快报归母)](版本选择留在读取方)"""
        return self._load_period_rows(period, "express_raw", "ts_code, ann_date, n_income")

    def save_express_period(self, period: str, raw_rows: list[tuple]) -> bool:
        return self._save_period_rows(period, "express_raw", "ts_code, ann_date, n_income", raw_rows)

    def purge_fina(self, period: str) -> None:
        """清除某报告期三池全部痕迹(行 + fetch_log), 供 --force-fina 强制重拉前调用"""
        conn = self._conn()
        with conn:
            for table in ("fina_raw", "bs_raw", "express_raw"):
                conn.execute(f"DELETE FROM {table} WHERE end_date=?", (period,))
                conn.execute(
                    "DELETE FROM fetch_log WHERE trade_date=? AND api=?", (period, table)
                )

    # ---------- 除息日记录(dividend ex_date=D, 按日不可变) ----------

    def load_dividend_ex(self, date_str: str) -> list[dict] | None:
        """读取某日 dividend(ex_date=D) 全记录(与 market_data._fetch_ex_div_records 同形状); None=未完整"""
        if self._disabled:
            return None
        try:
            conn = self._conn()
            logged = conn.execute(
                "SELECT 1 FROM fetch_log WHERE trade_date=? AND api='dividend_ex'", (date_str,)
            ).fetchone()
            if logged is None:
                return None
            rows = conn.execute(
                "SELECT ts_code, cash_div, cash_div_tax, stk_div, stk_bo_rate, stk_co_rate,"
                " div_proc, div_listdate FROM dividend_ex WHERE ex_date=? ORDER BY ts_code, seq",
                (date_str,),
            ).fetchall()
            self._failures = 0
            return [
                {
                    "ts_code": r["ts_code"],
                    "cash_div": r["cash_div"] if r["cash_div"] is not None else 0.0,
                    "cash_div_tax": r["cash_div_tax"] if r["cash_div_tax"] is not None else 0.0,
                    "stk_div": r["stk_div"] if r["stk_div"] is not None else 0.0,
                    "stk_bo_rate": r["stk_bo_rate"] if r["stk_bo_rate"] is not None else 0.0,
                    "stk_co_rate": r["stk_co_rate"] if r["stk_co_rate"] is not None else 0.0,
                    "div_proc": r["div_proc"],
                    "div_listdate": r["div_listdate"],
                }
                for r in rows
            ]
        except sqlite3.Error as err:
            self._bail(err)
            return None

    def save_dividend_ex(self, date_str: str, records: list[dict]) -> bool:
        """写穿某日除息/送转全记录(已按 _fetch_ex_div_records 的口径预处理)"""
        if self._disabled or not records:
            return False
        try:
            conn = self._conn()
            with conn:
                conn.execute("DELETE FROM dividend_ex WHERE ex_date=?", (date_str,))
                conn.executemany(
                    "INSERT INTO dividend_ex(ex_date, ts_code, seq, cash_div, cash_div_tax,"
                    " stk_div, stk_bo_rate, stk_co_rate, div_proc, div_listdate)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            date_str, r["ts_code"], i,
                            _f(r["cash_div"]), _f(r["cash_div_tax"]), _f(r["stk_div"]),
                            _f(r["stk_bo_rate"]), _f(r["stk_co_rate"]),
                            str(r["div_proc"]), str(r["div_listdate"]),
                        )
                        for i, r in enumerate(records)
                    ],
                )
                count = conn.execute(
                    "SELECT COUNT(*) FROM dividend_ex WHERE ex_date=?", (date_str,)
                ).fetchone()[0]
                self._log_fetch(conn, date_str, "dividend_ex", count)
            self._failures = 0
            return True
        except sqlite3.Error as err:
            self._bail(err)
            return False

    # ---------- 交易日历(连续跨度覆盖) ----------

    def get_trading_days(self, start_str: str, end_str: str) -> list[str] | None:
        """从库内连续覆盖跨度取交易日列表(升序); None=[start,end] 未被任何跨度完整覆盖"""
        if self._disabled or start_str > end_str:
            return None
        try:
            conn = self._conn()
            spans = conn.execute(
                "SELECT span_start, span_end FROM trade_cal_spans"
            ).fetchall()
            if not any(r["span_start"] <= start_str and end_str <= r["span_end"] for r in spans):
                return None
            rows = conn.execute(
                "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>=? AND cal_date<=?"
                " ORDER BY cal_date",
                (start_str, end_str),
            ).fetchall()
            self._failures = 0
            return [r["cal_date"] for r in rows]
        except sqlite3.Error as err:
            self._bail(err)
            return None

    def save_trade_cal(self, start_str: str, end_str: str, rows: list[tuple[str, int]]) -> None:
        """写穿一段交易日历(行 + 跨度并集合并); rows=[(cal_date, is_open)]

        只有当返回行数 == 区间自然日数(接口对区间内每个自然日都有一行)时才登记跨度,
        防御未来日期未发布导致的"假覆盖"; 行数据无论是否登记跨度都照常入库
        """
        if self._disabled or not rows:
            return
        try:
            conn = self._conn()
            with conn:
                conn.executemany(
                    "INSERT INTO trade_cal(cal_date, is_open) VALUES (?,?)"
                    " ON CONFLICT(cal_date) DO UPDATE SET is_open=excluded.is_open",
                    rows,
                )
                expected = (datetime.strptime(end_str, "%Y%m%d") - datetime.strptime(start_str, "%Y%m%d")).days + 1
                if len(rows) != expected:
                    return  # 覆盖不完整(如区间伸进未发布的未来日历), 不登记跨度
                spans = [
                    (r["span_start"], r["span_end"])
                    for r in conn.execute("SELECT span_start, span_end FROM trade_cal_spans")
                ]
                spans.append((start_str, end_str))
                merged: list[list[str]] = []
                for s, e in sorted(spans):
                    if merged and datetime.strptime(s, "%Y%m%d") <= datetime.strptime(merged[-1][1], "%Y%m%d") + timedelta(days=1):
                        merged[-1][1] = max(merged[-1][1], e)
                    else:
                        merged.append([s, e])
                conn.execute("DELETE FROM trade_cal_spans")
                conn.executemany(
                    "INSERT INTO trade_cal_spans(span_start, span_end) VALUES (?,?)", merged
                )
            self._failures = 0
        except sqlite3.Error as err:
            self._bail(err)

    # ---------- 样本空间月度快照(历史月不可变) ----------

    def is_month_volatile(self, month_key: str) -> bool:
        """月份(YYYYMM)是否为当前月——当月快照可能仍在滚动, 读取绕库直走网络"""
        return month_key >= datetime.now().strftime("%Y%m")

    def load_index_weight_month(self, index_code: str, month_key: str) -> dict[str, set[str]] | None:
        """读取某指数某月的成分快照 {快照日: {con_code}}; None=未完整拉取过"""
        if self._disabled:
            return None
        key = f"{index_code}|{month_key}"
        try:
            conn = self._conn()
            logged = conn.execute(
                "SELECT 1 FROM fetch_log WHERE trade_date=? AND api='index_weight'", (key,)
            ).fetchone()
            if logged is None:
                return None
            rows = conn.execute(
                "SELECT trade_date, con_code FROM index_weight WHERE index_code=?"
                " AND substr(trade_date,1,6)=?",
                (index_code, month_key),
            ).fetchall()
            per_month: dict[str, set[str]] = {}
            for r in rows:
                per_month.setdefault(r["trade_date"], set()).add(r["con_code"])
            self._failures = 0
            return per_month
        except sqlite3.Error as err:
            self._bail(err)
            return None

    def save_index_weight_month(self, index_code: str, month_key: str, per_month: dict[str, set[str]]) -> bool:
        """写穿某指数某月快照(整月 DELETE+INSERT); 空月不落库(当月尚未发布快照)"""
        if self._disabled or not per_month:
            return False
        try:
            conn = self._conn()
            with conn:
                conn.execute(
                    "DELETE FROM index_weight WHERE index_code=? AND substr(trade_date,1,6)=?",
                    (index_code, month_key),
                )
                conn.executemany(
                    "INSERT INTO index_weight(index_code, trade_date, con_code) VALUES (?,?,?)",
                    [
                        (index_code, trade_date, con_code)
                        for trade_date, codes in sorted(per_month.items())
                        for con_code in sorted(codes)
                    ],
                )
                count = conn.execute(
                    "SELECT COUNT(*) FROM index_weight WHERE index_code=? AND substr(trade_date,1,6)=?",
                    (index_code, month_key),
                ).fetchone()[0]
                self._log_fetch(conn, f"{index_code}|{month_key}", "index_weight", count)
            self._failures = 0
            return True
        except sqlite3.Error as err:
            self._bail(err)
            return False

    # ---------- 树构建快照(日级新鲜度) ----------

    def _snapshot_is_fresh(self, api: str) -> bool:
        """快照是否为今日拉取(树数据每日可能变化: 新上市/退市/成分调整, 次日强制刷新)"""
        row = self._conn().execute(
            "SELECT fetched_at FROM snapshots WHERE api=?", (api,)
        ).fetchone()
        return row is not None and row["fetched_at"][:10] == datetime.now().strftime("%Y-%m-%d")

    def load_stock_basic(self) -> list[dict] | None:
        """读取股票基础信息快照(L/D/P 三态合一); None=无当日快照"""
        if self._disabled:
            return None
        try:
            if not self._snapshot_is_fresh("stock_basic"):
                return None
            rows = self._conn().execute(
                "SELECT ts_code, name, list_date, delist_date, list_status FROM stock_basic"
            ).fetchall()
            self._failures = 0
            return [
                {
                    "ts_code": r["ts_code"],
                    "name": r["name"],
                    "list_date": r["list_date"],
                    "delist_date": r["delist_date"],
                    "list_status": r["list_status"],
                }
                for r in rows
            ]
        except sqlite3.Error as err:
            self._bail(err)
            return None

    def save_stock_basic(self, rows: list[dict]) -> bool:
        """写穿股票基础信息快照(整表替换 + snapshots.fetched_at=now)"""
        if self._disabled or not rows:
            return False
        try:
            conn = self._conn()
            with conn:
                conn.execute("DELETE FROM stock_basic")
                conn.executemany(
                    "INSERT INTO stock_basic(ts_code, name, list_date, delist_date, list_status)"
                    " VALUES (?,?,?,?,?)",
                    [
                        (
                            r["ts_code"], str(r.get("name") or ""),
                            str(r.get("list_date") or ""), r.get("delist_date"),
                            str(r.get("list_status") or ""),
                        )
                        for r in rows
                    ],
                )
                conn.execute(
                    "INSERT INTO snapshots(api, fetched_at) VALUES ('stock_basic', ?)"
                    " ON CONFLICT(api) DO UPDATE SET fetched_at=excluded.fetched_at",
                    (datetime.now().isoformat(timespec="seconds"),),
                )
            self._failures = 0
            return True
        except sqlite3.Error as err:
            self._bail(err)
            return False

    def load_index_member_all(self) -> tuple[list[tuple], list[tuple]] | None:
        """读取申万成分历史归属快照: (Y 当前成分 records, N 历史退出 records); None=无当日快照

        元素 (ts_code, l3_code, in_date, out_date|None), 与 industry_tree._pull 返回同构
        """
        if self._disabled:
            return None
        try:
            if not self._snapshot_is_fresh("index_member_all"):
                return None
            rows = self._conn().execute(
                "SELECT ts_code, l3_code, in_date, out_date, is_new FROM index_member_all"
            ).fetchall()
            self._failures = 0
            y_records: list[tuple] = []
            n_records: list[tuple] = []
            for r in rows:
                # out_date 以空串入库(WITHOUT ROWID 的 PK 列隐含 NOT NULL, None 存不进), 读出还原
                rec = (r["ts_code"], r["l3_code"], r["in_date"], r["out_date"] or None)
                (y_records if r["is_new"] == "Y" else n_records).append(rec)
            return y_records, n_records
        except sqlite3.Error as err:
            self._bail(err)
            return None

    def save_index_member_all(self, y_records: list[tuple], n_records: list[tuple]) -> bool:
        """写穿成分归属快照(整表替换 + snapshots.fetched_at=now)"""
        if self._disabled or not y_records:
            return False
        try:
            conn = self._conn()
            with conn:
                conn.execute("DELETE FROM index_member_all")
                conn.executemany(
                    "INSERT INTO index_member_all(ts_code, l3_code, in_date, out_date, is_new)"
                    " VALUES (?,?,?,?,?)",
                    [(c, l3, in_s, out_s or "", "Y") for c, l3, in_s, out_s in y_records]
                    + [(c, l3, in_s, out_s or "", "N") for c, l3, in_s, out_s in n_records],
                )
                conn.execute(
                    "INSERT INTO snapshots(api, fetched_at) VALUES ('index_member_all', ?)"
                    " ON CONFLICT(api) DO UPDATE SET fetched_at=excluded.fetched_at",
                    (datetime.now().isoformat(timespec="seconds"),),
                )
            self._failures = 0
            return True
        except sqlite3.Error as err:
            self._bail(err)
            return False



    # ---------- 体检与维护(供 market_cache.py CLI) ----------

    def purge(self, date_str: str) -> None:
        """彻底清除某日全部痕迹(两张表行 + fetch_log), 供 --force 强制重拉前调用"""
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM daily WHERE trade_date=?", (date_str,))
            conn.execute("DELETE FROM daily_basic WHERE trade_date=?", (date_str,))
            conn.execute("DELETE FROM dividend_ex WHERE ex_date=?", (date_str,))
            conn.execute(
                "DELETE FROM fetch_log WHERE trade_date=? AND api IN ('daily','daily_basic','dividend_ex')",
                (date_str,),
            )

    def logged_dates(self, api: str) -> list[str]:
        """某接口已完成全市场拉取的日期列表(升序)"""
        rows = self._conn().execute(
            "SELECT trade_date FROM fetch_log WHERE api=? ORDER BY trade_date", (api,)
        ).fetchall()
        return [r["trade_date"] for r in rows]

    def stats(self) -> dict:
        """体检统计: 各接口覆盖键数/范围、行数对账、库文件大小、树快照新鲜度"""
        conn = self._conn()
        out: dict = {"db_path": str(self._db_path)}
        # fetch_log 键→行数对账(fetch_log 的 api 记表名——load/save/purge 三端一致;
        # 展示名另行映射)
        key_tables = (
            ("daily", "daily"),
            ("daily_basic", "daily_basic"),
            ("fina_raw", "fina_raw"),
            ("bs_raw", "bs_raw"),
            ("express_raw", "express_raw"),
            ("dividend_ex", "dividend_ex"),
        )
        for api, table in key_tables:
            logged = conn.execute(
                "SELECT trade_date, row_count FROM fetch_log WHERE api=? ORDER BY trade_date",
                (api,),
            ).fetchall()
            mismatch = []
            date_col = (
                "ex_date" if table == "dividend_ex"
                else "trade_date" if table in ("daily", "daily_basic")
                else "end_date"
            )
            for r in logged:
                actual = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {date_col}=?",
                    (r["trade_date"],),
                ).fetchone()[0]
                if actual != r["row_count"]:
                    mismatch.append((r["trade_date"], r["row_count"], actual))
            display = {
                "fina_raw": "fina_indicator_vip",
                "bs_raw": "balancesheet_vip",
                "express_raw": "express_vip",
            }.get(api, api)
            out[display] = {
                "dates": len(logged),
                "first": logged[0]["trade_date"] if logged else None,
                "last": logged[-1]["trade_date"] if logged else None,
                "row_count_mismatch": mismatch,
            }
        # index_weight(键为 "指数|月份")
        logged_iw = conn.execute(
            "SELECT trade_date, row_count FROM fetch_log WHERE api='index_weight' ORDER BY trade_date"
        ).fetchall()
        out["index_weight"] = {
            "dates": len(logged_iw),
            "first": logged_iw[0]["trade_date"] if logged_iw else None,
            "last": logged_iw[-1]["trade_date"] if logged_iw else None,
            "row_count_mismatch": [],
        }
        # 交易日历覆盖跨度
        spans = conn.execute(
            "SELECT span_start, span_end FROM trade_cal_spans ORDER BY span_start"
        ).fetchall()
        out["trade_cal"] = {"spans": [(r["span_start"], r["span_end"]) for r in spans]}
        # 树快照新鲜度
        for api in ("stock_basic", "index_member_all"):
            row = conn.execute(
                "SELECT fetched_at FROM snapshots WHERE api=?", (api,)
            ).fetchone()
            out[api] = {"fetched_at": row["fetched_at"] if row else None}
        try:
            out["db_size_mb"] = round(self._db_path.stat().st_size / 1024 / 1024, 2)
        except OSError:
            out["db_size_mb"] = None
        return out
