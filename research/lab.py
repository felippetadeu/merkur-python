"""
Laboratório de pesquisa: backtester rápido em pandas/numpy para explorar ideias
antes de confirmá-las no Freqtrade.

Regras de execução (iguais às do Freqtrade, de forma conservadora):
  - sinais calculados no FECHAMENTO da vela; entrada na ABERTURA da vela seguinte;
  - taxa de 0,1% na entrada e na saída;
  - stop e alvo checados dentro da vela; se os dois forem atingidos na mesma vela,
    assume-se o stop (pior caso); gaps abrem no preço de abertura;
  - carteira: capital dividido igualmente entre os pares (uma "fatia" por par),
    cada fatia compõe seus próprios resultados; no máximo uma posição por par.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

FEE = 0.001
TF_PER_YEAR = {"4h": 6 * 365, "1d": 365}

PERIODS = {
    "P1": ("2019-09-01", "2023-01-01"),
    "P2": ("2023-01-01", "2025-01-01"),
    "P3": ("2025-01-01", "2026-10-11"),
}


# ----------------------------------------------------------------- dados
def load(path: str | Path) -> dict[str, pd.DataFrame]:
    raw = pd.read_parquet(path)
    raw["date"] = pd.to_datetime(raw["date"], utc=True)
    out = {}
    for pair, df in raw.groupby("pair"):
        df = df.drop(columns="pair").sort_values("date").drop_duplicates("date")
        out[pair] = df.set_index("date")
    return out


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df.resample(rule, label="left", closed="left").agg(agg).dropna()


# ------------------------------------------------------------- simulação
@dataclass
class Trade:
    pair: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    reason: str
    tag: str = ""

    @property
    def ret(self) -> float:
        return (self.exit * (1 - FEE)) / (self.entry * (1 + FEE)) - 1


@dataclass
class Signals:
    """Saída de uma estratégia para um par (todas as séries no índice das velas).

    enter: bool  - entrar na abertura da PRÓXIMA vela
    stop: float  - stop inicial (preço absoluto) definido na vela do sinal (NaN = sem stop)
    target: float - alvo (preço absoluto) definido na vela do sinal (NaN = sem alvo)
    exit: bool   - sair na abertura da próxima vela (sinal de saída)
    trail: float - nível de trailing stop calculado em cada vela (o stop só sobe)
    tag: str     - nome do sinal
    """

    enter: pd.Series
    stop: pd.Series | None = None
    target: pd.Series | None = None
    exit: pd.Series | None = None
    trail: pd.Series | None = None
    tag: pd.Series | None = None
    max_hold: int | None = None


def simulate_pair(pair: str, df: pd.DataFrame, sig: Signals, start, end) -> list[Trade]:
    idx = df.index
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    enter = sig.enter.reindex(idx).fillna(False).to_numpy(bool)
    stop0 = (sig.stop.reindex(idx) if sig.stop is not None else pd.Series(np.nan, idx)).to_numpy(float)
    tgt0 = (sig.target.reindex(idx) if sig.target is not None else pd.Series(np.nan, idx)).to_numpy(float)
    ex = (sig.exit.reindex(idx).fillna(False) if sig.exit is not None else pd.Series(False, idx)).to_numpy(bool)
    trail = (sig.trail.reindex(idx) if sig.trail is not None else pd.Series(np.nan, idx)).to_numpy(float)
    tags = (sig.tag.reindex(idx).fillna("") if sig.tag is not None else pd.Series("", idx)).to_numpy(object)

    start_i = idx.searchsorted(pd.Timestamp(start, tz="UTC"))
    end_i = idx.searchsorted(pd.Timestamp(end, tz="UTC"))
    trades: list[Trade] = []
    i = start_i
    while i < min(end_i, n) - 1:
        if not enter[i]:
            i += 1
            continue
        j = i + 1  # vela de entrada
        entry = o[j]
        stop = stop0[i] if np.isfinite(stop0[i]) and stop0[i] < entry else np.nan
        target = tgt0[i] if np.isfinite(tgt0[i]) and tgt0[i] > entry else np.nan
        tag = tags[i]
        exit_px, reason, k = None, "", j
        while k < n:
            # 1) stop dentro da vela (gap abaixo do stop sai na abertura)
            if np.isfinite(stop) and l[k] <= stop:
                exit_px, reason = (o[k] if o[k] < stop else stop), "stop"
                break
            # 2) alvo dentro da vela
            if np.isfinite(target) and h[k] >= target:
                exit_px, reason = (o[k] if o[k] > target else target), "alvo"
                break
            # 3) fim do período: fecha no fechamento
            if k >= end_i - 1 or k == n - 1:
                exit_px, reason = c[k], "fim"
                break
            # 4) sinal de saída / tempo -> sai na abertura da próxima vela
            if ex[k] or (sig.max_hold and (k - j + 1) >= sig.max_hold):
                exit_px, reason = o[k + 1], ("sinal" if ex[k] else "tempo")
                k = k + 1
                break
            # 5) atualiza trailing com dados da vela já fechada
            if np.isfinite(trail[k]):
                stop = trail[k] if not np.isfinite(stop) else max(stop, trail[k])
            k += 1
        trades.append(Trade(pair, idx[j], idx[k], entry, exit_px, reason, tag))
        i = k  # próxima entrada só depois de sair
    return trades


@dataclass
class Result:
    trades: list[Trade]
    equity: pd.Series
    start: str
    end: str
    tf: str
    bh: float = np.nan
    metrics: dict = field(default_factory=dict)


def portfolio(data: dict[str, pd.DataFrame], strat, start, end, tf="4h", **params) -> Result:
    """Roda a estratégia em todos os pares; capital dividido igualmente entre eles."""
    pairs = list(data)
    all_trades = []
    curves = []
    bh = []
    for pair in pairs:
        df = data[pair]
        sig = strat(df, pair=pair, data=data, **params)
        trades = simulate_pair(pair, df, sig, start, end)
        all_trades += trades
        win = df.loc[(df.index >= pd.Timestamp(start, tz="UTC")) & (df.index < pd.Timestamp(end, tz="UTC"))]
        if len(win) < 10:
            continue
        # curva da fatia: compõe os retornos na data de saída de cada trade
        s = pd.Series(1.0, index=win.index)
        mult = 1.0
        rets = sorted(trades, key=lambda t: t.exit_time)
        r_idx = 0
        vals = []
        open_trades = sorted(trades, key=lambda t: t.entry_time)
        # marcação a mercado: enquanto em posição, valor acompanha o fechamento
        pos = None
        ti = 0
        closes = win["close"].to_numpy(float)
        times = win.index
        for t_i, t in enumerate(times):
            while ti < len(open_trades) and open_trades[ti].entry_time <= t and pos is None:
                pos = open_trades[ti]
                ti += 1
            if pos is not None and t >= pos.exit_time:
                mult *= 1 + pos.ret
                pos = None
                while ti < len(open_trades) and open_trades[ti].entry_time <= t and pos is None:
                    pos = open_trades[ti]
                    ti += 1
            if pos is not None:
                vals.append(mult * (closes[t_i] * (1 - FEE)) / (pos.entry * (1 + FEE)))
            else:
                vals.append(mult)
        curves.append(pd.Series(vals, index=times))
        bh.append(win["close"].iloc[-1] / win["close"].iloc[0] - 1)
    eq = pd.concat(curves, axis=1).ffill().fillna(1.0).mean(axis=1)
    res = Result(all_trades, eq, start, end, tf, bh=float(np.mean(bh)) if bh else np.nan)
    res.metrics = metrics(res)
    return res


def metrics(res: Result) -> dict:
    eq = res.equity
    if len(eq) < 2:
        return {}
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    total = eq.iloc[-1] - 1
    cagr = (eq.iloc[-1]) ** (1 / years) - 1 if years > 0 and eq.iloc[-1] > 0 else -1
    dd = (eq / eq.cummax() - 1).min()
    rets = np.array([t.ret for t in res.trades])
    wins, losses = rets[rets > 0].sum(), -rets[rets < 0].sum()
    pf = wins / losses if losses > 0 else np.inf
    return {
        "trades": len(rets),
        "total": total,
        "cagr": cagr,
        "max_dd": dd,
        "pf": pf,
        "win": (rets > 0).mean() if len(rets) else np.nan,
        "avg": rets.mean() if len(rets) else np.nan,
        "calmar": cagr / abs(dd) if dd < 0 else np.inf,
        "bh": res.bh,
    }


def passes(m: dict) -> bool:
    return (
        m.get("cagr", -1) >= 0.15
        and m.get("max_dd", -1) >= -0.25
        and m.get("pf", 0) >= 1.3
        and m.get("trades", 0) >= 40
    )


def fmt(m: dict) -> str:
    if not m:
        return "sem dados"
    return (
        f"trades {m['trades']:4d} | total {m['total']*100:7.1f}% | anual {m['cagr']*100:6.1f}% | "
        f"DD {m['max_dd']*100:6.1f}% | PF {m['pf']:5.2f} | acerto {m['win']*100:5.1f}% | "
        f"calmar {m['calmar']:5.2f} | B&H {m['bh']*100:7.1f}%"
    )


def evaluate(data, strat, tf="4h", periods=("P1", "P2"), verbose=True, label=None, **params):
    out = {}
    for p in periods:
        s, e = PERIODS[p]
        r = portfolio(data, strat, s, e, tf=tf, **params)
        out[p] = r.metrics
        if verbose:
            mark = "OK " if passes(r.metrics) else "   "
            print(f"  {mark}{p} {fmt(r.metrics)}")
    return out
