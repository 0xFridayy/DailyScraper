"""Broker learning orchestrator: `daily | weekly | render` (BROKER_LEARNING.md §1, §7).

    py broker_learning_run.py daily  [--dry-run] [--no-fetch] [--raw-dir DIR] [--legacy-cache]
                                     [--tickers A,B] [--db PATH] [--out DIR]
    py broker_learning_run.py weekly [--no-fetch] [--raw-dir DIR] [--legacy-cache]
                                     [--tickers A,B] [--db PATH] [--history-dir DIR]
    py broker_learning_run.py render [--dry-run] [--raw-dir DIR] [--legacy-cache]
                                     [--tickers A,B] [--db PATH] [--out DIR]

daily   watchlist -> fetch -> ruleset v1 on the last session -> live ledger
        (insert-only) -> score live outcomes that are now known -> dashboard ->
        Telegram (not with --dry-run).
weekly  whole market -> ruleset v1 on every session -> retrospective rule stats,
        weights, broker scores, broker profitability, alpha cases and broker
        lift, keyed by as_of -> history export of the last 20 sessions.
render  the dashboard again from the cache and the DB; writes nothing to the DB.

Everything shown or stored is OBSERVABLE BROKER INVENTORY, NOT BENEFICIAL
OWNERSHIP, and research-grade. Nothing here feeds daily_picks.py, the ARB veto
or any ML experiment.

WHY THE LIVE LEDGER ONLY TAKES FRESH TICKERS
--------------------------------------------
The prospective ledger is the one part of this that is point-in-time by
construction, and only because each row is written on the day it describes
(§2.4). A ticker whose last session is older than the run's newest session
(suspended, or its fetch failed and a stale cache file is all there is) would
be recording an OLD session today, which is a backfill. So a live row is
written only for an eligible ticker whose last session is the run's
data_through, and after a fetch only tickers that fetched cleanly this run are
read at all. The dashboard still shows the rest, with a warning.

WHY WEEKLY WRITES NO STATS WHEN THE FETCH IS BROKEN
---------------------------------------------------
Weekly tables are INSERT OR IGNORE keyed by as_of, so the first write for a
week is the one that stands. Stats from a fetch that lost more than 30% of the
market would describe a different universe, and a clean re-run the same week
could not replace them. When price_audit.should_fail_run says the run failed,
the run is recorded as failed and no weekly table is touched. The daily run
is the opposite case: its live rows are individually valid, and a row not
written today can never be written later, so it keeps what it fetched and
still exits non-zero.

WHY A DRY RUN WRITES TO A COPY
------------------------------
`daily --dry-run` exercises the whole path, including the ledger writes, but a
local trial must not put rows into the committed broker_learning.db: the
ledger is insert-only, so a trial row could never be taken back. Without an
explicit --db, a dry run works on a temporary copy that is deleted afterwards.
For the same reason `daily --no-fetch` is refused without --dry-run or an
explicit --db: cached sessions are days or weeks old when they are read, so
rows recorded from them would sit in the committed "prospective" ledger with
their outcomes already known, and could never be taken out.

WHY THE UNIVERSE BIG-MOVE RATE LIVES IN runs.note
-------------------------------------------------
The alpha section compares its cases with "how many eligible names went
>= +50% in 60 sessions at all" (hold_60, the holder's return, with the share
of those windows that bridged a suspension beside it). Those are numbers per
weekly run, over rows the DB does not keep, and the §5 schema has no table
for them. The weekly run's `note` (a free-text column) carries them as JSON
together with the run's counts; the earliest successful run for an as_of is
read, because that is the run whose rows the INSERT OR IGNORE tables
actually hold.

The §4.5 net-of-cost trade stats of the buy rules travel the same way
(`net_trade_stats`), and the scorecard prints them under each buy rule.

WHY ZERO-SESSION TICKERS ARE `empty`, NOT FAILED
-----------------------------------------------
A delisted or long-suspended ticker answers with no sessions at all
(broker_collect's module docstring). It was attempted, so it stays in the
failure-rate denominator, but it is not a failure: a universe with many dead
names must not trip the 30% exit. The same holds for such a file in the
cache. The dashboard lists them apart from real failures. A run in which no
ticker produced data is still a failed run.

WHY THE HISTORY EXPORT OVERLAPS
-------------------------------
The API forgets anything older than a rolling year (§4.7). Each weekly run
writes the last 20 sessions, about four weeks, to broker_<as_of>.parquet and
ohlc_<as_of>.parquet, so one missed Saturday leaves no hole. The files
therefore overlap, and a reader must resolve the overlap per (date, ticker)
PARTITION, never per broker row: for each (date, ticker), take every broker
row from the newest file whose ohlc file covers that (date, ticker), and
nothing from older files. All-zero broker rows are not written, so a newer
file cannot say "this broker is now zero"; a (date, ticker, broker) dedupe
would keep a row the vendor later restated away. The files are paid NeoBDM
data and the repo is public: they are gitignored and leave the runner only as
release assets, and only when the owner turns that on.

Secrets: this module never prints a token, a cookie or a URL. Telegram errors
are reported by broker_dashboard as exception types only.
"""

