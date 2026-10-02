"""SQLite schema and I/O for targeted_actor_panel.db, the targeted broker actor panel.

A product of its own, beside broker_learning.db and never inside it. What it
holds is a SELECTOR UNION: for each ticker and discovery_as_of, the brokers
that the 16 v1 selectors (targeted_selectors) expanded to, which is typically
10-25 of the 101 codes. It is not full-universe broker data and nothing here
may be read as if it were (section "NOT FULL UNIVERSE").

DISCOVERY_AS_OF
---------------
The last session of the returned date axis, the session the vendor anchors
C5/C20/C50 at (the last session on or before the requested end_date). Both
requests of a ticker must agree on it, and on the whole axis and OHLC, or the
ticker is refused before anything is written. A snapshot is keyed by
(ticker, discovery_as_of).

TABLES
------
  panel_meta            product identity: what this file is and is not
  panel_runs            one row per collection run
  panel_snapshots       one row per (ticker, discovery_as_of): the accepted union
  panel_captures        the two captures (request groups A and B) behind a
                        snapshot: ordered tokens, the vendor's echo, the brokers
                        each request returned, manifest capture id, digests
  panel_sessions        the returned session axis with its OHLC, once
  observed_brokers      each broker of the union, once, with the request(s)
                        that returned it
  observed_series       each observed broker's six daily series, once per
                        session, however many selectors chose the broker
  selection_status      one row per selector of a snapshot: RESOLVED,
                        UNRESOLVED_BOUNDARY_TIE or INSUFFICIENT_HISTORY
                        (targeted_actor_panel, DERIVED MEMBERSHIP). Only a
                        RESOLVED selector has membership rows; the others say
                        why they have none, so "no rows" is never ambiguous.
                        qualifying_count counts the brokers OF THE RETURNED
                        UNION with a window value of the selector's sign, not
                        the universe's
  selection_membership  ticker -> horizon -> metric -> rank -> broker, window
                        value; DERIVED locally from the union (the vendor
                        returns no rank and no per-selector attribution). Keyed
                        by broker: members with equal window values share a
                        rank (tied = 1), and no tie is broken

COVERAGE
--------
Per (ticker, discovery_as_of, broker, session):

  OBSERVED_NONZERO  the broker was returned and at least one of its six
                    values that session is not zero
  OBSERVED_ZERO     the broker was returned and all six values that session
                    are an explicit zero
  UNOBSERVED        the broker was not returned: it was not in the union

UNOBSERVED is never stored as a row and never becomes zero. coverage() answers
it with values None. A broker whose whole series is zero was still returned:
it is OBSERVED_ZERO every session, never UNOBSERVED.

WRITES
------
A snapshot is written in one transaction or not at all, and never rewritten:
the table is keyed by (ticker, discovery_as_of) like broker_learning.db's
weekly tables. A second capture of the same key with the same content
(content_sha256) is a no-op ("identical"); with different content it is a
"conflict" that the collector refuses loudly rather than overwrite.

NOT FULL UNIVERSE
-----------------
panel_meta says full_universe = false, and every snapshot row carries
coverage_scope TARGETED_SELECTOR_UNION under a CHECK constraint. connect()
refuses any of the repository's other databases, and any file whose
panel_meta does not name this product, so targeted rows cannot land in
broker_learning.db's insert-only ledgers or in neobdm.db. The metrics that
need every broker (broker-summed adv20/val20, active broker counts,
concentration, group totals, market-wide per-session broker ranks, the weekly
learning, profitability and lift) are guarded against it in coverage_guard.

Standard library only.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB_NAME = "targeted_actor_panel.db"
DB_PATH = os.path.join(HERE, DB_NAME)

PRODUCT = "targeted_broker_actor_panel"
SCHEMA_VERSION = "1"
COLLECTION_MODE = "TARGETED_SELECTORS"        # inventory_capture.TARGETED_SELECTORS
COVERAGE_SCOPE = "TARGETED_SELECTOR_UNION"
PROVENANCE = "DERIVED_FROM_SELECTOR_UNION"

OBSERVED_ZERO = "OBSERVED_ZERO"
OBSERVED_NONZERO = "OBSERVED_NONZERO"
UNOBSERVED = "UNOBSERVED"

RESOLVED = "RESOLVED"
UNRESOLVED_BOUNDARY_TIE = "UNRESOLVED_BOUNDARY_TIE"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
SELECTION_STATUSES = (RESOLVED, UNRESOLVED_BOUNDARY_TIE, INSUFFICIENT_HISTORY)
SIDECARS = ("-journal", "-wal", "-shm")     # SQLite's files beside a database

SERIES_FIELDS = ("blot", "bval", "slot", "sval", "nlot", "nval")
OHLC_FIELDS = ("open", "high", "low", "close", "volume", "volume_sma20")

# The repository's other databases. Targeted rows never go into any of them.
FOREIGN_DATABASES = ("broker_learning.db", "neobdm.db", "neobdm_ownership.db",
                     "daily_picks.db", "txchart_history.db", "neobdm_reconcile_test.db")

# One place for the constants the CHECK constraints pin.
_Q = {"mode": COLLECTION_MODE, "scope": COVERAGE_SCOPE, "prov": PROVENANCE}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS panel_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    f"""CREATE TABLE IF NOT EXISTS panel_runs (
        run_id TEXT PRIMARY KEY,
        collection_mode TEXT NOT NULL CHECK (collection_mode = '{_Q["mode"]}'),
        selector_plan TEXT NOT NULL, manifest_run_id TEXT, manifest_path TEXT,
        started_utc TEXT NOT NULL, finished_utc TEXT, status TEXT NOT NULL,
        requested_start_date TEXT, requested_end_date TEXT,
        tickers_requested INT, tickers_ok INT, tickers_identical INT,
        tickers_failed INT, tickers_empty INT, note TEXT)""",
    f"""CREATE TABLE IF NOT EXISTS panel_snapshots (
        ticker TEXT NOT NULL, discovery_as_of TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES panel_runs (run_id),
        collection_mode TEXT NOT NULL CHECK (collection_mode = '{_Q["mode"]}'),
        coverage_scope TEXT NOT NULL CHECK (coverage_scope = '{_Q["scope"]}'),
        selector_plan TEXT NOT NULL,
        requested_start_date TEXT NOT NULL, requested_end_date TEXT NOT NULL,
        investor_type TEXT NOT NULL,
        first_session TEXT NOT NULL, last_session TEXT NOT NULL,
        session_count INT NOT NULL, observed_broker_count INT NOT NULL,
        content_sha256 TEXT NOT NULL, recorded_utc TEXT NOT NULL,
        CHECK (last_session = discovery_as_of),
        PRIMARY KEY (ticker, discovery_as_of))""",
    """CREATE TABLE IF NOT EXISTS panel_captures (
        ticker TEXT NOT NULL, discovery_as_of TEXT NOT NULL,
        request_group TEXT NOT NULL CHECK (request_group IN ('A', 'B')),
        horizons TEXT NOT NULL, selector_tokens TEXT NOT NULL,
        capture_id TEXT NOT NULL, manifest_run_id TEXT NOT NULL, attempt INT NOT NULL,
        query_sha256 TEXT NOT NULL, http_status INT,
        response_bytes INT, response_sha256 TEXT, response_text_sha256 TEXT,
        captured_at TEXT, vendor_success INT NOT NULL CHECK (vendor_success = 1),
        vendor_meta_brokers TEXT NOT NULL,
        expanded_brokers TEXT NOT NULL, unexplained_brokers TEXT NOT NULL,
        source_status TEXT NOT NULL CHECK (source_status = 'ACCEPTED'),
        PRIMARY KEY (ticker, discovery_as_of, request_group),
        FOREIGN KEY (ticker, discovery_as_of) REFERENCES panel_snapshots (ticker, discovery_as_of))""",
    """CREATE TABLE IF NOT EXISTS panel_sessions (
        ticker TEXT NOT NULL, discovery_as_of TEXT NOT NULL,
        session_date TEXT NOT NULL, session_index INT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, volume_sma20 REAL,
        PRIMARY KEY (ticker, discovery_as_of, session_date),
        FOREIGN KEY (ticker, discovery_as_of) REFERENCES panel_snapshots (ticker, discovery_as_of))""",
    """CREATE TABLE IF NOT EXISTS observed_brokers (
        ticker TEXT NOT NULL, discovery_as_of TEXT NOT NULL, broker TEXT NOT NULL,
        in_request_a INT NOT NULL, in_request_b INT NOT NULL,
        nonzero_sessions INT NOT NULL,
        CHECK (in_request_a IN (0, 1) AND in_request_b IN (0, 1)
               AND in_request_a + in_request_b >= 1),
        PRIMARY KEY (ticker, discovery_as_of, broker),
        FOREIGN KEY (ticker, discovery_as_of) REFERENCES panel_snapshots (ticker, discovery_as_of))""",
    f"""CREATE TABLE IF NOT EXISTS observed_series (
        ticker TEXT NOT NULL, discovery_as_of TEXT NOT NULL, broker TEXT NOT NULL,
        session_date TEXT NOT NULL,
        blot INT NOT NULL, bval REAL NOT NULL, slot INT NOT NULL, sval REAL NOT NULL,
        nlot INT NOT NULL, nval REAL NOT NULL,
        coverage TEXT NOT NULL CHECK (coverage IN ('{OBSERVED_ZERO}', '{OBSERVED_NONZERO}')),
        PRIMARY KEY (ticker, discovery_as_of, broker, session_date),
        FOREIGN KEY (ticker, discovery_as_of, broker)
            REFERENCES observed_brokers (ticker, discovery_as_of, broker),
        FOREIGN KEY (ticker, discovery_as_of, session_date)
            REFERENCES panel_sessions (ticker, discovery_as_of, session_date))""",
    f"""CREATE TABLE IF NOT EXISTS selection_status (
        ticker TEXT NOT NULL, discovery_as_of TEXT NOT NULL,
        horizon TEXT NOT NULL CHECK (horizon IN ('C5', 'C20', 'C50', 'ALL')),
        metric TEXT NOT NULL CHECK (metric IN ('NB_LOT', 'NS_LOT', 'NB_VAL', 'NS_VAL')),
        selector_token TEXT NOT NULL,
        request_group TEXT NOT NULL CHECK (request_group IN ('A', 'B')),
        selector_capture_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('{RESOLVED}', '{UNRESOLVED_BOUNDARY_TIE}',
                                               '{INSUFFICIENT_HISTORY}')),
        sessions_required INT, sessions_available INT NOT NULL,
        window_first_session TEXT, window_last_session TEXT,
        qualifying_count INT, member_count INT NOT NULL CHECK (member_count BETWEEN 0 AND 5),
        boundary_tie_value REAL, boundary_tie_brokers TEXT,
        CHECK (status = '{RESOLVED}' OR member_count = 0),
        CHECK ((status = '{UNRESOLVED_BOUNDARY_TIE}') = (boundary_tie_brokers IS NOT NULL)),
        CHECK (status != '{INSUFFICIENT_HISTORY}'
               OR (sessions_required IS NOT NULL AND sessions_available < sessions_required)),
        PRIMARY KEY (ticker, discovery_as_of, horizon, metric),
        FOREIGN KEY (ticker, discovery_as_of) REFERENCES panel_snapshots (ticker, discovery_as_of))""",
    f"""CREATE TABLE IF NOT EXISTS selection_membership (
        ticker TEXT NOT NULL, discovery_as_of TEXT NOT NULL,
        horizon TEXT NOT NULL CHECK (horizon IN ('C5', 'C20', 'C50', 'ALL')),
        metric TEXT NOT NULL CHECK (metric IN ('NB_LOT', 'NS_LOT', 'NB_VAL', 'NS_VAL')),
        rank INT NOT NULL CHECK (rank BETWEEN 1 AND 5),
        broker TEXT NOT NULL, window_value REAL NOT NULL,
        window_first_session TEXT NOT NULL, window_last_session TEXT NOT NULL,
        window_sessions INT NOT NULL, tied INT NOT NULL CHECK (tied IN (0, 1)),
        selector_token TEXT NOT NULL,
        request_group TEXT NOT NULL CHECK (request_group IN ('A', 'B')),
        selector_capture_id TEXT NOT NULL, union_capture_ids TEXT NOT NULL,
        provenance TEXT NOT NULL CHECK (provenance = '{_Q["prov"]}'),
        PRIMARY KEY (ticker, discovery_as_of, horizon, metric, broker),
        FOREIGN KEY (ticker, discovery_as_of, broker)
            REFERENCES observed_brokers (ticker, discovery_as_of, broker),
        FOREIGN KEY (ticker, discovery_as_of, horizon, metric)
            REFERENCES selection_status (ticker, discovery_as_of, horizon, metric))""",
    """CREATE INDEX IF NOT EXISTS membership_by_broker
        ON selection_membership (broker, ticker, discovery_as_of)""",
]

# Independently versioned, additive extension. panel_meta and every v1 table
# retain their exact meaning, so old readers keep working. No live collector
# invokes these APIs. Acceptance markers are written AFTER the source/body
# transaction commits; a missing marker is unknown availability, not recorded_utc.
INVENTORY_EVIDENCE_VERSION = "1"
SCHEMA += [
    """CREATE TABLE IF NOT EXISTS inventory_snapshot_acceptances (
        ticker TEXT NOT NULL, discovery_as_of TEXT NOT NULL,
        content_sha256 TEXT NOT NULL, durable_accepted_at TEXT NOT NULL,
        extension_version TEXT NOT NULL CHECK (extension_version = '1'),
        PRIMARY KEY (ticker, discovery_as_of),
        FOREIGN KEY (ticker, discovery_as_of) REFERENCES panel_snapshots (ticker, discovery_as_of))""",
    """CREATE TABLE IF NOT EXISTS inventory_evidence_revisions (
        ticker TEXT NOT NULL, anchor TEXT NOT NULL, cutoff TEXT NOT NULL,
        observation_revision INTEGER NOT NULL CHECK (observation_revision > 0),
        parent_revision INTEGER, evidence_schema_version TEXT NOT NULL CHECK (evidence_schema_version = '1'),
        availability_cutoff TEXT NOT NULL, content_sha256 TEXT NOT NULL, observation_json TEXT NOT NULL,
        PRIMARY KEY (ticker, anchor, cutoff, observation_revision))""",
    """CREATE TABLE IF NOT EXISTS inventory_evidence_acceptances (
        ticker TEXT NOT NULL, anchor TEXT NOT NULL, cutoff TEXT NOT NULL,
        observation_revision INTEGER NOT NULL, durable_accepted_at TEXT NOT NULL,
        PRIMARY KEY (ticker, anchor, cutoff, observation_revision),
        FOREIGN KEY (ticker, anchor, cutoff, observation_revision)
          REFERENCES inventory_evidence_revisions (ticker, anchor, cutoff, observation_revision))""",
]
for _table in ("inventory_snapshot_acceptances", "inventory_evidence_revisions", "inventory_evidence_acceptances"):
    for _action in ("UPDATE", "DELETE"):
        SCHEMA.append(f"CREATE TRIGGER IF NOT EXISTS immutable_{_table}_{_action.lower()} "
                      f"BEFORE {_action} ON {_table} BEGIN SELECT RAISE(ABORT, 'immutable inventory evidence'); END")

META = {"product": PRODUCT, "schema_version": SCHEMA_VERSION,
        "collection_mode": COLLECTION_MODE, "coverage_scope": COVERAGE_SCOPE,
        "full_universe": "false"}

SNAPSHOT_TABLES = ("panel_captures", "panel_sessions", "observed_brokers",
                   "observed_series", "selection_status", "selection_membership")


class NotTargetedPanelError(ValueError):
    """The path is not, and must not become, a targeted actor panel database."""


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── connection ───────────────────────────────

def check_path(path):
    """`path` unless it names one of the repository's other databases."""
    if os.path.basename(str(path)).lower() in FOREIGN_DATABASES:
        raise NotTargetedPanelError(
            f"{os.path.basename(str(path))} is another product; targeted selector data never "
            f"goes into it (use {DB_NAME})")
    return path


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def ensure_schema(conn):
    """Create the tables and the product identity, or NotTargetedPanelError for
    a database that already holds something else."""
    tables = _tables(conn)
    if tables and "panel_meta" not in tables:
        raise NotTargetedPanelError(
            f"database holds tables {sorted(tables)[:5]} but no panel_meta: not a {PRODUCT}")
    if "panel_meta" in tables:
        meta = dict(conn.execute("SELECT key, value FROM panel_meta"))
        wrong = {k: meta.get(k) for k, v in META.items() if meta.get(k) not in (None, v)}
        if wrong or meta.get("product") != PRODUCT:
            raise NotTargetedPanelError(f"panel_meta is not this product's: {wrong or meta}")
    with conn:
        for statement in SCHEMA:
            conn.execute(statement)
        conn.executemany("INSERT OR IGNORE INTO panel_meta (key, value) VALUES (?, ?)",
                         sorted(META.items()))


