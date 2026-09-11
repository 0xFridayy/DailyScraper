"""Turn the inventory_raw/*.json.gz cache into two parquet tables.

  ohlc.parquet          date, ticker, open, high, low, close, volume
  broker_daily.parquet  date, ticker, broker, nlot, nval, blot, bval, slot, sval

Rows where a broker did nothing that day are dropped, so broker_daily is sparse.
Streamed through pyarrow so the whole universe never has to sit in memory.

This is the INGEST layer: a faithful, lossless transcription of the immutable
vendor cache. It performs no economic normalisation -- no volume repair, no
basis harmonisation. Those live in normalize_market_data.py, which reads the
same raw cache and emits provenance artifacts alongside it.

Lot columns are int64. The vendor reports lots as exact integers, so int64 is
the faithful representation. The previous float32 was lossy well below its
2**24 exact-integer ceiling, because downstream `blot * 100.0` on a float32
column evaluates in float32; the product is a multiple of 4 and so stays exact
only to 2**26, i.e. from 671,089 lots upward. Integrality is asserted before
the cast so a future cache that violates it fails loudly rather than being
silently truncated.

Measured on the frozen Experiment #1F population -- the 297 covered approved
tickers, 2,689,458 broker rows, 8,068,374 lot values. Scope matters: these are
IN-UNIVERSE figures, and the market-wide table is roughly twice the size, so
the two must not be quoted interchangeably.

    non-integral or null lot values      0
    max |lot|                            80,883,692
    individual lot values > 2**24        108
    rows with blot     >= 671,089        12,440   (0.46% of rows)
    rows with any lot  >= 671,089        15,179   (0.56% of rows)

The corresponding market-wide figures over all 5,609,444 rows are 132 rows
carrying a lot above 2**24, 18,559 rows (0.33%) with blot >= 671,089 and 22,967
rows (0.41%) with any lot >= 671,089. Row counts and individual-value counts are
different quantities and are reported separately above for that reason.
"""
import argparse
import datetime
import glob
import gzip
import json
import math
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


# --------------------------------------------------------------------------
# strict candidate ingest (Experiment #1F Phase 2)
# --------------------------------------------------------------------------
#
# The default path above is the Data Factory's, and its behaviour is unchanged:
# it globs the whole shared cache, tolerates an unreadable file by skipping it,
# and pads or truncates a broker series to the date axis. Those are reasonable
# for a market-wide operational rebuild that must not die on one bad ticker.
#
# They are NOT acceptable for #1F, where a silently zero-filled series would
# manufacture "the broker did nothing" days that the source never asserted, and
# a skipped file would silently shrink the approved population. The strict path
# below therefore fails closed on every one of those, and refuses to write
# anywhere a production artifact could be confused for its output.
#
# Measured on the frozen 297: 179,982 broker series, 100% exactly the length of
# their date axis, and zero raw lot values that are bool, string, null, non-finite
# or non-integral. Strict mode costs nothing on this population and removes the
# silent-corruption path entirely.

PRODUCTION_ARTIFACTS = ("ohlc.parquet", "broker_daily.parquet")
CANDIDATE_BROKER_NAME = "broker_daily_v2.parquet"

VALUE_FIELDS = ("nval", "bval", "sval")
ALL_FIELDS = ("nlot", "nval", "blot", "bval", "slot", "sval")

#: Rupiah value conservation tolerance. Values arrive as float64 rupiah totals
#: whose magnitude reaches ~1e13; a float64 carries ~15-16 significant decimal
#: digits, so one ulp at 1e13 is ~2e-3. Summing thousands of them accumulates a
#: few ulp. Half a rupiah is far above that noise floor and far below one whole
#: rupiah, the smallest economically meaningful unit -- the same bound the
#: merged invariant checks already use.
RUPIAH_TOLERANCE = 0.5


class StrictSourceError(ValueError):
    """A source value outside the contract the strict candidate path requires."""


def strict_lot(value, ticker, broker, field, index):
    """A lot is a counting quantity: an exact, finite, non-negative-domain integer.

    Validates the RAW JSON object before any float coercion, because
    np.asarray(..., dtype=float64) happily turns True into 1.0 and "5" into 5.0
    and so destroys exactly the type evidence this check exists to read.
    """
    where = f"{ticker}/{broker}/{field}[{index}]"
    if isinstance(value, bool):
        raise StrictSourceError(f"{where}: bool is not a lot ({value!r})")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StrictSourceError(f"{where}: non-finite lot ({value!r})")
        if not value.is_integer():
            raise StrictSourceError(f"{where}: non-integral lot ({value!r})")
        return int(value)
    raise StrictSourceError(f"{where}: {type(value).__name__} is not a lot ({value!r})")


