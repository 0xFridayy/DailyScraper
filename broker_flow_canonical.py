"""Read-only canonical view of broker_flow (HANDOFF Lampiran T).

  RAW        broker_flow.date                          acquisition key, never rewritten
  EVIDENCE   evidence/broker_flow_date_evidence.json   PR #74, broker_flow_regime.py
  CANONICAL  this module                               session-aligned, PROVEN only, deduped

broker_flow.date is when a row was acquired, not one convention for which
market session it holds (Lampiran R, S). The canonical session of every
(broker_flow.date, regime) comes ONLY from the evidence manifest; this module
never derives a session from the calendar or the raw date.

  load_canonical_broker_flow    the trusted view: SOURCE_DATED_BACKFILL,
                                SCAN_VERIFIED and CONTENT_MATCHED rows, at most
                                one row per (canonical_session_date, ticker,
                                broker_code)
  inspect_broker_flow_evidence  every raw row with its evidence record and what
                                the trusted view did with it; nothing upgraded

SNAPSHOT CONTRACT. A manifest describes one exact database. Everything below
fails closed (nothing is returned) and nothing is ever guessed:
  ManifestInvalid   the manifest is malformed, inconsistent, or asserts what the
                    reader cannot re-verify (below)
  ManifestMismatch  the database is not the one the manifest describes: a
                    (date, regime) group without a record (every nightly capture
                    adds one), a record without rows, a changed row count, or any
                    group whose rows no longer hash to rows_sha256 (BACKFILL too:
                    the nightly top-up rewrites backfill values); .diff lists them
                    with bfr.verify_manifest's keys. Also a broker_flow_scan run
                    that no longer proves its record.
  SourceStateError  the database file is multiply linked, not quiescent
                    (-wal/-shm/-journal beside it, unknown format), contains
                    duplicate raw keys, or changed while it was read
So the committed audited manifest reads neobdm.db exactly as the audit saw it
(git show 1aeca53:neobdm.db); a later, frozen database needs a manifest built
for it (broker_flow_regime.py build --new-snapshot --out ..., with the audited
--broker-daily parquet or none). Any CONTENT_MATCHED record in it that is not
exactly the audited one makes the reader refuse the whole manifest (below).

TRUST ROOTS. The audited manifest is recognised by content, never by its
label: sha256(bfr.dumps(manifest)) == AUDITED_MANIFEST_SHA256 (the git blob;
line-ending independent). Every PROVEN claim of any manifest is re-verified:
  SOURCE_DATED_BACKFILL  structurally: BACKFILL regime (bval IS NULL), canonical
                         == broker_flow.date <= BACKFILL_END, the generator's ref
  SCAN_VERIFIED          PR #74's own proof, bfr.scan_evidence, re-run on the
                         database's broker_flow_scan and live rows (it checks the
                         source session against the IDX calendar; the session is
                         the source's label, never the calendar's answer), and
                         the run fingerprint must equal the record's
  CONTENT_MATCHED        needs the gitignored broker_daily.parquet, so the reader
                         cannot redo it: accepted only as the audited manifest's
                         own record (same date, canonical, row_count, rows_sha256)
  MIXED                  exactly the AUDITED_MIXED dates, in both directions
Downgrades to INFERRED_ONLY are allowed; nothing unverifiable is upgraded.
An exact audited conflict remains quarantined while all of its raw group keys,
row counts and row hashes remain present, even if a later manifest downgrades
one side. The anchor preserves a known conflict; it does not promote the
downgraded record into the canonical view.

TRUST. INFERRED_ONLY (a calendar guess) and MIXED (rows of two sessions) never
enter the trusted view: normally listed in .excluded and visible in inspection
with canonical_session_date None. A downgraded record that belongs to an
unchanged anchored conflict is instead marked QUARANTINED with the other raw
conflict records, so accounting remains mutually exclusive.

DEDUPE. One session can be held by several acquisition dates (weekend/holiday
copies; session 2026-07-03 by BACKFILL 07-03 and LIVE 07-05). Per session:
  - its trusted records must share ONE capture_class, else the session is
    quarantined (CAPTURE_CLASSES_DIFFER): backfill netval is lot * close with
    bval/sval/bavg/savg NULL, live netval is value-based; merging would mix
    measurements, and choosing one would be an invented ranking;
  - per (ticker, broker_code) the copies must be identical on (bval, sval,
    netval, bavg, savg), NULL included, else the session is quarantined
    (VALUES_CONFLICT). The whole session, not the key: dropping only the
    conflicting keys would leave a cross-section missing exactly the flows
    both captures saw;
  - otherwise each key is ONE row: the survivor is the copy with the earliest
    acquisition_date, copy_acquisition_dates lists every date holding it, keys
    held by only some copies are kept (union), values are never summed.

MISSINGNESS. Absence is not zero: no row is synthesized, NULL stays None.
get() returns None only for a broker not observed in a covered session and
raises SessionNotCovered for a session with no trusted rows. What a row covers
depends on its capture_class, and none is a full broker universe:
  SELECTOR_UNION_BACKFILL  per ticker, the TOP_5_NB/NS selector union; netval =
                           nlot*100*close/1e9 (0 means nlot 0); other fields NULL
  DOM_TOP15                per BROKER, its rendered top-15 buy and sell tables
                           limited to tracked tickers; a side table can be
                           missing (08-22), so a missing row is weak evidence
  FULL_CALLBACK            per broker, the full PR #72 callback tables
LIVE values are billions of Rupiah at 0.1: 0.0 with an average price > 0 is an
observed value below 0.05bn; a side with value 0 AND average 0 is a side the
table did not show (unknown under DOM_TOP15, not an observed zero).

READ-ONLY. bfr.connect_readonly (mode=ro&immutable=1): no lock, journal, WAL
or write. Multiply linked database files are refused because an alias can hide
sidecars. Raw (date, ticker, broker_code) keys must be unique. The file's
(size, mtime_ns, sha256) is checked before and after the read (the
targeted_actor_observations convention). The manifest is only read.

Usage (a debug summary for verification):
  py -3 broker_flow_canonical.py --db DB [--manifest PATH]
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import broker_flow_regime as bfr

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MANIFEST = os.path.join(HERE, bfr.MANIFEST_PATH)

TRUSTED_CLASSES = (bfr.SOURCE_DATED_BACKFILL, bfr.SCAN_VERIFIED, bfr.CONTENT_MATCHED)

# backfill_inventory.BACKFILL_END (not imported: that module pulls in
# playwright). SOURCE_DATED_BACKFILL is proven by the generation path, which
# only ever writes days <= this; a bval-NULL row after it has no such proof.
BACKFILL_END = "2026-07-04"

# The committed audited-2026-09-30 manifest: sha256 of bfr.dumps(parsed), which
# is the git blob whatever the checkout's line endings. The only manifest whose
# CONTENT_MATCHED records are accepted as they stand.
AUDITED_MANIFEST_SHA256 = "146b3efd7969ed22b0858ed5eac54f227199319d8bf9bcac7b6d211515788025"
AUDITED_MANIFEST_PATH = DEFAULT_MANIFEST

# EvidenceRow.status: what the trusted view did with a raw row.
CANONICAL = "CANONICAL"      # the surviving row of its (session, ticker, broker)
DUPLICATE = "DUPLICATE"      # an identical copy of a CANONICAL row, acquired later
QUARANTINED = "QUARANTINED"  # its session has heterogeneous or conflicting copies
EXCLUDED = "EXCLUDED"        # INFERRED_ONLY / MIXED: no proven session
NOT_COVERED = "NOT_COVERED"  # SessionNotCovered.status: no trusted record holds it

# QuarantinedSession.reasons
CAPTURE_CLASSES_DIFFER = "CAPTURE_CLASSES_DIFFER"
VALUES_CONFLICT = "VALUES_CONFLICT"

# generator_version 1: regime, capture_class and evidence_kind of each class.
_CLASS_RULES = {
    bfr.SOURCE_DATED_BACKFILL: (bfr.BACKFILL, bfr.SELECTOR_UNION_BACKFILL, "generation_path"),
    bfr.SCAN_VERIFIED: (bfr.LIVE, bfr.FULL_CALLBACK, "broker_flow_scan"),
    bfr.CONTENT_MATCHED: (bfr.LIVE, bfr.DOM_TOP15, "content_match"),
    bfr.INFERRED_ONLY: (bfr.LIVE, bfr.DOM_TOP15, "idx_calendar_inference"),
    bfr.MIXED: (bfr.LIVE, bfr.DOM_TOP15, "git_rerun_diff"),
}
_REQUIRED = ("broker_flow_date", "regime", "row_count", "rows_sha256", "date_class",
             "evidence_level", "capture_class", "canonical_session_date",
             "inferred_session_date", "inferred_matches_canonical", "evidence_kind",
             "evidence_ref", "evidence_hash", "content_match")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SIDECARS = ("-wal", "-shm", "-journal")


class ManifestInvalid(ValueError):
    """The manifest is malformed, inconsistent or unverifiable; nothing is read."""


class ManifestMismatch(ValueError):
    """The database is not the one the manifest describes. .diff: {"missing",
    "gone", "count_changed", "content_changed"} lists of (date, regime), as in
    bfr.verify_manifest, or {"scan": [...]}."""

    def __init__(self, message, diff):
        super().__init__(message)
        self.diff = diff


class SourceStateError(RuntimeError):
    """The database file is not a quiescent SQLite file, or changed while read."""


class SessionNotCovered(LookupError):
    """No trusted rows exist for the session: .status QUARANTINED or NOT_COVERED.
    Not the same as a broker absent from a covered session (get() -> None)."""

    def __init__(self, session, status):
        super().__init__(f"{session}: no trusted broker_flow rows ({status})")
        self.session = session
        self.status = status


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    """One observed (session, ticker, broker). Values are raw broker_flow
    values; NULL stays None, and see MISSINGNESS for what 0.0 means."""
    canonical_session_date: str       # from the manifest only (ISO str)
    ticker: str
    broker_code: str
    acquisition_date: str             # broker_flow.date of the surviving raw row
    bval: float | None
    sval: float | None
    netval: float | None
    bavg: float | None
    savg: float | None
    regime: str
    date_class: str
    capture_class: str
    evidence_level: str
    rows_sha256: str                  # manifest hash of the survivor's acquisition record
    copy_acquisition_dates: tuple     # every acquisition date holding this identical row


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One raw broker_flow row and its evidence, as inspected."""
    acquisition_date: str
    ticker: str
    broker_code: str
    bval: float | None
    sval: float | None
    netval: float | None
    bavg: float | None
    savg: float | None
    regime: str
    date_class: str
    capture_class: str
    evidence_level: str
    canonical_session_date: str | None     # None unless PROVEN
    inferred_session_date: str | None      # the manifest's calendar guess; never canonical
    rows_sha256: str
    status: str                            # CANONICAL / DUPLICATE / QUARANTINED / EXCLUDED
    survivor_acquisition_date: str | None  # CANONICAL / DUPLICATE: the row that survived


