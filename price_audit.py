"""
price_history integrity audit + repair.

WHY THIS EXISTS
---------------
price_history contains ticker cross-contamination: identical OHLCV rows
written under multiple tickers on the same date (see audit output). Suspected
cause is scrape_ticker() in backfill_inventory.py selecting a ticker via
keyboard.type() + fixed wait_for_timeout(), then reading EXTRACT_JS off a
chart that has not finished re-rendering - so the PREVIOUS ticker's series
gets stored under the CURRENT ticker's name. Fixed timeouts on a dynamic
Plotly page are inherently racy; the fix upstream is to wait on a condition
(chart title / first data point matching the requested ticker) rather than a
duration. Until that is fixed, re-scraping will keep reintroducing this.

WHY IT ALSO CORRUPTS broker_flow
---------------------------------
insert_ticker_data() derives netval as (lot_diff * 100 * close) / 1e9 using
price_by_date from the SAME scraped payload. A wrong close means a
proportionally wrong netval. That is repairable WITHOUT re-scraping, because
lot_diff is unaffected:

    netval_correct = netval_wrong * (close_correct / close_wrong)

DETECTORS
---------
1. limit_violation  (HIGH confidence - these are physically impossible)
   IDX auto-rejection caps a daily move at +35/25/20% (tiered by prev close)
   and -15%. Any close-to-close move outside that band cannot happen on a
   real IDX listing. Note: a genuine corporate action (stock split, reverse
   split, rights issue) also breaks this test, so violations are reviewed,
   not auto-deleted.

2. cross_ticker_dup (HIGH confidence)
   Identical (open, high, low, close, volume) under 2+ tickers on one date.
   Two different IDX names matching on all five values including raw volume
   is not a coincidence.

3. series_break     (MEDIUM confidence)
   Close jumps >5x or <0.2x versus the ticker's own rolling median, then
   returns. Catches contamination that slipped past 1 and 2.

USAGE
-----
    py price_audit.py audit                  # report only, no writes
    py price_audit.py quarantine             # mark bad rows, write audit table
    py price_audit.py repair corrected.csv   # apply fixes + rescale netval

corrected.csv columns: date,ticker,open,high,low,close,volume
Get it from any reliable OHLCV source for the flagged (date, ticker) pairs.
"""

import sys
import sqlite3
import numpy as np
import pandas as pd

DB_PATH = "neobdm.db"
OHLCV = ["open", "high", "low", "close", "volume"]


def ara_bound(prev_close):
    if pd.isna(prev_close):
        return np.nan
    if prev_close < 200:
        return 0.35
    if prev_close <= 5000:
        return 0.25
    return 0.20


ARB_BOUND = -0.15
TOL = 0.005  # half a percentage point of slack for rounding


def load(conn):
    px = pd.read_sql("SELECT rowid AS rid, * FROM price_history", conn)
    return px.sort_values(["ticker", "date"]).reset_index(drop=True)


def detect(px):
    px = px.copy()
    g = px.groupby("ticker")
    px["prev_close"] = g["close"].shift(1)
    px["pct_chg"] = px["close"] / px["prev_close"] - 1
    px["ara"] = px["prev_close"].apply(ara_bound)

    px["limit_violation"] = (
        (px["pct_chg"] > px["ara"] + TOL) | (px["pct_chg"] < ARB_BOUND - TOL)
    ).fillna(False)

    dup_mask = px.duplicated(["date"] + OHLCV, keep=False)
    px["cross_ticker_dup"] = dup_mask & px[OHLCV].notna().all(axis=1)

    med = g["close"].transform(lambda s: s.rolling(21, center=True, min_periods=5).median())
    ratio = px["close"] / med
    px["series_break"] = ((ratio > 5) | (ratio < 0.2)).fillna(False)

    px["suspect"] = px[["limit_violation", "cross_ticker_dup", "series_break"]].any(axis=1)
    return px


def _reasons(row):
    return "+".join(
        r for r in ["limit_violation", "cross_ticker_dup", "series_break"] if row[r]
    )


def report(px):
    n = len(px)
    bad = px[px["suspect"]]
    print(f"price_history: {n} rows, {px['date'].nunique()} dates, {px['ticker'].nunique()} tickers")
    print(f"SUSPECT: {len(bad)} rows ({len(bad)/n*100:.1f}%), "
          f"{bad['date'].nunique()} dates, {bad['ticker'].nunique()} tickers\n")

    print("by detector:")
    for c in ["limit_violation", "cross_ticker_dup", "series_break"]:
        print(f"  {c:18s} {int(px[c].sum()):5d}")

    print("\nworst tickers (suspect rows / total rows):")
    t = px.groupby("ticker").agg(bad=("suspect", "sum"), tot=("suspect", "size"))
    t["pct"] = (t["bad"] / t["tot"] * 100).round(1)
    print(t[t["bad"] > 0].sort_values("bad", ascending=False).head(15).to_string())

    print("\nsample collision groups (identical OHLCV, different tickers):")
    d = px[px["cross_ticker_dup"]]
    grp = d.groupby(["date"] + OHLCV)["ticker"].apply(list).reset_index()
    for _, r in grp.head(8).iterrows():
        print(f"  {r['date']}  close={r['close']:>9.0f}  {r['ticker']}")

    print("\nbroker_flow rows riding on suspect prices (netval will be wrong):")
    return bad


