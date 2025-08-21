import datetime
from config import logger
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


def load_bar_data_example() -> None:
    """读取 vnpy 数据库中的 K 线数据"""
    database = get_database()
    logger.info(type(database))  # 根据 SETTINGS["database.name"] 确定数据库类型
    bar_data_list = database.load_bar_data(
        symbol="600036",
        exchange=Exchange.SSE,
        interval=Interval.DAILY,
        start=datetime.datetime(2025, 8, 8),
        end=datetime.datetime(2025, 8, 8),
    )
    for bar_data in bar_data_list:
        logger.info(bar_data)


if __name__ == '__main__':
    save_bar_data_example()
    load_bar_data_example()
