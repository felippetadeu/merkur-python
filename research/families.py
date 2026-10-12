"""
Famílias de estratégias para o laboratório (sinais em velas DIÁRIAS).

Cada função recebe o dataframe de um par (índice = data, colunas OHLCV), o nome
do par e o dicionário com todos os pares (para filtros de regime do BTC), e
devolve um lab.Signals.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from lab import Signals


# ------------------------------------------------------------ indicadores
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def btc_regime(data: dict, index: pd.Index, n: int = 100, kind: str = "sma") -> pd.Series:
    btc = data.get("BTC/USDT")
    if btc is None:
        return pd.Series(True, index=index)
    ma = sma(btc["close"], n) if kind == "sma" else ema(btc["close"], n)
    ok = btc["close"] > ma
    return ok.reindex(index).ffill().fillna(False).astype(bool)


def bullish_candle(df: pd.DataFrame) -> pd.Series:
    """Padrões de alta simples (sem TA-Lib): engolfo de alta e martelo."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    prev_bear = c.shift(1) < o.shift(1)
    engulf = prev_bear & (c > o) & (c >= o.shift(1)) & (o <= c.shift(1))
    lower_wick = np.minimum(o, c) - l
    upper_wick = h - np.maximum(o, c)
    hammer = (lower_wick >= 2 * body) & (upper_wick <= 0.3 * rng) & (body >= 0.05 * rng)
    return (engulf | hammer).fillna(False)


# ------------------------------------------------------------- famílias
def donchian(df, pair, data, entry_n=20, exit_n=10, atr_stop=2.5, atr_trail=None,
             regime_n=None, min_hist=60, **_):
    """A. Rompimento: fecha acima da máxima de entry_n dias; sai abaixo da mínima de exit_n."""
    a = atr(df)
    hh = df["high"].rolling(entry_n).max().shift(1)
    ll = df["low"].rolling(exit_n).min()
    enter = df["close"] > hh
    if regime_n:
        enter &= btc_regime(data, df.index, regime_n)
    enter &= df["close"].notna() & (np.arange(len(df)) >= min_hist)
    trail = ll.copy()
    if atr_trail:
        chand = df["close"].rolling(entry_n).max() - atr_trail * a
        trail = np.maximum(trail, chand)
    return Signals(
        enter=enter,
        stop=df["close"] - atr_stop * a,
        trail=trail,
        tag=pd.Series("rompimento", index=df.index),
    )


def ma_trend(df, pair, data, fast=20, slow=100, atr_stop=3.0, regime_n=None, **_):
    """B. Tendência: entra quando a EMA rápida cruza acima da lenta; sai no cruzamento inverso."""
    f, s = ema(df["close"], fast), ema(df["close"], slow)
    above = f > s
    enter = above & ~above.shift(1, fill_value=False)
    if regime_n:
        reg = btc_regime(data, df.index, regime_n)
        enter = above & reg & ~(above & reg).shift(1, fill_value=False)
    exit_ = ~above
    if regime_n:
        exit_ |= ~btc_regime(data, df.index, regime_n)
    a = atr(df)
    return Signals(enter=enter, stop=df["close"] - atr_stop * a, exit=exit_,
                   tag=pd.Series("tendencia", index=df.index))


def pullback(df, pair, data, trend_n=100, rsi_n=2, rsi_buy=10, rsi_sell=70, atr_stop=2.5,
             use_candle=False, regime_n=None, max_hold=10, **_):
    """C. Pullback em tendência: acima da média longa, compra o recuo (RSI curto baixo
    ou candle de alta depois do recuo); sai quando o RSI curto se recupera."""
    c = df["close"]
    trend = c > sma(c, trend_n)
    r = rsi(c, rsi_n)
    if use_candle:
        dip = (r.shift(1) < rsi_buy) & bullish_candle(df)
    else:
        dip = r < rsi_buy
    enter = trend & dip
    if regime_n:
        enter &= btc_regime(data, df.index, regime_n)
    a = atr(df)
    return Signals(enter=enter, stop=c - atr_stop * a, exit=r > rsi_sell, max_hold=max_hold,
                   tag=pd.Series("pullback", index=df.index))


def breakout_candle(df, pair, data, entry_n=20, exit_n=10, atr_stop=2.5, regime_n=100,
                    vol_mult=1.0, **_):
    """E. Mistura: rompimento de Donchian confirmado por vela forte (corpo > 50% da
    amplitude, fechando no terço superior) e volume acima da média, com BTC em alta."""
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    rng = (h - l).replace(0, np.nan)
    strong = ((c - o) / rng > 0.5) & ((c - l) / rng > 0.67)
    hh = h.rolling(entry_n).max().shift(1)
    enter = (c > hh) & strong & (v > v.rolling(20).mean() * vol_mult)
    if regime_n:
        enter &= btc_regime(data, df.index, regime_n)
    a = atr(df)
    return Signals(enter=enter, stop=c - atr_stop * a, trail=l.rolling(exit_n).min(),
                   tag=pd.Series("rompimento_candle", index=df.index))