def connect(path=DB_PATH):
    conn = sqlite3.connect(check_path(path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        ensure_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def panel_meta(conn):
    return dict(conn.execute("SELECT key, value FROM panel_meta"))


# ── runs ─────────────────────────────────────

RUN_FIELDS = ("manifest_run_id", "manifest_path", "finished_utc", "status",
              "requested_start_date", "requested_end_date", "tickers_requested",
              "tickers_ok", "tickers_identical", "tickers_failed", "tickers_empty", "note")


def start_run(conn, run_id, selector_plan, started_utc, **fields):
    unknown = set(fields) - set(RUN_FIELDS)
    if unknown:
        raise ValueError(f"panel_runs: unknown field(s) {sorted(unknown)}")
    row = {"run_id": run_id, "collection_mode": COLLECTION_MODE, "selector_plan": selector_plan,
           "started_utc": started_utc, "status": "running", **fields}
    cols = sorted(row)
    with conn:
        conn.execute(f"INSERT INTO panel_runs ({', '.join(cols)}) "
                     f"VALUES ({', '.join('?' * len(cols))})", [row[c] for c in cols])
    return run_id


def finish_run(conn, run_id, **fields):
    unknown = set(fields) - set(RUN_FIELDS)
    if unknown:
        raise ValueError(f"panel_runs: unknown field(s) {sorted(unknown)}")
    if not fields:
        return
    cols = sorted(fields)
    with conn:
        cur = conn.execute(f"UPDATE panel_runs SET {', '.join(f'{c} = ?' for c in cols)} "
                           "WHERE run_id = ?", [fields[c] for c in cols] + [run_id])
    if cur.rowcount == 0:
        raise ValueError(f"no run {run_id!r} to finish")


# ── snapshots ────────────────────────────────

INSERTED = "inserted"
IDENTICAL = "identical"
CONFLICT = "conflict"


def _json(value):
    return json.dumps(value, separators=(",", ":"))


def session_coverage(values):
    """OBSERVED_ZERO when all six values of a returned broker-session are zero."""
    return OBSERVED_ZERO if all(values[f] == 0 for f in SERIES_FIELDS) else OBSERVED_NONZERO


def record_snapshot(conn, snap):
    """Write one validated snapshot (targeted_actor_panel.build_snapshot) in a
    single transaction: INSERTED, or IDENTICAL / CONFLICT, writing nothing,
    when (ticker, discovery_as_of) is already recorded (module docstring)."""
    key = (snap["ticker"], snap["discovery_as_of"])
    dates = snap["dates"]
    if not dates or dates[-1] != snap["discovery_as_of"]:
        raise ValueError(f"{key}: discovery_as_of must be the last session of the axis")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT content_sha256 FROM panel_snapshots "
                           "WHERE ticker = ? AND discovery_as_of = ?", key).fetchone()
        if row is not None:
            conn.rollback()
            return IDENTICAL if row[0] == snap["content_sha256"] else CONFLICT
        conn.execute(
            "INSERT INTO panel_snapshots (ticker, discovery_as_of, run_id, collection_mode, "
            "coverage_scope, selector_plan, requested_start_date, requested_end_date, "
            "investor_type, first_session, last_session, session_count, "
            "observed_broker_count, content_sha256, recorded_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*key, snap["run_id"], COLLECTION_MODE, COVERAGE_SCOPE, snap["selector_plan"],
             snap["requested_start_date"], snap["requested_end_date"], snap["investor_type"],
             dates[0], dates[-1], len(dates), len(snap["brokers"]), snap["content_sha256"],
             snap.get("recorded_utc") or utc_now()))
        conn.executemany(
            "INSERT INTO panel_captures (ticker, discovery_as_of, request_group, horizons, "
            "selector_tokens, capture_id, manifest_run_id, attempt, query_sha256, http_status, "
            "response_bytes, response_sha256, response_text_sha256, captured_at, "
            "vendor_success, vendor_meta_brokers, expanded_brokers, unexplained_brokers, "
            "source_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*key, c["request_group"], _json(list(c["horizons"])), _json(list(c["selector_tokens"])),
              c["capture_id"], c["manifest_run_id"], c["attempt"], c["query_sha256"],
              c["http_status"], c["response_bytes"], c["response_sha256"],
              c["response_text_sha256"], c["captured_at"], int(c["vendor_success"] is True),
              _json(list(c["vendor_meta_brokers"])), _json(sorted(c["expanded_brokers"])),
              _json(sorted(c["unexplained_brokers"])), c["source_status"])
             for c in snap["captures"]])
        conn.executemany(
            "INSERT INTO panel_sessions (ticker, discovery_as_of, session_date, session_index, "
            "open, high, low, close, volume, volume_sma20) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*key, d, i, *(snap["ohlc"][i].get(f) for f in OHLC_FIELDS))
             for i, d in enumerate(dates)])
        brokers = snap["brokers"]
        conn.executemany(
            "INSERT INTO observed_brokers (ticker, discovery_as_of, broker, in_request_a, "
            "in_request_b, nonzero_sessions) VALUES (?, ?, ?, ?, ?, ?)",
            [(*key, b, int("A" in brokers[b]["returned_by"]), int("B" in brokers[b]["returned_by"]),
              sum(1 for i in range(len(dates))
                  if any(brokers[b]["series"][f][i] != 0 for f in SERIES_FIELDS)))
             for b in sorted(brokers)])
        series_rows = []
        for b in sorted(brokers):
            s = brokers[b]["series"]
            for i, d in enumerate(dates):
                values = {f: s[f][i] for f in SERIES_FIELDS}
                series_rows.append((*key, b, d, *(values[f] for f in SERIES_FIELDS),
                                    session_coverage(values)))
        conn.executemany(
            "INSERT INTO observed_series (ticker, discovery_as_of, broker, session_date, "
            "blot, bval, slot, sval, nlot, nval, coverage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            series_rows)
        conn.executemany(
            "INSERT INTO selection_status (ticker, discovery_as_of, horizon, metric, "
            "selector_token, request_group, selector_capture_id, status, sessions_required, "
            "sessions_available, window_first_session, window_last_session, qualifying_count, "
            "member_count, boundary_tie_value, boundary_tie_brokers) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*key, st["horizon"], st["metric"], st["selector_token"], st["request_group"],
              st["selector_capture_id"], st["status"], st["sessions_required"],
              st["sessions_available"], st["window_first_session"], st["window_last_session"],
              st["qualifying_count"], st["member_count"], st["boundary_tie_value"],
              None if st["boundary_tie_brokers"] is None else _json(st["boundary_tie_brokers"]))
             for st in snap["selection_status"]])
        conn.executemany(
            "INSERT INTO selection_membership (ticker, discovery_as_of, horizon, metric, rank, "
            "broker, window_value, window_first_session, window_last_session, window_sessions, "
            "tied, selector_token, request_group, selector_capture_id, union_capture_ids, "
            "provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*key, m["horizon"], m["metric"], m["rank"], m["broker"], m["window_value"],
              m["window_first_session"], m["window_last_session"], m["window_sessions"],
              int(m["tied"]), m["selector_token"], m["request_group"], m["selector_capture_id"],
              _json(list(m["union_capture_ids"])), PROVENANCE) for m in snap["membership"]])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return INSERTED


