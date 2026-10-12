"""
D. Rotação por momentum (nível de carteira).

A cada `rebalance` velas, ordena os pares pelo retorno dos últimos `lookback` períodos e
fica comprado nos `top_k` mais fortes, em partes iguais, desde que:
  - o momentum do par seja positivo (momentum absoluto);
  - o BTC esteja acima da sua média de `regime_n` períodos (senão fica 100% em USDT).
Opcional: stop diário por par (sai do par até o próximo rebalanceamento).

Custos: 0,1% sobre o volume girado em cada rebalanceamento.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from lab import FEE


def rotation(data: dict[str, pd.DataFrame], start, end, lookback=30, top_k=3, rebalance=7,
             regime_n=100, abs_mom=True, vol_adjust=False, skip_recent=0):
    closes = pd.DataFrame({p: d["close"] for p, d in data.items()}).sort_index()
    opens = pd.DataFrame({p: d["open"] for p, d in data.items()}).reindex(closes.index)
    btc = closes["BTC/USDT"]
    regime = btc > btc.rolling(regime_n, min_periods=regime_n).mean()

    mom = closes.shift(skip_recent) / closes.shift(skip_recent + lookback) - 1
    if vol_adjust:
        vol = closes.pct_change().rolling(lookback).std()
        score = mom / vol
    else:
        score = mom

    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end, tz="UTC")
    idx = closes.index[(closes.index >= s) & (closes.index < e)]

    weights = pd.Series(0.0, index=closes.columns)
    equity = 1.0
    curve = []
    n_trades = 0
    trade_rets = []
    entry_px = {}
    for k, t in enumerate(idx):
        i = closes.index.get_loc(t)
        # 1) retorno do dia com os pesos atuais (abertura->abertura aproximado por fechamento->fechamento)
        if k > 0:
            prev = closes.iloc[i - 1]
            r = (closes.iloc[i] / prev - 1).fillna(0.0)
            equity *= 1 + float((weights * r).sum())
        # 2) rebalanceia no fechamento das velas de rebalanceamento (executa no fechamento,
        #    custo cobrado sobre o giro)
        if k % rebalance == 0:
            sc = score.iloc[i].dropna()
            target = pd.Series(0.0, index=closes.columns)
            if bool(regime.iloc[i]) and len(sc):
                cand = sc[sc > 0] if abs_mom else sc
                chosen = cand.sort_values(ascending=False).head(top_k).index
                if len(chosen):
                    target[chosen] = 1.0 / top_k
            # cálculo de trades fechados para estatística
            for p in closes.columns:
                if weights[p] > 0 and target[p] == 0:
                    trade_rets.append(closes.iloc[i][p] / entry_px[p] * (1 - FEE) ** 2 - 1)
                    n_trades += 1
                if weights[p] == 0 and target[p] > 0:
                    entry_px[p] = closes.iloc[i][p]
            turnover = float((target - weights).abs().sum())
            equity *= 1 - turnover * FEE
            weights = target
        curve.append(equity)
    eq = pd.Series(curve, index=idx)
    return eq, np.array(trade_rets)


def rot_metrics(eq: pd.Series, trade_rets: np.ndarray, data, start, end) -> dict:
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1
    dd = (eq / eq.cummax() - 1).min()
    wins, losses = trade_rets[trade_rets > 0].sum(), -trade_rets[trade_rets < 0].sum()
    bh = []
    for d in data.values():
        w = d.loc[(d.index >= pd.Timestamp(start, tz="UTC")) & (d.index < pd.Timestamp(end, tz="UTC")), "close"]
        if len(w) > 10:
            bh.append(w.iloc[-1] / w.iloc[0] - 1)
    return {
        "trades": len(trade_rets),
        "total": eq.iloc[-1] - 1,
        "cagr": cagr,
        "max_dd": dd,
        "pf": wins / losses if losses > 0 else np.inf,
        "win": (trade_rets > 0).mean() if len(trade_rets) else np.nan,
        "avg": trade_rets.mean() if len(trade_rets) else np.nan,
        "calmar": cagr / abs(dd) if dd < 0 else np.inf,
        "bh": float(np.mean(bh)),
    }
