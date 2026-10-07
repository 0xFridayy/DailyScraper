"""Read-only Market Intelligence consumer of the targeted broker actor panel.

targeted_actor_panel.db (targeted_actor_db) holds, per ticker and discovery_as_of,
the SELECTOR UNION of the 16 v1 selectors: the brokers the vendor chose, their
daily series, and the selection membership derived and stored at collection.
This module reads one such snapshot and returns a deterministic,
machine-readable OBSERVATION of it (schema targeted_actor_observation v1) for
later Market Intelligence reasoning. It is a reader, not a second collector and
not a signal system: it restates what the panel stored, consolidated per
broker, plus each observed broker's own window sums and, where the data
supports one, an implied price.

NOT HERE
--------
No owner, bandar, retail or smart-money label; no accumulation/distribution
intent, entry/exit, recommendation or score; no concentration, market share or
any other metric that needs a market denominator (the union is not the market);
no rerun of the collector's selection. Those belong to later interpretation.

READ-ONLY
---------
Nothing here writes the source: no database write, no PRAGMA that persists, no
journal, -wal or -shm created, changed or removed, no output file (the CLI prints
to stdout). targeted_actor_db.connect() is never used: it creates a missing file
and writes its schema. A missing database is an error, never created.
observe() and list_snapshots() only read through a connection that
open_readonly() made, and it sets PRAGMA query_only.

EXTERNAL STABILITY CONTRACT
---------------------------
v1 reads only a QUIESCENT source: the producer (targeted_actor_panel, or any
other writer) must be fully closed before this consumer is invoked. Live WAL is
not supported. mode=ro alone does not keep the read-only promise for WAL: SQLite
then creates -wal and -shm when they are absent and writes read marks into an
existing -shm (test_targeted_actor_observations, section 10).

Supported, read with mode=ro&immutable=1, and nothing else:

  ROLLBACK_JOURNAL   header file format 1, and no -wal, -shm or -journal beside it
  WAL_CHECKPOINTED   header file format 2 (WAL), fully checkpointed, and no -wal,
                     -shm or -journal beside it: with no -wal every committed
                     transaction is in the main file

Refused with ReadOnlySourceStateError before SQLite opens the file: any -wal, any
-shm, any -journal (a live writer, a writer in this or another process, crash
recovery, a partial sidecar state), and an unknown header format.

immutable=1 makes SQLite open no journal, -wal or -shm and take no lock, even if
the source changes between inspection and open. It also means SQLite does no
locking and no change detection: it gives NO protection against concurrent
modification, and active modification is unsupported. As a detection guard,
the source state and the main file's identity (size, mtime_ns, sha256) are
recorded at inspection and checked again right after the open and at the start
and end of every public read; any difference raises ReadOnlySourceStateError and
the read's result is not returned. That guard detects a change; it does not make
a concurrent change safe. Nothing here copies, checkpoints or deletes anything,
or changes the journal mode.

IDENTITY
--------
A snapshot is (ticker, discovery_as_of), the primary key of panel_snapshots.
run_id, content_sha256 and recorded_utc are provenance only. With
discovery_as_of omitted, observe() takes the ticker's greatest discovery_as_of
(ISO dates, so string order is date order), never the most recently recorded.

THE OBSERVATION
---------------
  contract          what the document claims and does not claim
  snapshot, source  identity; provenance, with both captures as stored
  basis_reference   the known share-basis conflict intervals applied
  axis, horizons    the sessions (and OHLC); per horizon, history only
  selectors         the 16 stored selector statuses with their members, in plan
                    order: these are authoritative
  observed_brokers  the union; selected_brokers and unexplained_brokers
                    partition it
  brokers           per observed broker: every stored selection reason,
                    membership_by_horizon, window sums, implied prices, series

A broker absent from observed_brokers is UNOBSERVED: its flows are unknown,
never zero. selected_brokers have at least one stored RESOLVED membership row;
unexplained_brokers have none. "Unexplained" is only that property of this
product's resolved rows: it does not mean the vendor cannot explain the broker,
that the broker had no flow, that it was padding, or that it is irrelevant. A
capture's own unexplained_brokers, carried as stored in source, is the same
property for one request.

MEMBERSHIP BY HORIZON
---------------------
For every observed broker, metric and horizon, the selector
TOP_5_{metric}_{horizon}:

  MEMBER        a stored membership row exists
  NOT_MEMBER    the selector is RESOLVED and no stored row exists
  UNDETERMINED  the selector is UNRESOLVED_BOUNDARY_TIE or INSUFFICIENT_HISTORY;
                boundary_tie_candidate marks the brokers the tie names

The scope is RETURNED_UNION_UNDER_RECORDED_SELECTOR_ASSUMPTIONS: RESOLVED was
decided over the returned union under the selector semantics recorded at
collection. It does not prove that an unreturned sixth broker could not have
tied at the market-wide boundary. The horizons are nested windows anchored at
discovery_as_of (C5 inside C20 inside C50 inside ALL), so membership at several
horizons is not independent evidence. resolved_member_horizons lists the MEMBER
horizons. There is no score.

A horizon object states history availability only. It never says whether a
selector could be observed there: the 16 selector statuses say that.

IMPLIED PRICE
-------------
Per observed broker, horizon and side (buy: blot, bval; sell: slot, sval), over
the horizon's window, the first status that applies:

  WITHHELD_VALUE_WITHOUT_REPORTED_LOTS  a session of the window reports value
                                        with zero lots on that side; that value
                                        is reported as
                                        value_without_reported_lots_rp and is
                                        never priced
  NO_LOTS                               no lots and no value on that side
  WITHHELD_KNOWN_BASIS_CONFLICT         the window's span, first to last
                                        session, overlaps a known conflict
                                        interval
  CALCULATED                            sum(value) / (100 * sum(lots))

implied_price_from_reported_lots_rp_per_share is that ratio of sums and nothing
else: not a mean of daily prices, not nval / nlot, not a verified execution
price, not a cost basis. Nothing here says what value without reported lots
represents. A horizon whose history is INSUFFICIENT has no window: its entry in
windows is null and nothing is summed or priced.

BASIS REFERENCE
---------------
observed_basis_factor.json: every regime of the ticker, whatever its
classification, is a known conflict interval (the rule of
broker_book.load_basis_regimes). No overlapping interval means
NO_KNOWN_CONFLICT_NOT_VERIFIED, never a verified basis. The document names the
file by source_id, never by path, and canonical_json_sha256 is the sha256 of the
canonical JSON of the parsed file (sorted keys, compact separators, ASCII), so a
CRLF checkout hashes like an LF one; it is not a hash of the file's raw bytes. A
missing or malformed file is a BasisReferenceError.

INTEGRITY
---------
observe() fails closed (PanelIntegrityError) unless the stored snapshot holds
together. These are this consumer's conditions for producing a v1 observation.
The panel's SQLite schema does not enforce them, and the collector does not
enforce the sign and lots-without-value rules either:

  series     every observed broker has exactly one row per session; gross lots
             (blot, slot) are integers >= 0; gross values (bval, sval) are
             finite and >= 0; nlot == blot - slot; |nval - (bval - sval)| <= 0.5
             rupiah (build_inventory_db.RUPIAH_TOLERANCE); no lots with zero
             value on the same side; each row's coverage label agrees with its
             values
  structure  the snapshot row describes its axis and brokers under the v1 plan;
             the two captures carry the plan's horizons and tokens, and their
             expanded and unexplained brokers agree with the stored rows; the
             16 selector statuses carry the plan's tokens, request groups,
             capture ids and history windows; a RESOLVED selector's
             member_count is its number of stored rows, and any other status
             has none; every member is observed and was returned by its own
             request; window fields, signs, ranks and tied flags agree with the
             STORED values; union_capture_ids are A then B; provenance is
             DERIVED_FROM_SELECTOR_UNION

The stored selection is the product. Nothing here re-derives it or imports the
collector.

DETERMINISM
-----------
observation_json() is canonical: sorted keys, compact separators, no NaN. The
document depends only on the stored snapshot and the basis reference's content:
not on the database path, the machine, the time, or on whether it was asked for
by date or as the latest.

CLI (JSON on stdout; an error prints to stderr, prints nothing to stdout, and exits
1, or 2 for a usage error such as an unknown option):

    py -3 targeted_actor_observations.py list --db PATH [TICKER]
    py -3 targeted_actor_observations.py observe --db PATH TICKER [--as-of D] [--no-series]

Standard library, targeted_actor_db and targeted_selectors only.
"""

import argparse
import copy
import hashlib
import json
import math
import os
import pathlib
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date

