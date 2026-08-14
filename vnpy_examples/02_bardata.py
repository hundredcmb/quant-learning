# 直接运行本脚本时，把仓库根目录加入 sys.path，以便 import config 等根目录模块
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import logger
from datetime import datetime
from functools import lru_cache
from vnpy.trader.object import BarData
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange, Interval

# 初始化数据库对象, 类型为 SETTINGS["database.name"], 会自动设置数据库连接参数
database = get_database()
logger.info(f"type(database)={type(database)}")


def save_bar_data_example() -> None:
    """把外部 K 线数据导入 vnpy 数据库"""
    bar_data: BarData = BarData(
        gateway_name="DB",
        symbol="600036",
        exchange=Exchange.SSE,
        datetime=datetime(2025, 8, 8),
        interval=Interval.DAILY,
        volume=456421.53,
        turnover=2044145.472,
        open_interest=0,
        open_price=45.2,
        high_price=45.28,
        low_price=44.53,
        close_price=44.53,
    )
    # stream: 插入一批连续的最新数据=True, 补充不连续的历史数据=False
    # 唯一约束(symbol, exchange, interval, datetime), 冲突会废除旧数据并插入新数据
    database.save_bar_data([bar_data], stream=False)


@lru_cache(maxsize=999)
def load_bar_data_example(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start: datetime,
    end: datetime
) -> list[BarData]:
    """
    - 读取 vnpy 数据库中的 K 线数据
    - 注意：应使用 lru 缓存, 避免频繁调用相同的数据接口
    """
    bar_data_list: list[BarData] = database.load_bar_data(symbol, exchange, interval, start, end)
    return bar_data_list


if __name__ == '__main__':
    b_list = load_bar_data_example(
        symbol="600036",
        exchange=Exchange.SSE,
        interval=Interval.DAILY,
        start=datetime(2025, 8, 1),
        end=datetime(2025, 8, 15),
    )
    for b_data in b_list:
        logger.info(b_data)

    # 清除缓存
    load_bar_data_example.cache_clear()
