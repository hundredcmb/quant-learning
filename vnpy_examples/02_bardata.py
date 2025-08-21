import datetime
from config import logger
from functools import lru_cache
from vnpy.trader.object import BarData
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange, Interval


def save_bar_data_example() -> None:
    """把外部 K 线数据导入 vnpy 数据库"""
    database = get_database()
    logger.info(type(database))  # 根据 SETTINGS["database.name"] 确定数据库类型
    bar_data: BarData = BarData(
        gateway_name="DB",
        symbol="600036",
        exchange=Exchange.SSE,
        datetime=datetime.datetime(2025, 8, 8),
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
    database = get_database()
    logger.info(type(database))  # 根据 SETTINGS["database.name"] 确定数据库类型
    bar_data_list: list[BarData] = database.load_bar_data(symbol, exchange, interval, start, end)
    return bar_data_list


if __name__ == '__main__':
    save_bar_data_example()
    bar_data_list = load_bar_data_example(
        symbol="600036",
        exchange=Exchange.SSE,
        interval=Interval.DAILY,
        start=datetime.datetime(2025, 8, 1),
        end=datetime.datetime(2025, 8, 15),
    )
    for bar_data in bar_data_list:
        logger.info(bar_data)

    # 清除缓存
    load_bar_data_example.cache_clear()