def strict_rupiah(value, ticker, broker, field, index):
    """A rupiah total is finite and numeric. Never coerced, never defaulted."""
    where = f"{ticker}/{broker}/{field}[{index}]"
    if isinstance(value, bool):
        raise StrictSourceError(f"{where}: bool is not a value ({value!r})")
    if isinstance(value, (int, float)):
        out = float(value)
        if not math.isfinite(out):
            raise StrictSourceError(f"{where}: non-finite value ({value!r})")
        return out
    raise StrictSourceError(f"{where}: {type(value).__name__} is not a value ({value!r})")


def strict_dates(dates, ticker):
    """Real, unique, ascending ISO calendar dates."""
    if not dates:
        raise StrictSourceError(f"{ticker}: empty date axis")
    seen = set()
    for d in dates:
        if not isinstance(d, str):
            raise StrictSourceError(f"{ticker}: non-string date {d!r}")
        try:
            datetime.date.fromisoformat(d)
        except ValueError:
            raise StrictSourceError(f"{ticker}: not a real calendar date {d!r}")
        if d in seen:
            raise StrictSourceError(f"{ticker}: duplicate date {d!r}")
        seen.add(d)
    return list(dates)


def strict_ohlc_domain(data, ticker):
    """Fail-closed OHLC source-domain check.

    The frozen ohlc.parquet is already proven to reproduce this population
    exactly, so the candidate build emits no ohlc_v2 -- duplicating a verified
    artifact would add a second identity to reconcile and no information. The
    price domain is still validated here, without errors="coerce", so a
    malformed price fails the candidate build rather than becoming a silent NaN.
    """
    checked = 0
    for row in (data.get("ohlc") or []):
        date = row.get("date")
        if not isinstance(date, str):
            raise StrictSourceError(f"{ticker}: non-string ohlc date {date!r}")
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            raise StrictSourceError(f"{ticker}: not a real ohlc date {date!r}")
        for key in ("open", "high", "low", "close", "volume"):
            value = row.get(key)
            if value is None:
                continue                      # absent is absent, not malformed
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StrictSourceError(
                    f"{ticker} {date}: {key} is {type(value).__name__} ({value!r})")
            if not math.isfinite(float(value)):
                raise StrictSourceError(f"{ticker} {date}: non-finite {key}")
            checked += 1
    return checked


#: One sentence, shared by every caller, so the two strict readers cannot end up
#: explaining the same refusal differently.
ZERO_FILL_REFUSAL = (
    "Zero-filling the missing field would turn an absent measurement into an "
    "asserted zero, and an all-zero mask cannot drop the row because the field "
    "the broker IS present in is non-zero. The strict path refuses rather than "
    "inventing the value.")


def partially_present_brokers(data, fields=ALL_FIELDS):
    """Brokers present in SOME required broker fields and absent from others.

    THE SINGLE DEFINITION OF "strict raw source" broker-field completeness.
    Both strict readers call it -- strict_ticker_frame() here, and
    experiment_1f_normalization.strict_totals() -- because two subtly different
    validators would mean two different answers to "is this source acceptable",
    and the ingest path and the normalisation path must not disagree about that.

    Both readers initialise their accumulators with zeros, so skipping a missing
    series silently converts MISSING into ASSERTED ZERO. A broker absent from
    EVERY field is a different case: it contributes nothing anywhere, the
    all-zero mask drops it, and nothing is invented -- so it is not reported.

    Returns a list of human-readable descriptions; each caller raises its own
    error type, which keeps this helper free of any dependency on either.
    """
    maps = {f: (data.get(f) if isinstance(data.get(f), dict) else {})
            for f in fields}
    union = sorted({b for m in maps.values() for b in m})
    partial = []
    for broker in union:
        absent = [f for f in fields if maps[f].get(broker) is None]
        if absent and len(absent) != len(fields):
            partial.append(f"{broker} missing {absent}")
    return partial


