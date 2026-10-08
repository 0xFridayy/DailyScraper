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
1. limit_violation (reference-scoped diagnostic, requiring review)
   IDX auto-rejection caps a daily move at +35/25/20% (tiered by reference)
   and -15%. Compare actual prices with the resolved session reference.
   A raw discontinuity alone cannot establish corruption or a corporate action.
   Confirmed scoped event references resolve admission independently of that
   discontinuity. Violations remain review findings rather than automatic deletions.

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
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neobdm.db")
OHLCV = ["open", "high", "low", "close", "volume"]


# Compatibility exports. Financial rules have one dependency-free owner.
from price_contract import ara_bound, ARB_BOUND, TOL, RAW_ACTUAL, quarantine_recoverable, ReferenceResult
from price_contract_frame import (default_registry, annotate_prices, span_result, _seal_price_frame,
                                  frame_as_of, independent_price_defects, label_column)


def load(conn):
    px = pd.read_sql("SELECT rowid AS rid, * FROM price_history", conn)
    return px.sort_values(["ticker", "date"]).reset_index(drop=True)


def detect(px, trusted=None, *, registry=None, representation=None):
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
    registry = registry or default_registry()
    # Independent defects invalidate baselines before chaining references.
    defects = independent_price_defects(px, registry)
    duplicate, duplicate_identity, series_break = (defects[c] for c in
        ("cross_ticker_dup", "duplicate_identity", "series_break"))
    external_trust = pd.Series(list(trusted) if trusted is not None else True, index=px.index)
    baseline_trust = external_trust & ~duplicate & ~duplicate_identity & ~series_break
    px = annotate_prices(px, registry=registry, representation=representation, trusted=baseline_trust)
    g = px.groupby("ticker")
    px["prev_close"] = px["previous_actual_close"]
    px["pct_chg"] = px["close"] / px["prev_close"] - 1  # raw discontinuity diagnostic
    px["ara"] = px["limit_reference_price"].apply(ara_bound)
    px["limit_violation"] = px["limit_admission_status"].eq("OUT_OF_BAND")

    px["cross_ticker_dup"] = duplicate
    px["duplicate_identity"] = duplicate_identity

    px["series_break"] = series_break

    px["suspect"] = px[["limit_violation", "cross_ticker_dup", "duplicate_identity", "series_break", "domain_violation"]].any(axis=1)
    px.attrs["price_contract"]["producer_columns"] = ["prev_close", "pct_chg", "ara", "limit_violation",
        "cross_ticker_dup", "duplicate_identity", "series_break", "suspect"]
    return _seal_price_frame(px)


