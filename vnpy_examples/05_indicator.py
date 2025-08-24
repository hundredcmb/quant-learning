from config import logger
from datetime import datetime
from vnpy.trader.object import BarData
from vnpy.trader.utility import ArrayManager
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange, Interval


def indicator_kdj_example(
    bardata_list: list[BarData],
    fastk_period: int = 9,
    slowk_period: int = 3,
    slowd_period: int = 3,
) -> tuple[float, float, float]:
    """
    - 计算 bardata_list 中最后一天的 kdj 值
    - 结果与东方财富以及同花顺相同
    - 底层使用 TA-lib 库, 参数需要经过处理才能传入
    - 注意: 滑动窗口中的有效 bardata 至少需要 40 个
    """
    # 创建一个滑动窗口并补充数据
    am = ArrayManager(size=40)
    for bar in bardata_list:
        am.update_bar(bar=bar)

    # 计算指标
    k, d = am.stoch(
        fastk_period=fastk_period,
        slowk_period=slowk_period * 2 - 1,
        slowk_matype=1,
        slowd_period=slowd_period * 2 - 1,
        slowd_matype=1,
    )
    j = 3 * k - 2 * d

    f_time = bardata_list[-1].datetime.strftime("%Y-%m-%d")
    code = f"{bardata_list[-1].symbol}.{bardata_list[-1].exchange.value}"
    logger.info(f"{f_time} {code} (K, D, J) = ({k:.2f}, {d:.2f}, {j:.2f})")
    return k, d, j


def indicator_macd_example(
    bardata_list: list[BarData],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[float, float, float]:
    """
    - 计算 bardata_list 中最后一天的 MACD 指标的 3 个值 (DIF, DEA, MACD/BAR)
    - 结果与东方财富以及同花顺相同
    - 底层使用 TA-lib 库
    - 涉及股价的计算, 对比数据要使用不复权价格
    - 注意: 滑动窗口中的有效 bardata 至少需要 120 个
    """
    # 创建一个滑动窗口并补充数据
    am = ArrayManager(size=120)
    for bar in bardata_list:
        am.update_bar(bar=bar)

    # 计算指标
    dif, dea, hist = am.macd(
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
    )
    bar = 2 * (dif - dea)

    f_time = bardata_list[-1].datetime.strftime("%Y-%m-%d")
    code = f"{bardata_list[-1].symbol}.{bardata_list[-1].exchange.value}"
    logger.info(f"{f_time} {code} (DIF, DEA, MACD/BAR) = ({dif:.2f}, {dea:.2f}, {bar:.2f})")
    return dif, dea, bar


def indicator_bbi_example(
    bardata_list: list[BarData],
    p1: int = 3,
    p2: int = 6,
    p3: int = 12,
    p4: int = 24,
) -> float:
    """
    - 计算 bardata_list 中最后一天的 bbi 值
    - 结果与东方财富以及同花顺相同
    - 底层使用 TA-lib 库
    - 涉及股价的计算, 对比数据要使用不复权价格
    - 注意: 滑动窗口中的有效 bardata 至少需要 p4(默认24) 个
    """
    # 创建一个滑动窗口并补充数据
    am = ArrayManager(size=24)
    for bar in bardata_list:
        am.update_bar(bar=bar)

    # 计算指标
    ma3 = am.sma(n=p1)
    ma6 = am.sma(n=p2)
    ma12 = am.sma(n=p3)
    ma24 = am.sma(n=p4)
    bbi = (ma3 + ma6 + ma12 + ma24) / 4

    f_time = bardata_list[-1].datetime.strftime("%Y-%m-%d")
    code = f"{bardata_list[-1].symbol}.{bardata_list[-1].exchange.value}"
    logger.info(f"{f_time} {code} BBI = {bbi:.2f}")
    return bbi


if __name__ == '__main__':
    database = get_database()
    logger.info(type(database))  # 根据 SETTINGS["database.name"] 确定数据库类型
    b_list: list[BarData] = database.load_bar_data(
        symbol="600036",
        exchange=Exchange.SSE,
        interval=Interval.DAILY,
        start=datetime(2024, 7, 1),
        end=datetime(2025, 2, 28),
    )

    indicator_kdj_example(bardata_list=b_list)
    indicator_bbi_example(bardata_list=b_list)
    indicator_macd_example(bardata_list=b_list)
