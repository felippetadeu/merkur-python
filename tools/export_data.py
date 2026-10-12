"""
Exporta o histórico baixado pelo Freqtrade para o log, em blocos base64.

Uso (dentro do container de pesquisa):
    python tools/export_data.py <pasta de dados> <timeframe> <par1> <par2> ...

Saída: linhas "DATA|<seq>|<total>|<bloco>" e uma linha final "DATA_SHA256|<hash>",
que permitem remontar e conferir o arquivo fora do container.
"""

import base64
import hashlib
import io
import sys
import time
from pathlib import Path

import pandas as pd

CHUNK = 6000


def main() -> None:
    datadir = Path(sys.argv[1])
    timeframe = sys.argv[2]
    pairs = sys.argv[3:]

    frames = []
    for pair in pairs:
        fname = datadir / f"{pair.replace('/', '_')}-{timeframe}.feather"
        if not fname.exists():
            print(f"DATA_MISSING|{pair}", flush=True)
            continue
        df = pd.read_feather(fname)
        df["pair"] = pair
        frames.append(df[["pair", "date", "open", "high", "low", "close", "volume"]])
    data = pd.concat(frames, ignore_index=True)
    # float32 + byte_stream_split + zstd forte: reduz bastante o tamanho do texto no log
    for col in ("open", "high", "low", "close", "volume"):
        data[col] = data[col].astype("float32")
    data["pair"] = data["pair"].astype("category")

    buf = io.BytesIO()
    data.to_parquet(
        buf,
        engine="pyarrow",
        compression="zstd",
        compression_level=19,
        use_byte_stream_split=["open", "high", "low", "close", "volume"],
        use_dictionary=["pair"],
        index=False,
    )
    raw = buf.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    b64 = base64.b64encode(raw).decode()
    chunks = [b64[i : i + CHUNK] for i in range(0, len(b64), CHUNK)]

    print(f"DATA_INFO|rows={len(data)}|bytes={len(raw)}|chunks={len(chunks)}", flush=True)
    for i, chunk in enumerate(chunks):
        print(f"DATA|{i}|{len(chunks)}|{chunk}", flush=True)
        time.sleep(0.02)  # evita estourar o limite de linhas por segundo do log
    print(f"DATA_SHA256|{digest}", flush=True)


if __name__ == "__main__":
    main()