import argparse
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import broker_book as bb
import broker_collect as bc
import broker_dashboard as bd
import broker_learning as bl
import broker_learning_db as bdb
import broker_rules as br
from price_audit import should_fail_run

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(HERE, "broker_history")
HISTORY_OVERLAP = 20          # sessions per weekly export; ~4 weeks of overlap
BOOK_ROWS = 12                # per ticker card, by |position|
DISTRIBUTORS = 3              # "who is distributing": the 3 largest 5-day net sellers
ALPHA_CASES_SHOWN = 20
LIFT_SHOWN = 15
LIFT_MIN_CASES = 3            # a lift from one or two cases is noise
PROGRESS_EVERY = 100          # weekly: log every N tickers
REASON_MAX = 120
EMPTY_REASON = "0 sesi (delisting / suspensi panjang)"

# live_signals.features: the numbers behind each rule, as broker_rules.evaluate
# names them, plus the ticker-level context every rule reads. Per rule rather
# than all explain columns on every row: the ledger is committed daily and six
# copies of 27 fields per ticker-day would be mostly repetition.
FEATURE_COMMON = ("close", "rv20", "adv20", "val20")
RULE_FEATURES = {
    "R1": ("NLH", "costH", "gapH"),
    "R2": ("w_broker", "w_nl60", "w_cost60", "w_gain", "w_nl5"),
    "R3": ("NLH", "costH", "gapH"),
    "R4": ("f_broker", "f_nl5", "f_cost5", "f_gap"),
    "R5": ("s_brokers", "s_nl60", "s_nl5"),
    "R6": ("a_broker", "a_nl60", "a_nl60_adv", "a_cost60", "a_gap", "a_buydays",
           "range60", "drift60"),
}

# Weekly keeps only what the metric layer reads from each rule row, so ~1,000
# tickers x ~240 sessions stay small in memory.
WEEKLY_ROW_COLS = (["date", "ticker", "eligible", "rv20"] + br.RULE_IDS
                   + ["a_broker", "a_nl60_adv", "a_gap", "range60", "val20"])

log = logging.getLogger("broker_learning_run")


# ── small helpers ──────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tickers_arg(text):
    """'a, B,,c' -> ['A', 'B', 'C'] (order kept, duplicates dropped); '' -> []."""
    out = []
    for t in (text or "").split(","):
        t = t.strip().upper()
        if t and t not in out:
            out.append(t)
    return out


def _reason(e):
    """A short, log-safe failure reason. Payload errors describe the data, and
    the cache path never involves a URL, so the message itself is safe."""
    return f"{type(e).__name__}: {str(e)[:REASON_MAX]}"


def _jsonable(x):
    """ctx values as plain JSON types; NaN/inf/NA -> None (missing, never 0)."""
    if x is None or isinstance(x, str):
        return x
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return float(x) if math.isfinite(float(x)) else None
    if x is pd.NA or x is pd.NaT:
        return None
    if isinstance(x, pd.Timestamp):
        return x.strftime("%Y-%m-%d")
    return str(x)


def _feature_value(v):
    v = _jsonable(v)
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 2 ** 53:
            return int(v)                   # lots and round prices: exact
        return float(f"{v:.8g}")            # ratios and costs: 8 digits is ample
    return v


def features_json(row, rule_id):
    """Compact JSON of the explain columns behind one rule (see RULE_FEATURES)."""
    cols = FEATURE_COMMON + RULE_FEATURES.get(rule_id, tuple(br.EXPLAIN_FLOAT + br.EXPLAIN_STR))
    return json.dumps({c: _feature_value(row.get(c)) for c in cols},
                      separators=(",", ":"), allow_nan=False)


def _records(frame, cols=None):
    if frame is None or len(frame) == 0:
        return []
    if cols is not None:
        frame = frame[[c for c in cols if c in frame.columns]]
    return [_jsonable(r) for r in frame.to_dict("records")]


# ── per ticker ─────────────────────────────────────────────────────────────

def load_ticker(ticker, data, regimes):
    """Bundle + rule rows for one cached payload. Raises bb.PayloadError."""
    bundle = bb.ticker_bundle(data, ticker, regimes)
    return {"ticker": ticker, "bundle": bundle, "rows": br.evaluate(bundle["state"])}


def _empty_payload(t, data, empty):
    """True (and t recorded in `empty`) for a payload with zero sessions."""
    if bc.session_count(data) != 0:
        return False
    empty[t] = EMPTY_REASON
    return True


def load_items(tickers, mode, raw_dir, legacy, regimes, failed, empty=None):
    """[item] for every requested ticker whose cache file loads and validates.

    A missing or unreadable file or a payload that fails validation goes to
    `failed` with a reason; the run renders without it rather than stopping. A payload with
    zero sessions goes to `empty` instead (module docstring).
    """
    items, seen, unreadable = [], set(), {}
    empty = {} if empty is None else empty
    for t, data in bc.iter_cached(tickers, mode, raw_dir=raw_dir, legacy=legacy,
                                  unreadable=unreadable):
        seen.add(t)
        if _empty_payload(t, data, empty):
            continue
        try:
            items.append(load_ticker(t, data, regimes))
        except bb.PayloadError as e:
            failed[t] = _reason(e)
    failed.update(unreadable)
    for t in tickers or []:
        if t not in seen and t not in failed:
            failed[t] = "no cached payload"
    return items


