"""
price_history integrity audit + repair.

WHY THIS EXISTS
---------------
price_history contains ticker cross-contamination: identical OHLCV rows
written under multiple tickers on the same date (see audit output). The cause
was scrape_ticker() in backfill_inventory.py selecting a ticker via
keyboard.type() + Enter and then reading EXTRACT_JS after a fixed
wait_for_timeout(), so the PREVIOUS ticker's series could still be on screen
and get stored under the CURRENT ticker's name.

Confirmed a race rather than a consistent mis-pick: on 2026-08-21 an unchanged
rerun healed 899 of 1,400 contaminated rows and broke zero new ones, taking
CDIA and COIN from 231 bad rows each to 0. Both fixed waits have since been
replaced with waits on a condition, and the guards below back that up. The
historical backlog still in the table is what this module is for.

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
    py price_audit.py count                  # total suspect count
    py price_audit.py count cross_ticker_dup # one detector only (the CI gate)
    py price_audit.py quarantine             # mark bad rows, write audit table
    py price_audit.py repair corrected.csv   # apply fixes + rescale netval
    py price_audit.py reconcile-cross-dups ohlc.parquet [--apply]
                                             # remove proven cloned wrong bars;
                                             # dry-run unless --apply is present

corrected.csv columns: date,ticker,open,high,low,close,volume
Get it from any reliable OHLCV source for the flagged (date, ticker) pairs.
"""

import os
import re
import sys
import sqlite3
from datetime import datetime, timezone
import numpy as np
import pandas as pd

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neobdm.db")
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


def detect(px, trusted=None):
    """Flag suspect rows. `trusted` is an optional boolean mask aligned to px.

    A row that is NOT trusted (typically: already quarantined) still gets
    judged itself, but is never used as the PREVIOUS close another row is
    measured against. Without that, one known-bad close manufactures a
    limit_violation on the perfectly good row that follows it:

        MDIA  08-13  95   <- KIOS's price, quarantined weeks ago
              08-24  252  <- correct, and unique, but 95 -> 252 is +171%

    which then gets reported as the scraper "writing bad rows again". It is the
    same mistake add_forward_returns() guards against at the target end — a
    window that spans a removed row describes a move that never happened — so
    the baseline is dropped rather than bridged to the last surviving close: a
    multi-day jump cannot be judged against a one-day ARA/ARB band either.
    """
    px = px.copy()
    g = px.groupby("ticker")
    if trusted is None:
        px["prev_close"] = g["close"].shift(1)
    else:
        base = px["close"].where(np.asarray(trusted, dtype=bool))
        px["prev_close"] = base.groupby(px["ticker"]).shift(1)
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


# ─────────────────────────────────────────────
#  SCRAPE-TIME GUARDS
#
#  Used by backfill_inventory.py while scraping. They live here, not there,
#  because there they would be unimportable without playwright and NeoBDM
#  credentials — and therefore untestable in CI, which is the one place a
#  regression in them needs to be caught.
# ─────────────────────────────────────────────

def series_signature(price_payload):
    """Cheap identity of a scraped OHLCV series.

    Used by backfill_inventory.py as its last line of defence: two different
    stocks cannot produce byte-identical OHLCV, so if consecutive tickers do,
    the second one was read off a chart that had not re-rendered yet. Lives
    here rather than in the scraper so it is importable — and therefore
    testable in CI — without playwright or NeoBDM credentials.
    """
    p = price_payload or {}
    if not p.get("x"):
        return None
    c = p.get("close") or []
    return (len(p["x"]), tuple(p["x"][:3]), tuple(p["x"][-3:]),
            tuple(c[:3]), tuple(c[-3:]))


def ticker_from_title(title):
    """The 4-letter code a chart title claims to be showing, or None.

    Returning None for a title that carries no code is deliberate: the title
    format is not guaranteed, so absence must not be treated as a mismatch.
    When a code IS present it is authoritative.
    """
    m = re.search(r"\b([A-Z]{4})\b", str(title or ""))
    return m.group(1) if m else None


