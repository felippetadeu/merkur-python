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
             regime_n=None, min_hist=60, regime_exit=False, **_):
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
    exit_ = ~btc_regime(data, df.index, regime_n) if (regime_n and regime_exit) else None
    return Signals(
        enter=enter,
        stop=df["close"] - atr_stop * a,
        trail=trail,
        exit=exit_,
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
                    vol_mult=1.0, regime_exit=False, body_min=0.5, close_pos=0.67, **_):
    """E. Mistura: rompimento de Donchian confirmado por vela forte (corpo > 50% da
    amplitude, fechando no terço superior) e volume acima da média, com BTC em alta."""
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    rng = (h - l).replace(0, np.nan)
    strong = ((c - o) / rng > body_min) & ((c - l) / rng > close_pos)
    hh = h.rolling(entry_n).max().shift(1)
    enter = (c > hh) & strong & (v > v.rolling(20).mean() * vol_mult)
    if regime_n:
        enter &= btc_regime(data, df.index, regime_n)
    a = atr(df)
    exit_ = ~btc_regime(data, df.index, regime_n) if (regime_n and regime_exit) else None
    return Signals(enter=enter, stop=c - atr_stop * a, trail=l.rolling(exit_n).min(), exit=exit_,
                   tag=pd.Series("rompimento_candle", index=df.index))


def relative_strength(df: pd.DataFrame, data: dict, n: int) -> pd.Series:
    """Retorno do par em n períodos dividido pelo retorno do BTC no mesmo intervalo."""
    btc = data["BTC/USDT"]["close"].reindex(df.index).ffill()
    return (df["close"] / df["close"].shift(n)) / (btc / btc.shift(n))


def breakout_rs(df, pair, data, rs_n=60, rs_min=1.0, candle=True, **kw):
    """F3: rompimento (com ou sem vela forte) só em moedas mais fortes que o BTC.
    O próprio BTC passa sempre no filtro (ele é a referência)."""
    base = breakout_candle(df, pair, data, **kw) if candle else donchian(df, pair, data, **kw)
    if pair != "BTC/USDT":
        base.enter = base.enter & (relative_strength(df, data, rs_n) > rs_min).fillna(False)
    return base


def regime_series(data: dict, index: pd.Index, kind: str) -> pd.Series:
    """Filtros de regime do BTC.
    sma100 / sma200: preço acima da média; golden: SMA50 > SMA200 e preço > SMA200;
    dual: preço > SMA100 e SMA100 subindo (vs 20 dias atrás)."""
    btc = data["BTC/USDT"]["close"]
    if kind == "sma100":
        ok = btc > sma(btc, 100)
    elif kind == "sma200":
        ok = btc > sma(btc, 200)
    elif kind == "golden":
        ok = (sma(btc, 50) > sma(btc, 200)) & (btc > sma(btc, 200))
    elif kind == "dual":
        m = sma(btc, 100)
        ok = (btc > m) & (m > m.shift(20))
    else:
        raise ValueError(kind)
    return ok.reindex(index).ffill().fillna(False).astype(bool)


def trend_system(df, pair, data, entry_n=30, exit_n=10, atr_stop=2.5, regime="sma100",
                 pair_trend=None, candle=True, regime_exit=False, **_):
    """Rompimento com filtros configuráveis de regime (BTC) e de tendência do próprio par."""
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    rng = (h - l).replace(0, np.nan)
    enter = c > h.rolling(entry_n).max().shift(1)
    if candle:
        strong = ((c - o) / rng > 0.5) & ((c - l) / rng > 0.67)
        enter &= strong & (v > v.rolling(20).mean())
    reg = regime_series(data, df.index, regime)
    enter &= reg
    if pair_trend:
        enter &= c > sma(c, pair_trend)
    a = atr(df)
    return Signals(enter=enter.fillna(False), stop=c - atr_stop * a, trail=l.rolling(exit_n).min(),
                   exit=(~reg) if regime_exit else None, tag=pd.Series("tendencia", index=df.index))


def mean_reversion(df, pair, data, kind="rsi2", trend_n=200, buy=10, sell=70, atr_stop=3.0,
                   max_hold=7, regime=None, bb_n=20, bb_k=2.0, **_):
    """Reversão à média: compra excessos de queda e vende no repique.
    rsi2: RSI(2) < buy, sai com RSI(2) > sell
    bb:   fecha abaixo da banda inferior de Bollinger, sai ao voltar para a média
    ibs:  fecha no extremo inferior da vela (IBS < buy/100) após queda, sai com IBS alto
    Filtro de tendência longa do par (preço > SMA trend_n) opcional (trend_n=None desliga)."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    if kind == "rsi2":
        r = rsi(c, 2)
        enter, exit_ = r < buy, r > sell
    elif kind == "bb":
        m = sma(c, bb_n)
        sd = c.rolling(bb_n).std()
        enter, exit_ = c < m - bb_k * sd, c > m
    elif kind == "ibs":
        ibs = (c - l) / (h - l).replace(0, np.nan)
        enter = (ibs < buy / 100) & (c < c.shift(1))
        exit_ = ibs > sell / 100
    else:
        raise ValueError(kind)
    if trend_n:
        enter &= c > sma(c, trend_n)
    if regime:
        enter &= regime_series(data, df.index, regime)
    a = atr(df)
    return Signals(enter=enter.fillna(False), stop=c - atr_stop * a, exit=exit_.fillna(False),
                   max_hold=max_hold, tag=pd.Series("reversao", index=df.index))


def liquidity_rank(data: dict, n: int = 90) -> pd.DataFrame:
    """Ranking diário por volume financeiro médio (preço x volume) dos últimos n dias.
    1 = mais negociado. Usa só dados passados."""
    dv = pd.DataFrame({p: (d["close"] * d["volume"]).rolling(n, min_periods=n // 2).mean() for p, d in data.items()})
    return dv.rank(axis=1, ascending=False)


def trend_liquid(df, pair, data, top_n=6, liq_n=90, **kw):
    """Rompimento só nas `top_n` moedas mais negociadas (ranking recalculado todo dia)."""
    sig = trend_system(df, pair, data, **kw)
    rank = data.get("_liq_rank")
    if rank is None:
        raise ValueError("calcule data['_liq_rank'] antes")
    ok = (rank[pair].reindex(df.index) <= top_n).fillna(False)
    sig.enter = sig.enter & ok
    return sig
