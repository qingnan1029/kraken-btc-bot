"""Baseline SMA crossover for BTC/USD four-hour candles."""

from freqtrade.strategy import IStrategy
from pandas import DataFrame


class BtcUsdSmaCross(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    startup_candle_count = 61
    process_only_new_candles = True
    position_adjustment_enable = False
    minimal_roi = {}
    # Keep the baseline driven by the crossover during research and paper trading.
    stoploss = -1.0
    use_exit_signal = True
    exit_profit_only = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma_difference"] = dataframe["close"].rolling(20).mean() - dataframe["close"].rolling(60).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        current = dataframe["sma_difference"]
        crossover = (current.shift(1) <= 0) & (current > 0) & (dataframe["volume"] > 0)
        dataframe.loc[crossover, ["enter_long", "enter_tag"]] = (1, "sma_cross_up")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        current = dataframe["sma_difference"]
        crossover = (current.shift(1) >= 0) & (current < 0) & (dataframe["volume"] > 0)
        dataframe.loc[crossover, ["exit_long", "exit_tag"]] = (1, "sma_cross_down")
        return dataframe
