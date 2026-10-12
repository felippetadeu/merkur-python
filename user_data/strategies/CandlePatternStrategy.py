# pragma pylint: disable=missing-docstring, invalid-name
"""
CandlePatternStrategy

Estratégia baseada em padrões de candle (velas japonesas) com filtros de contexto.

Entrada (compra):
  - Um padrão de reversão de ALTA na vela que acabou de fechar:
    martelo, engolfo de alta, estrela da manhã ou linha de perfuração.
  - Contexto de queda: RSI abaixo de `buy_rsi` (o padrão aparece depois de uma queda).
  - Volume da vela acima da média de 20 velas multiplicada por `buy_volume_factor`.

Saída (venda):
  - Um padrão de reversão de BAIXA (estrela cadente, engolfo de baixa, estrela da tarde,
    nuvem negra ou enforcado) com RSI acima de `sell_rsi`;
  - ou RSI muito alto (sobrecompra extrema);
  - além do stoploss, do trailing stop e do ROI mínimo definidos abaixo.

O Freqtrade só analisa velas fechadas, então os sinais nunca usam uma vela em formação.

Aviso: padrões de candle indicam probabilidades, não certezas. Sempre faça backtest
e rode em simulação (dry-run) antes de pensar em operar com dinheiro real.
"""

import numpy as np
from pandas import DataFrame

import talib.abstract as ta
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy


class CandlePatternStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    can_short = False

    # Lucro mínimo para sair, por tempo de operação (minutos -> lucro).
    # 0 min: 12% | após 2 dias: 6% | após 4 dias: 3% | após 8 dias: sai no zero a zero.
    minimal_roi = {
        "0": 0.12,
        "2880": 0.06,
        "5760": 0.03,
        "11520": 0.0,
    }

    # Perda máxima por operação.
    stoploss = -0.06

    # Trailing stop: depois de 4% de lucro, protege o ganho a 2% do topo.
    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.04
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # Velas necessárias antes de o primeiro sinal ser válido (RSI, médias de volume e EMA).
    startup_candle_count: int = 60

    # Parâmetros ajustáveis (podem ser otimizados com hyperopt).
    buy_rsi = IntParameter(25, 50, default=45, space="buy", optimize=True)
    buy_volume_factor = DecimalParameter(0.8, 2.0, default=1.0, decimals=1, space="buy", optimize=True)
    sell_rsi = IntParameter(50, 75, default=55, space="sell", optimize=True)
    sell_rsi_extreme = IntParameter(70, 90, default=78, space="sell", optimize=True)

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    plot_config = {
        "main_plot": {
            "ema50": {"color": "orange"},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "purple"}},
            "Volume": {
                "volume": {"type": "bar", "color": "gray"},
                "volume_mean": {"color": "blue"},
            },
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Indicadores de contexto
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["volume_mean"] = dataframe["volume"].rolling(20).mean()

        # Padrões de ALTA (TA-Lib retorna +100 quando o padrão de alta aparece)
        dataframe["cdl_hammer"] = ta.CDLHAMMER(dataframe)
        dataframe["cdl_engulfing"] = ta.CDLENGULFING(dataframe)  # +100 alta / -100 baixa
        dataframe["cdl_morningstar"] = ta.CDLMORNINGSTAR(dataframe)
        dataframe["cdl_piercing"] = ta.CDLPIERCING(dataframe)

        # Padrões de BAIXA (TA-Lib retorna -100 quando o padrão de baixa aparece)
        dataframe["cdl_shootingstar"] = ta.CDLSHOOTINGSTAR(dataframe)
        dataframe["cdl_eveningstar"] = ta.CDLEVENINGSTAR(dataframe)
        dataframe["cdl_darkcloud"] = ta.CDLDARKCLOUDCOVER(dataframe)
        dataframe["cdl_hangingman"] = ta.CDLHANGINGMAN(dataframe)

        # Colunas booleanas para cada padrão, já no sentido certo
        dataframe["bull_hammer"] = dataframe["cdl_hammer"] > 0
        dataframe["bull_engulfing"] = dataframe["cdl_engulfing"] > 0
        dataframe["bull_morningstar"] = dataframe["cdl_morningstar"] > 0
        dataframe["bull_piercing"] = dataframe["cdl_piercing"] > 0

        dataframe["bear_shootingstar"] = dataframe["cdl_shootingstar"] < 0
        dataframe["bear_engulfing"] = dataframe["cdl_engulfing"] < 0
        dataframe["bear_eveningstar"] = dataframe["cdl_eveningstar"] < 0
        dataframe["bear_darkcloud"] = dataframe["cdl_darkcloud"] < 0
        dataframe["bear_hangingman"] = dataframe["cdl_hangingman"] < 0

        dataframe["bullish_pattern"] = (
            dataframe["bull_hammer"]
            | dataframe["bull_engulfing"]
            | dataframe["bull_morningstar"]
            | dataframe["bull_piercing"]
        )
        dataframe["bearish_pattern"] = (
            dataframe["bear_shootingstar"]
            | dataframe["bear_engulfing"]
            | dataframe["bear_eveningstar"]
            | dataframe["bear_darkcloud"]
            | dataframe["bear_hangingman"]
        )

        # Nome do padrão, usado nas tags (aparece no Telegram e nos relatórios de backtest)
        dataframe["bull_name"] = np.select(
            [
                dataframe["bull_morningstar"],
                dataframe["bull_engulfing"],
                dataframe["bull_piercing"],
                dataframe["bull_hammer"],
            ],
            ["estrela_da_manha", "engolfo_de_alta", "linha_de_perfuracao", "martelo"],
            default="",
        )
        dataframe["bear_name"] = np.select(
            [
                dataframe["bear_eveningstar"],
                dataframe["bear_engulfing"],
                dataframe["bear_darkcloud"],
                dataframe["bear_shootingstar"],
                dataframe["bear_hangingman"],
            ],
            ["estrela_da_tarde", "engolfo_de_baixa", "nuvem_negra", "estrela_cadente", "enforcado"],
            default="",
        )

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = (
            dataframe["bullish_pattern"]
            & (dataframe["rsi"] < self.buy_rsi.value)
            & (dataframe["volume"] > dataframe["volume_mean"] * self.buy_volume_factor.value)
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[conditions, "enter_long"] = 1
        dataframe.loc[conditions, "enter_tag"] = dataframe.loc[conditions, "bull_name"]

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pattern_exit = (
            dataframe["bearish_pattern"]
            & (dataframe["rsi"] > self.sell_rsi.value)
            & (dataframe["volume"] > 0)
        )
        rsi_exit = (dataframe["rsi"] > self.sell_rsi_extreme.value) & (dataframe["volume"] > 0)

        dataframe.loc[pattern_exit | rsi_exit, "exit_long"] = 1
        dataframe.loc[pattern_exit, "exit_tag"] = dataframe.loc[pattern_exit, "bear_name"]
        dataframe.loc[rsi_exit & ~pattern_exit, "exit_tag"] = "rsi_sobrecomprado"

        return dataframe