@dataclass(frozen=True)
class ExcludedRecord:
    acquisition_date: str
    regime: str
    date_class: str
    evidence_level: str
    capture_class: str
    row_count: int


@dataclass(frozen=True)
class QuarantinedSession:
    canonical_session_date: str
    records: tuple      # (acquisition_date, regime, anchoring date/capture classes, row_count)
    reasons: tuple      # CAPTURE_CLASSES_DIFFER and/or VALUES_CONFLICT
    conflicting_keys: int
    sample_keys: tuple  # up to 5 conflicting (ticker, broker_code), sorted
    rows: int           # raw rows withheld


@dataclass(frozen=True)
class CanonicalBrokerFlow:
    rows: tuple               # CanonicalRow, sorted by (session, ticker, broker_code)
    sessions: dict            # covered session -> its one capture_class, sorted
    excluded: tuple           # ExcludedRecord: INFERRED_ONLY / MIXED, withheld
    quarantined: tuple        # QuarantinedSession, withheld
    accounting: dict          # raw rows: canonical + duplicate + quarantined + excluded
    db_path: str
    db_sha256: str
    manifest_path: str
    manifest_sha256: str      # sha256(bfr.dumps(manifest)): line-ending independent
    audited: bool             # the committed audited manifest (by content)
    snapshot: str | None      # the manifest's own input.snapshot label (informational)
    source_commit: str | None
    _index: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "_index", {
            (r.canonical_session_date, r.ticker, r.broker_code): r for r in self.rows})

    def get(self, canonical_session_date, ticker, broker_code):
        """The observed row; None if the broker was not observed in this
        covered session (absence is not zero); SessionNotCovered if the session
        has no trusted rows at all."""
        if canonical_session_date not in self.sessions:
            quarantined = {q.canonical_session_date for q in self.quarantined}
            raise SessionNotCovered(canonical_session_date,
                                    QUARANTINED if canonical_session_date in quarantined
                                    else NOT_COVERED)
        return self._index.get((canonical_session_date, ticker, broker_code))

    @property
    def duplicates_collapsed(self):
        return self.accounting["duplicate"]

    def summary(self):
        return {
            "db": {"path": self.db_path, "sha256": self.db_sha256},
            "manifest": {"path": self.manifest_path, "sha256": self.manifest_sha256,
                         "audited": self.audited, "snapshot": self.snapshot,
                         "source_commit": self.source_commit},
            "sessions": len(self.sessions),
            "sessions_by_capture_class": dict(sorted(Counter(self.sessions.values()).items())),
            "rows": len(self.rows),
            "rows_by_date_class": dict(sorted(Counter(r.date_class for r in self.rows).items())),
            "accounting": dict(self.accounting),
            "excluded": {e.acquisition_date: e.date_class for e in self.excluded},
            "quarantined": {q.canonical_session_date: {"reasons": list(q.reasons),
                                                       "records": [list(r) for r in q.records],
                                                       "conflicting_keys": q.conflicting_keys,
                                                       "rows": q.rows}
                            for q in self.quarantined},
        }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def load_canonical_broker_flow(db_path, manifest_path=DEFAULT_MANIFEST, *, trust="proven"):
    """The trusted canonical view of db_path under manifest_path. trust is
    "proven" only: excluded rows are never promoted; inspect them with
    inspect_broker_flow_evidence. Raises ManifestInvalid, ManifestMismatch,
    SourceStateError."""
    if trust != "proven":
        raise ValueError(f"trust={trust!r}: the canonical view is PROVEN rows only; "
                         "use inspect_broker_flow_evidence to look at excluded rows")
    r = _read(db_path, manifest_path)
    quarantined_keys = _quarantined_record_keys(r.quarantined)
    rows = []
    for (session, ticker, broker), copies in sorted(r.survivors.items()):
        acq, regime, values = copies[0]
        rec = r.records[(acq, regime)]
        rows.append(CanonicalRow(session, ticker, broker, acq, *values, regime,
                                 rec["date_class"], rec["capture_class"], rec["evidence_level"],
                                 rec["rows_sha256"], tuple(c[0] for c in copies)))
    excluded = tuple(ExcludedRecord(d, regime, rec["date_class"], rec["evidence_level"],
                                    rec["capture_class"], rec["row_count"])
                     for (d, regime), rec in sorted(r.records.items())
                     if rec["date_class"] not in TRUSTED_CLASSES
                     and (d, regime) not in quarantined_keys)
    return CanonicalBrokerFlow(
        rows=tuple(rows), sessions=r.sessions, excluded=excluded, quarantined=r.quarantined,
        accounting=r.accounting, db_path=r.db_path, db_sha256=r.db_sha256,
        manifest_path=os.path.abspath(manifest_path), manifest_sha256=r.manifest_sha256,
        audited=r.audited, snapshot=r.manifest["input"].get("snapshot"),
        source_commit=r.manifest["input"].get("source_commit"))