def strict_ticker_frame(data, ticker):
    """One ticker's broker rows, or a hard failure. No padding, no truncation."""
    dates = strict_dates(data.get("date") or [], ticker)
    n = len(dates)
    strict_ohlc_domain(data, ticker)

    present = {f: data.get(f) for f in ALL_FIELDS}
    for field, src in present.items():
        if src is not None and not isinstance(src, dict):
            raise StrictSourceError(f"{ticker}: {field} is {type(src).__name__}, not a mapping")
    brokers = sorted({b for src in present.values() if isinstance(src, dict) for b in src})
    if not brokers:
        return None

    partial = partially_present_brokers(data)
    if partial:
        raise StrictSourceError(
            f"{ticker}: {len(partial)} broker(s) appear in some required fields "
            f"but not all: {partial[:5]}. {ZERO_FILL_REFUSAL}")

    cols = {}
    for field in ALL_FIELDS:
        src = present.get(field) or {}
        matrix = np.zeros((len(brokers), n), dtype=np.int64
                          if field in LOT_FIELDS else np.float64)
        for bi, broker in enumerate(brokers):
            series = src.get(broker)
            if series is None:
                # Reachable only for a broker absent from EVERY field, which the
                # guard above has already established. The all-zero mask below
                # then drops the row, so nothing is invented.
                continue
            if not isinstance(series, list):
                raise StrictSourceError(
                    f"{ticker}/{broker}/{field}: {type(series).__name__}, not a list")
            if len(series) != n:
                raise StrictSourceError(
                    f"{ticker}/{broker}/{field}: series length {len(series)} != "
                    f"{n} sessions; padding or truncating would invent data the "
                    "source never asserted")
            coerce = strict_lot if field in LOT_FIELDS else strict_rupiah
            for i, value in enumerate(series):
                matrix[bi, i] = coerce(value, ticker, broker, field, i)
        cols[field] = matrix.ravel()

    mask = np.zeros(len(brokers) * n, dtype=bool)
    for field in ALL_FIELDS:
        mask |= cols[field] != 0
    if not mask.any():
        return None

    kept = {f: cols[f][mask] for f in ALL_FIELDS}
    frame = pd.DataFrame({
        "date": np.tile(np.asarray(dates, dtype=object), len(brokers))[mask],
        "ticker": ticker,
        "broker": np.repeat(np.asarray(brokers, dtype=object), n)[mask],
        **kept,
    })[["date", "ticker", "broker", "nlot", "nval", "blot", "bval", "slot", "sval"]]

    bad = frame["nlot"].to_numpy() != (frame["blot"].to_numpy() - frame["slot"].to_numpy())
    if bad.any():
        raise StrictSourceError(f"{ticker}: {int(bad.sum())} rows violate nlot == blot - slot")
    drift = np.abs(frame["nval"].to_numpy()
                   - (frame["bval"].to_numpy() - frame["sval"].to_numpy()))
    if (drift > RUPIAH_TOLERANCE).any():
        raise StrictSourceError(
            f"{ticker}: {int((drift > RUPIAH_TOLERANCE).sum())} rows violate "
            f"nval == bval - sval beyond {RUPIAH_TOLERANCE} rupiah "
            f"(worst {drift.max():g})")
    return frame


def guard_candidate_out_dir(out_dir):
    """Refuse to write a candidate build anywhere production could be meant."""
    out_dir = os.path.abspath(out_dir)
    if out_dir == HERE:
        raise SystemExit("strict candidate ingest refuses to write to the repo root; "
                         "pass an isolated --out-dir")
    for name in PRODUCTION_ARTIFACTS:
        if os.path.exists(os.path.join(out_dir, name)):
            raise SystemExit(f"{out_dir} already holds {name}; strict candidate "
                             "ingest refuses to write beside a production artifact")
    return out_dir


def build_candidate_broker(source_dir, tickers, out_dir):
    """Lossless broker table for exactly `tickers`, read from an owned snapshot.

    Offline, source-faithful: int64 lots, no factor scaling, no volume repair,
    no economic normalisation of any kind. Rows where a broker did nothing that
    day carry no information and are dropped, the same sparsity rule the frozen
    artifact uses, so the two remain directly comparable.
    """
    out_dir = guard_candidate_out_dir(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    final = os.path.join(out_dir, CANDIDATE_BROKER_NAME)
    tmp = final + ".tmp"

    stats = {"tickers": 0, "tickers_with_rows": 0, "rows": 0,
             "ohlc_values_checked": 0, "empty": []}
    writer = pq.ParquetWriter(tmp, BRK_SCHEMA, compression="zstd")
    try:
        for ticker in sorted(tickers):
            path = os.path.join(source_dir, ticker + ".json.gz")
            if not os.path.isfile(path):
                raise SystemExit(f"{ticker}: required source missing at {path}")
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as exc:
                raise SystemExit(f"{ticker}: required source unreadable ({exc})")
            stats["tickers"] += 1
            stats["ohlc_values_checked"] += strict_ohlc_domain(data, ticker)
            frame = strict_ticker_frame(data, ticker)
            if frame is None:
                stats["empty"].append(ticker)
                continue
            writer.write_table(pa.Table.from_pandas(frame, schema=BRK_SCHEMA,
                                                    preserve_index=False))
            stats["tickers_with_rows"] += 1
            stats["rows"] += len(frame)
    except BaseException:
        writer.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    writer.close()
    os.replace(tmp, final)
    stats["path"] = final
    return stats


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
