# pragma pylint: disable=missing-docstring, invalid-name
"""
CandleFreqAIStrategy - padrões de candle filtrados por machine learning (FreqAI).

Ideia ("meta-labeling"):
  - As REGRAS de candle encontram os candidatos: padrão de reversão de alta, vela de
    confirmação e risco entre 1% e 8%, com stop abaixo do padrão (mesma lógica da v2).
  - O MODELO decide quais candidatos valem a pena. Ele recebe a anatomia das velas
    (corpo, sombras, amplitude), os padrões da TA-Lib, RSI, ATR, distância das médias,
    posição nas Bandas de Bollinger e volume, no 4h e no diário, também do BTC,
    e estima a probabilidade de o preço subir 2 ATR antes de cair 1 ATR nas próximas
    30 velas (5 dias).
  - Só entra quando essa probabilidade passa de `ml_threshold` e o FreqAI considera
    os dados parecidos com os de treino (do_predict == 1).

Os filtros fixos de tendência e de regime do BTC da v2 ficam desligados aqui:
o modelo recebe essas informações como atributos e aprende quando importam.

O FreqAI retreina o modelo periodicamente (no backtest, a cada `backtest_period_days`),
então cada período é sempre previsto por um modelo que não viu aqueles dados.

Requer a imagem freqtradeorg/freqtrade:stable_freqai e o bloco "freqai" do
config-freqai.json. Rode com: --freqaimodel LightGBMClassifier
"""

import sys
from pathlib import Path

import numpy as np
from pandas import DataFrame

import talib.abstract as ta

# O Freqtrade carrega estratégias pelo caminho do arquivo; para herdar de outra
# estratégia da mesma pasta, a pasta precisa estar no sys.path.
sys.path.append(str(Path(__file__).parent))
from CandleProStrategy import CandleProBase  # noqa: E402


TALIB_PATTERNS = [
    "CDLHAMMER",
    "CDLINVERTEDHAMMER",
    "CDLENGULFING",
    "CDLMORNINGSTAR",
    "CDLPIERCING",
    "CDLHARAMI",
    "CDLDOJI",
    "CDLDRAGONFLYDOJI",
    "CDLSHOOTINGSTAR",
    "CDLEVENINGSTAR",
    "CDLDARKCLOUDCOVER",
    "CDLHANGINGMAN",
    "CDL3WHITESOLDIERS",
    "CDL3BLACKCROWS",
]


def first_touch_labels(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, atr: np.ndarray,
    horizon: int, up_atr: float, down_atr: float,
) -> np.ndarray:
    """'win' se o preço sobe up_atr*ATR antes de cair down_atr*ATR nas próximas `horizon` velas."""
    n = len(close)
    labels = np.full(n, "loss", dtype=object)
    for i in range(n):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        up = close[i] + up_atr * atr[i]
        down = close[i] - down_atr * atr[i]
        end = min(n, i + 1 + horizon)
        for j in range(i + 1, end):
            if low[j] <= down:  # stop primeiro (conservador se ambos na mesma vela)
                break
            if high[j] >= up:
                labels[i] = "win"
                break
    return labels


class CandleFreqAIStrategy(CandleProBase):
    use_trend = False
    use_btc_regime = False
    use_confirmation = True
    use_location = True
    exit_mode = "target"

    ml_threshold = 0.45
    label_horizon = 30
    label_up_atr = 2.0
    label_down_atr = 1.0

    plot_config = {
        "main_plot": {"ema50": {"color": "orange"}, "entry_stop": {"color": "red"}},
        "subplots": {
            "Probabilidade": {"win": {"color": "green"}},
            "FreqAI": {"do_predict": {"color": "brown"}},
        },
    }

    # ---------------------------------------------------------- FreqAI: atributos
    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int, metadata: dict, **kwargs) -> DataFrame:
        # Multiplicado por indicator_periods_candles, timeframes e pares correlatos
        dataframe["%-rsi-period"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-atr_pct-period"] = ta.ATR(dataframe, timeperiod=period) / dataframe["close"]
        dataframe["%-roc-period"] = ta.ROC(dataframe, timeperiod=period)
        ema = ta.EMA(dataframe, timeperiod=period)
        dataframe["%-dist_ema-period"] = dataframe["close"] / ema - 1
        bb = ta.BBANDS(dataframe, timeperiod=period, nbdevup=2.0, nbdevdn=2.0)
        width = (bb["upperband"] - bb["lowerband"]).replace(0, np.nan)
        dataframe["%-bb_pos-period"] = (dataframe["close"] - bb["lowerband"]) / width
        vol_mean = dataframe["volume"].rolling(period).mean()
        vol_std = dataframe["volume"].rolling(period).std().replace(0, np.nan)
        dataframe["%-vol_z-period"] = (dataframe["volume"] - vol_mean) / vol_std
        return dataframe

    def feature_engineering_expand_basic(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        # Anatomia da vela, expandida por timeframes, velas anteriores e pares correlatos
        rng = (dataframe["high"] - dataframe["low"]).replace(0, np.nan)
        dataframe["%-body"] = (dataframe["close"] - dataframe["open"]) / rng
        dataframe["%-upper_wick"] = (
            dataframe["high"] - dataframe[["open", "close"]].max(axis=1)
        ) / rng
        dataframe["%-lower_wick"] = (
            dataframe[["open", "close"]].min(axis=1) - dataframe["low"]
        ) / rng
        dataframe["%-range_pct"] = rng / dataframe["close"]
        dataframe["%-pct_change"] = dataframe["close"].pct_change()
        return dataframe

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        # Padrões clássicos da TA-Lib só no timeframe base (-1, 0 ou +1)
        for fn in TALIB_PATTERNS:
            dataframe[f"%-{fn.lower()}"] = getattr(ta, fn)(dataframe) / 100.0
        return dataframe

    # ------------------------------------------------------------ FreqAI: alvo
    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        self.freqai.class_names = ["loss", "win"]
        atr = ta.ATR(dataframe, timeperiod=14)
        dataframe["&-outcome"] = first_touch_labels(
            dataframe["high"].to_numpy(dtype=float),
            dataframe["low"].to_numpy(dtype=float),
            dataframe["close"].to_numpy(dtype=float),
            atr.to_numpy(dtype=float),
            self.label_horizon,
            self.label_up_atr,
            self.label_down_atr,
        )
        return dataframe

    # ------------------------------------------------------------ estratégia
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        dataframe = self.add_rule_columns(dataframe, metadata)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cond = self.rule_entry_conditions(dataframe)
        if "win" in dataframe.columns:
            cond &= (dataframe["do_predict"] == 1) & (dataframe["win"] >= self.ml_threshold)
        else:
            cond &= False
        dataframe.loc[cond, "enter_long"] = 1
        dataframe.loc[cond, "enter_tag"] = "ml_" + dataframe.loc[cond, "signal_name"]
        return dataframe