# ── reads ────────────────────────────────────

def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def latest_as_of(conn, ticker):
    """The latest discovery_as_of recorded for `ticker`, or None."""
    return conn.execute("SELECT MAX(discovery_as_of) FROM panel_snapshots WHERE ticker = ?",
                        (ticker,)).fetchone()[0]


def _as_of(conn, ticker, discovery_as_of):
    return discovery_as_of if discovery_as_of is not None else latest_as_of(conn, ticker)


def snapshot(conn, ticker, discovery_as_of=None):
    """The snapshot row with its captures (JSON lists decoded), or None."""
    as_of = _as_of(conn, ticker, discovery_as_of)
    rows = _rows(conn, "SELECT * FROM panel_snapshots WHERE ticker = ? AND discovery_as_of = ?",
                 (ticker, as_of))
    if not rows:
        return None
    snap = rows[0]
    snap["captures"] = _rows(conn, "SELECT * FROM panel_captures WHERE ticker = ? AND "
                                   "discovery_as_of = ? ORDER BY request_group", (ticker, as_of))
    for c in snap["captures"]:
        for k in ("horizons", "selector_tokens", "vendor_meta_brokers", "expanded_brokers",
                  "unexplained_brokers"):
            c[k] = json.loads(c[k])
    return snap


_HORIZON_ORDER = ("CASE horizon WHEN 'C5' THEN 1 WHEN 'C20' THEN 2 WHEN 'C50' THEN 3 "
                  "ELSE 4 END, CASE metric WHEN 'NB_LOT' THEN 1 WHEN 'NS_LOT' THEN 2 "
                  "WHEN 'NB_VAL' THEN 3 ELSE 4 END")


