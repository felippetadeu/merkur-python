# pragma pylint: disable=missing-docstring, invalid-name
"""
IntradayBreakout - rompimento de curto prazo em velas de 4h ou 1h.

Mesma ideia da estratégia diária (seguir tendências), mas com velas menores e um stop
móvel bem mais curto, o que deixa as operações com poucos dias de duração.

  ENTRADA   - fechamento acima da máxima das últimas `entry_n` velas;
              opcional (Breakout1h): vela forte (corpo > 50% da amplitude, fechando no
              terço superior) e volume acima da média de 20 velas.
  REGIME    - só com o BTC acima da média de 100 dias no gráfico DIÁRIO
              (a vela diária só é usada depois de fechada).
  LIQUIDEZ  - só nas `top_n` moedas mais negociadas dos últimos 90 dias
              (volume financeiro, ranking refeito a cada vela, só com dados passados).
  STOP      - inicial a `atr_stop` ATRs abaixo do fechamento do rompimento; depois sobe
              para a mínima das últimas `exit_n` velas (nunca desce).
  SAÍDA     - só pelo stop.
  TAMANHO   - arrisca `risk_per_trade` do saldo até o stop inicial, limitado a saldo / vagas.

Validação (research/): aprovada em 3 períodos (2019-22, 2023-24, 2025-26), em 12 e em
32 moedas (incluindo as que colapsaram), com custos dobrados e com ~90% dos vizinhos
de parâmetros (±20%) também aprovados.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from pandas import DataFrame

import talib.abstract as ta
from freqtrade.exchange import timeframe_to_minutes, timeframe_to_prev_date, timeframe_to_seconds
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, merge_informative_pair, stoploss_from_absolute


class IntradayBreakout(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    can_short = False

    entry_n = 120
    exit_n = 18
    atr_stop = 2.5
    use_candle = False
    top_n = 6
    liq_days = 90
    btc_ma_days = 100
    risk_per_trade = 0.015

    stoploss = -0.30
    use_custom_stoploss = True
    minimal_roi = {"0": 100}
    trailing_stop = False
    use_exit_signal = False
    process_only_new_candles = True
    startup_candle_count: int = 600

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    plot_config = {
        "main_plot": {"donchian_high": {"color": "green"}, "trail_stop": {"color": "red"}},
        "subplots": {"Liquidez": {"liq_rank": {"color": "blue"}}},
    }

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._stop_cache: dict = {}

    @property
    def _liq_candles(self) -> int:
        return self.liq_days * (24 * 60 // timeframe_to_minutes(self.timeframe))

    def informative_pairs(self):
        stake = self.config.get("stake_currency", "USDT")
        return [(f"BTC/{stake}", "1d")]

    def _liquidity_rank(self) -> pd.DataFrame:
        n = self._liq_candles
        cols = {}
        for p in self.dp.current_whitelist():
            d = self.dp.get_pair_dataframe(pair=p, timeframe=self.timeframe)
            if d.empty:
                continue
            dv = (d["close"] * d["volume"]).rolling(n, min_periods=n // 2).mean()
            cols[p] = pd.Series(dv.to_numpy(), index=d["date"])
        return pd.DataFrame(cols).rank(axis=1, ascending=False)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["donchian_high"] = dataframe["high"].rolling(self.entry_n).max().shift(1)
        dataframe["trail_stop"] = dataframe["low"].rolling(self.exit_n).min()
        dataframe["entry_stop"] = dataframe["close"] - self.atr_stop * dataframe["atr"]

        rng = (dataframe["high"] - dataframe["low"]).replace(0, np.nan)
        body = (dataframe["close"] - dataframe["open"]) / rng
        close_pos = (dataframe["close"] - dataframe["low"]) / rng
        vol_ok = dataframe["volume"] > dataframe["volume"].rolling(20).mean()
        dataframe["strong_candle"] = (body > 0.5) & (close_pos > 0.67) & vol_ok

        # Regime do BTC no diário, sem olhar o futuro
        stake = self.config.get("stake_currency", "USDT")
        btc = self.dp.get_pair_dataframe(pair=f"BTC/{stake}", timeframe="1d")
        btc = btc[["date", "close"]].copy()
        btc["btc_ok"] = (btc["close"] > btc["close"].rolling(self.btc_ma_days).mean()).astype(float)
        dataframe = merge_informative_pair(
            dataframe, btc[["date", "btc_ok"]], self.timeframe, "1d",
            ffill=True, append_timeframe=False, suffix="btc1d",
        )
        dataframe["btc_ok"] = dataframe["btc_ok_btc1d"].fillna(0).astype(bool)

        # Ranking de liquidez entre os pares da lista
        rank = self._liquidity_rank()
        pair = metadata["pair"]
        if pair in rank.columns:
            dataframe = dataframe.merge(rank[pair].rename("liq_rank"), left_on="date", right_index=True, how="left")
        else:
            dataframe["liq_rank"] = np.nan
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cond = (
            (dataframe["close"] > dataframe["donchian_high"])
            & dataframe["btc_ok"]
            & (dataframe["liq_rank"] <= self.top_n)
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

    # ------------------------------------------------------------ stops
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

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs) -> float | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return None
        stop = self._initial_stop(pair, trade, dataframe)
        if stop is None:
            return None
        closed = dataframe.loc[dataframe["date"] < timeframe_to_prev_date(self.timeframe, current_time)]
        if not closed.empty and pd.notna(closed["trail_stop"].iloc[-1]):
            if closed["date"].iloc[-1] >= timeframe_to_prev_date(self.timeframe, trade.open_date_utc):
                stop = max(stop, float(closed["trail_stop"].iloc[-1]))
        if stop >= current_rate:
            return 0.0001
        return stoploss_from_absolute(stop, current_rate, is_short=trade.is_short, leverage=trade.leverage)

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float | None, max_stake: float,
                            leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return proposed_stake
        signal = dataframe.loc[dataframe["date"] < timeframe_to_prev_date(self.timeframe, current_time)]
        if signal.empty:
            return proposed_stake
        stop = signal["entry_stop"].iloc[-1]
        if pd.isna(stop) or stop >= current_rate:
            return proposed_stake
        dist = (current_rate - stop) / current_rate
        risk_stake = self.wallets.get_total_stake_amount() * self.risk_per_trade / dist
        return max(min(proposed_stake, risk_stake, max_stake), min_stake or 0)


class Breakout4h(IntradayBreakout):
    """4h: rompimento de 20 dias (120 velas), stop móvel na mínima de 3 dias (18 velas)."""

    timeframe = "4h"
    entry_n = 120
    exit_n = 18
    atr_stop = 2.5
    use_candle = False
    startup_candle_count: int = 600


class Breakout1h(IntradayBreakout):
    """1h: rompimento de 5 dias (120 velas) com vela forte, stop móvel na mínima de 48h."""

    timeframe = "1h"
    entry_n = 120
    exit_n = 48
    atr_stop = 3.0
    use_candle = True
    startup_candle_count: int = 2200
