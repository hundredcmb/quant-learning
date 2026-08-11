"""MySQL 连接自检：验证 database/session.py 能否从 vnpy 配置动态获取数据库信息。

用法（用 vnpy 自带 Python 运行）：
    C:\\veighna_studio\\python.exe test_mysql_connection.py
"""
from sqlalchemy import create_engine, text

from database import session
from vnpy.trader.setting import SETTINGS


def main() -> None:
    print("===== vnpy 数据库配置（脱敏） =====")
    for key in (
        "database.name",
        "database.host",
        "database.port",
        "database.database",
        "database.user",
        "database.timezone",
    ):
        print(f"  SETTINGS[{key!r}] = {SETTINGS[key]!r}")

    print("===== 连接 MySQL =====")
    # 单独创建带连接超时的引擎，避免网络不通时长时间卡住
    engine = create_engine(
        session.DB_MYSQL_URL,
        connect_args={"connect_timeout": 10},
        pool_pre_ping=True,
    )
    with engine.connect() as conn:
        print("  SELECT 1    ->", conn.execute(text("SELECT 1")).scalar())
        print("  VERSION()   ->", conn.execute(text("SELECT VERSION()")).scalar())
        print("  DATABASE()  ->", conn.execute(text("SELECT DATABASE()")).scalar())
        rows = conn.execute(text("SHOW TABLES")).fetchall()
        print(f"  表数量       -> {len(rows)}")
        print("  前 10 张表   ->", [row[0] for row in rows[:10]])
    print("===== 连接成功 =====")


if __name__ == "__main__":
    main()