def selection_status(conn, ticker, discovery_as_of=None):
    """Every selector's status for a snapshot, in plan order (horizon, metric).
    A selector without membership rows says why here."""
    as_of = _as_of(conn, ticker, discovery_as_of)
    rows = _rows(conn, "SELECT * FROM selection_status WHERE ticker = ? AND discovery_as_of = ? "
                       f"ORDER BY {_HORIZON_ORDER}", (ticker, as_of))
    for r in rows:
        if r["boundary_tie_brokers"] is not None:
            r["boundary_tie_brokers"] = json.loads(r["boundary_tie_brokers"])
    return rows


def membership(conn, ticker, discovery_as_of=None, horizon=None, metric=None):
    """ticker -> horizon -> metric -> rank -> broker -> window value, as rows
    ordered by horizon (C5, C20, C50, ALL), metric, rank and broker. Every row
    is DERIVED_FROM_SELECTOR_UNION: the rank is ours, not the vendor's, and
    tied members share it. Only RESOLVED selectors have rows
    (selection_status says why the others have none)."""
    as_of = _as_of(conn, ticker, discovery_as_of)
    sql = ("SELECT * FROM selection_membership WHERE ticker = ? AND discovery_as_of = ?")
    params = [ticker, as_of]
    if horizon is not None:
        sql += " AND horizon = ?"
        params.append(horizon)
    if metric is not None:
        sql += " AND metric = ?"
        params.append(metric)
    sql += f" ORDER BY {_HORIZON_ORDER}, rank, broker"
    rows = _rows(conn, sql, params)
    for r in rows:
        r["union_capture_ids"] = json.loads(r["union_capture_ids"])
    return rows