def bagholders_from_payloads(payloads, n=2):
    """Rank observable broker inventory accumulated across API time blocks.

    Each block may expose a different top-broker set. Aggregating blocks keeps
    an older accumulator visible even if it stopped buying recently. This is
    observable broker inventory, not beneficial ownership: nominees, transfers,
    and brokers outside each block's top list are not visible here.
    """
    totals = {}
    observed_dates = set()

    def _total(series):
        return sum(v for v in (series or []) if isinstance(v, (int, float)))

    for payload in payloads or []:
        data = (payload or {}).get("data") or {}
        nlot = data.get("nlot") or {}
        nval = data.get("nval") or {}
        observed_dates.update(str(d) for d in (data.get("date") or []) if d)
        for code, lots in nlot.items():
            row = totals.setdefault(code, {"cum": 0.0, "value": 0.0})
            row["cum"] += _total(lots)
            row["value"] += _total(nval.get(code))

    holders = []
    for code, row in totals.items():
        cum = row["cum"]
        if cum <= 0:
            continue
        shares = cum * 100
        holders.append({
            "code": code, "cum": cum,
            "avg": (row["value"] / shares) if shares else 0,
            "observed_trading_days": len(observed_dates),
        })
    holders.sort(key=lambda h: h["cum"], reverse=True)
    return holders[:n]


def inventory_date_blocks(payload, trading_days=60, block_days=20):
    """Exact non-overlapping trading-date blocks discovered from API data."""
    data = (payload or {}).get("data") or {}
    dates = [str(d) for d in (data.get("date") or []) if d]
    if not dates:
        dates = [str(row.get("date")) for row in (data.get("ohlc") or [])
                 if isinstance(row, dict) and row.get("date")]
    dates = sorted(set(dates))[-trading_days:]
    blocks = []
    for i in range(0, len(dates), block_days):
        chunk = dates[i:i + block_days]
        if chunk:
            blocks.append((chunk[0], chunk[-1]))
    return blocks


def bagholders_from_payload(payload, n=2):
    """Rank brokers by cumulative NET LOT in an /api/inventory response.

    Lives here for the same reason as the guards above: inside neobdm_scraper.py
    it would be unimportable without playwright, and therefore untestable in CI.
    That mattered — the DOM version of this feature broke when NeoBDM retired
    /inventory/ and printed "Bag holder: -" every day for weeks, because an empty
    result is indistinguishable from "no data" at the formatting layer and
    nothing could exercise it.

    `nlot` is per-day net lot per broker (verified in HANDOFF Appendix N: NOT
    cumulative), so the bag-holder position is its sum over the window. Average
    cost comes from `nval`, which the same appendix confirms is full-precision
    Rupiah rather than a truncated display string:

        avg = sum(nval) / (sum(nlot) * 100 shares)

    Only net ACCUMULATORS are returned. The old code sorted by cumulative net and
    took the top n unconditionally, so on a ticker every broker was dumping it
    would report a net SELLER as a "bag holder" — the opposite of the term.
    """
    return bagholders_from_payloads([payload], n=n)


# The scheduled scrape runs 07:00 Asia/Kuala_Lumpur, before IDX opens, which is
# the ONLY reason `scrape date - 1 == data date` (Appendix E) holds. IDX opens
# 09:00 WIB (UTC+7) = 10:00 in the UTC+8 timezone this repo schedules on.
IDX_OPEN_HOUR_LOCAL = 10


def date_offset_holds(now_local):
    """Is `market_summary_daily.date - 1 == data date` true for a run started now?

    Only before the market opens. The screener serves the last COMPLETED
    session, so a pre-open run gets yesterday's close and the whole pipeline's
    one-day offset is correct. Run after the close and it serves TODAY's close,
    which then gets stored under today's date and silently breaks the offset for
    that date.

    Learned by doing it: a manual `workflow_dispatch` at 21:09 WIB on 2026-08-27
    overwrote that morning's correctly-offset rows with same-day closes, and
    check_signal_integrity's cross-source check went from 100% to 85%. Nothing
    in the workflow or the scraper refused or even warned. See HANDOFF Appendix Q.

    `now_local` is a datetime already in the scrape timezone.
    """
    return now_local.hour < IDX_OPEN_HOUR_LOCAL


