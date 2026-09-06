"""Turn the inventory_raw/*.json.gz cache into two parquet tables.

  ohlc.parquet          date, ticker, open, high, low, close, volume
  broker_daily.parquet  date, ticker, broker, nlot, nval, blot, bval, slot, sval

Rows where a broker did nothing that day are dropped, so broker_daily is sparse.
Streamed through pyarrow so the whole universe never has to sit in memory.

This is the INGEST layer: a faithful, lossless transcription of the immutable
vendor cache. It performs no economic normalisation -- no volume repair, no
basis harmonisation. Those live in normalize_market_data.py, which reads the
same raw cache and emits provenance artifacts alongside it.

Lot columns are int64. The vendor reports lots as exact integers (measured:
63,679,923 values, zero non-integral, zero null, max 80,883,692), so int64 is
the faithful representation. The previous float32 was lossy well below its
2**24 exact-integer ceiling, because downstream `blot * 100.0` on a float32
column evaluates in float32 and loses precision from 671,089 lots upward --
0.30% of in-universe rows, not the 105 values that exceed 2**24. Integrality
is asserted before the cast so a future cache that violates it fails loudly
rather than being silently truncated.
"""
import argparse
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

LOT_FIELDS = ("nlot", "blot", "slot")

OHLC_SCHEMA = pa.schema([
    ("date", pa.string()), ("ticker", pa.string()),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
    ("close", pa.float64()), ("volume", pa.float64()),
])
BRK_SCHEMA = pa.schema([
    ("date", pa.string()), ("ticker", pa.string()), ("broker", pa.string()),
    ("nlot", pa.int64()), ("nval", pa.float64()),
    ("blot", pa.int64()), ("bval", pa.float64()),
    ("slot", pa.int64()), ("sval", pa.float64()),
])


def exact_lots(values, ticker, field):
    """Cast a float64 lot vector to int64, refusing anything not exactly integral.

    The vendor's lot fields are a counting quantity. A non-integral or non-finite
    value means the source contract changed, and silently rounding it would hide
    exactly the class of defect this rebuild exists to remove.
    """
    if not np.isfinite(values).all():
        bad = int((~np.isfinite(values)).sum())
        raise ValueError(f"{ticker}: {bad} non-finite {field} values; "
                         "lots must be finite integers")
    rounded = np.rint(values)
    if not np.array_equal(values, rounded):
        bad = int((values != rounded).sum())
        worst = float(np.abs(values - rounded).max())
        raise ValueError(f"{ticker}: {bad} non-integral {field} values "
                         f"(max fractional part {worst:g}); lots must be integers")
    return rounded.astype(np.int64)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=HERE,
                    help="directory to write the parquets into (default: repo root). "
                         "Point this at a scratch directory to build a candidate "
                         "rebuild without touching the current artifacts.")
    a = ap.parse_args()
    out_dir = os.path.abspath(a.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    files = sorted(glob.glob(os.path.join(RAW, "*.json.gz")))
    print(f"{len(files)} cached tickers -> {out_dir}")
    w_o = pq.ParquetWriter(os.path.join(out_dir, "ohlc.parquet"), OHLC_SCHEMA,
                           compression="zstd")
    w_b = pq.ParquetWriter(os.path.join(out_dir, "broker_daily.parquet"), BRK_SCHEMA,
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
                    kept = {k: cols[k][mask] for k in fields}
                    for k in LOT_FIELDS:
                        kept[k] = exact_lots(kept[k], t, k)
                    df = pd.DataFrame({
                        "date": np.tile(np.asarray(dates, dtype=object), nb)[mask],
                        "ticker": t,
                        "broker": np.repeat(np.asarray(brokers, dtype=object), nd)[mask],
                        **kept,
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