def inspect_broker_flow_evidence(db_path, manifest_path=DEFAULT_MANIFEST):
    """Every raw broker_flow row, once, with its evidence record and status,
    sorted by (acquisition_date, regime, ticker, broker_code). The same checks
    as load_canonical_broker_flow; nothing is filtered, merged or upgraded."""
    r = _read(db_path, manifest_path)
    quarantined_keys = _quarantined_record_keys(r.quarantined)
    out = []
    for key in sorted(r.groups):
        d, regime = key
        rec = r.records[key]
        session = rec["canonical_session_date"]
        for ticker, broker, values in r.groups[key]:
            survivor = None
            if key in quarantined_keys:
                status = QUARANTINED
            elif rec["date_class"] not in TRUSTED_CLASSES:
                status = EXCLUDED
            else:
                survivor = r.survivors[(session, ticker, broker)][0][0]
                status = CANONICAL if survivor == d else DUPLICATE
            out.append(EvidenceRow(d, ticker, broker, *values, regime, rec["date_class"],
                                   rec["capture_class"], rec["evidence_level"], session,
                                   rec["inferred_session_date"], rec["rows_sha256"], status,
                                   survivor))
    return out


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

@dataclass
class _Read:
    manifest: dict
    manifest_sha256: str
    audited: bool
    db_path: str        # resolved: what was checked is what SQLite opened
    db_sha256: str
    records: dict       # (date, regime) -> manifest record
    groups: dict        # (date, regime) -> [(ticker, broker, values)] in (ticker, broker) order
    survivors: dict     # (session, ticker, broker) -> [(acq, regime, values)], earliest first
    sessions: dict
    quarantined: tuple
    accounting: dict