def _last_date(item):
    dates = item["bundle"]["ohlc"]["date"]
    return str(dates.iloc[-1]) if len(dates) else None


def _true(v):
    """Only a real boolean True counts; a missing or NaN flag is not a signal."""
    return isinstance(v, (bool, np.bool_)) and bool(v)


def _fired(last):
    return [r for r in br.RULE_IDS if _true(last.get(r))]


def _score(rows, weights):
    return float(br.composite_score(rows.iloc[[-1]], weights).iloc[0])


def live_signal_rows(items, weights, data_through, captured_utc):
    """live_signals rows: one per rule for each eligible ticker whose last
    session is data_through (see "fresh tickers" in the module docstring)."""
    out = []
    for item in items:
        rows = item["rows"]
        if not len(rows) or _last_date(item) != data_through:
            continue
        last = rows.iloc[-1]
        if not _true(last["eligible"]):
            continue
        score = _score(rows, weights)
        for rule_id in br.RULE_IDS:
            out.append({
                "session_date": str(last["date"]), "ticker": item["ticker"],
                "ruleset": br.RULESET, "rule_id": rule_id,
                "fired": int(_true(last[rule_id])), "score": score,
                "captured_utc": captured_utc, "features": features_json(last, rule_id),
            })
    return out


def _book_rows(book):
    if book is None or len(book) == 0:
        return []
    top = book.assign(_abs=book["position_lots"].abs()).sort_values(
        ["_abs", "broker"], ascending=[False, True], kind="stable").head(BOOK_ROWS)
    return _records(top, ["broker", "position_lots", "avg_cost", "realized_rp", "unrealized_rp",
                          "total_rp", "pct_vs_cost", "net1d", "net5d", "bavg20", "savg20"])


def _facts(book):
    """Largest holder and their cost, biggest winner, who is distributing."""
    if book is None or len(book) == 0:
        return {}
    facts = {}
    pos = book[book["position_lots"] > 0]
    if len(pos):
        r = pos.sort_values(["position_lots", "broker"], ascending=[False, True]).iloc[0]
        facts["top_holder"] = _jsonable({"broker": r["broker"], "position_lots": r["position_lots"],
                                         "avg_cost": r["avg_cost"]})
    total = pd.to_numeric(book["total_rp"], errors="coerce")
    win = book[total > 0]
    if len(win):
        r = win.assign(_t=total[total > 0]).sort_values(["_t", "broker"],
                                                        ascending=[False, True]).iloc[0]
        facts["biggest_winner"] = _jsonable({"broker": r["broker"], "total_rp": r["total_rp"]})
    dist = book[book["net5d"] < 0].sort_values(["net5d", "broker"]).head(DISTRIBUTORS)
    facts["distributing"] = _records(dist, ["broker", "net5d"])
    return facts


def ticker_ctx(item, weights, data_through=None):
    """One dashboard card. A ticker whose last session is older than the run's
    data_through keeps its book and chart but shows no signal: rules that
    fired on an old session are not today's signals."""
    b, rows = item["bundle"], item["rows"]
    close = b["ohlc"]["close"].astype(float)
    last = rows.iloc[-1] if len(rows) else pd.Series(dtype=object)
    c1 = float(close.iloc[-1]) if len(close) else np.nan
    c0 = float(close.iloc[-2]) if len(close) > 1 else np.nan
    current = len(rows) > 0 and (data_through is None or _last_date(item) == data_through)
    return _jsonable({
        "ticker": item["ticker"],
        "close": c1,
        "change_1d": c1 / c0 - 1 if c0 > 0 and c1 > 0 else None,
        "data_through": _last_date(item),
        "anchor": b["anchor"], "basis_ok": bool(b["basis_ok"]),
        "eligible": _true(last.get("eligible")),
        "fired": _fired(last) if current else [],
        "score": _score(rows, weights) if current else None,
        "explain": br.explain(last) if current else [],
        "book": _book_rows(b["book"]),
        "curves": b["curves"],
        "facts": _facts(b["book"]),
    })


def _ticker_order(t):
    score = t.get("score") or 0.0
    return (0 if t.get("fired") else 1, -abs(score), t.get("ticker") or "")


# ── dashboard context from the DB ──────────────────────────────────────────

def _weekly_as_of(conn):
    # rule_stats gets all 24 (rule, h) rows every weekly run, even with no
    # event, so its as_of is THE latest weekly run. Every weekly section reads
    # that one as_of: a week with no alpha case must show none, not last week's.
    return bdb.latest_as_of(conn, "rule_stats")