def should_fail_run(n_failed, n_total, max_failure_rate=0.30):
    """Should a backfill run exit non-zero?

    run_backfill() used to print its failures and return 0 regardless. That made
    a TOTAL failure indistinguishable from success at the workflow level: no
    tickers scraped means price_history is unchanged, which means the
    contamination gate sees no growth and passes, which means the commit step
    finds nothing to commit — and the run goes green having done nothing.

    A few failures are normal (a suspended ticker, a name with no chart), so the
    threshold is a rate rather than "any failure at all". Lives here so it is
    testable without playwright.
    """
    if n_total <= 0:
        return True                       # nothing attempted is itself a failure
    return (n_failed / n_total) > max_failure_rate


# ─────────────────────────────────────────────
#  CLEAN-PANEL HELPERS
#
#  Import clean_panel() instead of reading price_history directly. Filtering
#  the quarantine out is necessary but NOT sufficient: a plain
#  groupby().shift(-h) does not know a row was removed, so it joins the last
#  surviving row to the next surviving one ACROSS the gap and manufactures a
#  return that never happened. Measured on this DB: filtering alone left
#  50 fabricated targets, among them ELTY +92% and TEBE +51%, which by
#  themselves pushed target kurtosis from 4.4 to 12.1 and kept 7 rows outside
#  the ARA/ARB band that the filter was supposed to have eliminated. With the
#  guard below, limit violations in the target drop to zero.
# ─────────────────────────────────────────────

def load_clean(conn, strict=False):
    """price_history with quarantined (date, ticker) rows dropped.

    Prefers the price_quarantine table written by the `quarantine` command. If
    that table is absent the detectors are run in-memory instead, so a caller
    that forgot to quarantine still gets clean rows rather than silently
    training on dirty ones. Pass strict=True to require the table instead.
    """
    px = load(conn)
    has_table = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='price_quarantine'"
    ).fetchone()[0]

    if has_table:
        q = pd.read_sql("SELECT date, ticker FROM price_quarantine", conn)
    elif strict:
        raise RuntimeError("price_quarantine missing — run: py price_audit.py quarantine")
    else:
        d = detect(px)
        q = d.loc[d["suspect"], ["date", "ticker"]]

    bad = set(zip(q["date"], q["ticker"]))
    keep = [(d, t) not in bad for d, t in zip(px["date"], px["ticker"])]
    return px[keep].reset_index(drop=True)