def _read(db_path, manifest_path):
    manifest, digest = _load_json(manifest_path)
    audited = digest == AUDITED_MANIFEST_SHA256
    records = check_manifest(manifest, audited)

    # One resolved name for the sidecar check, the identity hash and the open
    # (another spelling of the same file must not skip the sidecar check).
    db_path = os.fspath(Path(db_path).resolve(strict=True))
    # All database I/O inside one identity bracket; every check after it runs
    # on what was read, so a file that changed meanwhile is refused, not judged.
    before = _source_identity(db_path)
    con = bfr.connect_readonly(db_path)
    try:
        flow = con.execute(f"SELECT {', '.join(bfr.COLUMNS)} FROM broker_flow "
                           "ORDER BY date, bval IS NULL, ticker, broker_code").fetchall()
        scan = bfr._scan_rows(con)
    except sqlite3.DatabaseError as e:
        raise SourceStateError(f"{db_path}: broker_flow cannot be read: {e}") from None
    finally:
        con.close()
    if _source_identity(db_path) != before:
        raise SourceStateError(f"{db_path} changed while it was read; nothing returned")

    _check_raw_keys_unique(flow)
    groups = _check_groups(flow, records)
    _check_scans(scan, records, groups)
    survivors, sessions, quarantined = _collapse(records, groups)
    quarantined_keys = _quarantined_record_keys(quarantined)
    accounting = {
        "canonical": len(survivors),
        "duplicate": sum(len(c) - 1 for c in survivors.values()),
        "quarantined": sum(q.rows for q in quarantined),
        "excluded": sum(rec["row_count"] for key, rec in records.items()
                        if rec["date_class"] not in TRUSTED_CLASSES
                        and key not in quarantined_keys),
        "raw": len(flow),
    }
    if sum(v for k, v in accounting.items() if k != "raw") != accounting["raw"]:
        raise RuntimeError(f"broker_flow rows not accounted for exactly once: {accounting}")
    return _Read(manifest, digest, audited, db_path, before[2], records, groups, survivors,
                 sessions, quarantined, accounting)


