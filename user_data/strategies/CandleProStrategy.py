# pragma pylint: disable=missing-docstring, invalid-name
"""
CandleProStrategy - versão 2 da estratégia de padrões de candle.

O backtest da versão 1 (CandlePatternStrategy) mostrou três problemas:
  1. comprava padrões de alta em qualquer contexto, inclusive no meio de quedas fortes;
  2. o stop era um percentual fixo, sem relação com a estrutura do gráfico;
  3. os ganhos eram cortados cedo (trailing de 2%) enquanto as perdas iam até o stop inteiro.

A versão 2 aplica a leitura clássica de candles (Steve Nison e afins):
  - CONTEXTO: só compra a favor da tendência maior (diário acima da média de 50 dias
    e com essa média subindo). Um martelo numa correção de alta vale muito mais
    que um martelo no meio de uma queda.
  - LOCALIZAÇÃO: o padrão precisa aparecer num suporte: na banda inferior de
    Bollinger ou num toque na média de 50 períodos do 4h (pullback).
  - CONFIRMAÇÃO (opcional): espera a vela seguinte fechar acima da máxima do padrão.
  - REGIME DO BTC (opcional): em cripto quase tudo segue o bitcoin; só compra com o
    BTC acima da média de 50 dias no diário.
  - RISCO PELA ESTRUTURA: o stop fica abaixo da mínima do padrão, com folga de meio ATR.
    O tamanho do risco (R) define o alvo: sai com 2R de lucro, ou usa um trailing por ATR.
    Ao atingir 1R, o stop vai para o preço de entrada (breakeven).
  - SAÍDA POR TEMPO: se depois de 30 velas (5 dias) a operação não andou, sai.

As subclasses no fim do arquivo ligam cada uma dessas peças, para medir no backtest
quanto cada uma contribui.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from pandas import DataFrame

import talib.abstract as ta
from freqtrade.exchange import timeframe_to_prev_date, timeframe_to_seconds
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, merge_informative_pair, stoploss_from_absolute


BULL_PATTERNS = {
    # coluna -> (função TA-Lib, nome usado nas tags)
    "bull_morningstar": ("CDLMORNINGSTAR", "estrela_da_manha"),
    "bull_engulfing": ("CDLENGULFING", "engolfo_de_alta"),
    "bull_piercing": ("CDLPIERCING", "linha_de_perfuracao"),
    "bull_hammer": ("CDLHAMMER", "martelo"),
}


def detect_bull_patterns(dataframe: DataFrame) -> DataFrame:
    """Marca os padrões de reversão de alta e o nome do padrão em cada vela."""
    for col, (fn, _) in BULL_PATTERNS.items():
        dataframe[col] = getattr(ta, fn)(dataframe) > 0
    dataframe["bull_pattern"] = dataframe[list(BULL_PATTERNS)].any(axis=1)
    dataframe["bull_name"] = np.select(
        [dataframe[c] for c in BULL_PATTERNS],
        [name for _, name in BULL_PATTERNS.values()],
        default="",
    )
    return dataframe


class CandleProBase(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    informative_timeframe = "1d"  # tendência maior do próprio par
    btc_timeframe = "1d"  # regime do BTC
    can_short = False

    # Stop real é calculado em custom_stoploss; este é só um teto de segurança.
    stoploss = -0.15
    use_custom_stoploss = True

    # ROI e trailing nativos desligados: as saídas são por R (custom_exit / custom_stoploss).
    minimal_roi = {"0": 100}
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False

    # 400 velas de 4h ~ 67 dias: suficiente para a média de 50 dias do diário.
    startup_candle_count: int = 400

    # --- Peças que cada variante liga ou desliga ---
    use_trend = True  # diário acima da EMA50 e EMA50 subindo
    use_location = True  # padrão num suporte (Bollinger inferior ou EMA50 do 4h)
    use_confirmation = False  # vela seguinte fecha acima da máxima do padrão
    use_btc_regime = False  # BTC acima da EMA50 no diário
    exit_mode = "target"  # "target" = alvo fixo em R | "trail" = trailing por ATR

    # --- Parâmetros de risco ---
    target_r = 2.0
    breakeven_r = 1.0
    trail_atr = 3.0
    trail_start_r = 1.0
    max_hold_candles = 30
    min_risk = 0.01  # risco mínimo de 1% (padrões minúsculos são ruído)
    max_risk = 0.08  # risco máximo de 8% (padrões gigantes deixam o stop longe demais)
    buy_rsi = 55
    volume_factor = 1.0

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    plot_config = {
        "main_plot": {
            "ema50": {"color": "orange"},
            "bb_lower": {"color": "gray"},
            "entry_stop": {"color": "red"},
        },
        "subplots": {"RSI": {"rsi": {"color": "purple"}}},
    }

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._stop_cache: dict = {}

    # ------------------------------------------------------------------ dados
    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        stake = self.config.get("stake_currency", "USDT")
        inf = [(pair, self.informative_timeframe) for pair in pairs]
        inf.append((f"BTC/{stake}", self.btc_timeframe))
        return inf

    def _daily_context(self, pair: str) -> DataFrame:
        daily = self.dp.get_pair_dataframe(pair=pair, timeframe=self.informative_timeframe)
        daily = daily[["date", "open", "high", "low", "close", "volume"]].copy()
        daily["ema50"] = ta.EMA(daily, timeperiod=50)
        daily["trend_ok"] = (daily["close"] > daily["ema50"]) & (
            daily["ema50"] > daily["ema50"].shift(3)
        )
        return daily

    def _btc_context(self) -> DataFrame:
        stake = self.config.get("stake_currency", "USDT")
        btc = self.dp.get_pair_dataframe(pair=f"BTC/{stake}", timeframe=self.btc_timeframe)
        btc = btc[["date", "close"]].copy()
        btc["btc_ok"] = btc["close"] > ta.EMA(btc, timeperiod=50)
        return btc[["date", "btc_ok"]]

    # ------------------------------------------------------------ indicadores
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.add_rule_columns(dataframe, metadata)
        return dataframe

    def add_rule_columns(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_lower"] = bb["lowerband"]
        dataframe["volume_mean"] = dataframe["volume"].rolling(20).mean()

        dataframe = detect_bull_patterns(dataframe)

        # Contexto do diário do próprio par (sem lookahead: merge_informative_pair
        # só usa velas diárias já fechadas).
        daily = self._daily_context(pair)
        dataframe = merge_informative_pair(
            dataframe, daily, self.timeframe, self.informative_timeframe, ffill=True
        )
        trend_col = f"trend_ok_{self.informative_timeframe}"
        dataframe["trend_ok"] = dataframe[trend_col].fillna(False).astype(bool)

        # Regime do BTC
        btc = self._btc_context()
        dataframe = merge_informative_pair(
            dataframe,
            btc,
            self.timeframe,
            self.btc_timeframe,
            ffill=True,
            append_timeframe=False,
            suffix="btc1d",
        )
        dataframe["btc_ok"] = dataframe["btc_ok_btc1d"].fillna(False).astype(bool)

        # Localização: o padrão (até 3 velas) tocou um suporte
        pattern_low = dataframe["low"].rolling(3).min()
        at_bb = pattern_low <= dataframe["bb_lower"]
        at_ema = (pattern_low <= dataframe["ema50"]) & (dataframe["close"] > dataframe["ema50"])
        dataframe["near_support"] = at_bb | at_ema

        # Setup na vela do padrão
        setup = dataframe["bull_pattern"] & (dataframe["rsi"] < self.buy_rsi)
        setup &= dataframe["volume"] > dataframe["volume_mean"] * self.volume_factor
        if self.use_location:
            setup &= dataframe["near_support"]
        setup_stop = pattern_low - 0.5 * dataframe["atr"]

        if self.use_confirmation:
            # Sinal na vela seguinte, se ela fechar acima da máxima do padrão
            prev_setup = setup.shift(1, fill_value=False)
            confirmed = (
                prev_setup
                & (dataframe["close"] > dataframe["high"].shift(1))
                & (dataframe["close"] > dataframe["open"])
            )
            dataframe["signal"] = confirmed
            dataframe["entry_stop"] = np.minimum(
                setup_stop.shift(1), dataframe["low"] - 0.5 * dataframe["atr"]
            )
            dataframe["signal_name"] = dataframe["bull_name"].shift(1).fillna("")
        else:
            dataframe["signal"] = setup
            dataframe["entry_stop"] = setup_stop
            dataframe["signal_name"] = dataframe["bull_name"]

        risk = (dataframe["close"] - dataframe["entry_stop"]) / dataframe["close"]
        dataframe["risk_pct"] = risk
        dataframe["risk_ok"] = (risk >= self.min_risk) & (risk <= self.max_risk)
        return dataframe

    # ---------------------------------------------------------------- entradas
    def rule_entry_conditions(self, dataframe: DataFrame) -> pd.Series:
        cond = dataframe["signal"] & dataframe["risk_ok"] & (dataframe["volume"] > 0)
        if self.use_trend:
            cond &= dataframe["trend_ok"]
        if self.use_btc_regime:
            cond &= dataframe["btc_ok"]
        return cond

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cond = self.rule_entry_conditions(dataframe)
        dataframe.loc[cond, "enter_long"] = 1
        dataframe.loc[cond, "enter_tag"] = dataframe.loc[cond, "signal_name"]
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Saídas são todas por risco (custom_stoploss / custom_exit).
        dataframe["exit_long"] = 0
        return dataframe

    # ------------------------------------------------------- gestão de risco
    def _trade_stop(self, pair: str, trade: Trade) -> float | None:
        key = (pair, trade.open_date_utc)
        if key in self._stop_cache:
            return self._stop_cache[key]

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        entry_candle = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
        signal_time = entry_candle - timedelta(seconds=timeframe_to_seconds(self.timeframe))
        row = dataframe.loc[dataframe["date"] == signal_time]
        if row.empty or pd.isna(row["entry_stop"].iloc[-1]):
            return None
        stop = float(row["entry_stop"].iloc[-1])
        if stop >= trade.open_rate:
            return None
        self._stop_cache[key] = stop
        return stop

    def _last_atr(self, pair: str) -> float | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return None
        atr = dataframe["atr"].iloc[-1]
        return None if pd.isna(atr) else float(atr)

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
        stop = self._trade_stop(pair, trade)
        if stop is None:
            return None
        risk = trade.open_rate - stop
        r_now = (current_rate - trade.open_rate) / risk

        new_stop = stop
        if r_now >= self.breakeven_r:
            new_stop = max(new_stop, trade.open_rate * 1.002)  # cobre as taxas
        if self.exit_mode == "trail" and r_now >= self.trail_start_r:
            atr = self._last_atr(pair)
            if atr:
                new_stop = max(new_stop, current_rate - self.trail_atr * atr)

        return stoploss_from_absolute(
            new_stop, current_rate, is_short=trade.is_short, leverage=trade.leverage
        )

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        stop = self._trade_stop(pair, trade)
        if stop is None:
            return None
        risk = trade.open_rate - stop
        r_now = (current_rate - trade.open_rate) / risk

        if self.exit_mode == "target" and r_now >= self.target_r:
            return f"alvo_{self.target_r:g}R"

        held = (current_time - trade.open_date_utc).total_seconds() / timeframe_to_seconds(
            self.timeframe
        )
        if held >= self.max_hold_candles and r_now < 0.5:
            return "tempo"
        return None


# ---------------------------------------------------------------------------
# Variantes para o backtest comparativo
# ---------------------------------------------------------------------------


class CandleV2Context(CandleProBase):
    """Tendência do diário + suporte + stop pela estrutura + alvo de 2R."""


class CandleV2Confirm(CandleProBase):
    """Igual à anterior, esperando a vela de confirmação."""

    use_confirmation = True


class CandleV2Regime(CandleProBase):
    """Confirmação + só opera com o BTC em tendência de alta no diário."""

    use_confirmation = True
    use_btc_regime = True


class CandleV2Trail(CandleProBase):
    """Confirmação + regime do BTC, deixando o lucro correr com trailing de 3 ATR."""

    use_confirmation = True
    use_btc_regime = True
    exit_mode = "trail"


class CandleV2Wide(CandleProBase):
    """Contexto + suporte, SEM breakeven: o trailing de 3 ATR só começa em 1,5R.

    Testa a hipótese de que mover o stop para a entrada em 1R tira o trade
    nos recuos normais de cripto, antes da alta continuar."""

    exit_mode = "trail"
    breakeven_r = 99.0
    trail_start_r = 1.5


class CandleV2Daily(CandleProBase):
    """A lógica da v2 Contexto no gráfico DIÁRIO, com a tendência medida no semanal.

    Nos livros de candles, os padrões são lidos sobretudo no diário: cada vela
    resume um dia inteiro de negociação e tem muito menos ruído que a de 4h."""

    timeframe = "1d"
    informative_timeframe = "1w"
    btc_timeframe = "1w"
    max_hold_candles = 20  # 20 dias
    max_risk = 0.12  # velas diárias são maiores; aceita stop um pouco mais longe
    startup_candle_count: int = 400
