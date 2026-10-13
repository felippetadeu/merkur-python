"""
Simulador por vagas em numpy (mesmas regras de slots.py, bem mais rápido).

Regras (iguais ao Freqtrade com max_open_trades = N e stake "unlimited"):
  - sinais no fechamento da vela; entrada na abertura da vela seguinte;
  - stop / alvo checados dentro da vela (stop primeiro; gap sai na abertura);
  - sinal de saída e saída por tempo executam na abertura da vela seguinte;
  - o trailing (stop móvel) é atualizado com a vela já fechada e vale a partir da próxima;
  - tamanho da posição = saldo (caixa + custo das posições abertas) / N, limitado ao caixa
    e, se `risk` for dado, a saldo * risk / distância até o stop inicial;
  - taxa de 0,1% na entrada e na saída.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from lab import FEE, Trade


class Market:
    """Matrizes OHLCV alinhadas (tempo x par), com NaN onde o par ainda não existia."""

    def __init__(self, data: dict[str, pd.DataFrame]):
        self.pairs = [p for p in data if not p.startswith("_")]
        idx = pd.DatetimeIndex(sorted(set().union(*[data[p].index for p in self.pairs])))
        self.index = idx
        self.frames = {p: data[p].reindex(idx) for p in self.pairs}
        stack = lambda col: np.column_stack([self.frames[p][col].to_numpy(float) for p in self.pairs])
        self.o, self.h, self.l, self.c, self.v = (stack(k) for k in ("open", "high", "low", "close", "volume"))
        self.raw = data

    def align(self, series_by_pair: dict[str, pd.Series], fill=np.nan, dtype=float) -> np.ndarray:
        cols = []
        for p in self.pairs:
            s = series_by_pair.get(p)
            if s is None:
                cols.append(np.full(len(self.index), fill, dtype=dtype))
            else:
                cols.append(s.reindex(self.index).fillna(fill).to_numpy(dtype))
        return np.column_stack(cols)


def run(mkt: Market, signals: dict, start, end, slots: int = 6, risk: float | None = None,
        priority: np.ndarray | None = None, max_hold: int | None = None):
    """signals: dict com matrizes (tempo x par): enter(bool), stop, target, exit(bool), trail."""
    T, P = mkt.c.shape
    s = mkt.index.searchsorted(pd.Timestamp(start, tz="UTC"))
    e = mkt.index.searchsorted(pd.Timestamp(end, tz="UTC"))
    o, h, l, c = mkt.o, mkt.h, mkt.l, mkt.c
    enter = signals["enter"]
    stop0 = signals.get("stop")
    tgt0 = signals.get("target")
    exs = signals.get("exit")
    trail = signals.get("trail")

    cash = 1.0
    # posições: dict par -> [qty, entry, stop, target, bars, pending, cost, last, t0]
    pos: dict[int, list] = {}
    trades = []
    curve = np.empty(e - s)
    for k, t in enumerate(range(s, e)):
        # ---- saídas
        for j in list(pos):
            ps = pos[j]
            ot, ht, lt = o[t, j], h[t, j], l[t, j]
            if np.isnan(ot):
                continue
            px, why = None, ""
            if ps[5]:
                px, why = ot, ps[5]
            elif ps[2] == ps[2] and lt <= ps[2]:
                px, why = (ot if ot < ps[2] else ps[2]), "stop"
            elif ps[3] == ps[3] and ht >= ps[3]:
                px, why = (ot if ot > ps[3] else ps[3]), "alvo"
            if px is not None:
                cash += ps[0] * px * (1 - FEE)
                trades.append((j, ps[8], t, ps[1], px, why))
                del pos[j]
                continue
            ps[4] += 1
            if exs is not None and exs[t, j]:
                ps[5] = "sinal"
            elif max_hold and ps[4] >= max_hold:
                ps[5] = "tempo"
            if trail is not None:
                tr = trail[t, j]
                if tr == tr and (ps[2] != ps[2] or tr > ps[2]):
                    ps[2] = tr
        # ---- entradas
        if t > s and len(pos) < slots:
            cand = np.nonzero(enter[t - 1])[0]
            if len(cand):
                if priority is not None:
                    pr = priority[t - 1, cand]
                    cand = cand[np.argsort(np.where(np.isnan(pr), np.inf, pr), kind="stable")]
                equity = cash + sum(ps[6] for ps in pos.values())
                for j in cand:
                    if len(pos) >= slots:
                        break
                    if j in pos or np.isnan(o[t, j]):
                        continue
                    entry = o[t, j]
                    stake = min(equity / slots, cash)
                    st = stop0[t - 1, j] if stop0 is not None else np.nan
                    if risk and st == st and st < entry:
                        stake = min(stake, equity * risk / ((entry - st) / entry))
                    if stake <= equity * 0.005:
                        continue
                    qty = stake / (entry * (1 + FEE))
                    cash -= stake
                    tg = tgt0[t - 1, j] if tgt0 is not None else np.nan
                    ps = [qty, entry, st if (st == st and st < entry) else np.nan,
                          tg if (tg == tg and tg > entry) else np.nan, 1, None, stake, entry, t]
                    # a própria vela de entrada pode bater stop / alvo
                    if ps[2] == ps[2] and l[t, j] <= ps[2]:
                        cash += qty * ps[2] * (1 - FEE)
                        trades.append((j, t, t, entry, ps[2], "stop"))
                        continue
                    if ps[3] == ps[3] and h[t, j] >= ps[3]:
                        cash += qty * ps[3] * (1 - FEE)
                        trades.append((j, t, t, entry, ps[3], "alvo"))
                        continue
                    if exs is not None and exs[t, j]:
                        ps[5] = "sinal"
                    if trail is not None:
                        tr = trail[t, j]
                        if tr == tr and (ps[2] != ps[2] or tr > ps[2]):
                            ps[2] = tr
                    pos[j] = ps
        # ---- marcação a mercado
        val = cash
        for j, ps in pos.items():
            ct = c[t, j]
            if ct == ct:
                ps[7] = ct
            val += ps[0] * ps[7] * (1 - FEE)
        curve[k] = val
    for j, ps in pos.items():
        trades.append((j, ps[8], e - 1, ps[1], ps[7], "fim"))
    eq = pd.Series(curve, index=mkt.index[s:e])
    tr_objs = [Trade(mkt.pairs[j], mkt.index[a], mkt.index[b], en, ex, why) for j, a, b, en, ex, why in trades]
    return eq, tr_objs


def metrics(eq: pd.Series, trades) -> dict:
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1
    dd = (eq / eq.cummax() - 1).min()
    r = np.array([t.ret for t in trades])
    w, lo = r[r > 0].sum(), -r[r < 0].sum()
    hold = np.array([(t.exit_time - t.entry_time).total_seconds() / 3600 for t in trades]) if trades else np.array([np.nan])
    return {"trades": len(r), "total": eq.iloc[-1] - 1, "cagr": cagr, "max_dd": dd,
            "pf": w / lo if lo > 0 else np.inf, "win": (r > 0).mean() if len(r) else np.nan,
            "avg": r.mean() if len(r) else np.nan, "calmar": cagr / abs(dd) if dd < 0 else np.inf,
            "hold_h": float(np.nanmedian(hold)), "bh": np.nan}


def passes(m: dict) -> bool:
    return m["cagr"] >= 0.15 and m["max_dd"] >= -0.25 and m["pf"] >= 1.3 and m["trades"] >= 40


def fmt(m: dict) -> str:
    return (f"n {m['trades']:4d} | anual {m['cagr']*100:6.1f}% | DD {m['max_dd']*100:6.1f}% | "
            f"PF {m['pf']:5.2f} | acerto {m['win']*100:4.1f}% | duração mediana {m['hold_h']:5.0f}h")