def _source_identity(path):
    """(size, mtime_ns, sha256) of a quiescent SQLite file, else SourceStateError.
    Refuses hard links, -wal/-shm/-journal beside it (a live or crashed writer:
    immutable=1 would ignore them), and unknown header formats. Reads only."""
    st = os.stat(path)
    if st.st_nlink > 1:
        raise SourceStateError(f"{path}: SQLite source has {st.st_nlink} hard links; "
                               "sidecars cannot be checked safely through an alias")
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 100 or not data.startswith(_SQLITE_MAGIC):
        raise SourceStateError(f"{path}: not an SQLite database")
    present = [s for s in _SIDECARS if os.path.exists(path + s)]
    if present:
        raise SourceStateError(f"{path}: {', '.join(present)} beside the database; only a "
                               "quiescent source is read: close the writer first")
    if (data[18], data[19]) not in ((1, 1), (2, 2)):
        raise SourceStateError(f"{path}: unknown file format {(data[18], data[19])}")
    st = os.stat(path)
    if st.st_nlink > 1:
        raise SourceStateError(f"{path}: SQLite source has {st.st_nlink} hard links; "
                               "sidecars cannot be checked safely through an alias")
    return st.st_size, st.st_mtime_ns, hashlib.sha256(data).hexdigest()


def _check_raw_keys_unique(flow):
    """Raw identity is (date, ticker, broker_code); duplicates are corruption."""
    seen, duplicates = set(), set()
    for row in flow:
        key = tuple(row[:3])
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if duplicates:
        keys = sorted(duplicates)
        raise SourceStateError(f"broker_flow contains duplicate raw keys {_few(keys)}")


def _check_groups(flow, records):
    """{(date, regime): [(ticker, broker, values)]}, if and only if the rows
    are exactly the manifest's: the same (date, regime) groups, and per group
    the same row_count and rows_sha256 (the bytes of bfr._group_hash, computed
    over exactly the rows returned)."""
    grouped = defaultdict(list)
    for row in flow:
        grouped[(row[0], bfr.regime_of(row[3]))].append(row)
    diff = {"missing": sorted(set(grouped) - set(records)),
            "gone": sorted(set(records) - set(grouped)),
            "count_changed": [], "content_changed": []}
    for key in sorted(set(grouped) & set(records)):
        n, digest = bfr._sha256_rows(grouped[key])
        if n != records[key]["row_count"]:
            diff["count_changed"].append(key)
        elif digest != records[key]["rows_sha256"]:
            diff["content_changed"].append(key)
    if any(diff.values()):
        raise ManifestMismatch(
            "the manifest does not describe this database: "
            + "; ".join(f"{k} {_few(v)}" for k, v in diff.items() if v)
            + ". A database the manifest was not built for needs its own manifest "
              "(broker_flow_regime.py build --new-snapshot --out ..., with the audited "
              "--broker-daily parquet or none); no session is guessed", diff)
    return {key: [(t, b, tuple(v)) for _, t, b, *v in rows] for key, rows in grouped.items()}


