# pragma pylint: disable=missing-docstring, invalid-name
"""
TrendBreakoutStrategy - seguidor de tendência por rompimento (gráfico diário).

Origem: a pesquisa (research/) mostrou que, em cripto, padrões de REVERSÃO por candle
não deram vantagem, mas ROMPIMENTOS a favor da tendência deram, de forma robusta,
em 12 pares e em vários períodos. É a lógica clássica das "tartarugas" (Donchian):

  ENTRADA  - fechamento acima da máxima dos últimos 30 dias (rompimento),
             só com o BTC acima da média de 100 dias (mercado em alta).
  STOP     - inicial a 2,5 ATR abaixo do fechamento do rompimento;
             depois sobe para a mínima dos últimos 10 dias (o stop nunca desce).
  SAÍDA    - só pelo stop: deixa a tendência correr enquanto ela durar.

TrendBreakoutCandle (a mistura com candles) exige ainda que a vela do rompimento seja
forte, com corpo maior que 50% da amplitude e fechamento no terço superior, e que o
volume esteja acima da média de 20 dias: o candle confirma o rompimento.

Carteira: até 12 posições (uma por par), cada uma com uma fração igual do saldo.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from pandas import DataFrame

import talib.abstract as ta
from freqtrade.exchange import timeframe_to_prev_date, timeframe_to_seconds
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute


class TrendBreakout(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = False

    # Parâmetros (centro da região robusta encontrada na pesquisa)
    entry_n = 30  # rompimento da máxima de N dias
    exit_n = 10  # stop móvel na mínima de N dias
    atr_stop = 2.5  # stop inicial em ATRs
    regime_n = 100  # média do BTC que define mercado em alta
    use_candle = False  # exige vela forte + volume no rompimento

    stoploss = -0.30  # teto de segurança; o stop real é o custom_stoploss
    use_custom_stoploss = True
    minimal_roi = {"0": 100}  # sem alvo: a tendência corre até o stop
    trailing_stop = False
    use_exit_signal = False
    process_only_new_candles = True
    startup_candle_count: int = 120

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    plot_config = {
        "main_plot": {
            "donchian_high": {"color": "green"},
            "trail_stop": {"color": "red"},
        },
        "subplots": {"BTC": {"btc_ok": {"color": "orange"}}},
    }

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._stop_cache: dict = {}

    def informative_pairs(self):
        stake = self.config.get("stake_currency", "USDT")
        return [(f"BTC/{stake}", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["donchian_high"] = dataframe["high"].rolling(self.entry_n).max().shift(1)
        dataframe["trail_stop"] = dataframe["low"].rolling(self.exit_n).min()
        dataframe["entry_stop"] = dataframe["close"] - self.atr_stop * dataframe["atr"]

        # Vela forte no rompimento
        rng = (dataframe["high"] - dataframe["low"]).replace(0, np.nan)
        body = (dataframe["close"] - dataframe["open"]) / rng
        close_pos = (dataframe["close"] - dataframe["low"]) / rng
        vol_ok = dataframe["volume"] > dataframe["volume"].rolling(20).mean()
        dataframe["strong_candle"] = (body > 0.5) & (close_pos > 0.67) & vol_ok

        # Regime do BTC (mesmo timeframe: a vela diária do BTC fecha junto com a do par)
        stake = self.config.get("stake_currency", "USDT")
        btc = self.dp.get_pair_dataframe(pair=f"BTC/{stake}", timeframe=self.timeframe)
        btc = btc[["date", "close"]].copy()
        btc["btc_ok"] = btc["close"] > btc["close"].rolling(self.regime_n).mean()
        dataframe = dataframe.merge(btc[["date", "btc_ok"]], on="date", how="left")
        dataframe["btc_ok"] = dataframe["btc_ok"].fillna(False).astype(bool)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cond = (
            (dataframe["close"] > dataframe["donchian_high"])
            & dataframe["btc_ok"]
            & (dataframe["volume"] > 0)
        )
        if self.use_candle:
            cond &= dataframe["strong_candle"]
        dataframe.loc[cond, "enter_long"] = 1
        dataframe.loc[cond, "enter_tag"] = "rompimento_vela" if self.use_candle else "rompimento"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe

    # ------------------------------------------------------------- stops
    def _initial_stop(self, pair: str, trade: Trade, dataframe: DataFrame) -> float | None:
        key = (pair, trade.open_date_utc)
        if key in self._stop_cache:
            return self._stop_cache[key]
        entry_candle = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
        signal_time = entry_candle - timedelta(seconds=timeframe_to_seconds(self.timeframe))
        row = dataframe.loc[dataframe["date"] == signal_time]
        if row.empty or pd.isna(row["entry_stop"].iloc[-1]):
            return None
        stop = float(row["entry_stop"].iloc[-1])
        self._stop_cache[key] = stop
        return stop

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return None
        stop = self._initial_stop(pair, trade, dataframe)
        if stop is None:
            return None
        # Mínima de N dias da última vela já FECHADA (sem olhar o futuro)
        closed = dataframe.loc[dataframe["date"] < timeframe_to_prev_date(self.timeframe, current_time)]
        if not closed.empty and pd.notna(closed["trail_stop"].iloc[-1]):
            # só considera velas fechadas depois da entrada
            if closed["date"].iloc[-1] >= timeframe_to_prev_date(self.timeframe, trade.open_date_utc):
                stop = max(stop, float(closed["trail_stop"].iloc[-1]))
        if stop >= current_rate:
            return 0.0001  # stop acima do preço atual: sair
        return stoploss_from_absolute(stop, current_rate, is_short=trade.is_short, leverage=trade.leverage)


class TrendBreakoutCandle(TrendBreakout):
    """Rompimento confirmado por vela forte e volume acima da média."""

    use_candle = True
