from copy import deepcopy
from datetime import datetime, timedelta

import pandas as pd
from pandas import DataFrame
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.database import get_database
from vnpy.trader.utility import round_to, ZoneInfo
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import HistoryRequest, BarData
from vnpy_tushare.tushare_datafeed import (
    to_ts_symbol,
    EXCHANGE_VT2TS,
    INTERVAL_VT2TS,
    INTERVAL_ADJUSTMENT_MAP,
)

from config import logger

# 初始化数据服务对象, 类型为 SETTINGS["datafeed.name"], 会自动设置 token 或 用户名密码
datafeed = get_datafeed()
logger.info(f"type(datafeed)={type(datafeed)}")

# 初始化数据库对象, 类型为 SETTINGS["database.name"], 会自动设置数据库连接参数
database = get_database()
logger.info(f"type(database)={type(database)}")

# 创建 tushare->vnpy 交易所枚举映射关系
EXCHANGE_TS2VT: dict[str, Exchange] = {v: k for k, v in EXCHANGE_VT2TS.items()}

# 中国上海时区
CHINA_TZ = ZoneInfo("Asia/Shanghai")


def datafeed_example(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start: datetime,
    end: datetime,
) -> tuple[bool, int]:
    """
    - 使用 vnpy 的数据服务功能下载指定的 K 线数据
    - 官方文档: https://www.vnpy.com/docs/cn/community/info/datafeed.html
    - 经过测试, 几种数据服务都需要订阅, tushare 性价比最高(200米/年), 适合散户
    """
    # 构造查询参数, 这里我们计划下载的是招商银行的历史日线数据
    req = HistoryRequest(
        symbol=symbol,  # A 股六位数字代码
        exchange=exchange,  # 交易所
        interval=interval,  # K 线级别
        start=start,  # 开始时间
        end=end,  # 结束时间
    )

    # 下载数据
    bardata_list: list[BarData] = datafeed.query_bar_history(req)
    logger.info(f"len(bardata_list)={len(bardata_list)}")

    # 把下载到的数据保存到数据库
    is_saved = True
    if len(bardata_list) > 0:
        is_saved = database.save_bar_data(bardata_list, stream=False)
    return is_saved, len(bardata_list)


def datafeed_forward_example(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start_date: datetime,
    end_date: datetime,
) -> tuple[bool, int]:
    """
    - 下载前复权股票数据到数据库(股票符号前添加F), end 参数是基准股价
    """
    # 这里可以直接使用 ts 数据接口, get_datafeed 已经设置过了 token
    import tushare as ts

    ts_symbol = to_ts_symbol(symbol, exchange)
    ts_interval = INTERVAL_VT2TS[interval]
    start: str = start_date.strftime("%Y%m%d %H:%M:%S")
    end: str = end_date.strftime("%Y-%m-%d %H:%M:%S")
    adjustment: timedelta = INTERVAL_ADJUSTMENT_MAP[interval]

    d1: DataFrame = ts.pro_bar(
        ts_code=ts_symbol,
        start_date=start,
        end_date=end,
        adj='qfq',
        freq=ts_interval,
    )

    df: DataFrame = deepcopy(d1)

    while True:
        if len(d1) != 8000:
            break
        tmp_end: str = d1["trade_time"].values[-1]

        d1 = ts.pro_bar(
            ts_code=ts_symbol,
            start_date=start,
            end_date=tmp_end,
            adj='qfq',
            freq=ts_interval
        )
        df = pd.concat([df[:-1], d1])

    bar_dict: dict[datetime, BarData] = {}
    data: list[BarData] = []

    # 处理原始数据中的NaN值
    df.fillna(0, inplace=True)

    if df is not None:
        for _ix, row in df.iterrows():
            if row["open"] is None:
                continue

            if interval.value == "d":
                dt_str: str = row["trade_date"]
                dt: datetime = datetime.strptime(dt_str, "%Y%m%d")
            else:
                dt_str = row["trade_time"]
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S") - adjustment

            dt = dt.replace(tzinfo=CHINA_TZ)

            turnover = row.get("amount", 0)
            if turnover is None:
                turnover = 0

            open_interest = row.get("oi", 0)
            if open_interest is None:
                open_interest = 0

            bar: BarData = BarData(
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                datetime=dt,
                open_price=round_to(row["open"], 0.000001),
                high_price=round_to(row["high"], 0.000001),
                low_price=round_to(row["low"], 0.000001),
                close_price=round_to(row["close"], 0.000001),
                volume=row["vol"],
                turnover=turnover,
                open_interest=open_interest,
                gateway_name="TS"
            )

            bar_dict[dt] = bar

    bar_keys: list[datetime] = sorted(bar_dict.keys(), reverse=False)
    for i in bar_keys:
        data.append(bar_dict[i])

    # 把下载到的数据保存到数据库
    bardata_list = data
    is_saved = True
    logger.info(f"len(bardata_list)={len(bardata_list)}")
    if len(bardata_list) > 0:
        # 为前复权的股票代码添加特殊符号
        for bar in bardata_list:
            bar.symbol = f"F{bar.symbol}"
        is_saved = database.save_bar_data(bardata_list, stream=False)
    return is_saved, len(bardata_list)


def datafeed_hs300_example(is_forward: bool = False):
    """
    - 把 沪深300 成分股的日线数据写入 vnpy 数据库
    - 适合导入天数多(超过1年)的场景
    - 如果只导入几天的数据会很慢, 更推荐使用原生的 tushare 接口
    """
    # 这里可以直接使用 ts 数据接口, get_datafeed 已经设置过了 token
    import tushare as ts
    pro = ts.pro_api()

    # 获取沪深300指数的成分股, tushare 只有每月首个交易日和最后一个交易日的数据
    df = pro.index_weight(index_code='399300.SZ', start_date='20250630', end_date='20250630')

    # 遍历所有成分股, 依次下载日线数据
    saved_stock_count = 0
    saved_bar_count = 0
    if df is not None:
        for _ix, row in df.iterrows():
            # 提取成分股的 symbol 和 exchange
            con_code = row['con_code']
            symbol = con_code.split(".")[0]
            exchange = EXCHANGE_TS2VT[con_code.split(".")[1]]

            # 导入数据
            logger.info(f"Importing {symbol}.{exchange.value} {_ix + 1}/300: ")
            if not is_forward:
                is_saved, bar_count = datafeed_example(
                    symbol=symbol,
                    exchange=exchange,
                    interval=Interval.DAILY,
                    start=datetime(2025, 1, 1),
                    end=datetime(2025, 8, 28),
                )
            else:
                is_saved, bar_count = datafeed_forward_example(
                    symbol=symbol,
                    exchange=exchange,
                    interval=Interval.DAILY,
                    start_date=datetime(2025, 1, 1),
                    end_date=datetime(2025, 8, 28),
                )
            if is_saved:
                saved_stock_count += 1
                saved_bar_count += bar_count
    logger.info(f"saved_stock_count={saved_stock_count}")
    logger.info(f"saved_bar_count={saved_bar_count}")


if __name__ == "__main__":
    # datafeed_hs300_example(is_forward=True)
    datafeed_forward_example(
        symbol="600036",
        exchange=Exchange.SSE,
        interval=Interval.DAILY,
        start_date=datetime(2016, 1, 1),
        end_date=datetime(2025, 8, 28),
    )
