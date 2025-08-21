from config import logger
from datetime import datetime
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import HistoryRequest, BarData


def datafeed_example():
    """
    - 使用 vnpy 的数据服务功能下载指定的 K 线数据
    - vnpy 客户端集成环境, 需要在客户端UI 或 配置文件 ~/.vntrader/vt_setting.json 中配置数据服务
    - 官方文档: https://www.vnpy.com/docs/cn/community/info/datafeed.html
    - 经过测试, 几种数据服务都需要订阅, tushare 性价比最高(200米/年), 适合散户
    """
    datafeed = get_datafeed()
    logger.info(type(datafeed)) # 根据 SETTINGS["datafeed.name"] 确定数据服务类型

    # 构造查询参数, 这里我们计划下载的是招商银行的历史日线数据
    req = HistoryRequest(
        symbol="600036", # A 股六位数字代码
        exchange=Exchange.SSE, # 交易所
        start=datetime(2024, 1, 1), # 开始时间
        end=datetime(2025, 8, 15), # 结束时间
        interval=Interval.DAILY, # K 线级别
    )

    # 下载数据
    bar_data_list: list[BarData] = datafeed.query_bar_history(req)

    # 把下载到的数据保存到数据库
    database = get_database()
    logger.info(type(database))  # 根据 SETTINGS["database.name"] 确定数据库类型
    database.save_bar_data(bar_data_list, stream=False)


if __name__ == "__main__":
    datafeed_example()
