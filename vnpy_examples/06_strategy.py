from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    ArrayManager,
)
from vnpy.trader.constant import Direction


class NoShortDoubleMaStrategy(CtaTemplate):
    """不做空的双均线策略"""

    author = "hundredcmb"

    fast_window: int = 20
    slow_window: int = 60

    fast_ma0: float = 0.0
    fast_ma1: float = 0.0
    slow_ma0: float = 0.0
    slow_ma1: float = 0.0

    count: int = 0
    cash: int = 0

    parameters = ["fast_window", "slow_window"]
    variables = ["fast_ma0", "fast_ma1", "slow_ma0", "slow_ma1"]

    def on_init(self) -> None:
        """
        Callback when strategy is inited.
        """
        self.write_log("策略初始化")
        self.am: ArrayManager = ArrayManager()
        self.load_bar(10)
        self.cash = self.cta_engine.capital

    def on_start(self) -> None:
        """
        Callback when strategy is started.
        """
        self.write_log("策略启动")
        self.put_event()

    def on_stop(self) -> None:
        """
        Callback when strategy is stopped.
        """
        self.write_log("策略停止")
        self.put_event()

    def on_tick(self, tick: TickData) -> None:
        """
        Callback of new tick data update.
        """
        pass

    def on_bar(self, bar: BarData) -> None:
        """
        Callback of new bar data update.
        """
        self.cancel_all()

        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        fast_ma = am.sma(self.fast_window, array=True)
        self.fast_ma0 = fast_ma[-1]
        self.fast_ma1 = fast_ma[-2]

        slow_ma = am.sma(self.slow_window, array=True)
        self.slow_ma0 = slow_ma[-1]
        self.slow_ma1 = slow_ma[-2]

        cta_engine: BacktestingEngine = self.cta_engine
        if self.fast_ma0 > self.slow_ma0 and self.count == 0:
            price = bar.close_price * 1.1
            volume = int(self.cash / price / 100) * 100
            self.buy(price, volume)
            # cta_engine.output(f"买入提交, {bar.datetime}, count={volume}, price={price:.2f}")
        elif self.fast_ma0 < self.slow_ma0 and self.count > 0:
            price = bar.close_price * 0.9
            self.sell(price, self.count)
            # cta_engine.output(f"卖出提交, {bar.datetime}, count={self.count}, price={price:.2f}")

        self.put_event()

    def on_order(self, order: OrderData) -> None:
        """
        Callback of new order data update.
        """
        self.cta_engine.output(f"{order.datetime} {order.vt_symbol} {order.direction.value} {order.offset.value} {order.price:.2f} {order.volume}")
        self.cta_engine.output(order.type)

    def on_trade(self, trade: TradeData) -> None:
        """
        Callback of new trade data update.
        """
        direction = trade.direction
        if direction == Direction.LONG:
            self.count += trade.volume
            self.cash -= trade.price * trade.volume
            self.cta_engine.output(f"买入成交, {trade.datetime}, count={trade.volume}, price={trade.price:.2f}")
        elif direction == Direction.SHORT:
            self.count -= trade.volume
            self.cash += trade.price * trade.volume
            self.cta_engine.output(f"卖出成交, {trade.datetime}, count={trade.volume}, price={trade.price:.2f}")

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """
        Callback of stop order update.
        """
        pass


if __name__ == '__main__':
    from datetime import datetime
    from vnpy.trader.constant import Interval
    from vnpy_ctastrategy.backtesting import BacktestingEngine, BacktestingMode

    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol="600036.SSE",
        interval=Interval.DAILY,
        start=datetime(2014, 1, 1),
        end=datetime(2025, 8, 22),
        mode=BacktestingMode.BAR,
        rate=0.0003354,  # A股费率 (0.854+0.854+5)/10000/2
        slippage=0,
        size=1,
        pricetick=0.01,
        capital=1000000,
    )
    engine.load_data()
    engine.add_strategy(NoShortDoubleMaStrategy, {
        "fast_window": 22,
        "slow_window": 85,
    })

    engine.run_backtesting()
    df = engine.calculate_result()
    engine.calculate_statistics()
