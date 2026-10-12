"""
Baixa o histórico de velas spot do repositório público da Binance (data.binance.vision)
e salva um parquet por timeframe em research_data/.

Uso: python tools/fetch_binance_vision.py <saida> <tf1,tf2> <SYMBOL1,SYMBOL2,...> <AAAA-MM inicial>
Ex.: python tools/fetch_binance_vision.py research_data 4h,1d BTCUSDT,ETHUSDT 2019-01

Usado pelo workflow .github/workflows/research-data.yml.
"""

from __future__ import annotations

import io
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/spot"
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]


def month_range(start: str, end: date) -> list[str]:
    y, m = map(int, start.split("-"))
    out = []
    while (y, m) < (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def fetch_zip(url: str) -> pd.DataFrame | None:
    for _ in range(3):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                with z.open(z.namelist()[0]) as f:
                    df = pd.read_csv(f, header=None, names=COLS)
            # alguns arquivos têm cabeçalho
            if not str(df.iloc[0, 0]).isdigit():
                df = df.iloc[1:]
            return df
        except Exception as exc:  # noqa: BLE001
            last = exc
    print(f"falhou: {url}: {last}", flush=True)
    return None


def to_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_numeric(df["open_time"])
    # a partir de 2025 os arquivos spot usam microssegundos
    unit_us = t > 10**14
    ts = pd.to_datetime(t.where(~unit_us, t // 1000), unit="ms", utc=True)
    out = pd.DataFrame({"date": ts})
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = pd.to_numeric(df[c]).astype("float64").to_numpy()
    return out


def fetch_symbol(symbol: str, tf: str, start: str) -> pd.DataFrame:
    today = date.today()
    urls = [f"{BASE}/monthly/klines/{symbol}/{tf}/{symbol}-{tf}-{m}.zip" for m in month_range(start, today)]
    d = date(today.year, today.month, 1)
    while d < today:
        urls.append(f"{BASE}/daily/klines/{symbol}/{tf}/{symbol}-{tf}-{d.isoformat()}.zip")
        d += timedelta(days=1)
    with ThreadPoolExecutor(8) as ex:
        parts = [p for p in ex.map(fetch_zip, urls) if p is not None and len(p)]
    if not parts:
        return pd.DataFrame()
    df = to_ohlcv(pd.concat(parts, ignore_index=True))
    return df.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def main() -> None:
    out_dir = Path(sys.argv[1])
    tfs = sys.argv[2].split(",")
    symbols = sys.argv[3].split(",")
    start = sys.argv[4]
    out_dir.mkdir(parents=True, exist_ok=True)
    for tf in tfs:
        frames = []
        for sym in symbols:
            df = fetch_symbol(sym, tf, start)
            if df.empty:
                print(f"{sym} {tf}: sem dados", flush=True)
                continue
            df.insert(0, "pair", sym.replace("USDT", "/USDT"))
            frames.append(df)
            print(f"{sym} {tf}: {len(df)} velas, {df['date'].min()} -> {df['date'].max()}", flush=True)
        data = pd.concat(frames, ignore_index=True)
        data.to_parquet(out_dir / f"binance_{tf}.parquet", compression="zstd", index=False)


if __name__ == "__main__":
    main()
