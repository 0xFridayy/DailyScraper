"""SQLite schema and I/O for broker_learning.db (BROKER_LEARNING.md §5).

The database is small and committed by CI, so it is also the audit trail of
what the ledger said and when. That is why every write except `runs` is
INSERT OR IGNORE:

  - live_signals / live_outcomes are the PROSPECTIVE ledger. A row is written
    once, on the day it was knowable, and never recomputed or backfilled. A
    rerun later in the day, or after the vendor restates history, must not be
    able to rewrite what was recorded; a missed day simply stays missing.
  - the weekly tables are keyed by as_of (the last session of the weekly
    fetch). Re-running the same week is a no-op, not a quiet overwrite, so
    "weights changed vs last week" always compares two runs that really
    happened.

`runs` is the one exception: it is a run log, and finish_run() updates the
row start_run() opened.

live_outcomes stores each row's own return, never its excess over the
watchlist: that excess depends on the rest of the session, and at h = 60 a
ticker whose exit a suspension delays is recorded weeks after the others.
live_summary() computes it at read time from everything recorded for the
session (broker_learning.live_outcome_rows says why).

Amendment A2 columns: at h = 60 every return is the holder's hold_60, so
live_outcomes.susp, rule_stats.susp_rate and alpha_cases.susp_60 record how
often that return bridged a suspension (NULL at the horizons that never
bridge one), and alpha_cases.visible says whether the case's accumulation
phase lies inside the data at all.

NaN is stored as NULL. A missing statistic is missing, never 0. Pure stdlib
sqlite3 + pandas; no network, no scraper import.
"""