def _reasons(row):
    return "+".join(
        r for r in ["limit_violation", "cross_ticker_dup", "duplicate_identity", "series_break", "domain_violation"] if row[r]
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
            "avg": None,
            "cost_status": "WITHHELD_UNKNOWN_SHARE_BASIS",
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


def inventory_window(now_utc, window_days):
    """(start_date, end_date) as YYYY-MM-DD for an /api/inventory request.

    The endpoint serves a ROLLING one-year window, and a start_date before it is
    not an error: it answers success with only the last ~20 sessions. 365 days
    back sat exactly ON that edge -- the 2026-09-24 top-up asked from 2025-09-24,
    the first day the chart's own picker offered -- so it had no margin at all.
    Which calendar the API counts in is unknown, and a runner in a UTC evening
    is already a day behind Jakarta, so the caller keeps a few days of margin
    and both bounds come from one UTC clock. Lives here so it is testable
    without playwright.
    """
    end = now_utc.astimezone(timezone.utc).date()
    return (end - timedelta(days=window_days)).isoformat(), end.isoformat()


def inventory_window_is_short(session_counts, min_sessions):
    """Did /api/inventory hand back its short fallback instead of the window?

    The fallback answers success, so a green run proves nothing: from 2026-08-31
    to 09-09 every tracked ticker came back with 20 sessions or fewer and every
    run went green. Judged on the LONGEST series in the run, not on each ticker
    or the first one, because a recent listing legitimately has less than a
    year (RSGK had 121 on 2026-09-24). Nothing stored at all is not a short
    window -- that is should_fail_run's case.
    """
    return bool(session_counts) and max(session_counts) < min_sessions


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

def adjudicate_quarantine(conn, *, registry=None, representation=None):
    """Current derived adjudication over immutable raw rows and old quarantine.

    A sole old ordinary-limit reason is superseded only by a current official
    reference admission. Independent and undocumented reasons remain blocked.
    """
    registry = registry or default_registry()
    px = load(conn)
    has_table = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='price_quarantine'"
    ).fetchone()[0]
    q = pd.read_sql("SELECT * FROM price_quarantine", conn) if has_table else pd.DataFrame()
    bad = set(zip(q.get("date", []), q.get("ticker", [])))
    trusted = [(d, t) not in bad for d, t in zip(px["date"], px["ticker"])]
    audited = detect(px, trusted=trusted, registry=registry, representation=representation)
    for row in q.to_dict("records"):
        key = row["date"], row["ticker"]
        current = audited[(audited.date == key[0]) & (audited.ticker == key[1])]
        if len(current) != 1:
            continue
        r = current.iloc[0]
        ref = ReferenceResult(r.limit_reference_status, r.limit_unresolved_reason,
                              r.limit_reference_price, r.limit_reference_kind)
        # Quarantine itself blocks anchor flags, so test actual full-bar admission.
        if quarantine_recoverable(row.get("reasons"), ref,
                bar_admitted=r.limit_admission_status == "IN_BAND" and not r.domain_violation,
                independent_defect=r.cross_ticker_dup or r.duplicate_identity or r.series_break,
                representation=audited.attrs["price_contract"]["input_representation"]):
            bad.discard(key)
    keep = [(d, t) not in bad for d, t in zip(audited["date"], audited["ticker"])]
    # A recovered event bar may now back the following ordinary session.
    audited = detect(px, trusted=keep, registry=registry, representation=representation)
    return audited, bad, bool(has_table)


def load_clean(conn, strict=False, *, registry=None, representation=None):
    audited, bad, has_table = adjudicate_quarantine(conn, registry=registry, representation=representation)
    if strict and not has_table:
        raise RuntimeError("price_quarantine missing")
    keep = [(d, t) not in bad for d, t in zip(audited.date, audited.ticker)]
    result = audited[pd.Series(keep, index=audited.index, dtype=bool) & ~audited["suspect"]].reset_index(drop=True)
    result.attrs.update(audited.attrs)
    return result


def _open_anchor_valid(px, g):
    """Open admission is independent of return comparability and later close."""
    return px["entry_open_admissible"]


def _return_context(px, all_dates, registry, representation, market, as_of):
    registry = registry or default_registry()
    as_of = frame_as_of(px, as_of)
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px = annotate_prices(px, registry=registry, representation=representation, market=market, as_of=as_of)
    from hashlib import sha256
    import json
    axis_digest = sha256(json.dumps(list(all_dates), separators=(",", ":")).encode()).hexdigest()
    identity = dict(px.attrs["price_contract"])
    if identity.get("label_axis_sha256") not in (None, axis_digest):
        from price_contract import UnsupportedPriceContract
        raise UnsupportedPriceContract("Changed verified session axis; rebuild labels from source observations")
    identity["label_axis_sha256"] = axis_digest
    px.attrs["price_contract"] = identity
    px["_pos"] = px["date"].map({d: i for i, d in enumerate(all_dates)})
    return px, registry, px.attrs["price_contract"]["input_representation"], as_of


