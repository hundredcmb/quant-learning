from typing import Generator
from config import DB_MYSQL_URL
from sqlalchemy import create_engine
from contextlib import contextmanager
from sqlalchemy.orm import sessionmaker, Session

# 创建数据库引擎
database_engine = create_engine(
    DB_MYSQL_URL,
    echo=False,  # 是否打印 SQL 语句
    pool_pre_ping=True,  # 检查连接是否可用
    pool_recycle=3600,  # 默认连接池中连接的闲置超时时间（秒）
    pool_size=10,  # 连接池保持的默认连接数
    max_overflow=20,  # 允许临时超出连接池大小的最大连接数
    pool_timeout=30,  # 获取连接的超时时间（秒）
    pool_use_lifo=True  # 使用LIFO（后进先出）策略管理连接池
)

# 创建会话工厂
session_local = sessionmaker(
    autocommit=False, # 自动提交事务
    autoflush=False, # 自动刷新实例
    bind=database_engine,
    expire_on_commit=False, # 实例过期时间
)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """数据库会话上下文管理器，自动处理会话生命周期：
    - 自动创建会话
    - 业务逻辑无异常时提交事务
    - 发生异常时回滚事务
    - 最终确保会话关闭
    """
    db_session_instance = session_local()
    try:
        yield db_session_instance
        db_session_instance.commit()  # 手动提交事务
    except Exception:
        db_session_instance.rollback()  # 异常时回滚
        raise
    finally:
        db_session_instance.close()  # 确保会话关闭


def get_db() -> Generator[Session, None, None]:
    """FastAPI依赖注入专用的数据库会话生成器，"""
    with db_session() as db:
        yield db