def add_forward_returns(px, all_dates, horizons=(1,), extremes=False):
    """Attach gap-guarded forward returns to a filtered price frame.

    all_dates: every date in the UNFILTERED panel, in order. Positions on this
    axis are what makes a removed row detectable — the surviving rows keep
    their original spacing, so a hole shows up as a jump in position.

    For each horizon h, `fwd_{h}` is only defined when the row h steps ahead in
    the ticker's surviving series also sits exactly h dates ahead on the panel
    axis. Because positions are strictly increasing, that single equality also
    proves every intermediate step was contiguous — so the same mask makes the
    `max_{h}` / `mdd_{h}` windows safe, not just the endpoint.

    A ticker suspended for a day is treated the same as a quarantined row:
    the window is dropped rather than bridged.
    """
    pos = {d: i for i, d in enumerate(all_dates)}
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["_pos"] = px["date"].map(pos)
    g = px.groupby("ticker")

    one_contig = (g["_pos"].shift(-1) - px["_pos"]) == 1
    one_return = g["close"].shift(-1) / px["close"] - 1
    one_upper = px["close"].apply(ara_bound) + TOL
    px["_step_valid"] = one_contig & one_return.between(ARB_BOUND - TOL, one_upper)
    g = px.groupby("ticker")

    for h in horizons:
        contig = (g["_pos"].shift(-h) - px["_pos"]) == h
        raw_return = g["close"].shift(-h) / px["close"] - 1
        # Every daily transition inside a multi-day window must be tradeable.
        # This masks a 3/5/10/20-day target that crosses a stock split just as
        # firmly as the one-day target on the split itself.
        step_window = g["_step_valid"].transform(
            lambda s: s.rolling(h, min_periods=h).sum().shift(-(h - 1)).eq(h)
        )
        valid = contig & step_window
        px[f"fwd_{h}"] = np.where(valid, raw_return, np.nan)

        if extremes:
            # rolling(h) on the -h shifted series spans exactly rows i+1..i+h
            hi = g["high"].transform(lambda s: s.shift(-h).rolling(h, min_periods=1).max())
            lo = g["low"].transform(lambda s: s.shift(-h).rolling(h, min_periods=1).min())
            px[f"max_{h}"] = np.where(valid, hi / px["close"] - 1, np.nan)
            px[f"mdd_{h}"] = np.where(valid, lo / px["close"] - 1, np.nan)

    return px.drop(columns=["_pos", "_step_valid"])


def add_lagged_returns(px, all_dates, lags=(1,)):
    """Backward-looking returns under the same guard.

    Features bridge a hole just as readily as targets do — a momentum_1d
    computed across a removed row is the same fabricated +92% move, only
    landing in X instead of y. It is the less dangerous of the two (a bad
    feature adds noise; a bad target adds false labels), but it is still an
    extreme value a tree will happily split on, so guard it too.
    """
    pos = {d: i for i, d in enumerate(all_dates)}
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["_pos"] = px["date"].map(pos)
    g = px.groupby("ticker")

    previous_one = g["close"].shift(1)
    one_contig = (px["_pos"] - g["_pos"].shift(1)) == 1
    one_return = px["close"] / previous_one - 1
    one_upper = previous_one.apply(ara_bound) + TOL
    px["_step_valid"] = one_contig & one_return.between(ARB_BOUND - TOL, one_upper)
    g = px.groupby("ticker")

    for k in lags:
        contig = (px["_pos"] - g["_pos"].shift(k)) == k
        previous = g["close"].shift(k)
        raw_return = px["close"] / previous - 1
        step_window = g["_step_valid"].transform(
            lambda s: s.rolling(k, min_periods=k).sum().eq(k)
        )
        valid = contig & step_window
        px[f"lag_{k}"] = np.where(valid, raw_return, np.nan)

    return px.drop(columns=["_pos", "_step_valid"])


def clean_panel(conn, horizons=(1,), lags=(), extremes=False, strict=False):
    """load_clean() + add_forward_returns() — the one call worth importing."""
    all_dates = sorted(
        r[0] for r in conn.execute("SELECT DISTINCT date FROM price_history").fetchall()
    )
    px = add_forward_returns(
        load_clean(conn, strict=strict), all_dates, horizons=horizons, extremes=extremes
    )
    if lags:
        px = add_lagged_returns(px, all_dates, lags=lags)
    return px


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


DETECTORS = ("limit_violation", "cross_ticker_dup", "series_break")