def _span_masks(px, starts, ends, all_dates, registry, representation, market,
                start_phase="CLOSE", end_phase="CLOSE", as_of=None):
    results = [span_result(t, a if isinstance(a, str) else None,
                           b if isinstance(b, str) else None, registry=registry,
                           representation=representation, market=market,
                           session_axis=all_dates, start_phase=start_phase,
                           end_phase=end_phase, as_of=as_of)
               for t, a, b in zip(px.ticker, starts, ends)]
    return (pd.Series([r.status == "COMPARABLE" for r in results], index=px.index, dtype=bool),
            pd.Series([r.reason for r in results], index=px.index, dtype=object))


def add_forward_returns(px, all_dates, horizons=(1,), extremes=False,
                        open_anchored=False, *, registry=None, representation=None,
                        market="REGULAR", as_of=None):
    """Raw anchors, independent admission, and full phase-aware Option A masks.

    Unknown legacy representation/registry coverage yields withheld labels.
    Explicit registry and representation arguments are required for certified
    reconstructed fixtures or a source adapter with evidence of actual prices.
    """
    px, registry, representation, as_of = _return_context(px, all_dates, registry, representation, market, as_of)
    g = px.groupby("ticker")
    next_admitted = g["price_step_admissible"].shift(-1).eq(True)
    px["_step_valid"] = next_admitted & (g["_pos"].shift(-1) - px["_pos"]).eq(1)
    g = px.groupby("ticker")
    if open_anchored:
        if not {"open", "high", "low"} <= set(px.columns):
            raise ValueError("open_anchored=True needs open, high, low")
        entry_open = g["open"].shift(-1)
        entry_ok = g["entry_open_admissible"].shift(-1).eq(True)
        entry_dates = g["date"].shift(-1)
        px["next_entry_open_admissible"] = entry_ok
    for h in horizons:
        if type(h) is not int or h <= 0:
            raise ValueError("positive integer horizon required")
        end_dates = g["date"].shift(-h)
        contig = (g["_pos"].shift(-h) - px["_pos"]).eq(h)
        step_window = g["_step_valid"].transform(
            lambda s: s.rolling(h, min_periods=h).sum().shift(-(h - 1)).eq(h))
        comparable, reasons = _span_masks(px, px.date, end_dates, all_dates, registry, representation, market, as_of=as_of)
        admitted = contig & step_window & px["close_anchor_admissible"]
        valid = admitted & comparable
        px[f"fwd_{h}"] = (g["close"].shift(-h) / px["close"] - 1).where(valid)
        px[f"fwd_{h}_reason"] = reasons.where(~valid, "").mask(~admitted & reasons.eq(""), "PRICE_PATH_UNAVAILABLE")
        if extremes:
            hi = g["high"].transform(lambda s: s.rolling(h, min_periods=h).max().shift(-h))
            lo = g["low"].transform(lambda s: s.rolling(h, min_periods=h).min().shift(-h))
            px[f"max_{h}"] = (hi / px["close"] - 1).where(valid)
            px[f"mdd_{h}"] = (lo / px["close"] - 1).where(valid)
        if open_anchored:
            oc_span, oc_reasons = _span_masks(px, entry_dates, end_dates, all_dates, registry, representation, market,
                                             "OPEN", "CLOSE", as_of)
            oc_admitted = admitted & entry_ok
            px[f"fwd_oc_{h}"] = (g["close"].shift(-h) / entry_open - 1).where(oc_admitted & oc_span)
            px[f"fwd_oc_{h}_reason"] = oc_reasons.where(~(oc_admitted & oc_span), "").mask(~oc_admitted & oc_reasons.eq(""), "PRICE_PATH_UNAVAILABLE")
            oo_dates = g["date"].shift(-(h + 1))
            oo_span, oo_reasons = _span_masks(px, entry_dates, oo_dates, all_dates, registry, representation, market,
                                             "OPEN", "OPEN", as_of)
            oo_contig = (g["_pos"].shift(-(h + 1)) - px["_pos"]).eq(h + 1)
            exit_ok = g["entry_open_admissible"].shift(-(h + 1)).eq(True)
            oo_admitted = oo_contig & step_window & entry_ok & exit_ok
            px[f"fwd_oo_{h}"] = (g["open"].shift(-(h + 1)) / entry_open - 1).where(oo_admitted & oo_span)
            px[f"fwd_oo_{h}_reason"] = oo_reasons.where(~(oo_admitted & oo_span), "").mask(~oo_admitted & oo_reasons.eq(""), "PRICE_PATH_UNAVAILABLE")
    if open_anchored:
        comparable, reasons = _span_masks(px, px.date, entry_dates, all_dates, registry, representation, market,
                                          "CLOSE", "OPEN", as_of)
        admitted = px["_step_valid"] & entry_ok
        px["gap_1"] = (entry_open / px["close"] - 1).where(admitted & comparable)
        px["gap_1_reason"] = reasons.where(~(admitted & comparable), "").mask(~admitted & reasons.eq(""), "PRICE_PATH_UNAVAILABLE")
    owned = set(px.attrs["price_contract"].get("label_columns", []))
    for h in horizons:
        owned.update((f"fwd_{h}", f"fwd_{h}_reason"))
        if extremes:
            owned.update((f"max_{h}", f"mdd_{h}"))
        if open_anchored:
            owned.update((f"fwd_oc_{h}", f"fwd_oc_{h}_reason", f"fwd_oo_{h}", f"fwd_oo_{h}_reason"))
    if open_anchored:
        owned.update(("gap_1", "gap_1_reason", "next_entry_open_admissible"))
    px.attrs["price_contract"]["label_columns"] = sorted(owned)
    return _seal_price_frame(px.drop(columns=["_pos", "_step_valid"]))