import os
import re
import sqlite3

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "broker_learning.db")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, kind TEXT, started_utc TEXT, finished_utc TEXT,
        status TEXT, tickers_ok INT, tickers_fail INT, data_through TEXT, note TEXT)""",
    # eligible rows only
    """CREATE TABLE IF NOT EXISTS live_signals (
        session_date TEXT, ticker TEXT, ruleset TEXT, rule_id TEXT,
        fired INT, score REAL, captured_utc TEXT, features TEXT,
        PRIMARY KEY (session_date, ticker, ruleset, rule_id))""",
    """CREATE TABLE IF NOT EXISTS live_outcomes (
        session_date TEXT, ticker TEXT, h INT, fwd_oo REAL, susp INT,
        exit_date TEXT, recorded_utc TEXT,
        PRIMARY KEY (session_date, ticker, h))""",
    """CREATE TABLE IF NOT EXISTS rule_stats (
        as_of TEXT, ruleset TEXT, rule_id TEXT, h INT, n_events INT, n_dates INT,
        mean_excess REAL, ci_lo REAL, ci_hi REAL, hit_rate REAL, base_rate REAL,
        hit_edge REAL, daily_hit_edge REAL, big_rate REAL, big_base_rate REAL,
        susp_rate REAL, low_n INT, status TEXT, window_start TEXT, window_end TEXT,
        PRIMARY KEY (as_of, ruleset, rule_id, h))""",
    # retrospective and descriptive: chosen by the future return (spec §4.6)
    """CREATE TABLE IF NOT EXISTS alpha_cases (
        as_of TEXT, ticker TEXT, session_date TEXT, hold_60 REAL, susp_60 INT,
        sessions_before INT, visible INT, rules_at_t TEXT, rules_prior20 TEXT, top_broker TEXT, top_nl60_adv REAL,
        top_cost_gap REAL, range60 REAL, val20 REAL,
        PRIMARY KEY (as_of, ticker, session_date))""",
    """CREATE TABLE IF NOT EXISTS rule_weights (
        as_of TEXT, ruleset TEXT, rule_id TEXT, weight REAL, n_dates INT,
        avg_excess_pct REAL,
        PRIMARY KEY (as_of, ruleset, rule_id))""",
    """CREATE TABLE IF NOT EXISTS broker_scores (
        as_of TEXT, broker TEXT, side TEXT, h INT, n_events INT, n_dates INT,
        n_tickers INT, mean_excess REAL, ci_lo REAL, ci_hi REAL, shrunk REAL,
        low_n INT,
        PRIMARY KEY (as_of, broker, side, h))""",
    """CREATE TABLE IF NOT EXISTS broker_profitability (
        as_of TEXT, broker TEXT, n_tickers INT, total_pnl_rp REAL,
        turnover_rp REAL, pnl_per_turnover REAL, share_profitable REAL,
        PRIMARY KEY (as_of, broker))""",
    """CREATE TABLE IF NOT EXISTS broker_lift (
        as_of TEXT, broker TEXT, n_cases_top INT, n_rows_top INT, case_share REAL,
        row_share REAL, lift REAL,
        PRIMARY KEY (as_of, broker))""",
]

TABLES = tuple(re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", s).group(1) for s in SCHEMA)
AS_OF_TABLES = ("rule_stats", "alpha_cases", "rule_weights", "broker_scores",
                "broker_profitability", "broker_lift")
LIVE_HORIZONS = (5, 10, 20, 60)   # broker_learning.HORIZONS; R6 is judged at 60
LIVE_SUMMARY_COLS = ["rule_id", "n_signals", "n_scored", "mean_excess",
                     "hit_rate", "base_rate", "susp_rate"]


# ── Schema ─────────────────────────────────────────────────────────────────

def ensure_schema(conn):
    for statement in SCHEMA:
        conn.execute(statement)
    conn.commit()


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    ensure_schema(conn)
    return conn


def _check_table(table, allowed=TABLES):
    # Table names cannot be bound as parameters, so they are only ever taken
    # from this fixed list before being put into SQL text.
    if table not in allowed:
        raise ValueError(f"unknown table {table!r}; expected one of {', '.join(allowed)}")


def _columns(conn, table):
    """[(name, is_primary_key)] in declared order."""
    _check_table(table)
    return [(r[1], bool(r[5])) for r in conn.execute(f"PRAGMA table_info({table})")]


def _sql_value(value):
    """A value sqlite3 can bind: NaN/NA -> NULL, numpy scalars -> Python."""
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return int(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, str):
        return value
    if value is pd.NA or value is pd.NaT:
        return None
    raise TypeError(f"cannot store {type(value).__name__} in broker_learning.db")


# ── Writes ─────────────────────────────────────────────────────────────────

def insert_rows(conn, table, rows):
    """INSERT OR IGNORE `rows` (dicts) into `table`; returns rows inserted.

    A key that is not a column of the table is an error, not something to
    drop quietly: it is almost always a renamed field, and dropping it would
    store NULL where a number was meant. A primary-key column that is missing
    or NULL is also an error, because SQLite treats NULL keys as distinct and
    OR IGNORE would then insert duplicates instead of ignoring them.
    """
    rows = list(rows)
    if not rows:
        _check_table(table)
        return 0
    cols = _columns(conn, table)
    names = [c for c, _ in cols]
    keys = [c for c, pk in cols if pk]
    params = []
    for row in rows:
        unknown = set(row) - set(names)
        if unknown:
            raise ValueError(f"{table}: unknown column(s) {sorted(unknown)}")
        values = [_sql_value(row.get(c)) for c in names]
        missing_key = [c for c in keys if values[names.index(c)] is None]
        if missing_key:
            raise ValueError(f"{table}: primary key column(s) {missing_key} missing")
        params.append(values)
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO {table} ({', '.join(names)}) "
        f"VALUES ({', '.join('?' * len(names))})",
        params,
    )
    conn.commit()
    return conn.total_changes - before


def start_run(conn, kind, started_utc):
    run_id = f"{kind}-{started_utc}"
    conn.execute(
        "INSERT INTO runs (run_id, kind, started_utc, status) VALUES (?, ?, ?, 'running')",
        (run_id, kind, started_utc),
    )
    conn.commit()
    return run_id


def finish_run(conn, run_id, **fields):
    names = [c for c, _ in _columns(conn, "runs") if c != "run_id"]
    unknown = set(fields) - set(names)
    if unknown:
        raise ValueError(f"runs: unknown field(s) {sorted(unknown)}")
    if not fields:
        return
    cols = sorted(fields)
    cur = conn.execute(
        f"UPDATE runs SET {', '.join(f'{c} = ?' for c in cols)} WHERE run_id = ?",
        [_sql_value(fields[c]) for c in cols] + [run_id],
    )
    if cur.rowcount == 0:
        raise ValueError(f"no run {run_id!r} to finish")
    conn.commit()


# ── Reads ──────────────────────────────────────────────────────────────────

def _frame(conn, sql, params=()):
    cur = conn.execute(sql, params)
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def live_signal_frame(conn):
    return _frame(conn, "SELECT * FROM live_signals ORDER BY session_date, ticker, ruleset, rule_id")


def pending_live(conn, horizons=LIVE_HORIZONS):
    """Every live (session_date, ticker) x h with no live_outcomes row yet.

    The whole eligible cross-section is pending, not only rows where a rule
    fired: the universe base rate and the watchlist mean behind `excess` need
    outcomes for the non-firing rows too. h = 60 is included because R6 is
    judged there; those rows simply stay pending for about three months.
    """
    live = _frame(conn, "SELECT DISTINCT session_date, ticker FROM live_signals")
    done = _frame(conn, "SELECT session_date, ticker, h FROM live_outcomes")
    cols = ["session_date", "ticker", "h"]
    if live.empty:
        return pd.DataFrame(columns=cols)
    grid = live.merge(pd.DataFrame({"h": [int(h) for h in horizons]}), how="cross")
    done["_done"] = True
    merged = grid.merge(done, on=cols, how="left")
    pending = merged[merged["_done"].isna()][cols]
    return pending.sort_values(cols).reset_index(drop=True)


def latest_as_of(conn, table):
    _check_table(table, AS_OF_TABLES)
    return conn.execute(f"SELECT MAX(as_of) FROM {table}").fetchone()[0]


def previous_as_of(conn, table):
    """The second latest as_of, or None: the run "vs last week" compares to."""
    _check_table(table, AS_OF_TABLES)
    row = conn.execute(
        f"SELECT DISTINCT as_of FROM {table} ORDER BY as_of DESC LIMIT 1 OFFSET 1"
    ).fetchone()
    return row[0] if row else None


def _load_as_of(conn, table, as_of, order):
    if as_of is None:
        as_of = latest_as_of(conn, table)
    frame = _frame(conn, f"SELECT * FROM {table} WHERE as_of = ? ORDER BY {order}",
                   (as_of,))
    return frame


def load_rule_stats(conn, as_of=None):
    return _load_as_of(conn, "rule_stats", as_of, "ruleset, rule_id, h")


def load_weights(conn, as_of=None):
    """{rule_id: weight} for as_of (latest if None); {} before the first weekly run."""
    frame = _load_as_of(conn, "rule_weights", as_of, "ruleset, rule_id")
    return {r: float(w) for r, w in zip(frame["rule_id"], frame["weight"]) if w is not None}


def load_broker_scores(conn, as_of=None):
    return _load_as_of(conn, "broker_scores", as_of, "broker, side, h")


def load_profitability(conn, as_of=None):
    return _load_as_of(conn, "broker_profitability", as_of, "broker")


def load_alpha_cases(conn, as_of=None):
    """Cases of one weekly run (latest if None), largest move first."""
    return _load_as_of(conn, "alpha_cases", as_of, "hold_60 DESC, ticker, session_date")


def load_broker_lift(conn, as_of=None):
    return _load_as_of(conn, "broker_lift", as_of, "broker")


def live_summary(conn, h=10):
    """Prospective ledger per rule_id at horizon h.

    n_signals   fired rows recorded
    n_scored    of those, rows with a recorded outcome at h
    mean_excess date-balanced (per-date mean, then across dates), as in the
                retrospective rule_stats. A row's excess is its fwd_oo minus
                the equal-weight mean fwd_oo of every eligible watchlist
                ticker recorded for that session at h (one bucket: the
                watchlist is far too small for volatility quintiles). It is
                computed here, not stored, so a ticker whose h = 60 exit a
                suspension delayed joins its session's benchmark when it is
                recorded, for every row of that session alike.
    hit_rate    share of scored signals with fwd_oo > 0
    base_rate   share of the scored live cross-section (every eligible
                ticker recorded that session) with fwd_oo > 0, on the dates
                the rule has scored signals, so the two are read together
    susp_rate   share of scored signals whose window bridged a suspension;
                NaN where no scored signal carries one (every h but 60)
    """
    sig = _frame(conn, "SELECT session_date, ticker, rule_id, fired FROM live_signals")
    if sig.empty:
        return pd.DataFrame(columns=LIVE_SUMMARY_COLS)
    outs = _frame(conn, "SELECT session_date, ticker, fwd_oo, susp FROM live_outcomes "
                        "WHERE h = ? AND fwd_oo IS NOT NULL", (int(h),))
    for col in ("fwd_oo", "susp"):
        outs[col] = pd.to_numeric(outs[col], errors="coerce")
    universe = sig[["session_date", "ticker"]].drop_duplicates().merge(
        outs, on=["session_date", "ticker"], how="inner")
    universe["excess"] = universe["fwd_oo"] - universe.groupby("session_date")["fwd_oo"].transform("mean")

    rows = []
    for rule_id, grp in sig.groupby("rule_id", sort=True):
        fired = grp[pd.to_numeric(grp["fired"], errors="coerce") == 1]
        scored = fired[["session_date", "ticker"]].merge(
            universe, on=["session_date", "ticker"], how="inner")
        per_date = scored.groupby("session_date")["excess"].mean()
        uni = universe[universe["session_date"].isin(set(scored["session_date"]))]
        rows.append({
            "rule_id": rule_id,
            "n_signals": int(len(fired)),
            "n_scored": int(len(scored)),
            "mean_excess": float(per_date.mean()) if len(per_date) else np.nan,
            "hit_rate": float((scored["fwd_oo"] > 0).mean()) if len(scored) else np.nan,
            "base_rate": float((uni["fwd_oo"] > 0).mean()) if len(uni) else np.nan,
            "susp_rate": float(scored["susp"].mean()) if scored["susp"].notna().any() else np.nan,
        })
    return pd.DataFrame(rows, columns=LIVE_SUMMARY_COLS)