import targeted_actor_db as tdb
import targeted_selectors as ts

HERE = os.path.dirname(os.path.abspath(__file__))
BASIS_FILE = os.path.join(HERE, "observed_basis_factor.json")
BASIS_SOURCE_ID = "observed_basis_factor.json"      # how the document names it: never a path

SCHEMA = "targeted_actor_observation"
SCHEMA_VERSION = "1"

SHARES_PER_LOT = 100            # broker_book.SHARES_PER_LOT
RUPIAH_TOLERANCE = 0.5          # build_inventory_db.RUPIAH_TOLERANCE, |nval - (bval - sval)|

SERIES_FIELDS = tdb.SERIES_FIELDS
LOT_FIELDS = ("blot", "slot", "nlot")
VALUE_FIELDS = ("bval", "sval", "nval")
SIDES = (("gross_buy", "blot", "bval"), ("gross_sell", "slot", "sval"))
GROUPS = tuple(spec.group for spec in ts.PLAN)                     # ("A", "B")
PLAN_SELECTORS = tuple((spec.group, sel) for spec in ts.PLAN for sel in ts.selectors_of(spec))
DIRECTIONS = ("NB", "NS")
UNITS = ("LOT", "VAL")

MEMBER, NOT_MEMBER, UNDETERMINED = "MEMBER", "NOT_MEMBER", "UNDETERMINED"
SUFFICIENT, INSUFFICIENT = "SUFFICIENT", "INSUFFICIENT"
WITHHELD_VALUE_WITHOUT_REPORTED_LOTS = "WITHHELD_VALUE_WITHOUT_REPORTED_LOTS"
NO_LOTS = "NO_LOTS"
WITHHELD_KNOWN_BASIS_CONFLICT = "WITHHELD_KNOWN_BASIS_CONFLICT"
CALCULATED = "CALCULATED"
PRICE_STATUSES = (WITHHELD_VALUE_WITHOUT_REPORTED_LOTS, NO_LOTS, WITHHELD_KNOWN_BASIS_CONFLICT,
                  CALCULATED)                                       # precedence order
KNOWN_CONFLICT = "KNOWN_CONFLICT"
NO_KNOWN_CONFLICT = "NO_KNOWN_CONFLICT_NOT_VERIFIED"
PRICE = "implied_price_from_reported_lots_rp_per_share"
UNPRICED = "value_without_reported_lots_rp"

CONTRACT = {
    "coverage_scope": tdb.COVERAGE_SCOPE,
    "full_universe": False,
    "absent_from_observed": tdb.UNOBSERVED,
    "unobserved_is_zero": False,
    "selected_means": "OBSERVED_WITH_AT_LEAST_ONE_STORED_RESOLVED_MEMBERSHIP",
    "unexplained_means": "OBSERVED_WITH_ZERO_STORED_RESOLVED_MEMBERSHIPS",
    "rank_provenance": tdb.PROVENANCE,
    "qualifying_count_scope": "RETURNED_UNION",
    "selector_resolution_scope": "RETURNED_UNION_UNDER_RECORDED_SELECTOR_ASSUMPTIONS",
    "horizon_order": list(ts.HORIZONS),
    "metric_order": list(ts.METRICS),
    "horizons_nested": True,
    "horizon_anchor": "DISCOVERY_AS_OF",
    "shares_per_lot": SHARES_PER_LOT,
    "value_unit": "IDR",
    "price_unit": "IDR_PER_SHARE",
    "price_method": "RATIO_OF_SUMS_OVER_REPORTED_LOTS",
    "basis_absence_means": NO_KNOWN_CONFLICT,
}

REQUIRED_TABLES = ("panel_meta", "panel_runs", "panel_snapshots") + tdb.SNAPSHOT_TABLES

# The quiescent source states v1 reads, and the one way it opens them (module
# docstring, EXTERNAL STABILITY CONTRACT).
ROLLBACK_JOURNAL = "ROLLBACK_JOURNAL"
WAL_CHECKPOINTED = "WAL_CHECKPOINTED"
OPEN_PARAMS = "mode=ro&immutable=1"
SIDECARS = ("-wal", "-shm", "-journal")
SQLITE_MAGIC = b"SQLite format 3\x00"

# Stored columns carried into the document as they are (the key columns aside).
CAPTURE_COLUMNS = ("request_group", "horizons", "selector_tokens", "capture_id", "manifest_run_id",
                   "attempt", "query_sha256", "http_status", "response_bytes", "response_sha256",
                   "response_text_sha256", "captured_at", "vendor_success", "vendor_meta_brokers",
                   "expanded_brokers", "unexplained_brokers", "source_status")
STATUS_COLUMNS = ("selector_token", "horizon", "metric", "request_group", "selector_capture_id",
                  "status", "sessions_required", "sessions_available", "window_first_session",
                  "window_last_session", "qualifying_count", "member_count", "boundary_tie_value",
                  "boundary_tie_brokers")
REASON_COLUMNS = ("selector_token", "horizon", "metric", "rank", "tied", "window_value",
                  "window_first_session", "window_last_session", "window_sessions",
                  "request_group", "selector_capture_id", "union_capture_ids", "provenance")

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


class SnapshotNotFoundError(LookupError):
    """No stored snapshot for the requested ticker (and discovery_as_of)."""


class PanelIntegrityError(ValueError):
    """The stored snapshot breaks a condition this reader needs (module
    docstring, INTEGRITY). Nothing is observed from it."""


class BasisReferenceError(ValueError):
    """observed_basis_factor.json is missing or malformed, so no basis
    statement can be made."""


class ObservationContractError(RuntimeError):
    """A document built here breaks its own v1 schema: a defect in this module,
    not in the data."""


class ReadOnlySourceStateError(Exception):
    """The source is not quiescent: a -wal, -shm or -journal is beside it, its
    header format is unknown, or it changed since open_readonly() inspected it
    (module docstring, EXTERNAL STABILITY CONTRACT)."""


class _Broken(Exception):
    """One broken INTEGRITY rule, before the snapshot key is prefixed."""


def _is_date(value):
    """A real calendar date spelled YYYY-MM-DD."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _check_ticker(ticker):
    if not isinstance(ticker, str) or not ts.TICKER_RE.fullmatch(ticker):
        raise ValueError(f"{ticker!r} is not a four-letter upper-case ticker")


# ── the read-only connection ─────────────────

class _PanelConnection(sqlite3.Connection):
    """A connection made by open_readonly(): it remembers the source state and
    main-file identity it was opened for, which every read checks again
    (_check_source)."""
    source_path = source_state = source_identity = None


def _file_identity(path):
    """(size, mtime_ns, sha256) of a file."""
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns, digest


def _source_state(path):
    """ROLLBACK_JOURNAL or WAL_CHECKPOINTED for a quiescent source (module
    docstring, EXTERNAL STABILITY CONTRACT); ReadOnlySourceStateError for any
    other state. Reads the first 100 bytes and checks which sidecars exist;
    writes nothing."""
    with open(path, "rb") as fh:
        header = fh.read(100)
    if len(header) < 100 or not header.startswith(SQLITE_MAGIC):
        raise tdb.NotTargetedPanelError(f"{path}: not an SQLite database")
    present = [s for s in SIDECARS if os.path.exists(path + s)]
    if present:
        raise ReadOnlySourceStateError(
            f"{path}: {' and '.join(present)} present beside the database; v1 reads only a "
            "quiescent source with no -wal, -shm or -journal: fully close the producer first")
    fmt = (header[18], header[19])              # write and read format: 1 rollback journal, 2 WAL
    if fmt == (1, 1):
        return ROLLBACK_JOURNAL
    if fmt == (2, 2):
        return WAL_CHECKPOINTED
    raise ReadOnlySourceStateError(f"{path}: unknown file format {fmt} in the database header")


def _check_source(conn):
    """ReadOnlySourceStateError unless the source is still quiescent, in the
    state `conn` was opened for, and its main file byte-identical. A detection
    guard: immutable=1 reads take no locks, so it cannot make a concurrent change
    safe, only refuse the read that saw one."""
    try:
        state = _source_state(conn.source_path)
        identity = _file_identity(conn.source_path)
    except (OSError, ReadOnlySourceStateError, tdb.NotTargetedPanelError) as e:
        raise ReadOnlySourceStateError(
            f"{conn.source_path}: the source changed since it was opened: {e}") from None
    if state != conn.source_state or identity != conn.source_identity:
        raise ReadOnlySourceStateError(
            f"{conn.source_path}: the source changed since it was opened ({conn.source_state} "
            f"-> {state}, main file {'unchanged' if identity == conn.source_identity else 'changed'}); "
            "reopen it with open_readonly() once the producer is closed")


def open_readonly(path=tdb.DB_PATH):
    """A read-only connection to a quiescent targeted actor panel database: the
    source state inspected, the file opened with mode=ro&immutable=1 (no journal,
    -wal or -shm is ever opened or created, and no lock is taken), PRAGMA
    query_only set, panel_meta checked to be exactly this product's, and the
    source checked again right after the open.

    tdb.NotTargetedPanelError for another product's database name or content;
    FileNotFoundError for a missing file, which is never created;
    ReadOnlySourceStateError for a source that is not quiescent, or that changed
    between inspection and open."""
    tdb.check_path(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path}: no such panel database (the reader never creates one)")
    path = os.path.abspath(path)
    state = _source_state(path)
    identity = _file_identity(path)
    uri = pathlib.Path(path).as_uri() + "?" + OPEN_PARAMS
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, factory=_PanelConnection)
    conn.source_path, conn.source_state, conn.source_identity = path, state, identity
    try:
        _check_source(conn)                     # changed between inspection and open?
        conn.execute("PRAGMA query_only = ON")
        _panel_meta(conn)
        _check_source(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _panel_meta(conn):
    """panel_meta, after checking that the database holds exactly this product."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        raise tdb.NotTargetedPanelError(f"not a {tdb.PRODUCT} database: no table(s) {missing}")
    meta = dict(conn.execute("SELECT key, value FROM panel_meta"))
    if meta != tdb.META:
        raise tdb.NotTargetedPanelError(f"panel_meta {meta} is not exactly {tdb.META}")
    return meta