def observed_brokers(conn, ticker, discovery_as_of=None):
    as_of = _as_of(conn, ticker, discovery_as_of)
    return _rows(conn, "SELECT * FROM observed_brokers WHERE ticker = ? AND discovery_as_of = ? "
                       "ORDER BY broker", (ticker, as_of))


def broker_series(conn, ticker, broker, discovery_as_of=None):
    """ticker -> broker -> session -> six daily series, in session order.

    [] for a broker that was not returned: it is UNOBSERVED, and an empty list
    is not a series of zeros (coverage() says which it is)."""
    as_of = _as_of(conn, ticker, discovery_as_of)
    return _rows(conn, "SELECT session_date, blot, bval, slot, sval, nlot, nval, coverage "
                       "FROM observed_series WHERE ticker = ? AND discovery_as_of = ? "
                       "AND broker = ? ORDER BY session_date", (ticker, as_of, broker))


def coverage(conn, ticker, discovery_as_of, broker, session_date):
    """{"state", "values"} of one broker-session (module docstring, COVERAGE).

    UNOBSERVED with values None for a broker outside the union. A session that
    is not on the snapshot's axis is a KeyError, not a coverage state: the
    panel says nothing about it for any broker."""
    if conn.execute("SELECT 1 FROM panel_sessions WHERE ticker = ? AND discovery_as_of = ? "
                    "AND session_date = ?", (ticker, discovery_as_of, session_date)).fetchone() is None:
        raise KeyError(f"{ticker} {discovery_as_of}: {session_date} is not a session of this snapshot")
    rows = _rows(conn, "SELECT blot, bval, slot, sval, nlot, nval, coverage FROM observed_series "
                       "WHERE ticker = ? AND discovery_as_of = ? AND broker = ? AND session_date = ?",
                 (ticker, discovery_as_of, broker, session_date))
    if not rows:
        return {"state": UNOBSERVED, "values": None}
    row = rows[0]
    return {"state": row.pop("coverage"), "values": row}


