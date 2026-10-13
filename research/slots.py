"""
Simulador de carteira por VAGAS (como o Freqtrade com max_open_trades = N e stake "unlimited").

- Cada dia: primeiro processa as saídas das posições abertas (stop, alvo, sinal, tempo,
  com as mesmas regras de lab.simulate_pair); depois abre novas posições na ABERTURA
  para os sinais da vela anterior, enquanto houver vagas.
- Cada nova posição recebe (patrimônio marcado a mercado na abertura) / N, limitado ao caixa.
- Sinais simultâneos são priorizados por `priority` (menor = primeiro); padrão: ordem do dicionário.
- Taxa de 0,1% na entrada e na saída.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from lab import FEE, Trade


def run_slots(data: dict[str, pd.DataFrame], strat, start, end, slots: int = 6,
              priority: pd.DataFrame | None = None, risk: float | None = None, **params):
    """risk: fração do saldo arriscada até o stop inicial (ex.: 0.02 = 2%).
    O tamanho fica limitado a saldo/slots. None = sempre saldo/slots."""
    pairs = [p for p in data if not p.startswith("_")]
    s, e = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    idx = sorted(set().union(*[data[p].index for p in pairs]))
    idx = pd.DatetimeIndex([t for t in idx if s <= t < e])

    sig, arr = {}, {}
    for p in pairs:
        df = data[p]
        sg = strat(df, pair=p, data=data, **params)
        sig[p] = sg
        ix = df.index
        def ser(x, fill):
            return (x.reindex(ix) if x is not None else pd.Series(fill, ix))
        arr[p] = dict(
            o=df["open"], h=df["high"], l=df["low"], c=df["close"],
            enter=ser(sg.enter, False).fillna(False).astype(bool),
            stop=ser(sg.stop, np.nan), target=ser(sg.target, np.nan),
            exit=ser(sg.exit, False).fillna(False).astype(bool),
            trail=ser(sg.trail, np.nan),
            tag=ser(sg.tag, "").fillna(""),
            max_hold=sg.max_hold,
        )

    cash = 1.0
    pos: dict[str, dict] = {}
    trades: list[Trade] = []
    curve = []
    prev_t = None
    for t in idx:
        # ---- saídas das posições abertas nesta vela
        for p in list(pos):
            a, ps = arr[p], pos[p]
            if t not in a["o"].index:
                continue
            o, h, l, c = a["o"][t], a["h"][t], a["l"][t], a["c"][t]
            exit_px, reason = None, ""
            if ps["pending_exit"]:
                exit_px, reason = o, ps["pending_exit"]
            elif np.isfinite(ps["stop"]) and l <= ps["stop"]:
                exit_px, reason = (o if o < ps["stop"] else ps["stop"]), "stop"
            elif np.isfinite(ps["target"]) and h >= ps["target"]:
                exit_px, reason = (o if o > ps["target"] else ps["target"]), "alvo"
            if exit_px is not None:
                cash += ps["qty"] * exit_px * (1 - FEE)
                trades.append(Trade(p, ps["t0"], t, ps["entry"], exit_px, reason, ps["tag"]))
                del pos[p]
                continue
            ps["bars"] += 1
            if a["exit"][t]:
                ps["pending_exit"] = "sinal"
            elif a["max_hold"] and ps["bars"] >= a["max_hold"]:
                ps["pending_exit"] = "tempo"
            tr = a["trail"][t]
            if np.isfinite(tr):
                ps["stop"] = tr if not np.isfinite(ps["stop"]) else max(ps["stop"], tr)

        # ---- entradas na abertura, para sinais da vela anterior
        if prev_t is not None and len(pos) < slots:
            cands = [p for p in pairs if p not in pos and prev_t in arr[p]["enter"].index
                     and arr[p]["enter"][prev_t] and t in arr[p]["o"].index]
            if priority is not None and cands:
                pr = priority.reindex([prev_t]).iloc[0] if prev_t in priority.index else None
                if pr is not None:
                    cands.sort(key=lambda p: pr.get(p, np.inf) if pd.notna(pr.get(p, np.nan)) else np.inf)
            # saldo como o Freqtrade calcula ("unlimited"): caixa + custo das posições abertas
            # (lucro não realizado não aumenta o tamanho das novas posições)
            equity = cash + sum(ps["cost"] for ps in pos.values())
            for p in cands:
                if len(pos) >= slots:
                    break
                a = arr[p]
                entry = a["o"][t]
                stake = min(equity / slots, cash)
                st_chk = a["stop"][prev_t]
                if risk and np.isfinite(st_chk) and st_chk < entry:
                    dist = (entry - st_chk) / entry
                    stake = min(stake, equity * risk / dist)
                if stake <= equity * 0.01:
                    break
                qty = stake / (entry * (1 + FEE))
                cash -= stake
                st0 = a["stop"][prev_t]
                tg0 = a["target"][prev_t]
                pos[p] = dict(qty=qty, entry=entry, t0=t, bars=0, pending_exit=None, last=entry, cost=stake,
                              stop=st0 if np.isfinite(st0) and st0 < entry else np.nan,
                              target=tg0 if np.isfinite(tg0) and tg0 > entry else np.nan,
                              tag=a["tag"][prev_t])
                # a vela de entrada também pode bater stop/alvo
                h, l = a["h"][t], a["l"][t]
                ps = pos[p]
                if np.isfinite(ps["stop"]) and l <= ps["stop"]:
                    cash += qty * ps["stop"] * (1 - FEE)
                    trades.append(Trade(p, t, t, entry, ps["stop"], "stop", ps["tag"]))
                    del pos[p]
                    continue
                if np.isfinite(ps["target"]) and h >= ps["target"]:
                    cash += qty * ps["target"] * (1 - FEE)
                    trades.append(Trade(p, t, t, entry, ps["target"], "alvo", ps["tag"]))
                    del pos[p]
                    continue
                ps["bars"] = 1
                if a["exit"][t]:
                    ps["pending_exit"] = "sinal"
                tr = a["trail"][t]
                if np.isfinite(tr):
                    ps["stop"] = tr if not np.isfinite(ps["stop"]) else max(ps["stop"], tr)

        # ---- marcação a mercado no fechamento
        for p, ps in pos.items():
            if t in arr[p]["c"].index:
                ps["last"] = arr[p]["c"][t]
        curve.append(cash + sum(ps["qty"] * ps["last"] * (1 - FEE) for ps in pos.values()))
        prev_t = t

    # fecha o que sobrou no fim
    for p, ps in pos.items():
        trades.append(Trade(p, ps["t0"], idx[-1], ps["entry"], ps["last"], "fim", ps["tag"]))
    eq = pd.Series(curve, index=idx)
    return eq, trades


def slot_metrics(eq: pd.Series, trades) -> dict:
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1
    dd = (eq / eq.cummax() - 1).min()
    r = np.array([t.ret for t in trades])
    w, lo = r[r > 0].sum(), -r[r < 0].sum()
    return {"trades": len(r), "total": eq.iloc[-1] - 1, "cagr": cagr, "max_dd": dd,
            "pf": w / lo if lo > 0 else np.inf, "win": (r > 0).mean() if len(r) else np.nan,
            "avg": r.mean() if len(r) else np.nan, "calmar": cagr / abs(dd) if dd < 0 else np.inf,
            "bh": np.nan}