@contextmanager
def _reading(conn):
    """One read transaction on a connection open_readonly() made, so every query
    of a call sees the same state. Any other connection is refused before
    anything is begun on it, and the source state is checked before the read and
    again after it."""
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("the connection can write (PRAGMA query_only is off): open the panel "
                         "with open_readonly(), never targeted_actor_db.connect()")
    if not isinstance(conn, _PanelConnection):
        raise ValueError("the connection was not made by open_readonly(), so its source state "
                         "was never checked: open the panel with open_readonly()")
    _check_source(conn)
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")
    _check_source(conn)                         # a read of a changed source is not returned


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


# ── public reads ─────────────────────────────

def list_snapshots(conn, ticker=None):
    """The stored snapshots ordered by (ticker, discovery_as_of): the identity,
    with run_id, content_sha256 and recorded_utc as provenance, and sizes."""
    if ticker is not None:
        _check_ticker(ticker)
    with _reading(conn):
        _panel_meta(conn)
        sql = ("SELECT ticker, discovery_as_of, run_id, content_sha256, recorded_utc, "
               "session_count, observed_broker_count FROM panel_snapshots")
        params = ()
        if ticker is not None:
            sql, params = sql + " WHERE ticker = ?", (ticker,)
        return _rows(conn, sql + " ORDER BY ticker, discovery_as_of", params)


def observe(conn, ticker, discovery_as_of=None, include_series=True):
    """The observation document of one stored snapshot (module docstring).

    discovery_as_of None takes the ticker's greatest discovery_as_of. With
    include_series False, the OHLC and the daily broker series are null and
    every other field is unchanged. Raises ValueError for a bad argument or a
    connection open_readonly() did not make, ReadOnlySourceStateError,
    SnapshotNotFoundError, PanelIntegrityError, BasisReferenceError, or
    ObservationContractError."""
    from price_contract import refuse_unmigrated
    refuse_unmigrated("targeted_actor_observations.observe")
    _check_ticker(ticker)
    if discovery_as_of is not None and not _is_date(discovery_as_of):
        raise ValueError(f"discovery_as_of {discovery_as_of!r} is not a YYYY-MM-DD date")
    if not isinstance(include_series, bool):
        raise ValueError(f"include_series must be True or False, not {include_series!r}")
    with _reading(conn):
        meta = _panel_meta(conn)
        as_of = _resolve(conn, ticker, discovery_as_of)
        stored = _load(conn, ticker, as_of)
    panel = _checked(stored)
    doc = _document(panel, meta, _basis_reference(), include_series)
    _check_document(doc)
    return doc


