"""Turn the inventory_raw/*.json.gz cache into two parquet tables.

  ohlc.parquet          date, ticker, open, high, low, close, volume
  broker_daily.parquet  date, ticker, broker, nlot, nval, blot, bval, slot, sval

Rows where a broker did nothing that day are dropped, so broker_daily is sparse.
Streamed through pyarrow so the whole universe never has to sit in memory.
"""
import glob
import gzip
import json
import os

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "inventory_raw")

OHLC_SCHEMA = pa.schema([
    ("date", pa.string()), ("ticker", pa.string()),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
    ("close", pa.float64()), ("volume", pa.float64()),
])
BRK_SCHEMA = pa.schema([
    ("date", pa.string()), ("ticker", pa.string()), ("broker", pa.string()),
    ("nlot", pa.float32()), ("nval", pa.float64()),
    ("blot", pa.float32()), ("bval", pa.float64()),
    ("slot", pa.float32()), ("sval", pa.float64()),
])


def main():
    files = sorted(glob.glob(os.path.join(RAW, "*.json.gz")))
    print(f"{len(files)} cached tickers")
    w_o = pq.ParquetWriter(os.path.join(HERE, "ohlc.parquet"), OHLC_SCHEMA,
                           compression="zstd")
    w_b = pq.ParquetWriter(os.path.join(HERE, "broker_daily.parquet"), BRK_SCHEMA,
                           compression="zstd")
    n_o = n_b = 0
    empty = []
    for i, f in enumerate(files, 1):
        t = os.path.basename(f)[:-8]
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as e:
            print(f"  {t}: unreadable ({e})")
            continue

        oh = d.get("ohlc") or []
        if oh:
            o = pd.DataFrame(oh)
            keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in o]
            o = o[keep].copy()
            o["ticker"] = t
            for c in ["open", "high", "low", "close", "volume"]:
                o[c] = pd.to_numeric(o.get(c), errors="coerce")
            o = o[["date", "ticker", "open", "high", "low", "close", "volume"]]
            w_o.write_table(pa.Table.from_pandas(o, schema=OHLC_SCHEMA,
                                                 preserve_index=False))
            n_o += len(o)

        dates = d.get("date") or []
        if dates:
            fields = ["nlot", "nval", "blot", "bval", "slot", "sval"]
            present = [k for k in fields if isinstance(d.get(k), dict)]
            brokers = sorted({b for k in present for b in d[k]})
            if brokers:
                nd, nb = len(dates), len(brokers)
                cols = {}
                for k in fields:
                    src = d.get(k) or {}
                    m = np.zeros((nb, nd), dtype=np.float64)
                    for bi, b in enumerate(brokers):
                        v = src.get(b)
                        if v:
                            a = np.asarray(v, dtype=np.float64)
                            m[bi, :len(a)] = a[:nd]
                    cols[k] = m.ravel()
                mask = np.zeros(nb * nd, dtype=bool)
                for k in fields:
                    mask |= cols[k] != 0
                if mask.any():
                    df = pd.DataFrame({
                        "date": np.tile(np.asarray(dates, dtype=object), nb)[mask],
                        "ticker": t,
                        "broker": np.repeat(np.asarray(brokers, dtype=object), nd)[mask],
                        **{k: cols[k][mask] for k in fields},
                    })[["date", "ticker", "broker", "nlot", "nval",
                        "blot", "bval", "slot", "sval"]]
                    w_b.write_table(pa.Table.from_pandas(df, schema=BRK_SCHEMA,
                                                        preserve_index=False))
                    n_b += len(df)
                else:
                    empty.append(t)
            else:
                empty.append(t)
        if i % 100 == 0:
            print(f"  [{i}/{len(files)}] ohlc={n_o:,} broker={n_b:,}")
    w_o.close()
    w_b.close()
    print(f"\nohlc.parquet         {n_o:,} rows")
    print(f"broker_daily.parquet {n_b:,} rows")
    if empty:
        print(f"{len(empty)} tickers had no broker activity at all: {empty[:20]}")


if __name__ == "__main__":
    main()