def accept_inventory_snapshot(conn, ticker, discovery_as_of):
    """Attest availability now, after a committed v1 source snapshot.

    Never backfill an acceptance marker from recorded_utc or the market date.
    Repeated acceptance preserves the first marker. Historical v1 sources with
    no marker are unavailable to the evidence reader until explicitly accepted.
    """
    import inventory_evidence as ie
    if conn.in_transaction:
        raise ValueError("commit the source snapshot before accepting it")
    snap = snapshot(conn, ticker, discovery_as_of)
    if snap is None:
        raise KeyError("no source snapshot")
    accepted = ie.utc_text(utc_now())
    if ie.timestamp(accepted) < max([ie.timestamp(snap["recorded_utc"])] +
                                  [ie.timestamp(c["captured_at"]) for c in snap["captures"]]):
        raise ValueError("acceptance precedes source recording/response")
    with conn:
        conn.execute("INSERT OR IGNORE INTO inventory_snapshot_acceptances VALUES (?, ?, ?, ?, ?)",
                     (ticker, discovery_as_of, snap["content_sha256"], accepted, INVENTORY_EVIDENCE_VERSION))
    return conn.execute("SELECT durable_accepted_at FROM inventory_snapshot_acceptances "
                        "WHERE ticker = ? AND discovery_as_of = ?", (ticker, discovery_as_of)).fetchone()[0]