def observation_json(doc):
    """The canonical JSON text of a document: sorted keys, compact separators,
    ASCII, NaN and infinities refused."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)


def observe_inventory_evidence(conn, ticker, *, anchor, cutoff, availability_cutoff,
                               broker_codes, scope, basis_reference_known_at=None,
                               discovery_as_of=None, observation_revision=1,
                               parent_revision=None, windows=(5, 20), market_observations=()):
    """Additive inventory_evidence v1 reader; observe() keeps its exact v1 output.

    Broker codes and market scope are explicitly supplied, never inferred from
    future selector membership. Only snapshots with a post-commit acceptance
    marker contribute. Old snapshots lacking a marker remain UNOBSERVED.
    Basis content and its post-commit acceptance must already be stored through
    targeted_actor_db.accept_inventory_basis_reference(). scope.basis_version
    selects that immutable content hash; the current basis file is never read.
    A reference not durably available by the cutoff contributes no evidence.
    The optional legacy basis_reference_known_at argument is only an equality
    assertion against that acceptance; it cannot establish availability.
    Unit/market metadata absent from v1 is declared in Scope, not
    invented from the OHLC arrays. Market volumes require separately validated
    MarketObservation inputs; stored inventory volume_sma20 is never used.
    """
    from price_contract import refuse_unmigrated
    refuse_unmigrated("targeted_actor_observations.observe_inventory_evidence")
    import inventory_evidence as ie
    _check_ticker(ticker)
    if not isinstance(scope, ie.Scope) or scope.capture_scope != tdb.COVERAGE_SCOPE:
        raise ValueError("this reader supports only declared TARGETED_SELECTOR_UNION scope")
    axis = ie.idx_session_axis(anchor, cutoff)
    available = ie.timestamp(availability_cutoff)
    parameters = dict(ticker=ticker, broker_codes=broker_codes, axis=axis,
                      availability_cutoff=availability_cutoff, observation_revision=observation_revision,
                      parent_revision=parent_revision, windows=windows, market_observations=market_observations,
                      compatibility_scopes=(scope,))
    with _reading(conn):
        _panel_meta(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "inventory_snapshot_acceptances" not in tables:
            accepted = []
        else:
            accepted = _rows(conn, "SELECT ticker, discovery_as_of, content_sha256, durable_accepted_at "
                                  "FROM inventory_snapshot_acceptances WHERE ticker = ?", (ticker,))
        accepted = [r for r in accepted if ie.timestamp(r["durable_accepted_at"]) <= available
                    and (discovery_as_of is None or r["discovery_as_of"] == discovery_as_of)]
        chosen = max(accepted, key=lambda r: r["discovery_as_of"]) if accepted else None
        stored = _load(conn, ticker, chosen["discovery_as_of"]) if chosen else None
        basis_record = tdb.inventory_basis_reference_as_of(
            conn, BASIS_SOURCE_ID, scope.basis_version, availability_cutoff)
    if stored is None or basis_record is None:
        return ie.build_inventory_evidence([], **parameters)
    panel = _checked(stored)
    snap = panel["snapshot"]
    if snap["content_sha256"] != chosen["content_sha256"] or snap["investor_type"] != scope.investor_type:
        raise PanelIntegrityError("inventory evidence acceptance/scope disagrees with source snapshot")
    accepted_at = chosen["durable_accepted_at"]
    if accepted_at != ie.utc_text(accepted_at) or ie.timestamp(accepted_at) < max(
            [ie.timestamp(snap["recorded_utc"])] +
            [ie.timestamp(c["captured_at"]) for c in snap["captures"]]):
        raise PanelIntegrityError("inventory evidence acceptance precedes source recording/response or is not canonical")
    if (basis_reference_known_at is not None
            and ie.timestamp(basis_reference_known_at) != ie.timestamp(basis_record["durable_accepted_at"])):
        raise ValueError("basis_reference_known_at must equal the stored durable basis acceptance")
    basis = _basis_reference_document(json.loads(basis_record["content_json"]))
    if basis["canonical_json_sha256"] != scope.basis_version:
        raise BasisReferenceError("accepted basis content disagrees with its immutable content hash")
    basis_capture = ie.Capture(BASIS_SOURCE_ID, basis["canonical_json_sha256"],
                              basis_record["durable_accepted_at"], basis_record["durable_accepted_at"],
                              basis["canonical_json_sha256"],
                              content_hash_kind="CANONICAL_JSON_SHA256")
    captures = {}
    for c in snap["captures"]:
        digest = c["response_sha256"] or c["response_text_sha256"]
        if not digest or not c["captured_at"]:
            raise PanelIntegrityError("capture lacks response availability/content hash")
        params = (("symbol", ticker), ("start_date", snap["requested_start_date"]),
                  ("end_date", snap["requested_end_date"]), ("investor_type", snap["investor_type"]))
        captures[c["request_group"]] = ie.Capture(
            "neobdm:/api/inventory", c["capture_id"], c["captured_at"], chosen["durable_accepted_at"], digest,
            request_parameters=params, requested_selectors=tuple(c["selector_tokens"]),
            returned_brokers=tuple(c["expanded_brokers"]), query_sha256=c["query_sha256"],
            content_hash_kind="RESPONSE_BYTES_SHA256" if c["response_sha256"] else "RESPONSE_TEXT_UTF8_SHA256")
    field_map = dict(zip(ie.FLOW_FIELDS, ("blot", "slot", "nlot", "bval", "sval", "nval")))
    observations = []
    for b in panel["codes"]:
        for i, d in enumerate(panel["dates"]):
            if not anchor <= d <= cutoff:
                continue
            conflict = any(first <= d <= last for first, last in basis["intervals"].get(ticker, []))
            for group in panel["returned"][b]:
                observations.append(ie.Observation(
                    ticker, b, d, observation_revision, scope, captures[group],
                    **{f: panel["series"][b][source][i] for f, source in field_map.items()},
                    coverage=ie.QUARANTINED if conflict else panel["series"][b]["coverage"][i],
                    null_reason="KNOWN_BASIS_CONFLICT" if conflict else None))
    return ie.build_inventory_evidence(observations, reference_captures=(basis_capture,), **parameters)


def inventory_evidence_revision(conn, ticker, anchor, cutoff, availability_cutoff=None,
                                request_contract_sha256=None):
    """Read a confirmed stored revision with the v1 reader's quiescence guards.

    A timestamp requests AS_OF; None explicitly requests LATEST_RETROSPECTIVE.
    Specify the immutable request digest when multiple request chains exist.
    """
    _check_ticker(ticker)
    with _reading(conn):
        _panel_meta(conn)
        if availability_cutoff is None:
            return tdb.latest_inventory_evidence(conn, ticker, anchor, cutoff, request_contract_sha256)
        return tdb.inventory_evidence_as_of(conn, ticker, anchor, cutoff, availability_cutoff,
                                          request_contract_sha256)


def _resolve(conn, ticker, discovery_as_of):
    """The stored discovery_as_of that (ticker, discovery_as_of) names, the
    latest when it is None."""
    dates = [r[0] for r in conn.execute("SELECT discovery_as_of FROM panel_snapshots "
                                        "WHERE ticker = ?", (ticker,))]
    if discovery_as_of is None:
        if not dates:
            raise SnapshotNotFoundError(f"{ticker}: no snapshot in this panel")
        bad = sorted({repr(d) for d in dates if not _is_date(d)})
        if bad:
            raise PanelIntegrityError(f"{ticker}: discovery_as_of {bad[:3]} is not a YYYY-MM-DD "
                                      "date, so the latest snapshot cannot be ordered")
        discovery_as_of = max(dates)
    elif discovery_as_of not in dates:
        raise SnapshotNotFoundError(f"{ticker} {discovery_as_of}: no such snapshot in this panel")
    if dates.count(discovery_as_of) != 1:        # the primary key: unreachable in schema v1
        raise PanelIntegrityError(f"{ticker} {discovery_as_of}: {dates.count(discovery_as_of)} "
                                  "snapshot rows for one (ticker, discovery_as_of)")
    return discovery_as_of


def _load(conn, ticker, as_of):
    """Every stored row of one snapshot, the JSON columns decoded by the
    panel's own read helpers."""
    key = (ticker, as_of)
    try:
        return {
            "snapshot": tdb.snapshot(conn, ticker, as_of),
            "sessions": _rows(conn, "SELECT session_date, session_index, "
                                    f"{', '.join(tdb.OHLC_FIELDS)} FROM panel_sessions "
                                    "WHERE ticker = ? AND discovery_as_of = ? "
                                    "ORDER BY session_index", key),
            "brokers": tdb.observed_brokers(conn, ticker, as_of),
            "series": _rows(conn, "SELECT broker, session_date, blot, bval, slot, sval, nlot, "
                                  "nval, coverage FROM observed_series WHERE ticker = ? AND "
                                  "discovery_as_of = ? ORDER BY broker, session_date", key),
            "statuses": tdb.selection_status(conn, ticker, as_of),
            "members": tdb.membership(conn, ticker, as_of),
        }
    except (ValueError, TypeError) as e:          # a stored JSON column that does not decode
        raise PanelIntegrityError(f"{ticker} {as_of}: a stored JSON column does not decode: "
                                  f"{e}") from None


# ── integrity ────────────────────────────────

def _checked(stored):
    """The stored snapshot, checked (module docstring, INTEGRITY) and indexed
    for _document; PanelIntegrityError at the first rule it breaks."""
    snap = stored["snapshot"]
    try:
        return _validate(stored)
    except _Broken as e:
        raise PanelIntegrityError(f"{snap['ticker']} {snap['discovery_as_of']}: {e}") from None


def _validate(stored):
    snap = stored["snapshot"]
    if (snap["collection_mode"], snap["coverage_scope"]) != (tdb.COLLECTION_MODE, tdb.COVERAGE_SCOPE):
        raise _Broken(f"collection_mode {snap['collection_mode']!r} / coverage_scope "
                      f"{snap['coverage_scope']!r} are not this product's")
    if snap["selector_plan"] != ts.PLAN_ID:
        raise _Broken(f"selector_plan {snap['selector_plan']!r} is not {ts.PLAN_ID}")
    if snap["investor_type"] != ts.INVESTOR_TYPE:
        raise _Broken(f"investor_type {snap['investor_type']!r} is not {ts.INVESTOR_TYPE}")
    dates, ohlc = _axis(snap, stored["sessions"])
    codes, returned, nonzero = _observed(snap, stored["brokers"])
    series = _series(codes, nonzero, dates, stored["series"])
    capture_id = _captures(snap["captures"], codes, returned)
    status_of, members_of = _selection(stored["statuses"], stored["members"], dates, returned,
                                       capture_id)
    _unexplained(snap["captures"], members_of)
    return {"snapshot": snap, "dates": dates, "ohlc": ohlc, "codes": codes, "returned": returned,
            "nonzero": nonzero, "series": series, "status_of": status_of,
            "members_of": members_of, "members": stored["members"]}


def _axis(snap, sessions):
    dates = [r["session_date"] for r in sessions]
    n = len(dates)
    if not n:
        raise _Broken("no sessions")
    if [r["session_index"] for r in sessions] != list(range(n)):
        raise _Broken("session_index is not 0..n-1 in session order")
    if not all(_is_date(d) for d in dates) or any(a >= b for a, b in zip(dates, dates[1:])):
        raise _Broken("the session axis is not strictly increasing YYYY-MM-DD dates")
    if ((dates[0], dates[-1], n) != (snap["first_session"], snap["last_session"],
                                     snap["session_count"])
            or dates[-1] != snap["discovery_as_of"]):
        raise _Broken("the snapshot row does not describe its session axis")
    start, end = snap["requested_start_date"], snap["requested_end_date"]
    if not (_is_date(start) and _is_date(end) and start <= dates[0] and dates[-1] <= end):
        raise _Broken(f"sessions {dates[0]}..{dates[-1]} are not inside the requested "
                      f"{start}..{end}")
    ohlc = {f: [r[f] for r in sessions] for f in tdb.OHLC_FIELDS}
    for f, values in ohlc.items():
        if any(v is not None and not _is_number(v) for v in values):
            raise _Broken(f"OHLC {f} holds a value that is neither a finite number nor null")
    return dates, ohlc


def _observed(snap, rows):
    codes = [r["broker"] for r in rows]
    if (not codes or not all(isinstance(b, str) and ts.BROKER_CODE_RE.fullmatch(b) for b in codes)
            or codes != sorted(set(codes))):
        raise _Broken("the observed brokers are not a non-empty set of two-letter codes")
    if snap["observed_broker_count"] != len(codes):
        raise _Broken(f"observed_broker_count {snap['observed_broker_count']!r} != "
                      f"{len(codes)} observed brokers")
    returned, nonzero = {}, {}
    for r in rows:
        flags = (r["in_request_a"], r["in_request_b"])
        if any(not _is_int(f) or f not in (0, 1) for f in flags) or sum(flags) == 0:
            raise _Broken(f"{r['broker']}: request flags {flags}")
        returned[r["broker"]] = tuple(g for g, f in zip(GROUPS, flags) if f)
        nonzero[r["broker"]] = r["nonzero_sessions"]
    return codes, returned, nonzero