def cmd_count(conn, reason=None):
    """Suspect count, for use as a CI regression gate.

    The absolute number is not the point - the backlog is large and shrinking.
    What matters is whether a scrape run made it BIGGER, so the workflow takes
    a reading before and after and compares.

    With no argument this totals all three detectors. With a detector name it
    counts only that one, and the topup workflow deliberately passes
    `cross_ticker_dup`:

      cross_ticker_dup is the ONLY detector whose growth means the SCRAPER
      regressed. It fires when one ticker's OHLCV is stored under another's
      name - exactly the bug this whole module exists for - and two different
      real IDX stocks cannot share byte-identical open/high/low/close/volume,
      so a correct scrape never raises it (it only ever heals old dups, taking
      the count down).

      limit_violation and series_break, gated against the total, froze
      price_history instead. They also fire on legitimate data: a +25% ARA day
      or a corporate action trips limit_violation (the module's own doctrine is
      to REVIEW those, not auto-act), and series_break's centered rolling median
      shifts at the fresh end of every series as new days arrive. So each
      correct nightly scrape added ~1 such row, the total ticked up, the gate
      blocked the commit, and price_history stopped advancing - the staleness
      check_signal_integrity.py then reported. See HANDOFF.md Appendix O.
    """
    px = detect(load(conn))
    if reason is None:
        print(int(px["suspect"].sum()))
    elif reason in DETECTORS:
        print(int(px[reason].sum()))
    else:
        raise SystemExit(f"unknown detector {reason!r}; choose one of {', '.join(DETECTORS)}")


def cmd_quarantine(conn):
    px = detect(load(conn))
    bad = px[px["suspect"]].copy()
    bad["reasons"] = bad.apply(_reasons, axis=1)
    conn.execute("""CREATE TABLE IF NOT EXISTS price_quarantine (
        date TEXT, ticker TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, reasons TEXT, PRIMARY KEY (date, ticker))""")
    previous = conn.execute("SELECT COUNT(*) FROM price_quarantine").fetchone()[0]
    # This table is a snapshot, not an append-only history. Without clearing it,
    # repaired rows stay quarantined forever even after a fresh audit clears them.
    conn.execute("DELETE FROM price_quarantine")
    conn.executemany(
        "INSERT OR REPLACE INTO price_quarantine VALUES (?,?,?,?,?,?,?,?)",
        bad[["date", "ticker", "open", "high", "low", "close", "volume", "reasons"]]
        .itertuples(index=False, name=None),
    )
    conn.commit()
    print(f"refreshed price_quarantine: {previous} previous rows -> {len(bad)} current "
          "suspects (originals left intact in price_history).")
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


def authoritative_duplicate_deletions(px, authoritative):
    """Return cloned rows that are not the authoritative member of a collision.

    Safety invariant: each identical-OHLCV collision group must contain exactly
    one row whose (date, ticker, OHLCV) agrees with the authoritative panel. If
    any group has zero or multiple confirmed members, refuse the entire repair;
    guessing which ticker is real would be worse than leaving it quarantined.
    """
    needed = {"date", "ticker", *OHLCV}
    missing = needed - set(authoritative.columns)
    if missing:
        raise ValueError(f"authoritative panel missing columns: {sorted(missing)}")

    source = authoritative[list(needed)].copy()
    source["date"] = source["date"].astype(str)
    source["ticker"] = source["ticker"].astype(str).str.upper()
    if source.duplicated(["date", "ticker"]).any():
        raise ValueError("authoritative panel has duplicate (date, ticker) keys")

    audited = detect(px)
    dup = audited[audited["cross_ticker_dup"]][
        ["rid", "date", "ticker", *OHLCV]
    ].copy()
    if dup.empty:
        return dup
    dup["date"] = dup["date"].astype(str)
    dup["ticker"] = dup["ticker"].astype(str).str.upper()

    source = source.rename(columns={c: f"{c}_source" for c in OHLCV})
    source["_source_present"] = True
    checked = dup.merge(source, on=["date", "ticker"], how="left")
    same = checked["_source_present"].fillna(False).to_numpy(dtype=bool)
    for col in OHLCV:
        left = pd.to_numeric(checked[col], errors="coerce").to_numpy(dtype=float)
        right = pd.to_numeric(checked[f"{col}_source"], errors="coerce").to_numpy(dtype=float)
        same &= np.isclose(left, right, rtol=0, atol=1e-9, equal_nan=False)
    checked["_confirmed"] = same
    checked["_collision"] = checked.groupby(
        ["date", *OHLCV], dropna=False, sort=False
    ).ngroup()
    confirmed_per_group = checked.groupby("_collision")["_confirmed"].transform("sum")
    unresolved = checked[confirmed_per_group != 1]
    if not unresolved.empty:
        n_groups = unresolved["_collision"].nunique()
        raise RuntimeError(
            f"refusing reconciliation: {n_groups} collision group(s) do not have "
            "exactly one authoritative member"
        )
    return checked.loc[~checked["_confirmed"], ["rid", "date", "ticker", *OHLCV]]