def rules_ctx(conn, as_of):
    stats = bdb.load_rule_stats(conn, as_of) if as_of else pd.DataFrame()
    if len(stats):
        stats = stats[stats["ruleset"] == br.RULESET]
    status = bl.primary_status(stats) if len(stats) else {}
    by_rule = {}
    for rec in _records(stats):
        by_rule.setdefault(rec["rule_id"], {})[str(int(rec["h"]))] = rec
    weights = bdb.load_weights(conn, as_of) if as_of else {}
    prev_as_of = bdb.previous_as_of(conn, "rule_weights")
    prev = bdb.load_weights(conn, prev_as_of) if prev_as_of else {}
    live = {h: bdb.live_summary(conn, h=h) for h in sorted(set(br.PRIMARY_H.values()))}
    net = _weekly_note(conn, as_of).get("net_trade_stats") or {}
    out = []
    for r in br.RULES:
        rid, h = r["id"], br.PRIMARY_H[r["id"]]
        lv = {"h": h, "n_signals": 0, "n_scored": 0, "mean_excess": None,
              "hit_rate": None, "base_rate": None, "susp_rate": None}
        row = live[h][live[h]["rule_id"] == rid]
        if len(row):
            lv.update(_records(row.drop(columns="rule_id"))[0])
        out.append(_jsonable({
            **r, "primary_h": h,
            "weight": weights.get(rid, 1.0), "prev_weight": prev.get(rid),
            "status": status.get(rid), "stats_as_of": as_of,
            "stats": by_rule.get(rid, {}), "live": lv,
            "net": net.get(rid) if isinstance(net, dict) else None,
        }))
    return out, weights


def learned_lines(rules):
    """'R4 bobot 1,00 -> 1,07 (n=12 tanggal, LOW_N)' for every rule that has a
    weight from the previous weekly run to compare with."""
    out = []
    for r in rules:
        w, p = r.get("weight"), r.get("prev_weight")
        if w is None or p is None:
            continue
        st = (r.get("stats") or {}).get(str(r.get("primary_h")), {})
        # an h = 60 number always carries its suspension share (Amendment A2)
        susp = (f", kena suspensi {bd.fmt_pct(st.get('susp_rate'), 0)}"
                if r.get("primary_h") == bl.HOLD_H else "")
        out.append(f"{r['id']} bobot {bd.fmt_num(p, 2)} -> {bd.fmt_num(w, 2)} "
                   f"(n={bd.fmt_int(st.get('n_dates'))} tanggal{susp}, "
                   f"{r.get('status') or 'belum dinilai'})")
    return out


def _weekly_note(conn, as_of):
    """The JSON note of the earliest successful weekly run for as_of, or {}."""
    if not as_of:
        return {}
    row = conn.execute(
        "SELECT note FROM runs WHERE kind = 'weekly' AND status = 'ok' AND data_through = ? "
        "ORDER BY started_utc LIMIT 1", (as_of,)).fetchone()
    try:
        note = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def alpha_ctx(conn, as_of):
    """Cases (largest hold_60 first) with their suspension and visibility
    flags, the lift table (visible cases only, see broker_learning), and the
    universe's h = 60 big-move rate and suspension share from runs.note."""
    cases = bdb.load_alpha_cases(conn, as_of) if as_of else pd.DataFrame()
    lift = bdb.load_broker_lift(conn, as_of) if as_of else pd.DataFrame()
    if len(lift):
        lift = lift[pd.to_numeric(lift["n_cases_top"]) >= LIFT_MIN_CASES]
        lift = lift.assign(_l=pd.to_numeric(lift["lift"], errors="coerce")).sort_values(
            ["_l", "broker"], ascending=[False, True], na_position="last").head(LIFT_SHOWN)
    visible = pd.to_numeric(cases["visible"], errors="coerce").eq(1) if len(cases) else None
    susp = pd.to_numeric(cases["susp_60"], errors="coerce") if len(cases) else None
    note = _weekly_note(conn, as_of)
    return {
        "as_of": as_of, "n_cases": int(len(cases)),
        "n_visible": int(visible.sum()) if len(cases) else 0,
        "susp_rate": _jsonable(susp.mean()) if len(cases) and susp.notna().any() else None,
        "cases": _records(cases.head(ALPHA_CASES_SHOWN) if len(cases) else cases,
                          ["ticker", "session_date", "hold_60", "susp_60", "sessions_before",
                           "visible", "rules_at_t", "rules_prior20", "top_broker",
                           "top_nl60_adv", "top_cost_gap", "range60", "val20"]),
        "lift": _records(lift, ["broker", "n_cases_top", "n_rows_top", "case_share",
                                "row_share", "lift"]),
        "base": {"big_base_rate_60": _jsonable(note.get("big_base_rate_60")),
                 "susp_rate_60": _jsonable(note.get("susp_rate_60"))},
    }


def brokers_ctx(conn, as_of):
    return {
        "as_of": as_of,
        "scores": _records(bdb.load_broker_scores(conn, as_of) if as_of else None),
        "profitability": _records(bdb.load_profitability(conn, as_of) if as_of else None),
    }