def _series_row_problem(r):
    """Why one stored broker-session breaks the series rules, or None."""
    for f in LOT_FIELDS:
        if not _is_int(r[f]):
            return f"{f} {r[f]!r} is not an integer lot count"
    for f in VALUE_FIELDS:
        if not _is_number(r[f]):
            return f"{f} {r[f]!r} is not a finite value"
    if r["blot"] < 0 or r["slot"] < 0:
        return f"negative gross lots (blot {r['blot']}, slot {r['slot']})"
    if r["bval"] < 0 or r["sval"] < 0:
        return f"negative gross value (bval {r['bval']}, sval {r['sval']})"
    if r["nlot"] != r["blot"] - r["slot"]:
        return f"nlot {r['nlot']} != blot - slot ({r['blot']} - {r['slot']})"
    if abs(r["nval"] - (r["bval"] - r["sval"])) > RUPIAH_TOLERANCE:
        return f"|nval - (bval - sval)| exceeds {RUPIAH_TOLERANCE} rupiah"
    if r["blot"] > 0 and r["bval"] == 0:
        return f"{r['blot']} buy lots reported with zero buy value"
    if r["slot"] > 0 and r["sval"] == 0:
        return f"{r['slot']} sell lots reported with zero sell value"
    zero = all(r[f] == 0 for f in SERIES_FIELDS)
    if r["coverage"] != (tdb.OBSERVED_ZERO if zero else tdb.OBSERVED_NONZERO):
        return f"coverage {r['coverage']!r} disagrees with its values"
    return None


def _series(codes, nonzero, dates, rows):
    columns = SERIES_FIELDS + ("coverage",)
    series = {b: {c: [] for c in columns} for b in codes}
    seen = {b: [] for b in codes}
    for r in rows:
        b = r["broker"]
        if b not in series:
            raise _Broken(f"series rows for {b!r}, which is not an observed broker")
        problem = _series_row_problem(r)
        if problem:
            raise _Broken(f"{b} {r['session_date']}: {problem}")
        seen[b].append(r["session_date"])
        for c in columns:
            series[b][c].append(float(r[c]) if c in VALUE_FIELDS else r[c])
    for b in codes:
        if seen[b] != dates:
            raise _Broken(f"{b}: {len(seen[b])} series rows do not match the {len(dates)} "
                          "sessions one for one (an observed broker has one row per session)")
        count = series[b]["coverage"].count(tdb.OBSERVED_NONZERO)
        if nonzero[b] != count:
            raise _Broken(f"{b}: nonzero_sessions {nonzero[b]!r} != {count} OBSERVED_NONZERO rows")
    return series


def _captures(captures, codes, returned):
    """{group: capture_id} of the two captures, checked against the plan and
    the observed brokers' request flags."""
    if [c["request_group"] for c in captures] != list(GROUPS):
        raise _Broken(f"captures {[c['request_group'] for c in captures]} are not the plan's "
                      f"requests {list(GROUPS)}")
    capture_id = {}
    for spec, c in zip(ts.PLAN, captures):
        g = spec.group
        if c["horizons"] != list(spec.horizons) or c["selector_tokens"] != list(spec.tokens):
            raise _Broken(f"capture {g} does not carry the v1 plan's horizons and tokens")
        echo = c["vendor_meta_brokers"]
        if (not isinstance(echo, list) or not all(isinstance(t, str) for t in echo)
                or sorted(echo) != sorted(spec.tokens)):
            raise _Broken(f"capture {g}'s vendor echo {echo!r} does not match its tokens")
        if c["vendor_success"] != 1 or c["source_status"] != "ACCEPTED":
            raise _Broken(f"capture {g} is not an accepted, successful capture")
        if not isinstance(c["capture_id"], str) or not c["capture_id"]:
            raise _Broken(f"capture {g} has no capture_id")
        if c["expanded_brokers"] != [b for b in codes if g in returned[b]]:
            raise _Broken(f"capture {g}'s expanded_brokers disagree with the observed brokers' "
                          "request flags")
        capture_id[g] = c["capture_id"]
    if len(set(capture_id.values())) != len(capture_id):
        raise _Broken("captures A and B share one capture_id")
    return capture_id


def _window_start(horizon, n):
    """Index of the first session of `horizon`'s window over n sessions, or
    None when the axis is shorter than the horizon (INSUFFICIENT history)."""
    k = ts.HORIZON_SESSIONS[horizon]
    if k is None:
        return 0
    return None if n < k else n - k


def _selection(statuses, members, dates, returned, capture_id):
    """({(horizon, metric): status row}, {(horizon, metric): [membership rows]}),
    each stored status and member checked against the plan and the other
    stored rows. Nothing is re-derived."""
    n = len(dates)
    status_of = {(st["horizon"], st["metric"]): st for st in statuses}
    if (len(statuses) != len(PLAN_SELECTORS)
            or set(status_of) != {(sel.period, sel.metric) for _, sel in PLAN_SELECTORS}):
        raise _Broken(f"{len(statuses)} selector statuses, not exactly the "
                      f"{len(PLAN_SELECTORS)} of the v1 plan")
    members_of = {key: [] for key in status_of}
    for m in members:
        key = (m["horizon"], m["metric"])
        if key not in members_of:
            raise _Broken(f"a membership row for {key}, which is not a v1 selector")
        members_of[key].append(m)
    for group, sel in PLAN_SELECTORS:
        st = status_of[(sel.period, sel.metric)]
        rows = members_of[(sel.period, sel.metric)]
        lo = _window_start(sel.period, n)
        window = (None, None) if lo is None else (dates[lo], dates[-1])
        if (st["selector_token"], st["request_group"], st["selector_capture_id"]) != (
                sel.token, group, capture_id[group]):
            raise _Broken(f"{sel.token}: its status row names {st['selector_token']!r}, request "
                          f"{st['request_group']!r}, capture {st['selector_capture_id']!r}")
        if st["status"] not in tdb.SELECTION_STATUSES:
            raise _Broken(f"{sel.token}: unknown status {st['status']!r}")
        if (st["sessions_required"], st["sessions_available"]) != (sel.sessions, n):
            raise _Broken(f"{sel.token}: sessions {st['sessions_available']!r} of "
                          f"{st['sessions_required']!r} on an axis of {n}")
        if (st["window_first_session"], st["window_last_session"]) != window:
            raise _Broken(f"{sel.token}: window {st['window_first_session']!r}.."
                          f"{st['window_last_session']!r} is not {sel.period}'s {window}")
        if (st["status"] == tdb.INSUFFICIENT_HISTORY) != (lo is None):
            raise _Broken(f"{sel.token}: {st['status']} with {n} sessions for {sel.period}")
        if st["status"] == tdb.RESOLVED:
            q = st["qualifying_count"]
            if not _is_int(q) or st["member_count"] != len(rows) or len(rows) != min(sel.n, q):
                raise _Broken(f"{sel.token}: member_count {st['member_count']!r}, "
                              f"qualifying_count {q!r} and {len(rows)} stored membership rows "
                              "disagree")
            if st["boundary_tie_value"] is not None or st["boundary_tie_brokers"] is not None:
                raise _Broken(f"{sel.token}: RESOLVED with a boundary tie recorded")
        elif rows or st["member_count"] != 0:
            raise _Broken(f"{sel.token} is {st['status']} but has membership rows")
        elif st["status"] == tdb.UNRESOLVED_BOUNDARY_TIE:
            tied, edge, q = st["boundary_tie_brokers"], st["boundary_tie_value"], st["qualifying_count"]
            if (not isinstance(tied, list) or len(tied) < 2
                    or not all(isinstance(b, str) and b in returned for b in tied)
                    or len(set(tied)) != len(tied)):
                raise _Broken(f"{sel.token}: boundary_tie_brokers {tied!r} are not two or more "
                              "distinct observed brokers")
            if (not _is_number(edge) or edge * sel.sign <= 0
                    or (sel.unit == "LOT" and edge != int(edge))):
                raise _Broken(f"{sel.token}: boundary_tie_value {edge!r}")
            if not _is_int(q) or q <= sel.n:
                raise _Broken(f"{sel.token}: a boundary tie with qualifying_count {q!r}")
        elif (st["qualifying_count"], st["boundary_tie_value"], st["boundary_tie_brokers"]) != (
                None, None, None):
            raise _Broken(f"{sel.token}: INSUFFICIENT_HISTORY with counts or a tie recorded")
        for m in rows:
            problem = _member_problem(m, st, sel, returned, capture_id, n)
            if problem:
                raise _Broken(f"{sel.token} {m['broker']}: {problem}")
        for m in rows:
            problem = _rank_problem(m, rows, sel)
            if problem:
                raise _Broken(f"{sel.token} {m['broker']}: {problem}")
    return status_of, members_of