def _check_scans(scan, records, groups):
    """PR #74's scan proof, re-run for every LIVE record on the rows just read:
    a date a PERSISTED run proves must be SCAN_VERIFIED with that run's session
    and fingerprint, and a SCAN_VERIFIED record must be proven by one."""
    for key, rec in sorted(records.items()):
        d, regime = key
        if regime != bfr.LIVE:
            continue

        def mismatch(why):
            return ManifestMismatch(f"{key} {rec['date_class']}: broker_flow_scan {why}",
                                    {"scan": [key]})

        try:
            sc = bfr.scan_evidence(scan, d, Counter(b for _, b, _ in groups[key]))
        except bfr.ProvenanceContradiction as e:
            raise mismatch(f"contradicts itself: {e}") from None
        if rec["date_class"] != bfr.SCAN_VERIFIED:
            if sc is not None:
                raise mismatch(f"run {sc['run_started_utc']} proves session {sc['session_date']}; "
                               "the record must be SCAN_VERIFIED")
            continue
        if sc is None:
            raise mismatch("has no PERSISTED run for this date")
        got = (sc["session_date"], "sha256:" + sc["sha256"], sc["run_started_utc"])
        want = (rec["canonical_session_date"], rec["evidence_hash"],
                rec["scan"].get("run_started_utc"))
        if got != want:
            raise mismatch(f"run proves {got}, the record says {want}")


def _collapse_records(records, groups):
    """Collapse the trusted records supplied by the caller."""
    by_session = defaultdict(list)
    for key, rec in records.items():
        if rec["date_class"] in TRUSTED_CLASSES:
            by_session[rec["canonical_session_date"]].append(key)
    survivors, sessions, quarantined = {}, {}, []
    for session, keys in sorted(by_session.items()):
        keys.sort()
        copies = defaultdict(list)
        for d, regime in keys:
            for ticker, broker, values in groups[(d, regime)]:
                copies[(ticker, broker)].append((d, regime, values))
        conflicts = sorted(k for k, cs in copies.items() if len({c[2] for c in cs}) > 1)
        classes = {records[k]["capture_class"] for k in keys}
        reasons = (((CAPTURE_CLASSES_DIFFER,) if len(classes) > 1 else ())
                   + ((VALUES_CONFLICT,) if conflicts else ()))
        if reasons:
            recs = tuple((d, regime, records[(d, regime)]["date_class"],
                          records[(d, regime)]["capture_class"], records[(d, regime)]["row_count"])
                         for d, regime in keys)
            quarantined.append(QuarantinedSession(session, recs, reasons, len(conflicts),
                                                  tuple(conflicts[:5]), sum(r[4] for r in recs)))
            continue
        sessions[session] = classes.pop()
        for (ticker, broker), cs in copies.items():
            survivors[(session, ticker, broker)] = cs   # (acquisition_date, regime) order
    return survivors, sessions, tuple(quarantined)


def _anchored_quarantine_records(records, groups):
    """Audited conflict records whose exact raw groups are still present.

    A later manifest may downgrade evidence, but matching acquisition keys,
    row counts, and row hashes still prove that the audited conflict groups
    themselves have not changed. Only sessions that conflict under those
    audited records are returned.
    """
    anchor = _audited_records()
    unchanged = {
        key: anchored for key, anchored in anchor.items()
        if key in records
        and (records[key]["row_count"], records[key]["rows_sha256"])
        == (anchored["row_count"], anchored["rows_sha256"])
    }
    _, _, quarantined = _collapse_records(unchanged, groups)
    return {
        q.canonical_session_date: {
            (d, regime): unchanged[(d, regime)] for d, regime, *_ in q.records
        }
        for q in quarantined
    }


def _collapse(records, groups):
    """Trusted rows per canonical session, retaining unchanged audited conflicts."""
    survivors, sessions, quarantined = _collapse_records(records, groups)
    quarantined = list(quarantined)
    for session, anchored in _anchored_quarantine_records(records, groups).items():
        combined = {
            key: rec for key, rec in records.items()
            if rec["date_class"] in TRUSTED_CLASSES
            and rec["canonical_session_date"] == session
        }
        for key, rec in anchored.items():
            combined.setdefault(key, rec)
        _, _, forced = _collapse_records(combined, groups)
        if not forced:
            raise RuntimeError(f"anchored quarantine for {session} no longer conflicts")
        survivors = {key: copies for key, copies in survivors.items() if key[0] != session}
        sessions.pop(session, None)
        quarantined = [q for q in quarantined if q.canonical_session_date != session]
        quarantined.extend(forced)
    return survivors, sessions, tuple(sorted(quarantined,
                                              key=lambda q: q.canonical_session_date))


def _quarantined_record_keys(quarantined):
    return {(d, regime) for q in quarantined for d, regime, *_ in q.records}


def _few(keys, n=5):
    return str(keys[:n]) + (f" and {len(keys) - n} more" if len(keys) > n else "")


# --------------------------------------------------------------------------
# Manifest validation
# --------------------------------------------------------------------------

def _load_json(path):
    """(manifest, sha256(bfr.dumps(manifest))) from one read of the file."""
    with open(path, "rb") as f:
        raw = f.read()

    def no_duplicate_keys(pairs):
        keys = [k for k, _ in pairs]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate keys {dupes}")
        return dict(pairs)

    try:
        manifest = json.loads(raw.decode("ascii"), object_pairs_hook=no_duplicate_keys)
        return manifest, hashlib.sha256(bfr.dumps(manifest).encode("ascii")).hexdigest()
    except (UnicodeError, ValueError, TypeError) as e:
        raise ManifestInvalid(f"{path} is not an ASCII JSON manifest: {e}") from None


