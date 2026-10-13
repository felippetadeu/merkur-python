"""
Famílias de estratégias para velas de 4h e 1h (todas geram matrizes para fastslots.run).

Convenções:
  - parâmetros em NÚMERO DE VELAS do timeframe usado;
  - informações do diário entram sem olhar o futuro: o valor da vela diária D só fica
    disponível na vela intradiária que fecha junto com ela (como o merge do Freqtrade).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from families import atr, ema, rsi, sma
from fastslots import Market

TF_HOURS = {"1h": 1, "4h": 4}


# ------------------------------------------------------------ utilidades
def daily_to_intraday(daily_series: pd.Series, index: pd.DatetimeIndex, tf: str) -> pd.Series:
    """Leva um valor calculado na vela diária D para as velas intradiárias que fecham
    em D+1 00:00 ou depois (sem lookahead)."""
    shift = pd.Timedelta(days=1) - pd.Timedelta(hours=TF_HOURS[tf])
    s = daily_series.copy()
    s.index = s.index + shift
    return s.reindex(index, method="ffill")


def resample_daily(df: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df.resample("1D", label="left", closed="left").agg(agg).dropna()


def strong_candle(df, body_min=0.5, close_pos=0.67, vol_n=20):
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    rng = (h - l).replace(0, np.nan)
    return (((c - o) / rng > body_min) & ((c - l) / rng > close_pos) & (v > v.rolling(vol_n).mean())).fillna(False)


def bullish_reversal(df):
    """Engolfo de alta ou martelo (sem TA-Lib)."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    engulf = (c.shift(1) < o.shift(1)) & (c > o) & (c >= o.shift(1)) & (o <= c.shift(1))
    lw = np.minimum(o, c) - l
    uw = h - np.maximum(o, c)
    hammer = (lw >= 2 * body) & (uw <= 0.3 * rng) & (body >= 0.05 * rng)
    return (engulf | hammer).fillna(False)


class Context:
    """Pré-calcula contexto compartilhado: regime do BTC (diário), tendência diária de
    cada par e ranking de liquidez."""

    def __init__(self, mkt: Market, tf: str, btc_ma=100, liq_days=90):
        self.mkt, self.tf = mkt, tf
        data = mkt.raw
        btc_d = resample_daily(data["BTC/USDT"])
        btc_ok = btc_d["close"] > sma(btc_d["close"], btc_ma)
        self.btc_ok = daily_to_intraday(btc_ok.astype(float), mkt.index, tf).fillna(0).astype(bool).to_numpy()
        self.daily = {p: resample_daily(data[p]) for p in mkt.pairs}
        per_day = 24 // TF_HOURS[tf]
        dv = {p: (data[p]["close"] * data[p]["volume"]).rolling(liq_days * per_day, min_periods=liq_days * per_day // 2).mean()
              for p in mkt.pairs}
        table = pd.DataFrame({p: s.reindex(mkt.index) for p, s in dv.items()})
        self.liq_rank = table.rank(axis=1, ascending=False).to_numpy()

    def daily_trend(self, kind="sma50", n=None):
        out = {}
        for p, d in self.daily.items():
            if kind == "sma50":
                ok = d["close"] > sma(d["close"], n or 50)
            elif kind == "donch":
                ok = d["close"] >= d["high"].rolling(n or 20).max() * 0.9
            elif kind == "ema_cross":
                ok = ema(d["close"], 20) > ema(d["close"], n or 50)
            else:
                raise ValueError(kind)
            out[p] = daily_to_intraday(ok.astype(float), self.mkt.index, self.tf)
        return self.mkt.align(out, fill=0.0).astype(bool)


# ------------------------------------------------------------- famílias
def breakout(ctx: Context, entry_n=180, exit_n=60, atr_stop=2.5, atr_n=14, candle=True,
             top_n=6, use_btc=True, daily_trend=None):
    """Rompimento de Donchian intradiário (entry_n/exit_n em velas)."""
    mkt = ctx.mkt
    enter, stop, trail = {}, {}, {}
    for p in mkt.pairs:
        df = mkt.raw[p]
        hh = df["high"].rolling(entry_n).max().shift(1)
        sig = df["close"] > hh
        if candle:
            sig &= strong_candle(df)
        enter[p] = sig
        stop[p] = df["close"] - atr_stop * atr(df, atr_n)
        trail[p] = df["low"].rolling(exit_n).min()
    E = mkt.align(enter, fill=0.0).astype(bool)
    if use_btc:
        E &= ctx.btc_ok[:, None]
    if top_n:
        E &= ctx.liq_rank <= top_n
    if daily_trend:
        E &= ctx.daily_trend(*daily_trend)
    return {"enter": E, "stop": mkt.align(stop), "trail": mkt.align(trail)}


def pullback(ctx: Context, rsi_n=2, buy=10, sell=70, atr_stop=2.0, candle=False, top_n=6,
             use_btc=True, daily_trend=("sma50", 50), max_hold=None, target_atr=None):
    """Compra recuos de curto prazo dentro de uma tendência diária de alta."""
    mkt = ctx.mkt
    enter, stop, exit_, target = {}, {}, {}, {}
    for p in mkt.pairs:
        df = mkt.raw[p]
        r = rsi(df["close"], rsi_n)
        a = atr(df)
        if candle:
            sig = (r.shift(1) < buy) & bullish_reversal(df)
        else:
            sig = r < buy
        enter[p] = sig
        stop[p] = df["close"] - atr_stop * a
        exit_[p] = r > sell
        if target_atr:
            target[p] = df["close"] + target_atr * a
    E = mkt.align(enter, fill=0.0).astype(bool)
    if use_btc:
        E &= ctx.btc_ok[:, None]
    if top_n:
        E &= ctx.liq_rank <= top_n
    if daily_trend:
        E &= ctx.daily_trend(*daily_trend)
    out = {"enter": E, "stop": mkt.align(stop), "exit": mkt.align(exit_, fill=0.0).astype(bool)}
    if target_atr:
        out["target"] = mkt.align(target)
    return out