def add_lagged_returns(px, all_dates, lags=(1,), *, registry=None, representation=None,
                       market="REGULAR", as_of=None):
    px, registry, representation, as_of = _return_context(px, all_dates, registry, representation, market, as_of)
    g = px.groupby("ticker")
    px["_step_valid"] = px["price_step_admissible"] & (px["_pos"] - g["_pos"].shift(1)).eq(1)
    g = px.groupby("ticker")
    for k in lags:
        if type(k) is not int or k <= 0:
            raise ValueError("positive integer lag required")
        starts = g["date"].shift(k)
        comparable, reasons = _span_masks(px, starts, px.date, all_dates, registry, representation, market, as_of=as_of)
        contig = (px["_pos"] - g["_pos"].shift(k)).eq(k)
        steps = g["_step_valid"].transform(lambda s: s.rolling(k, min_periods=k).sum().eq(k))
        admitted = contig & steps & g["close_anchor_admissible"].shift(k).eq(True)
        valid = admitted & comparable
        px[f"lag_{k}"] = (px["close"] / g["close"].shift(k) - 1).where(valid)
        px[f"lag_{k}_reason"] = reasons.where(~valid, "").mask(~admitted & reasons.eq(""), "PRICE_PATH_UNAVAILABLE")
    owned = set(px.attrs["price_contract"].get("label_columns", []))
    for k in lags:
        owned.update((f"lag_{k}", f"lag_{k}_reason"))
    px.attrs["price_contract"]["label_columns"] = sorted(owned)
    return _seal_price_frame(px.drop(columns=["_pos", "_step_valid"]))


def clean_panel(conn, horizons=(1,), lags=(), extremes=False, strict=False,
                open_anchored=False, *, registry=None, representation=None, market="REGULAR", as_of=None):
    registry = registry or default_registry()
    all_dates = sorted(r[0] for r in conn.execute("SELECT DISTINCT date FROM price_history"))
    px = add_forward_returns(load_clean(conn, strict=strict, registry=registry, representation=representation),
                             all_dates, horizons, extremes, open_anchored, registry=registry,
                             representation=representation, market=market, as_of=as_of)
    if lags:
        px = add_lagged_returns(px, all_dates, lags, registry=registry, representation=representation,
                                market=market, as_of=as_of)
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

    print("\nbroker_flow rows associated with suspects (requires independent review):")
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