def check_manifest(m, audited=False):
    """{(broker_flow_date, regime): record} of a structurally valid,
    self-consistent generator-v1 manifest whose PROVEN claims the reader can
    stand behind (module docstring, TRUST ROOTS), else ManifestInvalid.
    audited: m is the committed audited manifest (by content). Does not look at
    the database."""
    if not isinstance(m, dict):
        raise ManifestInvalid("manifest is not a JSON object")
    for k, want in (("schema", bfr.SCHEMA), ("generator", "broker_flow_regime.py"),
                    ("generator_version", bfr.GENERATOR_VERSION)):
        if m.get(k) != want:
            raise ManifestInvalid(f"{k} {m.get(k)!r} != {want!r}")
    if not isinstance(m.get("input"), dict) or not isinstance(m["input"].get("broker_flow"), dict):
        raise ManifestInvalid("input.broker_flow missing")
    records = m.get("records")
    if not isinstance(records, list) or not records:
        raise ManifestInvalid("records: the manifest classifies nothing")
    out = {}
    for r in records:
        key = _check_record(r, m)
        if key in out:
            raise ManifestInvalid(f"duplicate record for {key}")
        out[key] = r
    # self-consistency, with PR #74's own summarizers
    if bfr._summary(records) != m.get("summary"):
        raise ManifestInvalid("summary does not match the records")
    bf = m["input"]["broker_flow"]
    if bfr._regime_counts(records) != bf.get("by_regime") or \
            sum(r["row_count"] for r in records) != bf.get("rows"):
        raise ManifestInvalid("input.broker_flow does not match the records")
    if not audited:
        label = m["input"].get("snapshot")
        if label == bfr.AUDITED_SNAPSHOT["name"]:
            raise ManifestInvalid(f"labelled {label} but not the committed audited manifest "
                                  f"(sha256 {AUDITED_MANIFEST_SHA256[:12]}...)")
        _check_content_matches_are_audited(out)
    return out


def _check_content_matches_are_audited(records):
    matched = {k: r for k, r in records.items() if r["date_class"] == bfr.CONTENT_MATCHED}
    if not matched:
        return
    anchor = _audited_records()
    fields = ("date_class", "capture_class", "canonical_session_date", "row_count", "rows_sha256")
    for key, r in sorted(matched.items()):
        a = anchor.get(key)
        if a is None or any(a[f] != r[f] for f in fields):
            raise ManifestInvalid(
                f"{key} CONTENT_MATCHED {r['canonical_session_date']}: a content match cannot be "
                "re-verified without broker_daily.parquet, and this is not the audited "
                "manifest's record; only the audited manifest may assert it")


def _audited_records():
    """The audited manifest's records, from AUDITED_MANIFEST_PATH, only if that
    file is still exactly the audited manifest."""
    try:
        m, digest = _load_json(AUDITED_MANIFEST_PATH)
    except (OSError, ManifestInvalid) as e:
        raise ManifestInvalid(f"the audited manifest {AUDITED_MANIFEST_PATH} cannot be "
                              f"read: {e}") from None
    if digest != AUDITED_MANIFEST_SHA256:
        raise ManifestInvalid(f"the audited manifest {AUDITED_MANIFEST_PATH} is not the audited "
                              f"content (sha256 {digest[:12]} != {AUDITED_MANIFEST_SHA256[:12]})")
    return {(r["broker_flow_date"], r["regime"]): r for r in m["records"]}