def _member_problem(m, st, sel, returned, capture_id, n):
    """Why one stored membership row disagrees with its selector's status row
    and the observed brokers, or None."""
    if m["provenance"] != tdb.PROVENANCE:
        return f"provenance {m['provenance']!r} is not {tdb.PROVENANCE}"
    if (m["selector_token"], m["request_group"], m["selector_capture_id"]) != (
            st["selector_token"], st["request_group"], st["selector_capture_id"]):
        return "its selector fields disagree with the selector's status row"
    if m["broker"] not in returned:
        return "not an observed broker"
    if st["request_group"] not in returned[m["broker"]]:
        return f"not returned by request {st['request_group']}, which carried the selector"
    window = (st["window_first_session"], st["window_last_session"],
              n if sel.sessions is None else sel.sessions)
    if (m["window_first_session"], m["window_last_session"], m["window_sessions"]) != window:
        return "its window fields disagree with the selector's window"
    if m["union_capture_ids"] != [capture_id[g] for g in GROUPS]:
        return f"union_capture_ids {m['union_capture_ids']!r} are not the captures A then B"
    value = m["window_value"]
    if not _is_number(value) or value * sel.sign <= 0:
        return f"window_value {value!r} does not have the {sel.tx} sign"
    if sel.unit == "LOT" and value != int(value):
        return f"LOT window_value {value!r} is not a whole number"
    if not _is_int(m["rank"]) or not 1 <= m["rank"] <= sel.n:
        return f"rank {m['rank']!r} is not 1..{sel.n}"
    if m["tied"] not in (0, 1):
        return f"tied {m['tied']!r} is not 0 or 1"
    return None


def _rank_problem(m, rows, sel):
    """Why a member's stored rank or tied flag disagrees with the STORED values
    of its selector's members, or None."""
    value = m["window_value"]
    better = sum(1 for o in rows if sel.sign * o["window_value"] > sel.sign * value)
    if m["rank"] != 1 + better:
        return f"rank {m['rank']} but {better} stored member(s) have a better value"
    shared = sum(1 for o in rows if o["window_value"] == value)
    if bool(m["tied"]) != (shared > 1):
        return f"tied {m['tied']} but {shared} stored member(s) have its value"
    return None


def _unexplained(captures, members_of):
    """Each capture's stored unexplained_brokers must be its returned brokers
    without a stored RESOLVED membership through that request."""
    explained = {g: set() for g in GROUPS}
    for rows in members_of.values():
        for m in rows:
            explained[m["request_group"]].add(m["broker"])
    for c in captures:
        want = [b for b in c["expanded_brokers"] if b not in explained[c["request_group"]]]
        if c["unexplained_brokers"] != want:
            raise _Broken(f"capture {c['request_group']}'s unexplained_brokers "
                          f"{c['unexplained_brokers']} disagree with its stored RESOLVED members "
                          f"(expected {want})")


# ── the basis reference ──────────────────────

def _basis_reference():
    """{"canonical_json_sha256", "intervals": {ticker: [(first, last), ...]}} of BASIS_FILE
    (module docstring, BASIS REFERENCE), or BasisReferenceError."""
    try:
        with open(BASIS_FILE, "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise BasisReferenceError(f"{BASIS_SOURCE_ID} cannot be read as JSON "
                                  f"({type(e).__name__}: {e}); no basis statement can be made "
                                  "without it") from None
    return _basis_reference_document(doc)


def _basis_reference_document(doc):
    """Validate immutable reference content using the unchanged v1 interval rule."""
    regimes = doc.get("regimes") if isinstance(doc, dict) else None
    if not isinstance(regimes, list):
        raise BasisReferenceError(f"{BASIS_SOURCE_ID} has no regimes list")
    intervals = {}
    for reg in regimes:
        if not isinstance(reg, dict):
            raise BasisReferenceError(f"{BASIS_SOURCE_ID}: a regime that is not an object")
        t, first, last = reg.get("ticker"), reg.get("regime_first_date"), reg.get("regime_last_date")
        if not (isinstance(t, str) and t and _is_date(first) and _is_date(last) and first <= last):
            raise BasisReferenceError(f"{BASIS_SOURCE_ID}: a regime without a ticker and "
                                      f"first <= last dates: {str(reg)[:200]}")
        intervals.setdefault(t, set()).add((first, last))
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {"canonical_json_sha256": hashlib.sha256(canonical.encode("ascii")).hexdigest(),
            "intervals": {t: sorted(v) for t, v in intervals.items()}}


# ── the document ─────────────────────────────

def _metric_value(value, sel):
    """A stored window value in its unit: an exact int for LOT, a float for VAL."""
    if value is None:
        return None
    return int(value) if sel.unit == "LOT" else float(value)


def _cell(st, broker, held):
    status = st["status"]
    if status == tdb.RESOLVED:
        state = MEMBER if held else NOT_MEMBER
    else:
        state = UNDETERMINED
    return {"state": state, "selector_status": status,
            "boundary_tie_candidate": (status == tdb.UNRESOLVED_BOUNDARY_TIE
                                       and broker in st["boundary_tie_brokers"])}


def _window(series, lo, known_conflict):
    """One horizon's window of one broker's series, from session `lo` to the
    last: the six sums and both sides' implied prices (module docstring,
    IMPLIED PRICE)."""
    sums = {f: sum(series[f][lo:]) if f in LOT_FIELDS else math.fsum(series[f][lo:])
            for f in SERIES_FIELDS}
    out = {"sums": sums}
    for side, lot_field, value_field in SIDES:
        lots, values = series[lot_field][lo:], series[value_field][lo:]
        unpriced = [v for q, v in zip(lots, values) if q == 0 and v != 0]
        price = None
        if unpriced:
            status = WITHHELD_VALUE_WITHOUT_REPORTED_LOTS
        elif sums[lot_field] == 0:
            status = NO_LOTS
        elif known_conflict:
            status = WITHHELD_KNOWN_BASIS_CONFLICT
        else:
            status = CALCULATED
            price = sums[value_field] / (SHARES_PER_LOT * sums[lot_field])
        out[side] = {"status": status, PRICE: price, UNPRICED: math.fsum(unpriced)}
    return out


