from vnpy.trader.constant import (
    Status,
    Exchange,
    Interval,
    Direction,
)
from vnpy_ctastrategy import (
    BarData,
    TickData,
    StopOrder,
    TradeData,
    OrderData,
    CtaTemplate,
    ArrayManager,
)
from vnpy_tushare.tushare_datafeed import EXCHANGE_VT2TS

import numpy as np
from datetime import datetime

# 创建 tushare->vnpy 交易所枚举映射关系
EXCHANGE_TS2VT: dict[str, Exchange] = {v: k for k, v in EXCHANGE_VT2TS.items()}


class NoShortDailyDoubleMaStrategy(CtaTemplate):
    """
    - 不做空的日线双均线策略(仅用于回测学习, 无 tick 数据)
    - 策略细节: 如果n日收盘时快均线在上时, n+1日开盘时满仓; n日收盘时慢均线在上时, n+1日开盘时空仓
    - 支持开启除权策略(贴近真实的交易): 股权登记日的当天会空仓, 如果除权除息日快均线在上, 下一天开盘满仓
    """

    author = "hundredcmb"

    fast_window: int = 30
    slow_window: int = 90

    # 0=忽略分红使用不复权股价模拟(使用的本地的数据库, 支持 vnpy 的参数优化)
    # 1=每次分红之前逃权(每次回测会调用tushare接口, 不支持 vnpy 的参数优化)
    skip_ex: int = 0

    # 最新日线的快慢均线值
    fast_ma0: float = 0.0
    slow_ma0: float = 0.0

    # 上一交易日的日线的快慢均线值
    fast_ma1: float = 0.0
    slow_ma1: float = 0.0

    # 股权登记日
    record_dates: set[str] = set()

    # 除权除息日
    ex_dates: set[str] = set()

    # 提前注册的需要挂卖单的日期
    sell_dates: set[str] = set()

    # 账户剩余资金, 而剩余持股数为 self.pos
    cash: float = 0.0

    parameters = ["fast_window", "slow_window", "skip_ex"]
    variables = ["fast_ma0", "fast_ma1", "slow_ma0", "slow_ma1", "cash"]

    @staticmethod
    def strftime_tushare(date: datetime):
        """格式化成tushare的日期字符串"""
        return datetime.strftime(date, "%Y%m%d")

    def init_sell_dates(self):
        """
        初始化强制卖单日集合
        - 股权登记日前一天的bar
        - 最后一天前一天的bar
        """
        if self.skip_ex == 0:
            return

        import tushare as ts
        pro = ts.pro_api()
        symbol = self.cta_engine.symbol
        exchange: str = EXCHANGE_VT2TS[self.cta_engine.exchange]

        # 拉取股票的分红送转信息
        df = pro.dividend(ts_code=f'{symbol}.{exchange}', fields='ts_code,div_proc,stk_div,record_date,ex_date')
        for _idx, row in df.iterrows():
            if row['div_proc'] == "实施":
                self.record_dates.add(row['record_date'])
                self.ex_dates.add(row['ex_date'])

        # 拉取历史交易日信息
        start_date = self.strftime_tushare(self.cta_engine.start)
        end_date = self.strftime_tushare(self.cta_engine.end)
        df = pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date)

        # 提取股权登记日前一天
        is_record_date = False
        is_last_date = 0
        for _idx, row in df.iterrows():
            cal_date = row["cal_date"]
            is_open = int(row["is_open"])
            if not is_open:
                continue

            if is_last_date == 0:
                is_last_date = 1
            elif is_last_date == 1:
                self.sell_dates.add(cal_date)
                is_last_date = 2

            if is_record_date:
                self.sell_dates.add(cal_date)
                is_record_date = False
            if cal_date in self.record_dates:
                is_record_date = True

    def on_init(self) -> None:
        """
        Callback when strategy is inited.
        """
        self.am: ArrayManager = ArrayManager(size=self.slow_window)
        self.cash = self.cta_engine.capital

        # 补充策略开始日以前的均线数据, 由于股票交易日不连续, days要填大一些
        self.load_bar(days=self.slow_window * 2, interval=Interval.DAILY, use_database=True)

        # 初始化卖出日集合
        self.init_sell_dates()

    def on_start(self) -> None:
        """
        Callback when strategy is started.
        """
        pass

    def on_stop(self) -> None:
        """
        Callback when strategy is stopped.
        """
        pass

    def on_tick(self, tick: TickData) -> None:
        """
        Callback of new tick data update.
        """
        pass

    def on_bar(self, bar: BarData) -> None:
        """
        Callback of new bar data update.
        """
        # 有新的日线数据, 取消所有未成交委托
        self.cancel_all()

        # 更新滑动窗口
        am = self.am
        am.update_bar(bar)
        if not am.inited or not self.trading:
            return

        # 更新快均线, 提取近两天的值
        fast_ma: np.ndarray = am.sma(n=self.fast_window, array=True)
        self.fast_ma0 = float(fast_ma[-1].item())
        self.fast_ma1 = float(fast_ma[-2].item())

        # 更新慢均线, 提取近两天的值
        slow_ma: np.ndarray = am.sma(n=self.slow_window, array=True)
        self.slow_ma0 = float(slow_ma[-1].item())
        self.slow_ma1 = float(slow_ma[-2].item())

        # 股权登记日需要额外处理
        if self.strftime_tushare(bar.datetime) in self.sell_dates and self.pos > 0:
            price = bar.close_price * 0.9  # 确保下一交易日开盘竞价能够成交
            self.sell(price, self.pos)
        elif self.strftime_tushare(bar.datetime) in self.record_dates:
            pass
        else:
            # 上涨趋势就满仓, 下跌趋势就空仓
            if self.fast_ma0 >= self.slow_ma0 and self.pos == 0:
                price = bar.close_price * 1.1  # 确保下一交易日开盘竞价能够成交
                volume = int(self.cash / price / 100) * 100
                self.buy(price, volume)
            elif self.fast_ma0 < self.slow_ma0 and self.pos > 0:
                price = bar.close_price * 0.9  # 确保下一交易日开盘竞价能够成交
                self.sell(price, self.pos)

    def on_order(self, order: OrderData) -> None:
        """
        Callback of new order data update.
        """
        if order.status == Status.NOTTRADED:
            if order.direction == Direction.LONG:
                self.cta_engine.output(f"买入提交: count={order.volume}, price={order.price:.2f}"
                                       f", {order.datetime.strftime("%Y-%m-%d")}")
            elif order.direction == Direction.SHORT:
                self.cta_engine.output(f"卖出提交: count={order.volume}, price={order.price:.2f}"
                                       f", {order.datetime.strftime("%Y-%m-%d")}")
        elif order.status == Status.CANCELLED:
            if order.direction == Direction.LONG:
                self.cta_engine.output(f"买入撤单: count={order.volume}, price={order.price:.2f}"
                                       f", {order.datetime.strftime("%Y-%m-%d")}")
            elif order.direction == Direction.SHORT:
                self.cta_engine.output(f"卖出撤单: count={order.volume}, price={order.price:.2f}"
                                       f", {order.datetime.strftime("%Y-%m-%d")}")

    def on_trade(self, trade: TradeData) -> None:
        """
        Callback of new trade data update.
        """
        direction = trade.direction
        if direction == Direction.LONG:
            self.cash -= trade.price * trade.volume
            self.cta_engine.output(f"买入成交: count={trade.volume}, price={trade.price:.2f}"
                                   f", {trade.datetime.strftime("%Y-%m-%d")}")
        elif direction == Direction.SHORT:
            self.cash += trade.price * trade.volume
            self.cta_engine.output(f"卖出成交: count={trade.volume}, price={trade.price:.2f}"
                                   f", {trade.datetime.strftime("%Y-%m-%d")}")

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """
        Callback of stop order update.
        """
        pass


if __name__ == '__main__':
    from vnpy_ctastrategy.backtesting import BacktestingEngine, BacktestingMode

    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=f"600519.{Exchange.SSE.value}",  # 股票代码(代码.市场)
        rate=0.0003354,  # 单向手续费率(包括印花税), A股最低为(0.854+0.854+5)/10000/2
        slippage=0,  # 滑点, 成交可能的偏差
        size=1,  # 合约乘数, 股票实物为1
        pricetick=0.01,  # 最小价格变动
        capital=1000000,  # 初始资金
        interval=Interval.DAILY,
        mode=BacktestingMode.BAR,
        start=datetime(2016, 1, 1),
        end=datetime(2025, 8, 27),
    )
    engine.load_data()
    engine.add_strategy(NoShortDailyDoubleMaStrategy, {
        "fast_window": 30,
        "slow_window": 50,
        "skip_ex": 1,
    })

    engine.run_backtesting()
    df = engine.calculate_result()
    engine.calculate_statistics()