def _iso(value):
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _check_record(r, m):
    if not isinstance(r, dict):
        raise ManifestInvalid(f"record is not an object: {r!r:.80}")
    missing = [k for k in _REQUIRED if k not in r]
    if missing:
        raise ManifestInvalid(f"record {r.get('broker_flow_date')!r} lacks {missing}")
    d, regime, cls = r["broker_flow_date"], r["regime"], r["date_class"]
    if not _iso(d):
        raise ManifestInvalid(f"broker_flow_date {d!r} is not an ISO date")
    key = (d, regime)
    if regime not in (bfr.BACKFILL, bfr.LIVE):
        raise ManifestInvalid(f"{key}: unknown regime {regime!r}")
    if not isinstance(cls, str) or cls not in _CLASS_RULES:
        raise ManifestInvalid(f"{key}: unknown date_class {cls!r}")

    def bad(why):
        return ManifestInvalid(f"{key} {cls}: {why}")

    if r["evidence_level"] != bfr.EVIDENCE_LEVEL[cls]:
        raise bad(f"evidence_level {r['evidence_level']!r} != {bfr.EVIDENCE_LEVEL[cls]!r}")
    want_regime, want_capture, want_kind = _CLASS_RULES[cls]
    if regime != want_regime:
        raise bad(f"regime {regime} is not {want_regime} for this date_class")
    if r["capture_class"] != want_capture:
        raise bad(f"capture_class {r['capture_class']!r} is not {want_capture} for this date_class")
    if r["evidence_kind"] != want_kind:
        raise bad(f"evidence_kind {r['evidence_kind']!r} is not {want_kind!r}")
    n = r["row_count"]
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        raise bad(f"row_count {n!r} is not a positive integer")
    if not isinstance(r["rows_sha256"], str) or not _SHA256.fullmatch(r["rows_sha256"]):
        raise bad(f"rows_sha256 {r['rows_sha256']!r} is not a sha256")
    inferred = r["inferred_session_date"]
    if inferred is not None and not _iso(inferred):
        raise bad(f"inferred_session_date must be NULL or a strict ISO date, got {inferred!r}")

    # MIXED is exactly AUDITED_MIXED, both ways (the generator's first LIVE rule)
    audited_mixed = bfr.AUDITED_MIXED.get(d) if regime == bfr.LIVE else None
    if audited_mixed is not None and cls != bfr.MIXED:
        raise bad("this date is quarantined in AUDITED_MIXED; its record must be MIXED")
    if cls == bfr.MIXED:
        if audited_mixed is None:
            raise bad("MIXED outside AUDITED_MIXED (the only source of MIXED)")
        rerun = audited_mixed["rerun"]
        if (n, r["rows_sha256"]) != (rerun["live_rows"], rerun["rows_sha256"]) or \
                r["inferred_session_date"] is not None:
            raise bad("not the audited rerun state in AUDITED_MIXED")

    canonical = r["canonical_session_date"]
    if cls not in TRUSTED_CLASSES:
        if canonical is not None:
            raise bad(f"canonical_session_date must be NULL, got {canonical!r}")
        return key

    if not _iso(canonical):
        raise bad(f"needs an ISO canonical_session_date, got {canonical!r}")
    if canonical > d:
        raise bad(f"canonical_session_date {canonical} is after broker_flow_date {d}")
    if cls == bfr.SOURCE_DATED_BACKFILL:
        if canonical != d:
            raise bad(f"canonical_session_date {canonical} != broker_flow_date {d}")
        if d > BACKFILL_END:
            raise bad(f"backfill date after BACKFILL_END {BACKFILL_END}: not written by "
                      "backfill_inventory.insert_inventory, so not source-dated")
        if r["evidence_ref"] != bfr.BACKFILL_EVIDENCE_REF:
            raise bad(f"evidence_ref {r['evidence_ref']!r} is not the backfill generation path")
    elif cls == bfr.SCAN_VERIFIED:
        scan = r.get("scan")
        if not isinstance(scan, dict):
            raise bad("no scan evidence")
        if scan.get("session_date") != canonical or canonical == d:
            raise bad(f"scan.session_date {scan.get('session_date')!r} != canonical_session_date "
                      f"{canonical} (must be before {d})")
    else:   # CONTENT_MATCHED
        cm = r["content_match"]
        if not isinstance(cm, dict) or cm.get("matched_session") != canonical or \
                cm.get("full_agreement_sessions") != [canonical] or cm.get("row_count") != n:
            raise bad(f"content_match does not name canonical_session_date {canonical} as the "
                      "one session every row agrees with")
        try:
            lo = (date.fromisoformat(d) - timedelta(days=bfr.CANDIDATE_WINDOW_DAYS)).isoformat()
        except OverflowError:
            raise bad(f"broker_flow_date {d} has no content-match window") from None
        if canonical < lo:
            raise bad(f"canonical_session_date {canonical} is outside the content-match "
                      f"window [{lo}, {d}]")
        ev = m["input"].get("content_match_evidence")
        if not isinstance(ev, dict) or ev.get("status") != "AVAILABLE" or \
                not isinstance(ev.get("sha256"), str) or not ev["sha256"] or \
                r["evidence_hash"] != "sha256:" + ev["sha256"]:
            raise bad(f"evidence_hash {r['evidence_hash']!r} is not the recorded content-match "
                      "evidence (input.content_match_evidence)")
    return key


# --------------------------------------------------------------------------
# Debug summary
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    args = ap.parse_args(argv)
    try:
        cf = load_canonical_broker_flow(args.db, args.manifest)
    except (ManifestInvalid, ManifestMismatch, SourceStateError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(json.dumps(cf.summary(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