def _document(panel, meta, basis, include_series):
    snap, dates = panel["snapshot"], panel["dates"]
    ticker, as_of, n = snap["ticker"], snap["discovery_as_of"], len(dates)
    sel_of = {(sel.period, sel.metric): sel for _, sel in PLAN_SELECTORS}

    starts = {h: _window_start(h, n) for h in ts.HORIZONS}
    horizons = {h: {"required_sessions": ts.HORIZON_SESSIONS[h], "available_sessions": n,
                    "window_first_session": None if starts[h] is None else dates[starts[h]],
                    "window_last_session": None if starts[h] is None else dates[-1],
                    "history_status": INSUFFICIENT if starts[h] is None else SUFFICIENT}
                for h in ts.HORIZONS}
    intervals = basis["intervals"].get(ticker, [])
    conflict = {h: None if starts[h] is None else (
                    KNOWN_CONFLICT if any(first <= dates[-1] and last >= dates[starts[h]]
                                          for first, last in intervals)
                    else NO_KNOWN_CONFLICT)
                for h in ts.HORIZONS}

    selectors = []
    for _, sel in PLAN_SELECTORS:
        st = panel["status_of"][(sel.period, sel.metric)]
        entry = {c: st[c] for c in STATUS_COLUMNS}
        entry.update(
            boundary_tie_value=_metric_value(st["boundary_tie_value"], sel),
            boundary_tie_brokers=(None if st["boundary_tie_brokers"] is None
                                  else list(st["boundary_tie_brokers"])),
            direction=sel.tx, unit=sel.unit,
            members=[{"rank": m["rank"], "broker": m["broker"],
                      "window_value": _metric_value(m["window_value"], sel),
                      "tied": bool(m["tied"])}
                     for m in panel["members_of"][(sel.period, sel.metric)]])
        selectors.append(entry)

    brokers = {}
    for b in panel["codes"]:
        reasons = []
        for m in panel["members"]:                 # stored order: plan order, then rank
            if m["broker"] != b:
                continue
            sel = sel_of[(m["horizon"], m["metric"])]
            reason = {c: m[c] for c in REASON_COLUMNS}
            reason.update(tied=bool(m["tied"]), window_value=_metric_value(m["window_value"], sel),
                          union_capture_ids=list(m["union_capture_ids"]),
                          direction=sel.tx, unit=sel.unit)
            reasons.append(reason)
        held = {(r["horizon"], r["metric"]) for r in reasons}
        cells = {metric: {h: _cell(panel["status_of"][(h, metric)], b, (h, metric) in held)
                          for h in ts.HORIZONS}
                 for metric in ts.METRICS}
        series = panel["series"][b]
        brokers[b] = {
            "returned_by": list(panel["returned"][b]),
            "nonzero_sessions": panel["nonzero"][b],
            "selection_reasons": reasons,
            "horizons_selected": [h for h in ts.HORIZONS if any(r["horizon"] == h for r in reasons)],
            "directions_selected": [d for d in DIRECTIONS if any(r["direction"] == d for r in reasons)],
            "units_selected": [u for u in UNITS if any(r["unit"] == u for r in reasons)],
            "membership_by_horizon": cells,
            "resolved_member_horizons": {metric: [h for h in ts.HORIZONS
                                                  if cells[metric][h]["state"] == MEMBER]
                                         for metric in ts.METRICS},
            "windows": {h: None if starts[h] is None
                        else _window(series, starts[h], conflict[h] == KNOWN_CONFLICT)
                        for h in ts.HORIZONS},
            "series": ({c: list(series[c]) for c in SERIES_FIELDS + ("coverage",)}
                       if include_series else None),
        }

    selected = [b for b in panel["codes"] if brokers[b]["selection_reasons"]]
    captures = []
    for c in snap["captures"]:
        entry = {col: c[col] for col in CAPTURE_COLUMNS}
        entry["vendor_success"] = c["vendor_success"] == 1
        captures.append(entry)
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "contract": copy.deepcopy(CONTRACT),
        "snapshot": {"ticker": ticker, "discovery_as_of": as_of},
        "source": {
            "product": meta["product"], "panel_schema_version": meta["schema_version"],
            "collection_mode": snap["collection_mode"], "selector_plan": snap["selector_plan"],
            "investor_type": snap["investor_type"],
            "requested_start_date": snap["requested_start_date"],
            "requested_end_date": snap["requested_end_date"],
            "run_id": snap["run_id"], "content_sha256": snap["content_sha256"],
            "recorded_utc": snap["recorded_utc"], "captures": captures,
        },
        "basis_reference": {
            "source_id": BASIS_SOURCE_ID, "canonical_json_sha256": basis["canonical_json_sha256"],
            "known_conflict_intervals": [[first, last] for first, last in intervals],
            "known_basis_conflict_by_horizon": conflict,
        },
        "series_included": include_series,
        "axis": {"sessions": list(dates),
                 "ohlc": ({f: list(panel["ohlc"][f]) for f in tdb.OHLC_FIELDS}
                          if include_series else None)},
        "horizons": horizons,
        "selectors": selectors,
        "observed_brokers": list(panel["codes"]),
        "selected_brokers": selected,
        "unexplained_brokers": [b for b in panel["codes"] if b not in selected],
        "brokers": brokers,
    }


# ── the v1 output schema, checked on every document ──

class _Opt:
    def __init__(self, spec):
        self.spec = spec


class _List:
    def __init__(self, spec, size=None):
        self.spec, self.size = spec, size


class _Map:
    """A dict with exactly `keys` (broker codes when keys is None), each value `spec`."""

    def __init__(self, keys, spec):
        self.keys, self.spec = keys, spec


class _Enum:
    def __init__(self, *values):
        self.values = values


class _Const:
    def __init__(self, value):
        self.value = value


def _text(v):
    return isinstance(v, str)


def _integer(v):
    return _is_int(v)


def _number(v):
    return _is_number(v)


def _rupiah(v):
    return isinstance(v, float) and math.isfinite(v)


def _flag(v):
    return isinstance(v, bool)


def _day(v):
    return _is_date(v)


def _code(v):
    return isinstance(v, str) and bool(ts.BROKER_CODE_RE.fullmatch(v))


def _ticker(v):
    return isinstance(v, str) and bool(ts.TICKER_RE.fullmatch(v))


def _hex64(v):
    return isinstance(v, str) and bool(_HEX64_RE.fullmatch(v))


_HORIZON = _Enum(*ts.HORIZONS)
_METRIC = _Enum(*ts.METRICS)
_GROUP = _Enum(*GROUPS)
_SELECTOR_STATUS = _Enum(*tdb.SELECTION_STATUSES)
_SIDE = {"status": _Enum(*PRICE_STATUSES), PRICE: _Opt(_rupiah), UNPRICED: _rupiah}
_WINDOW = {"sums": {"blot": _integer, "bval": _rupiah, "slot": _integer, "sval": _rupiah,
                    "nlot": _integer, "nval": _rupiah},
           "gross_buy": _SIDE, "gross_sell": _SIDE}
_SERIES = {"blot": _List(_integer), "bval": _List(_rupiah), "slot": _List(_integer),
           "sval": _List(_rupiah), "nlot": _List(_integer), "nval": _List(_rupiah),
           "coverage": _List(_Enum(tdb.OBSERVED_ZERO, tdb.OBSERVED_NONZERO))}
_REASON = {"selector_token": _text, "horizon": _HORIZON, "metric": _METRIC,
           "direction": _Enum(*DIRECTIONS), "unit": _Enum(*UNITS), "rank": _integer,
           "tied": _flag, "window_value": _number, "window_first_session": _day,
           "window_last_session": _day, "window_sessions": _integer, "request_group": _GROUP,
           "selector_capture_id": _text, "union_capture_ids": _List(_text, 2),
           "provenance": _Const(tdb.PROVENANCE)}
_CELL = {"state": _Enum(MEMBER, NOT_MEMBER, UNDETERMINED), "selector_status": _SELECTOR_STATUS,
         "boundary_tie_candidate": _flag}
_BROKER = {"returned_by": _List(_GROUP), "nonzero_sessions": _integer,
           "selection_reasons": _List(_REASON), "horizons_selected": _List(_HORIZON),
           "directions_selected": _List(_Enum(*DIRECTIONS)), "units_selected": _List(_Enum(*UNITS)),
           "membership_by_horizon": _Map(ts.METRICS, _Map(ts.HORIZONS, _CELL)),
           "resolved_member_horizons": _Map(ts.METRICS, _List(_HORIZON)),
           "windows": _Map(ts.HORIZONS, _Opt(_WINDOW)), "series": _Opt(_SERIES)}
_SELECTOR = {"selector_token": _text, "horizon": _HORIZON, "metric": _METRIC,
             "direction": _Enum(*DIRECTIONS), "unit": _Enum(*UNITS), "request_group": _GROUP,
             "selector_capture_id": _text, "status": _SELECTOR_STATUS,
             "sessions_required": _Opt(_integer), "sessions_available": _integer,
             "window_first_session": _Opt(_day), "window_last_session": _Opt(_day),
             "qualifying_count": _Opt(_integer), "member_count": _integer,
             "boundary_tie_value": _Opt(_number), "boundary_tie_brokers": _Opt(_List(_code)),
             "members": _List({"rank": _integer, "broker": _code, "window_value": _number,
                               "tied": _flag})}
_CAPTURE = {"request_group": _GROUP, "horizons": _List(_HORIZON), "selector_tokens": _List(_text),
            "capture_id": _text, "manifest_run_id": _text, "attempt": _integer,
            "query_sha256": _text, "http_status": _Opt(_integer), "response_bytes": _Opt(_integer),
            "response_sha256": _Opt(_text), "response_text_sha256": _Opt(_text),
            "captured_at": _Opt(_text), "vendor_success": _Const(True),
            "vendor_meta_brokers": _List(_text), "expanded_brokers": _List(_code),
            "unexplained_brokers": _List(_code), "source_status": _Const("ACCEPTED")}
