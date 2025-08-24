from datetime import datetime
from vnpy.trader.database import get_database
from vnpy.trader.ui import create_qapp, QtCore
from vnpy.trader.constant import Exchange, Interval
from vnpy.chart import ChartWidget, VolumeItem, CandleItem

if __name__ == "__main__":
    app = create_qapp()

    # 加载数据
    database = get_database()
    bars = database.load_bar_data(
        "600036",
        Exchange.SSE,
        interval=Interval.DAILY,
        start=datetime(2023, 1, 1),
        end=datetime(2025, 8, 15)
    )

    # 创建图表窗口组件
    widget = ChartWidget()

    # 添加第一个绘图区域: 用于显示蜡烛图, 隐藏x轴的值(时间)的显示
    widget.add_plot(plot_name="candle", hide_x_axis=True)
    widget.add_item(CandleItem, item_name="candle", plot_name="candle")

    # 添加第二个绘图区域: 用于显示成交量, 最大高度限制为200像素
    widget.add_plot(plot_name="volume", maximum_height=200)
    widget.add_item(VolumeItem, item_name="volume", plot_name="volume")

    # 添加光标, 显示 某个bar蜡烛图的信息, 某个bar成交量信息, 光标x轴值, 光标y轴值
    widget.add_cursor()

    # 首次显示的历史数据数量
    n = 100
    history = bars[:n]
    new_data = bars[n:]
    widget.update_history(history)


    def update_bar() -> None:
        """定时更新数据"""
        if len(new_data) > 0:
            bar = new_data.pop(0)
            widget.update_bar(bar)


    # 创建Qt定时器, 用于定时触发更新, start 参数是触发间隔, 单位是毫秒
    timer = QtCore.QTimer()
    timer.timeout.connect(update_bar)
    timer.start(100)  # 这行注释掉就是静态的图

    # 启动图形界面
    widget.show()
    app.exec()