def build_ctx(conn, items, run, generated):
    """The broker_dashboard.render() context (contract: BROKER_LEARNING.md §6)."""
    as_of = _weekly_as_of(conn)
    rules, weights = rules_ctx(conn, as_of)
    dates = [d for d in (_last_date(i) for i in items) if d]
    data_through = max(dates) if dates else None
    tickers = sorted((ticker_ctx(i, weights, data_through) for i in items), key=_ticker_order)
    return {
        "generated_utc": generated.strftime("%Y-%m-%dT%H:%MZ"),
        "data_through": data_through,
        "ruleset": br.RULESET,
        "run": _jsonable(run),
        "rules": rules,
        "tickers": tickers,
        "brokers": brokers_ctx(conn, as_of),
        "alpha": alpha_ctx(conn, as_of),
        "learned": learned_lines(rules),
        "disclaimer": bd.DISCLAIMER,
    }


def _stale_warnings(items, data_through):
    out = []
    for i in items:
        d = _last_date(i)
        if d and data_through and d < data_through:
            out.append(f"{i['ticker']}: data s.d. {d}, lebih lama dari {data_through}; "
                       "sinyal tidak ditampilkan dan tidak dicatat di ledger live")
    return out


def _deliver(ctx, out_dir, dry_run):
    """Render, write, send. Returns (path, sent) with sent None on a dry run."""
    path = bd.write(bd.render(ctx), out_dir=out_dir)
    log.info(f"dashboard written: {path} ({os.path.getsize(path):,} bytes)")
    if dry_run:
        log.info("dry run: not sent to Telegram")
        return path, None
    sent = bd.send_from_env(path, bd.caption(ctx))
    log.info("telegram: sent" if sent else "telegram: NOT sent")
    return path, sent


# ── daily ──────────────────────────────────────────────────────────────────

class _LedgerDb:
    """The DB path a daily run writes to (see "dry run" in the module docstring)."""

    def __init__(self, explicit, dry_run):
        self.explicit, self.dry_run, self.tmp = explicit, dry_run, None

    def __enter__(self):
        if self.explicit:
            return self.explicit
        if not self.dry_run:
            return bdb.DB_PATH
        self.tmp = tempfile.mkdtemp(prefix="broker_learning_dry_")
        path = os.path.join(self.tmp, "broker_learning.db")
        if os.path.exists(bdb.DB_PATH):
            shutil.copy2(bdb.DB_PATH, path)
        log.info("dry run without --db: ledger writes go to a temporary copy")
        return path

    def __exit__(self, *exc):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def _fetch(tickers, mode, raw_dir, failed, conn, run_id, empty):
    """collect() -> the tickers that fetched cleanly; failures go to `failed`
    and zero-session tickers to `empty`. A window abort is recorded before the
    SystemExit propagates."""
    try:
        result = bc.collect(tickers, mode, raw_dir=raw_dir)
    except SystemExit:
        bdb.finish_run(conn, run_id, finished_utc=_stamp(_now()), status="aborted",
                       note="short-window abort: the API's rolling window moved")
        raise
    failed.update(result["failed"])
    empty.update(result.get("empty") or {})
    return list(result["ok"])


def run_daily(a):
    started = _now()
    stamp = _stamp(started)
    t0 = time.time()
    tickers = _tickers_arg(a.tickers) or bc.load_watchlist()
    regimes = bb.load_basis_regimes()
    with _LedgerDb(a.db, a.dry_run) as db_path:
        conn = bdb.connect(db_path)
        try:
            return _daily(a, conn, tickers, regimes, started, stamp, t0)
        finally:
            conn.close()


