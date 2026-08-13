"""
静态前复权纯算法（同花顺/东财同款）

移植自 vnpy_examples/03_datafeed.py 的 datafeed_static_forward_example，
仅保留算法部分，不依赖 vnpy 的 BarData / 时区 / 数据库 / round_to。

处理顺序与原实现一致：
  1) 先送转复权：除权日（ex_date）之前的所有 K 线价格 ÷ (1 + stk_div)
  2) 分红也受送转影响：除权日 ≤ 送转日的现金分红，先按送转比例缩小
  3) 再分红复权：除权日之前的所有 K 线价格 - cash_div

基准说明：这是“静态前复权”，以数据窗口末端为基准（窗口内最新价格为真实价），
与 pro_bar(adj='qfq') 的动态前复权不同；历史越长、累计分红越高，
早期价格越可能被压到接近 0 甚至为负（本文件自检中有示例）。

字段单位（已用 tushare dividend + adj_factor 实测交叉验证）：
  - stk_div 为每股送转股数：10 送 10 记 1.0，10 送 4 记 0.4
  - cash_div 为每股派现（元）：10 派 10 记 1.0，10 派 20 记 2.0
  - 官方 adj_factor 与 tushare daily 的 pre_close 同属“除权参考价”价格口径：
    因子在除息日跳升只是补偿除权价差，比值收益 = close/除权参考价 − 1，
    现金分红本身不计入收益；与本算法的“送转除、派现减”一致
"""

from collections.abc import Sequence
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd


def _round_half_up(value: float) -> float:
    """vnpy round_to(x, 0.01) 同款四舍五入，保留两位小数"""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def static_forward_adjust(
    bars: pd.DataFrame,
    dividends: pd.DataFrame,
    price_cols: Sequence[str] = ("open", "high", "low", "close"),
    round_price: bool = True,
) -> pd.DataFrame:
    """
    静态前复权（纯算法，返回新 DataFrame，不修改入参）

    bars: 不复权日线，必须含 trade_date（YYYYMMDD 字符串）与 OHLC 列
    dividends: 分红送转事件，必须含 ex_date（YYYYMMDD 字符串）、stk_div、cash_div；
        stk_div 为每股送转股数（10 送 10 记 1.0），cash_div 为每股派现元（10 派 10 记 1.0）；
        调用方需自行过滤 div_proc == "实施" 的事件
    """
    if "trade_date" not in bars.columns:
        raise ValueError("bars 必须包含 trade_date 列")
    missing_cols = [col for col in price_cols if col not in bars.columns]
    if missing_cols:
        raise ValueError(f"bars 缺少价格列: {missing_cols}")
    required_cols = {"ex_date", "stk_div", "cash_div"}
    if not required_cols.issubset(dividends.columns):
        raise ValueError(f"dividends 必须包含列: {required_cols}")

    result = bars.copy()
    events = dividends[list(required_cols)].copy()
    events = events[events["ex_date"].notna()]
    if events.empty:
        return result

    # 静态前复权锚定窗口末端：除权日在窗口最早 K 线之前的，不影响窗口内价格
    window_start = result["trade_date"].min()
    events = events[events["ex_date"] >= window_start]

    # 拆成送转事件与现金分红事件，统一按除权日从新到旧排序
    # （原代码依赖 tushare dividend 返回的降序，这里显式排序保证正确）
    stk_events = events[
        events["stk_div"].notna() & (events["stk_div"] != 0)
    ].sort_values("ex_date", ascending=False)
    cash_events = events[
        events["cash_div"].notna() & (events["cash_div"] != 0)
    ].sort_values("ex_date", ascending=False)

    # 1) 先送转复权（新 -> 旧）
    for _idx, ev in stk_events.iterrows():
        ex_date = ev["ex_date"]
        ex_ratio = 1.0 + float(ev["stk_div"])
        if ex_ratio == 1.0:
            continue
        mask = result["trade_date"] < ex_date
        for col in price_cols:
            result.loc[mask, col] = result.loc[mask, col] / ex_ratio
        # 分红也受送转影响：除权日 <= 送转日的现金分红按比例缩小
        cash_mask = cash_events["ex_date"] <= ex_date
        cash_events.loc[cash_mask, "cash_div"] = (
            cash_events.loc[cash_mask, "cash_div"] / ex_ratio
        )

    # 2) 再分红复权（新 -> 旧）
    for _idx, ev in cash_events.iterrows():
        ex_date = ev["ex_date"]
        cash_div = float(ev["cash_div"])
        mask = result["trade_date"] < ex_date
        for col in price_cols:
            result.loc[mask, col] = result.loc[mask, col] - cash_div

    # 与原实现一致，价格保留到分
    if round_price:
        for col in price_cols:
            result[col] = result[col].map(_round_half_up)

    return result


if __name__ == "__main__":
    # 自检：现金分红 10派10(除息日 1/2，每股派 1.0 元) + 10送10(除权日 1/3，每股送转 1.0)
    test_bars = pd.DataFrame({
        "trade_date": ["20240101", "20240102", "20240103", "20240104"],
        "open": [100.0, 99.0, 49.5, 49.5],
        "high": [100.0, 99.0, 49.5, 49.5],
        "low": [100.0, 99.0, 49.5, 49.5],
        "close": [100.0, 99.0, 49.5, 49.5],
    })
    test_dividends = pd.DataFrame({
        "ex_date": ["20240102", "20240103"],
        "stk_div": [0.0, 1.0],    # 1/2 现金分红；1/3 送转（每股 1.0 股）
        "cash_div": [1.0, 0.0],   # 1/2 每股派现 1.0 元
    })
    adjusted = static_forward_adjust(test_bars, test_dividends)
    print("自检: 静态前复权后的收盘价")
    print(adjusted[["trade_date", "close"]].to_string(index=False))

    expected = [49.5, 49.5, 49.5, 49.5]
    actual = adjusted["close"].tolist()
    assert actual == expected, f"复权结果与预期不符: {actual}"
    print("自检通过: 现金分红(受送转缩放) + 送转 复权结果正确")

    # 局限示例：长期高分红下，早期价格可能被减成负值
    neg_bars = pd.DataFrame({
        "trade_date": ["20200101", "20200102"],
        "close": [3.0, 3.0],
    })
    neg_dividends = pd.DataFrame({
        "ex_date": ["20200102"],
        "stk_div": [0.0],
        "cash_div": [10.0],
    })
    neg_adjusted = static_forward_adjust(neg_bars, neg_dividends, price_cols=("close",))
    print(
        f"局限示例: 派现 10 元、除息前收 3 元 -> 前复权价为 "
        f"{neg_adjusted.iloc[0]['close']}（可为 0 或负值）"
    )
