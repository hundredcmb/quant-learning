from config import logger
from datetime import datetime
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import HistoryRequest, BarData


def datafeed_example():
    datafeed = get_datafeed()
    logger.info(type(datafeed)) # 根据 SETTINGS["datafeed.name"] 确定数据服务类型
    req = HistoryRequest(
        symbol="600036",
        exchange=Exchange.SSE,
        start=datetime(2025, 8, 12),
        end=datetime(2025, 8, 13),
        interval=Interval.DAILY,
    )
    bar_data_list: list[BarData] = datafeed.query_bar_history(req)
    for bar_data in bar_data_list:
        logger.info(bar_data)


if __name__ == "__main__":
    datafeed_example()