def _daily(a, conn, tickers, regimes, started, stamp, t0):
    run_id = bdb.start_run(conn, "daily", stamp)
    failed, empty, warnings = {}, {}, []
    try:
        if a.no_fetch:
            load = tickers
            warnings.append("--no-fetch: data dari cache lokal, tidak diambil ulang")
        else:
            load = _fetch(tickers, bc.MODE_DAILY, a.raw_dir, failed, conn, run_id, empty)
        items = load_items(load, bc.MODE_DAILY, a.raw_dir, a.legacy_cache, regimes, failed,
                           empty)
        dates = [d for d in (_last_date(i) for i in items) if d]
        data_through = max(dates) if dates else None
        warnings += _stale_warnings(items, data_through)

        weights = bdb.load_weights(conn, _weekly_as_of(conn))
        live = live_signal_rows(items, weights, data_through, stamp)
        n_live = bdb.insert_rows(conn, "live_signals", live)
        n_out = 0
        if items:
            outs = bl.outcomes(pd.concat([i["bundle"]["ohlc"] for i in items], ignore_index=True))
            scored = bl.live_outcome_rows(bdb.pending_live(conn), outs, stamp)
            n_out = bdb.insert_rows(conn, "live_outcomes", scored)
        log.info(f"live ledger: {n_live} new signal rows ({len(live)} offered), "
                 f"{n_out} new outcome rows")

        n_fail = len(failed)
        run = {"kind": "daily", "tickers_ok": len(items), "tickers_fail": n_fail,
               "failed": failed, "empty": empty, "warnings": warnings}
        ctx = build_ctx(conn, items, run, started)
        fired = [t["ticker"] for t in ctx["tickers"] if t.get("fired")]
        log.info(f"data through {data_through}: {len(items)} tickers OK, {n_fail} failed, "
                 f"{len(empty)} empty, fired: {', '.join(fired) or 'none'}")
        _, sent = _deliver(ctx, a.out, a.dry_run)

        # empty tickers are attempted (denominator), not failed (numerator),
        # but a mostly-empty run is a broken one (broker_collect docstring)
        too_empty = bc.too_many_empty(len(empty), len(tickers))
        failing = should_fail_run(n_fail, len(tickers)) or not items or too_empty
        status = "failed" if failing else ("unsent" if sent is False else "ok")
        note = {"live_signals_new": n_live, "live_outcomes_new": n_out, "fired": fired,
                "sent": sent, "failed": dict(list(sorted(failed.items()))[:20]),
                "empty": sorted(empty)[:20]}
        bdb.finish_run(conn, run_id, finished_utc=_stamp(_now()), status=status,
                       tickers_ok=len(items), tickers_fail=n_fail, data_through=data_through,
                       note=json.dumps(_jsonable(note), separators=(",", ":")))
        log.info(f"daily done in {time.time() - t0:.1f}s: {status}")
        if failing:
            log.error(f"{len(empty)}/{len(tickers)} tickers with 0 sessions: over the empty limit"
                      if too_empty else
                      f"{n_fail}/{len(tickers)} tickers failed: over the failure limit")
        return 1 if status != "ok" else 0
    except SystemExit:
        raise
    except BaseException as e:
        bdb.finish_run(conn, run_id, finished_utc=_stamp(_now()), status="error",
                       note=type(e).__name__)
        raise


# ── weekly ─────────────────────────────────────────────────────────────────

class _Weekly:
    """Accumulates one ticker at a time, keeping only what the metric layer and
    the history export need (the full broker frames are dropped per ticker)."""

    def __init__(self, overlap=HISTORY_OVERLAP):
        self.overlap = overlap
        self.rows, self.events, self.ohlc, self.history = [], [], [], []
        self.books = {}

    def fresh_books(self, as_of):
        """{ticker: book} for the tickers whose data reaches as_of. A ticker
        that stopped trading months ago (suspended, delisted) would otherwise
        enter a "mark-to-market" P/L marked at a close from months before, the
        same staleness the daily path refuses to show as current."""
        return {t: book for t, (last, book) in self.books.items() if last == as_of}

    def add(self, item):
        b, rows = item["bundle"], item["rows"]
        self.rows.append(rows[WEEKLY_ROW_COLS])
        self.events.append(br.track_record_events(b["state"], rows["eligible"].to_numpy()))
        self.ohlc.append(b["ohlc"])
        # §4.5: profitability is over tickers eligible at the run's last
        # session with a clean basis. as_of is only known once every ticker is
        # in, so the book keeps its own last date and fresh_books() filters.
        if b["basis_ok"] and len(rows) and _true(rows["eligible"].iloc[-1]):
            self.books[item["ticker"]] = (_last_date(item), b["book"])
        dates = b["ohlc"]["date"]
        if len(dates):
            since = dates.iloc[max(0, len(dates) - self.overlap)]
            brk = b["brokers"]
            self.history.append((brk[brk["date"] >= since], b["ohlc"][dates >= since]))


def write_history(bundles_frames, as_of, out_dir=HISTORY_DIR, overlap_sessions=HISTORY_OVERLAP,
                  captured_utc=None):
    """Broker and OHLC rows of the LAST `overlap_sessions` sessions of the fetch
    to <out_dir>/broker_<as_of>.parquet and ohlc_<as_of>.parquet (zstd, with a
    captured_utc column). bundles_frames: iterable of (brokers, ohlc) frames.

    "Sessions of the fetch" is the union date axis across tickers, so a
    suspended ticker adds nothing older than the window. Returns the paths
    written ([] when there is nothing to write).
    """
    pairs = list(bundles_frames)
    ohlc = [o for _, o in pairs if o is not None and len(o)]
    if not ohlc:
        return []
    ohlc = pd.concat(ohlc, ignore_index=True)
    keep = set(sorted(ohlc["date"].unique())[-overlap_sessions:])
    brokers = [b for b, _ in pairs if b is not None and len(b)]
    brokers = (pd.concat(brokers, ignore_index=True) if brokers
               else bb.empty_brokers())
    captured_utc = captured_utc or _stamp(_now())
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for name, frame, key in (("broker", brokers, ["date", "ticker", "broker"]),
                             ("ohlc", ohlc, ["date", "ticker"])):
        frame = frame[frame["date"].isin(keep)].sort_values(key, kind="stable")
        frame = frame.assign(captured_utc=captured_utc).reset_index(drop=True)
        path = os.path.join(out_dir, f"{name}_{as_of}.parquet")
        frame.to_parquet(path, engine="pyarrow", compression="zstd", index=False)
        paths.append(path)
    return paths