def record_inventory_evidence(conn, doc):
    """Append an immutable evidence revision; finalize availability after commit.

    An interruption between commits leaves an unconfirmed body that readers
    exclude. Retrying identical content confirms it at the retry time. Same
    revision/different content is refused. Corrections require a new revision
    linked to the immediately preceding revision of this ticker/anchor/cutoff.
    """
    import inventory_evidence as ie
    ie.validate_document(doc)
    if any(row["scope"] is not None and row["scope"]["capture_scope"] != COVERAGE_SCOPE
           for broker in doc["brokers"].values() for row in broker["series"]):
        raise ValueError("targeted store accepts selector-union evidence only; explicit follow-up is deferred")
    if conn.in_transaction:
        raise ValueError("commit other writes before appending evidence")
    text, digest = ie.canonical_json(doc), ie.content_hash(doc)
    key = (doc["ticker"], doc["axis"]["start"], doc["axis"]["cutoff"])
    revision = doc["observation_revision"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = conn.execute("SELECT observation_revision, content_sha256 FROM inventory_evidence_revisions "
                                "WHERE ticker = ? AND anchor = ? AND cutoff = ? ORDER BY observation_revision", key).fetchall()
        existing = dict(previous).get(revision)
        if existing is not None:
            if existing != digest:
                raise ValueError("immutable evidence revision conflict")
            result = "identical"
        else:
            parent = previous[-1][0] if previous else None
            if doc["parent_revision"] != parent or revision != (parent or 0) + 1:
                raise ValueError("revision must link to the immediately preceding revision")
            if previous:
                if conn.execute("SELECT 1 FROM inventory_evidence_acceptances WHERE ticker = ? AND anchor = ? "
                                "AND cutoff = ? AND observation_revision = ?", (*key, parent)).fetchone() is None:
                    raise ValueError("parent revision is not durably confirmed")
                last = json.loads(conn.execute("SELECT observation_json FROM inventory_evidence_revisions "
                                               "WHERE ticker = ? AND anchor = ? AND cutoff = ? AND observation_revision = ?",
                                               (*key, parent)).fetchone()[0])
                if ie.timestamp(doc["availability_cutoff"]) < ie.timestamp(last["availability_cutoff"]):
                    raise ValueError("revision availability cannot move backwards")
            conn.execute("INSERT INTO inventory_evidence_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (*key, revision, parent, ie.VERSION, doc["availability_cutoff"], digest, text))
            result = "inserted"
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    # The body is now durable. No availability timestamp was assigned before it.
    accepted = ie.utc_text(utc_now())
    if doc["known_at"] is not None and ie.timestamp(accepted) < ie.timestamp(doc["known_at"]):
        raise ValueError("product acceptance precedes an input's availability")
    if doc["parent_revision"] is not None:
        parent_accepted = conn.execute("SELECT durable_accepted_at FROM inventory_evidence_acceptances "
                                       "WHERE ticker = ? AND anchor = ? AND cutoff = ? AND observation_revision = ?",
                                       (*key, doc["parent_revision"])).fetchone()[0]
        if ie.timestamp(accepted) < ie.timestamp(parent_accepted):
            raise ValueError("product acceptance precedes parent revision")
    with conn:
        conn.execute("INSERT OR IGNORE INTO inventory_evidence_acceptances VALUES (?, ?, ?, ?, ?)",
                     (*key, revision, accepted))
    return result


def inventory_evidence_as_of(conn, ticker, anchor, cutoff, availability_cutoff):
    """Latest confirmed revision available by a timezone-aware cutoff.

    The envelope's known_at is product durability; evidence.known_at is input
    availability. Return None for an old v1 database without this extension.
    """
    import inventory_evidence as ie
    if "inventory_evidence_acceptances" not in _tables(conn):
        return None
    available = ie.utc_text(availability_cutoff)
    row = conn.execute("SELECT r.observation_json, r.content_sha256, a.durable_accepted_at "
                       "FROM inventory_evidence_revisions r JOIN inventory_evidence_acceptances a "
                       "USING (ticker, anchor, cutoff, observation_revision) "
                       "WHERE ticker = ? AND anchor = ? AND cutoff = ? AND a.durable_accepted_at <= ? "
                       "AND r.availability_cutoff <= ? ORDER BY observation_revision DESC LIMIT 1",
                       (ticker, anchor, cutoff, available, available)).fetchone()
    if row is None:
        return None
    return {"view": "AS_OF", "evidence": json.loads(row[0]), "content_sha256": row[1], "known_at": row[2],
            "durable_accepted_at": row[2], "evidence_schema_version": INVENTORY_EVIDENCE_VERSION}


def latest_inventory_evidence(conn, ticker, anchor, cutoff):
    """Explicit retrospective view; never a substitute for historical as-of truth."""
    if "inventory_evidence_acceptances" not in _tables(conn):
        return None
    row = conn.execute("SELECT r.observation_json, r.content_sha256, a.durable_accepted_at "
                       "FROM inventory_evidence_revisions r JOIN inventory_evidence_acceptances a "
                       "USING (ticker, anchor, cutoff, observation_revision) "
                       "WHERE ticker = ? AND anchor = ? AND cutoff = ? "
                       "ORDER BY observation_revision DESC LIMIT 1", (ticker, anchor, cutoff)).fetchone()
    if row is None:
        return None
    return {"view": "LATEST_RETROSPECTIVE", "evidence": json.loads(row[0]), "content_sha256": row[1],
            "known_at": row[2], "durable_accepted_at": row[2], "evidence_schema_version": INVENTORY_EVIDENCE_VERSION}