DOCUMENT = {
    "schema": _Const(SCHEMA),
    "schema_version": _Const(SCHEMA_VERSION),
    "contract": {k: _Const(v) for k, v in CONTRACT.items()},
    "snapshot": {"ticker": _ticker, "discovery_as_of": _day},
    "source": {"product": _Const(tdb.PRODUCT), "panel_schema_version": _Const(tdb.SCHEMA_VERSION),
               "collection_mode": _Const(tdb.COLLECTION_MODE), "selector_plan": _Const(ts.PLAN_ID),
               "investor_type": _Const(ts.INVESTOR_TYPE), "requested_start_date": _day,
               "requested_end_date": _day, "run_id": _text, "content_sha256": _text,
               "recorded_utc": _text, "captures": _List(_CAPTURE, len(GROUPS))},
    "basis_reference": {"source_id": _Const(BASIS_SOURCE_ID), "canonical_json_sha256": _hex64,
                        "known_conflict_intervals": _List(_List(_day, 2)),
                        "known_basis_conflict_by_horizon": _Map(
                            ts.HORIZONS, _Opt(_Enum(KNOWN_CONFLICT, NO_KNOWN_CONFLICT)))},
    "series_included": _flag,
    "axis": {"sessions": _List(_day),
             "ohlc": _Opt({f: _List(_Opt(_number)) for f in tdb.OHLC_FIELDS})},
    "horizons": _Map(ts.HORIZONS, {"required_sessions": _Opt(_integer),
                                   "available_sessions": _integer,
                                   "window_first_session": _Opt(_day),
                                   "window_last_session": _Opt(_day),
                                   "history_status": _Enum(SUFFICIENT, INSUFFICIENT)}),
    "selectors": _List(_SELECTOR, len(PLAN_SELECTORS)),
    "observed_brokers": _List(_code),
    "selected_brokers": _List(_code),
    "unexplained_brokers": _List(_code),
    "brokers": _Map(None, _BROKER),
}


def _conform(value, spec, where):
    """ObservationContractError unless `value` has exactly the shape `spec`
    describes: every key present, no key added, every leaf of its type."""
    if isinstance(spec, dict):
        if not isinstance(value, dict) or set(value) != set(spec):
            got = sorted(value) if isinstance(value, dict) else type(value).__name__
            raise ObservationContractError(f"{where}: keys {got} are not exactly {sorted(spec)}")
        for key, sub in spec.items():
            _conform(value[key], sub, f"{where}.{key}")
    elif isinstance(spec, _Opt):
        if value is not None:
            _conform(value, spec.spec, where)
    elif isinstance(spec, _List):
        if not isinstance(value, list) or (spec.size is not None and len(value) != spec.size):
            raise ObservationContractError(f"{where}: not a list"
                                           + ("" if spec.size is None else f" of {spec.size}"))
        for i, item in enumerate(value):
            _conform(item, spec.spec, f"{where}[{i}]")
    elif isinstance(spec, _Map):
        if not isinstance(value, dict):
            raise ObservationContractError(f"{where}: not an object")
        if spec.keys is None:
            if not all(_code(k) for k in value):
                raise ObservationContractError(f"{where}: keys that are not broker codes")
        elif set(value) != set(spec.keys):
            raise ObservationContractError(f"{where}: keys {sorted(value)} are not exactly "
                                           f"{sorted(spec.keys)}")
        for key in value:
            _conform(value[key], spec.spec, f"{where}.{key}")
    elif isinstance(spec, _Enum):
        if not any(type(value) is type(v) and value == v for v in spec.values):
            raise ObservationContractError(f"{where}: {value!r} is not one of {spec.values}")
    elif isinstance(spec, _Const):
        if type(value) is not type(spec.value) or value != spec.value:
            raise ObservationContractError(f"{where}: {value!r} is not {spec.value!r}")
    elif not spec(value):
        raise ObservationContractError(f"{where}: {value!r} is not {spec.__name__.lstrip('_')}")


def _check_document(doc):
    """ObservationContractError unless `doc` conforms to DOCUMENT and its
    parts agree: lossless consolidation, the broker partition, the membership
    rules, the price rules, one value per session, canonical JSON."""
    _conform(doc, DOCUMENT, "document")
    problems = []
    n = len(doc["axis"]["sessions"])
    series_in = doc["series_included"]
    entries = doc["brokers"]
    if ((doc["axis"]["ohlc"] is not None) != series_in
            or any((e["series"] is not None) != series_in for e in entries.values())):
        problems.append("series_included disagrees with the OHLC and series present")
    columns = [] if doc["axis"]["ohlc"] is None else list(doc["axis"]["ohlc"].values())
    columns += [col for e in entries.values() if e["series"] for col in e["series"].values()]
    if any(len(col) != n for col in columns):
        problems.append("a daily column is not one value per session")
    if [s["selector_token"] for s in doc["selectors"]] != [sel.token for _, sel in PLAN_SELECTORS]:
        problems.append("selectors are not the 16 of the v1 plan in plan order")

    def unit_typed(value, unit):
        return _is_int(value) if unit == "LOT" else isinstance(value, float)
    for s in doc["selectors"]:
        values = [m["window_value"] for m in s["members"]]
        if s["boundary_tie_value"] is not None:
            values.append(s["boundary_tie_value"])
        if not all(unit_typed(v, s["unit"]) for v in values):
            problems.append(f"{s['selector_token']}: a value not typed by its unit")
    for b, e in entries.items():
        if not all(unit_typed(r["window_value"], r["unit"]) for r in e["selection_reasons"]):
            problems.append(f"{b}: a reason value not typed by its unit")

    observed = doc["observed_brokers"]
    with_reasons = [b for b in observed if b in entries and entries[b]["selection_reasons"]]
    if observed != sorted(set(observed)) or sorted(entries) != observed:
        problems.append("brokers are not exactly the observed brokers")
    if (doc["selected_brokers"] != with_reasons
            or doc["unexplained_brokers"] != [b for b in observed if b not in with_reasons]):
        problems.append("selected and unexplained brokers do not partition the observed brokers")
    held = sorted((s["horizon"], s["metric"], m["broker"], m["rank"], m["window_value"], m["tied"])
                  for s in doc["selectors"] for m in s["members"])
    given = sorted((r["horizon"], r["metric"], b, r["rank"], r["window_value"], r["tied"])
                   for b, e in entries.items() for r in e["selection_reasons"])
    if held != given:
        problems.append("the selection reasons are not the stored members one for one")

    status = {(s["horizon"], s["metric"]): s["status"] for s in doc["selectors"]}
    for b, e in entries.items():
        reasons = {(r["horizon"], r["metric"]) for r in e["selection_reasons"]}
        for metric, row in e["membership_by_horizon"].items():
            for h, cell in row.items():
                st = status[(h, metric)]
                want = (MEMBER if (h, metric) in reasons else NOT_MEMBER) if st == tdb.RESOLVED \
                    else UNDETERMINED
                if cell["state"] != want or cell["selector_status"] != st:
                    problems.append(f"{b} {metric} {h}: {cell} under {st}")
            if e["resolved_member_horizons"][metric] != [h for h in ts.HORIZONS
                                                         if row[h]["state"] == MEMBER]:
                problems.append(f"{b} {metric}: resolved_member_horizons disagree with the cells")
        for h, w in e["windows"].items():
            if (w is None) != (doc["horizons"][h]["history_status"] == INSUFFICIENT):
                problems.append(f"{b} {h}: a window where history is insufficient, or none where "
                                "it suffices")
            if w is None:
                continue
            for side, _, _ in SIDES:
                x = w[side]
                calculated = x["status"] == CALCULATED
                if (x[PRICE] is not None) != calculated or (calculated and x[PRICE] <= 0):
                    problems.append(f"{b} {h} {side}: a price without CALCULATED, or the reverse")
                if x[UNPRICED] < 0 or (x[UNPRICED] > 0) != (
                        x["status"] == WITHHELD_VALUE_WITHOUT_REPORTED_LOTS):
                    problems.append(f"{b} {h} {side}: {UNPRICED} disagrees with its status")
    try:
        observation_json(doc)
    except ValueError as e:
        problems.append(f"not canonical JSON: {e}")
    if problems:
        raise ObservationContractError("; ".join(problems[:5]))


# ── CLI ──────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only observations of the targeted broker actor panel (JSON on stdout).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="the stored snapshots")
    ls.add_argument("--db", required=True)
    ls.add_argument("ticker", nargs="?")
    ob = sub.add_parser("observe", help="one snapshot's observation, as canonical JSON")
    ob.add_argument("--db", required=True)
    ob.add_argument("--as-of", default=None, help="discovery_as_of; default: the latest")
    ob.add_argument("--no-series", action="store_true", help="omit the OHLC and daily series")
    ob.add_argument("ticker")
    a = ap.parse_args(argv)
    try:
        conn = open_readonly(a.db)
        try:
            if a.cmd == "list":
                text = json.dumps(list_snapshots(conn, a.ticker), sort_keys=True,
                                  separators=(",", ":"), allow_nan=False)
            else:
                text = observation_json(observe(conn, a.ticker, a.as_of,
                                                include_series=not a.no_series))
        finally:
            conn.close()
    except (OSError, ValueError, LookupError, RuntimeError, sqlite3.Error,
            ReadOnlySourceStateError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