def broker_flow_impact(conn, bad):
    keys = set(zip(bad["date"], bad["ticker"]))
    bf = pd.read_sql("SELECT date, ticker FROM broker_flow", conn)
    hit = bf.apply(lambda r: (r["date"], r["ticker"]) in keys, axis=1)
    print(f"  {int(hit.sum())} of {len(bf)} broker_flow rows ({hit.sum()/len(bf)*100:.1f}%)")


def cmd_audit(conn):
    px = detect(load(conn))
    bad = report(px)
    broker_flow_impact(conn, bad)
    out = bad.copy()
    out["reasons"] = out.apply(_reasons, axis=1)
    out[["date", "ticker", "open", "high", "low", "close", "volume", "pct_chg", "reasons"]] \
        .to_csv("price_audit_suspects.csv", index=False)
    print(f"\nwrote price_audit_suspects.csv ({len(out)} rows) "
          "- this is the (date,ticker) list to re-fetch prices for")


def cmd_quarantine(conn):
    px = detect(load(conn))
    bad = px[px["suspect"]].copy()
    bad["reasons"] = bad.apply(_reasons, axis=1)
    conn.execute("""CREATE TABLE IF NOT EXISTS price_quarantine (
        date TEXT, ticker TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, reasons TEXT, PRIMARY KEY (date, ticker))""")
    conn.executemany(
        "INSERT OR REPLACE INTO price_quarantine VALUES (?,?,?,?,?,?,?,?)",
        bad[["date", "ticker", "open", "high", "low", "close", "volume", "reasons"]]
        .itertuples(index=False, name=None),
    )
    conn.commit()
    print(f"quarantined {len(bad)} rows into price_quarantine (originals left intact "
          "in price_history - nothing deleted).")
    print("Backtests should exclude these until repaired, e.g.:")
    print("  LEFT JOIN price_quarantine q USING (date, ticker) WHERE q.date IS NULL")


def cmd_repair(conn, csv_path):
    fix = pd.read_csv(csv_path, dtype={"date": str, "ticker": str})
    missing = {"date", "ticker", "close"} - set(fix.columns)
    if missing:
        raise SystemExit(f"corrected.csv missing columns: {missing}")

    old = pd.read_sql("SELECT date, ticker, close FROM price_history", conn) \
        .rename(columns={"close": "close_old"})
    m = fix.merge(old, on=["date", "ticker"], how="inner")
    m = m[(m["close_old"] > 0) & (m["close"] > 0)]
    m["scale"] = m["close"] / m["close_old"]
    changed = m[(m["scale"] - 1).abs() > 1e-9]

    print(f"{len(m)} rows matched, {len(changed)} actually change price")

    cur = conn.cursor()
    for _, r in fix.iterrows():
        cur.execute(
            """UPDATE price_history SET open=?, high=?, low=?, close=?, volume=?
               WHERE date=? AND ticker=?""",
            (r.get("open"), r.get("high"), r.get("low"), r["close"], r.get("volume"),
             r["date"], r["ticker"]),
        )

    # netval = lot_diff * 100 * close / 1e9 -> lot_diff is unaffected by the
    # bad close, so rescaling by close_correct/close_wrong recovers it exactly.
    n_bf = 0
    for _, r in changed.iterrows():
        cur.execute(
            "UPDATE broker_flow SET netval = netval * ? WHERE date=? AND ticker=? AND bval IS NULL",
            (float(r["scale"]), r["date"], r["ticker"]),
        )
        n_bf += cur.rowcount
    conn.commit()

    print(f"updated {len(fix)} price_history rows; rescaled {n_bf} backfilled broker_flow "
          "netval rows (live rows with bval NOT NULL were scraped directly and left alone).")
    print("\nRe-run: py price_audit.py audit   to confirm the suspect count dropped.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "audit"
    conn = sqlite3.connect(DB_PATH)
    if cmd == "audit":
        cmd_audit(conn)
    elif cmd == "quarantine":
        cmd_quarantine(conn)
    elif cmd == "repair":
        cmd_repair(conn, sys.argv[2])
    else:
        raise SystemExit(__doc__)
    conn.close()