def cmd_reconcile_cross_dups(conn, parquet_path, apply=False):
    """Reconcile duplicate OHLCV groups against an authoritative parquet panel."""
    source = pd.read_parquet(parquet_path)
    targets = authoritative_duplicate_deletions(load(conn), source)
    before = int(detect(load(conn))["cross_ticker_dup"].sum())
    print(f"cross_ticker_dup rows before: {before}")
    print(f"proven cloned wrong rows: {len(targets)}")
    if len(targets):
        print(targets.groupby("ticker").size().sort_values(ascending=False).to_string())
    if not apply:
        print("DRY RUN — pass --apply to archive and remove these rows.")
        return

    conn.execute("""CREATE TABLE IF NOT EXISTS price_repair_archive (
        date TEXT, ticker TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, reason TEXT, archived_at TEXT,
        PRIMARY KEY (date, ticker)
    )""")
    conn.execute("DROP TABLE IF EXISTS temp.reconcile_targets")
    conn.execute("CREATE TEMP TABLE reconcile_targets (date TEXT, ticker TEXT, PRIMARY KEY(date,ticker))")
    conn.executemany(
        "INSERT INTO reconcile_targets VALUES (?,?)",
        targets[["date", "ticker"]].itertuples(index=False, name=None),
    )

    live_rows = conn.execute("""
        SELECT COUNT(*) FROM broker_flow b
        JOIN reconcile_targets t USING (date, ticker)
        WHERE b.bval IS NOT NULL
    """).fetchone()[0]

    stamp = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO price_repair_archive
        SELECT p.date, p.ticker, p.open, p.high, p.low, p.close, p.volume,
               'cross_ticker_dup_not_authoritative_member', ?
        FROM price_history p JOIN reconcile_targets t USING (date, ticker)
    """, (stamp,))
    broker_rows = conn.execute("""
        SELECT COUNT(*) FROM broker_flow b
        JOIN reconcile_targets t USING (date, ticker)
        WHERE b.bval IS NULL
    """).fetchone()[0]
    conn.execute("""
        DELETE FROM broker_flow
        WHERE bval IS NULL AND EXISTS (
            SELECT 1 FROM reconcile_targets t
            WHERE t.date=broker_flow.date AND t.ticker=broker_flow.ticker
        )
    """)
    conn.execute("""
        DELETE FROM price_history
        WHERE EXISTS (
            SELECT 1 FROM reconcile_targets t
            WHERE t.date=price_history.date AND t.ticker=price_history.ticker
        )
    """)
    after = int(detect(load(conn))["cross_ticker_dup"].sum())
    if after:
        conn.rollback()
        raise RuntimeError(
            f"reconciliation would leave {after} cross_ticker_dup rows; rolled back"
        )
    conn.commit()
    print(f"archived and removed {len(targets)} price rows and {broker_rows} derived "
          f"backfill broker rows; preserved {live_rows} independently scraped live "
          f"broker rows; cross_ticker_dup rows after: {after}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "audit"
    conn = sqlite3.connect(DB_PATH)
    if cmd == "audit":
        cmd_audit(conn)
    elif cmd == "count":
        cmd_count(conn, sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "quarantine":
        cmd_quarantine(conn)
    elif cmd == "repair":
        cmd_repair(conn, sys.argv[2])
    elif cmd == "reconcile-cross-dups":
        cmd_reconcile_cross_dups(
            conn, sys.argv[2], apply="--apply" in sys.argv[3:]
        )
    else:
        raise SystemExit(__doc__)
    conn.close()