def learn(rows, events, ohlc, books, as_of, window):
    """The weekly metric layer: {table: rows} plus the numbers for runs.note."""
    t = time.time()
    outs = bl.outcomes(ohlc)
    rows_x = bl.attach_excess(rows, outs)
    log.info(f"outcomes + excess: {len(rows_x):,} rows in {time.time() - t:.1f}s")
    ev_x = events.merge(rows_x[["date", "ticker"] + bl.X_COLS], on=["date", "ticker"],
                        how="left", validate="many_to_one")
    cases = bl.alpha_cases(rows_x, as_of)
    tables = {
        "rule_stats": bl.rule_stats(rows_x, as_of, window, ruleset=br.RULESET),
        "rule_weights": bl.rule_weights(rows_x, as_of, ruleset=br.RULESET),
        "broker_scores": bl.broker_scores(ev_x, as_of),
        "broker_profitability": bl.broker_profitability(books, as_of),
        "alpha_cases": cases,
        "broker_lift": bl.broker_lift(rows_x, cases, as_of),
    }
    elig = rows_x["eligible"].eq(True)
    hold60 = pd.to_numeric(rows_x[bl.ALPHA_RET], errors="coerce")
    susp60 = pd.to_numeric(rows_x[bl.SUSP_COL], errors="coerce")
    base = elig & hold60.notna()
    note = {
        "n_rows": int(len(rows_x)), "n_eligible": int(elig.sum()),
        "n_events": int(len(events)), "n_cases": len(cases),
        "n_cases_visible": sum(1 for c in cases if c["visible"] == 1),
        "n_rows_hold60": int(base.sum()),
        "big_base_rate_60": float((hold60[base] >= bl.ALPHA_MIN).mean()) if base.any() else None,
        "susp_rate_60": float(susp60[base].mean()) if base.any() else None,
        "fired": {r: int((elig & rows_x[r].eq(True)).sum()) for r in br.RULE_IDS},
        # §4.5's informational net-of-cost trade stats (dir = +1 rules, primary h):
        # no schema column holds them, so they ride in the note like the base rate.
        "net_trade_stats": bl.net_trade_stats(rows_x),
    }
    log.info(f"metric layer done in {time.time() - t:.1f}s")
    return tables, note


def run_weekly(a):
    started = _now()
    stamp = _stamp(started)
    t0 = time.time()
    regimes = bb.load_basis_regimes()
    conn = bdb.connect(a.db or bdb.DB_PATH)
    try:
        return _weekly(a, conn, regimes, stamp, t0)
    finally:
        conn.close()


def _weekly(a, conn, regimes, stamp, t0):
    run_id = bdb.start_run(conn, "weekly", stamp)
    failed, empty = {}, {}
    try:
        requested = _tickers_arg(a.tickers)
        if a.no_fetch:
            load = requested or None          # None: every cached file
        else:
            load = _fetch(requested or bc.load_universe(), bc.MODE_MARKET, a.raw_dir,
                          failed, conn, run_id, empty)
        acc, seen, unreadable = _Weekly(), set(), {}
        for t, data in bc.iter_cached(load, bc.MODE_MARKET, raw_dir=a.raw_dir,
                                      legacy=a.legacy_cache, unreadable=unreadable):
            seen.add(t)
            if _empty_payload(t, data, empty):
                continue
            try:
                acc.add(load_ticker(t, data, regimes))
            except bb.PayloadError as e:
                failed[t] = _reason(e)
            del data
            if len(seen) % PROGRESS_EVERY == 0:
                log.info(f"[{len(seen)}] tickers evaluated ({time.time() - t0:.0f}s)")
        # With load None (every cached file) there is no list to diff against,
        # so a corrupt file is only counted because iter_cached reports it.
        failed.update(unreadable)
        for t in load or []:
            if t not in seen and t not in failed:
                failed[t] = "no cached payload"
        # Every ticker asked for is evaluated, in `failed` (fetch, payload or
        # missing file) or in `empty` (zero sessions), so ok + failed + empty
        # is the attempted total. Empty ones are attempted, not failed.
        n_ok, n_fail, n_empty = len(acc.rows), len(failed), len(empty)
        n_total = n_ok + n_fail + n_empty
        log.info(f"evaluated {n_ok} tickers, {n_fail} failed, {n_empty} empty, "
                 f"in {time.time() - t0:.1f}s")

        too_empty = bc.too_many_empty(n_empty, n_total)
        if not acc.ohlc or should_fail_run(n_fail, n_total) or too_empty:
            reason = (f"{n_empty}/{n_total} tickers with 0 sessions: over the empty limit"
                      if too_empty else
                      f"{n_fail}/{n_total} tickers failed: over the failure limit")
            bdb.finish_run(conn, run_id, finished_utc=_stamp(_now()), status="failed",
                           tickers_ok=n_ok, tickers_fail=n_fail,
                           note=json.dumps({"reason": reason, "n_empty": n_empty,
                                            "failed": dict(list(sorted(failed.items()))[:20]),
                                            "empty": sorted(empty)[:20]}))
            log.error(f"{reason}, no weekly table written")
            return 1

        rows = pd.concat(acc.rows, ignore_index=True)
        events = pd.concat(acc.events, ignore_index=True)
        ohlc = pd.concat(acc.ohlc, ignore_index=True)
        acc.rows = acc.events = acc.ohlc = None
        as_of = str(ohlc["date"].max())
        window = (str(ohlc["date"].min()), as_of)
        books = acc.fresh_books(as_of)
        n_stale = len(acc.books) - len(books)
        if n_stale:
            log.info(f"profitability: {n_stale} eligible book(s) end before {as_of}, left out")
        tables, note = learn(rows, events, ohlc, books, as_of, window)
        note.update({"n_books": len(books), "n_books_stale": n_stale})
        inserted = {name: bdb.insert_rows(conn, name, recs) for name, recs in tables.items()}
        log.info("inserted: " + ", ".join(f"{k}={v}" for k, v in inserted.items()))
        paths = write_history(acc.history, as_of, out_dir=a.history_dir, captured_utc=stamp)
        acc.history = None
        note.update({"inserted": inserted, "history": [os.path.basename(p) for p in paths],
                     "failed": dict(list(sorted(failed.items()))[:20]),
                     "n_empty": n_empty, "empty": sorted(empty)[:20]})
        bdb.finish_run(conn, run_id, finished_utc=_stamp(_now()), status="ok",
                       tickers_ok=n_ok, tickers_fail=n_fail, data_through=as_of,
                       note=json.dumps(_jsonable(note), separators=(",", ":")))
        log.info(f"weekly done in {time.time() - t0:.1f}s, as_of {as_of}")
        return 0
    except SystemExit:
        raise
    except BaseException as e:
        bdb.finish_run(conn, run_id, finished_utc=_stamp(_now()), status="error",
                       note=type(e).__name__)
        raise


