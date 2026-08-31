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
      fetch_log(trade_date, api, row_count, fetched_at)
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

    # ---------- 体检与维护(供 market_cache.py CLI) ----------

    def purge(self, date_str: str) -> None:
        """彻底清除某日全部痕迹(两张表行 + fetch_log), 供 --force 强制重拉前调用"""
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM daily WHERE trade_date=?", (date_str,))
            conn.execute("DELETE FROM daily_basic WHERE trade_date=?", (date_str,))
            conn.execute("DELETE FROM fetch_log WHERE trade_date=?", (date_str,))

    def logged_dates(self, api: str) -> list[str]:
        """某接口已完成全市场拉取的日期列表(升序)"""
        rows = self._conn().execute(
            "SELECT trade_date FROM fetch_log WHERE api=? ORDER BY trade_date", (api,)
        ).fetchall()
        return [r["trade_date"] for r in rows]

    def stats(self) -> dict:
        """体检统计: 各接口覆盖日期数/范围、行数对账、库文件大小"""
        conn = self._conn()
        out: dict = {"db_path": str(self._db_path)}
        for api, table in (("daily", "daily"), ("daily_basic", "daily_basic")):
            logged = conn.execute(
                "SELECT trade_date, row_count FROM fetch_log WHERE api=? ORDER BY trade_date",
                (api,),
            ).fetchall()
            mismatch = []
            for r in logged:
                actual = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE trade_date=?", (r["trade_date"],)
                ).fetchone()[0]
                if actual != r["row_count"]:
                    mismatch.append((r["trade_date"], r["row_count"], actual))
            out[api] = {
                "dates": len(logged),
                "first": logged[0]["trade_date"] if logged else None,
                "last": logged[-1]["trade_date"] if logged else None,
                "row_count_mismatch": mismatch,
            }
        try:
            out["db_size_mb"] = round(self._db_path.stat().st_size / 1024 / 1024, 2)
        except OSError:
            out["db_size_mb"] = None
        return out