# ── render ─────────────────────────────────────────────────────────────────

def run_render(a):
    started = _now()
    tickers = _tickers_arg(a.tickers) or bc.load_watchlist()
    path = a.db or bdb.DB_PATH
    # Read-only in spirit: a missing DB is not created, the page just has no
    # learned sections yet.
    conn = bdb.connect(path if os.path.exists(path) else ":memory:")
    try:
        failed, empty = {}, {}
        items = load_items(tickers, bc.MODE_DAILY, a.raw_dir, a.legacy_cache,
                           bb.load_basis_regimes(), failed, empty)
        dates = [d for d in (_last_date(i) for i in items) if d]
        run = {"kind": "render", "tickers_ok": len(items), "tickers_fail": len(failed),
               "failed": failed, "empty": empty,
               "warnings": _stale_warnings(items, max(dates) if dates else None)}
        ctx = build_ctx(conn, items, run, started)
    finally:
        conn.close()
    _, sent = _deliver(ctx, a.out, a.dry_run)
    return 1 if sent is False or should_fail_run(len(failed), len(tickers)) else 0


# ── CLI ────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Broker learning dashboard (BROKER_LEARNING.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("daily", "weekly", "render"):
        p = sub.add_parser(name)
        p.add_argument("--raw-dir", default=bc.RAW_DIR,
                       help="cache root (default broker_learning_raw/)")
        p.add_argument("--legacy-cache", action="store_true",
                       help="read <raw-dir>/<T>.json.gz bare data dicts (inventory_raw/ layout)")
        p.add_argument("--tickers", default="", help="comma list instead of the default set")
        p.add_argument("--db", default=None, help="SQLite path (default broker_learning.db)")
        if name != "render":
            p.add_argument("--no-fetch", action="store_true", help="use the cache only")
        if name != "weekly":
            p.add_argument("--dry-run", action="store_true", help="do not send to Telegram")
            p.add_argument("--out", default=bd.OUT_DIR, help="dashboard output folder")
        else:
            p.add_argument("--history-dir", default=HISTORY_DIR,
                           help="history export folder (default broker_history/)")
    a = ap.parse_args(argv)
    if a.legacy_cache and a.cmd != "render" and not a.no_fetch:
        ap.error("--legacy-cache reads an old cache layout and needs --no-fetch")
    if a.cmd == "daily" and a.no_fetch and not (a.dry_run or a.db):
        # cached sessions are old by the time they are read: rows recorded
        # from them are not prospective, and the committed ledger is insert-only
        ap.error("daily --no-fetch needs --dry-run or an explicit --db: cached data "
                 "must not enter the committed live ledger")
    return a


def _load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Local runs keep secrets in .env (git-ignored), as neobdm_scraper does.

    Without this, a local run that never imports neobdm_scraper (--no-fetch,
    render) could not find the Telegram secrets. setdefault: a real
    environment variable, as in GitHub Actions, always wins.
    """
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _load_dotenv()
    a = parse_args(argv)
    return {"daily": run_daily, "weekly": run_weekly, "render": run_render}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
