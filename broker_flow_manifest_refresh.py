"""Refresh the broker_flow evidence manifest for a newer, frozen neobdm.db
(HANDOFF Lampiran U).

  AUDITED ANCHOR  evidence/broker_flow_date_evidence.json (PR #74), by content
  + CURRENT DB    one quiescent neobdm.db, read immutable
  = REFRESHED     a manifest describing exactly that database, written to an
                  explicit --out only after broker_flow_canonical (PR #75)
                  accepts it

Every refresh trusts the audited manifest directly, never an earlier refresh:
there is no manifest chain. The anchor is recognised by content only,
sha256(bfr.dumps(anchor)) == AUDITED_ANCHOR_SHA256, never by path, file name or
input.snapshot label; anything else is refused. The anchor is only read and is
never the output.

RULES, per (broker_flow.date, regime):
  BACKFILL of the anchor   SOURCE_DATED_BACKFILL re-derived by PR #74 from the
                           CURRENT rows (row_count, rows_sha256 current). The
                           nightly top-up rewrites backfill values; the proof is
                           the generation path, which a top-up does not change.
  LIVE of the anchor       historical acquisition rows never change: row_count
                           and rows_sha256 must be exactly the anchor's, else the
                           refresh is refused, whatever the class. Then:
    CONTENT_MATCHED        carried unchanged. broker_daily.parquet is not read;
                           the record is the anchor's, bound to the same rows.
    MIXED                  carried unchanged (PR #74 re-checks AUDITED_MIXED)
    INFERRED_ONLY          carried unchanged; must still be unproven
    SCAN_VERIFIED          re-proven from the CURRENT broker_flow_scan by PR #74's
                           scan_evidence; the proof must be the anchor's
  LIVE after the anchor    PR #74 rules without content-match evidence:
                           SCAN_VERIFIED when a PERSISTED run proves it, else
                           INFERRED_ONLY (canonical_session_date NULL)
  everything else          refused: an anchored group that is gone, a BACKFILL
                           date the anchor does not hold, a LIVE group the anchor
                           does not hold dated inside its coverage, a historical
                           date the current evidence classifies differently

So no CONTENT_MATCHED claim is created (new content matching is a later task),
none is downgraded (a changed or missing one is refused, never turned into
INFERRED_ONLY), and no canonical session comes from the calendar or the raw
date. PR #74 contradictions (scan runs, AUDITED_MIXED state) refuse the refresh.

SELF-VALIDATION. The manifest is written to a temp file beside --out, and
load_canonical_broker_flow(db, temp) must accept it (structure, the database's
groups and hashes, scan re-proof) on the same database bytes the refresh
classified, with every session the anchor quarantines still quarantined. Only
then os.replace(temp, out). Any failure removes the temp file and leaves an
existing --out untouched. Output is deterministic for the same database and
anchor (no wall clock, no paths).

SOURCE. As broker_flow_canonical: bfr.connect_readonly (mode=ro&immutable=1);
-wal/-shm/-journal beside the file, a hard-linked file, or a file that changes
while it is read raise bfc.SourceStateError. neobdm.db is never written.

Usage:
  py -3 broker_flow_manifest_refresh.py --db neobdm.db --out <new manifest path>
        [--source-commit SHA] [--anchor PATH]
"""

import argparse
import copy
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import broker_flow_canonical as bfc
import broker_flow_regime as bfr

HERE = os.path.dirname(os.path.abspath(__file__))
COMMITTED_ANCHOR = os.path.join(HERE, bfr.MANIFEST_PATH)

REFRESH_CONTRACT = "audited-anchor-refresh-v1"
# The audited-2026-09-30 manifest (PR #74), sha256 of bfr.dumps(parsed): the
# only trust root. broker_flow_canonical must anchor the same content.
AUDITED_ANCHOR_SHA256 = "146b3efd7969ed22b0858ed5eac54f227199319d8bf9bcac7b6d211515788025"

# What PR #74, run on the current database without the parquet, must call each
# anchored LIVE class. CONTENT_MATCHED needs the parquet, so it reads as
# INFERRED_ONLY; a scan run proving such a date would contradict the anchor.
_REDERIVED_AS = {bfr.CONTENT_MATCHED: bfr.INFERRED_ONLY, bfr.INFERRED_ONLY: bfr.INFERRED_ONLY,
                 bfr.MIXED: bfr.MIXED, bfr.SCAN_VERIFIED: bfr.SCAN_VERIFIED}
# A re-proven SCAN_VERIFIED record must carry the anchor's proof in these fields.
_SCAN_PROOF = ("canonical_session_date", "capture_class", "evidence_kind", "evidence_ref",
               "evidence_hash", "scan")

RULES = [
    "anchor: the audited manifest by content (sha256 of broker_flow_regime.dumps), never "
    "by path or label; every refresh starts from it, never from another refresh",
    "BACKFILL groups of the anchor: SOURCE_DATED_BACKFILL re-derived from the current rows",
    "LIVE groups of the anchor: row_count and rows_sha256 unchanged, else refused",
    "anchored CONTENT_MATCHED, MIXED, INFERRED_ONLY: carried unchanged; anchored "
    "SCAN_VERIFIED: re-proven from the current broker_flow_scan",
    "LIVE groups after the anchor: SCAN_VERIFIED if a PERSISTED scan run proves them, "
    "else INFERRED_ONLY; never CONTENT_MATCHED",
    "refused: anchored groups gone, BACKFILL dates the anchor does not hold, LIVE groups "
    "inside the anchor's coverage it does not hold, evidence contradicting the anchor",
    "published only after broker_flow_canonical accepts it with every anchored "
    "quarantine still quarantined",
]


class RefreshRefused(ValueError):
    """The refresh cannot stand behind a manifest for this database; nothing is
    written."""


# --------------------------------------------------------------------------
# Anchor and output path
# --------------------------------------------------------------------------

def load_anchor(path=None):
    """(manifest, sha256) of the audited anchor at path (default: the file
    broker_flow_canonical anchors), recognised by content only."""
    path = bfc.AUDITED_MANIFEST_PATH if path is None else path
    if bfc.AUDITED_MANIFEST_SHA256 != AUDITED_ANCHOR_SHA256:
        raise RefreshRefused(
            f"broker_flow_canonical anchors {bfc.AUDITED_MANIFEST_SHA256[:12]}, not the refresh "
            f"trust root {AUDITED_ANCHOR_SHA256[:12]}: the reader would not accept the refresh")
    try:
        anchor, digest = bfc._load_json(path)
    except OSError as e:
        raise RefreshRefused(f"the audited anchor {path} cannot be read: {e}") from None
    except bfc.ManifestInvalid as e:
        raise RefreshRefused(f"the audited anchor is not a manifest: {e}") from None
    if digest != AUDITED_ANCHOR_SHA256:
        label = anchor["input"].get("snapshot") if isinstance(anchor, dict) and \
            isinstance(anchor.get("input"), dict) else None
        raise RefreshRefused(f"{path} is not the audited anchor (sha256 {digest[:12]} != "
                             f"{AUDITED_ANCHOR_SHA256[:12]}); its label {label!r} grants nothing")
    try:
        bfc.check_manifest(anchor, audited=True)
    except bfc.ManifestInvalid as e:
        raise RefreshRefused(f"the audited anchor fails its own checks: {e}") from None
    return anchor, digest


def _same_file(a, b):
    """a and b name one file: the same resolved path (symlinks, junctions, case,
    '..', short names) or the same inode (hard links)."""
    a, b = os.fspath(a), os.fspath(b)
    if os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b)):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def check_out_path(out, db_path, anchor_path=None):
    """Refuse a missing --out and any --out that is, or aliases, the committed
    audited manifest, the anchor in use, or the source database."""
    if out is None or not os.fspath(out).strip():
        raise RefreshRefused("an explicit output path is required; the audited manifest is "
                             "never the output")
    protected = (("the committed audited manifest", COMMITTED_ANCHOR),
                 ("the audited anchor", bfc.AUDITED_MANIFEST_PATH),
                 ("the audited anchor", anchor_path),
                 ("the source database", db_path))
    for what, path in protected:
        if path is not None and _same_file(out, path):
            raise RefreshRefused(f"--out {os.fspath(out)} is {what} ({os.fspath(path)}); "
                                 "a refresh never writes over it")
    if os.path.isdir(out):
        raise RefreshRefused(f"--out {os.fspath(out)} is a directory")


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------

def _by_key(records):
    return {(r["broker_flow_date"], r["regime"]): r for r in records}


def _read_source(db_path, source_commit):
    """(PR #74 manifest of the database without content-match evidence,
    {(date, regime): [(ticker, broker, values)]}, identity), all read inside
    one source-identity bracket; a file that changed meanwhile is refused, not
    judged."""
    before = bfc._source_identity(db_path)
    con = bfr.connect_readonly(db_path)
    contradiction = None
    try:
        try:
            derived = bfr._build(con, None, source_commit, None)
        except bfr.ProvenanceContradiction as e:
            contradiction, derived = e, None
        flow = con.execute(f"SELECT {', '.join(bfr.COLUMNS)} FROM broker_flow "
                           "ORDER BY date, bval IS NULL, ticker, broker_code").fetchall()
    except sqlite3.DatabaseError as e:
        raise bfc.SourceStateError(f"{db_path}: broker_flow cannot be read: {e}") from None
    finally:
        con.close()
    if bfc._source_identity(db_path) != before:
        raise bfc.SourceStateError(f"{db_path} changed while it was read; nothing written")
    if contradiction is not None:
        raise RefreshRefused(f"PR #74 evidence contradicts itself: {contradiction}")
    groups = defaultdict(list)
    for d, t, b, *values in flow:
        groups[(d, bfr.regime_of(values[0]))].append((t, b, tuple(values)))
    return derived, dict(groups), before


def _reconcile(anchor, derived):
    """(records, inheritance) per the module RULES, else RefreshRefused listing
    every violation. derived: PR #74's classification of the current database
    without content-match evidence."""
    anchored = _by_key(anchor["records"])
    current = _by_key(derived["records"])
    coverage = max(d for d, _ in anchored)
    problems = [f"{key} {anchored[key]['date_class']}: anchored group is gone from the database"
                for key in sorted(set(anchored) - set(current))]
    records, changed, carried, new = [], [], Counter(), {}
    for rec in derived["records"]:
        key = (rec["broker_flow_date"], rec["regime"])
        a = anchored.get(key)
        if a is None:
            if key[1] == bfr.BACKFILL:
                problems.append(f"{key}: a BACKFILL date the anchor does not hold (the backfill "
                                f"path ended at {bfc.BACKFILL_END} before the anchor)")
            elif key[0] <= coverage:
                problems.append(f"{key}: a LIVE group the anchor does not hold, inside its "
                                f"coverage (<= {coverage})")
            else:
                new[key[0]] = rec["date_class"]
                records.append(rec)
            continue
        if key[1] == bfr.BACKFILL:
            if (rec["row_count"], rec["rows_sha256"]) != (a["row_count"], a["rows_sha256"]):
                changed.append(key[0])
            if rec != dict(a, row_count=rec["row_count"], rows_sha256=rec["rows_sha256"]):
                problems.append(f"{key}: re-derived record differs from the anchored one beyond "
                                "its rows")
            records.append(rec)
            continue
        cls = a["date_class"]
        if (rec["row_count"], rec["rows_sha256"]) != (a["row_count"], a["rows_sha256"]):
            problems.append(f"{key} {cls}: historical LIVE rows changed "
                            f"({a['row_count']}, {a['rows_sha256'][:12]}) -> "
                            f"({rec['row_count']}, {rec['rows_sha256'][:12]})")
            continue
        if rec["date_class"] != _REDERIVED_AS[cls]:
            problems.append(f"{key} {cls}: the current evidence classifies it {rec['date_class']}")
            continue
        if cls == bfr.SCAN_VERIFIED:
            moved = [f for f in _SCAN_PROOF if rec.get(f) != a.get(f)]
            if moved:
                problems.append(f"{key} SCAN_VERIFIED: the current scan run proves different "
                                f"{moved}")
                continue
        carried[cls] += 1
        records.append(a)
    if problems:
        raise RefreshRefused(f"{len(problems)} violation(s) of {REFRESH_CONTRACT}: "
                             + "; ".join(problems[:8])
                             + (f"; and {len(problems) - 8} more" if len(problems) > 8 else ""))
    inheritance = {
        "backfill": {"rederived": sum(1 for k in anchored if k[1] == bfr.BACKFILL),
                     "changed_since_anchor": changed},
        "anchored_live": dict(sorted(carried.items())),
        "new_live": new,
    }
    return records, inheritance


def build_refreshed_manifest(db_path, anchor_path=None, source_commit=None):
    """(manifest, context) for db_path under the audited anchor; nothing is
    written and nothing is self-validated (refresh() does both). Raises
    RefreshRefused, bfc.SourceStateError."""
    anchor, anchor_sha = load_anchor(anchor_path)
    db_path = os.fspath(Path(db_path).resolve(strict=True))
    derived, groups, identity = _read_source(db_path, source_commit)
    records, inheritance = _reconcile(anchor, derived)

    m = copy.deepcopy(derived)
    m["records"] = records
    m["summary"] = bfr._summary(records)
    m["input"]["snapshot"] = REFRESH_CONTRACT
    # Only the anchor's own CONTENT_MATCHED records cite it (their evidence_hash).
    m["input"]["content_match_evidence"] = copy.deepcopy(anchor["input"]["content_match_evidence"])
    m["refresh"] = {
        "contract": REFRESH_CONTRACT,
        "tool": "broker_flow_manifest_refresh.py",
        "anchor": {"manifest_sha256": anchor_sha,
                   "snapshot": anchor["input"].get("snapshot"),
                   "source_commit": anchor["input"].get("source_commit"),
                   "records": len(anchor["records"]),
                   "last_broker_flow_date": max(r["broker_flow_date"] for r in anchor["records"])},
        "current_db": {"sha256": identity[2], "bytes": identity[0]},
        "inheritance": inheritance,
        "content_match_evidence": "the anchor's: this refresh read no parquet and matched nothing; "
                                  "it backs only the anchor's CONTENT_MATCHED records carried here",
        "rules": RULES,
    }
    return m, {"anchor": anchor, "db_path": db_path, "groups": groups, "identity": identity}


def anchored_quarantine(anchor, groups):
    """Sessions the anchor's own trusted records quarantine on the current rows
    (the reader's collapse rule). Its LIVE groups are unchanged, and a backfill
    top-up cannot move a session or a capture class, so each of these is still
    a conflict in the refreshed view."""
    trusted = {key: r for key, r in _by_key(anchor["records"]).items()
               if r["date_class"] in bfc.TRUSTED_CLASSES}
    return {q.canonical_session_date for q in bfc._collapse_records(trusted, groups)[2]}


def self_validate(manifest, manifest_path, context):
    """The PR #75 reader's view of the database under the manifest file, if the
    reader accepts it and it is the view the refresh promises; else
    RefreshRefused (bfc.SourceStateError if the database moved)."""
    try:
        bfc.check_manifest(manifest, audited=False)
        cf = bfc.load_canonical_broker_flow(context["db_path"], manifest_path)
    except (bfc.ManifestInvalid, bfc.ManifestMismatch) as e:
        raise RefreshRefused(f"broker_flow_canonical refuses the refreshed manifest: {e}") from None
    if cf.db_sha256 != context["identity"][2]:
        raise bfc.SourceStateError(f"{context['db_path']} changed between the refresh and its "
                                   "validation; nothing written")
    cleared = sorted(s for s in anchored_quarantine(context["anchor"], context["groups"])
                     if s in cf.sessions
                     or s not in {q.canonical_session_date for q in cf.quarantined})
    if cleared:
        raise RefreshRefused(f"the refreshed view clears anchored quarantine {cleared}")
    return cf


def refresh(db_path, out, anchor_path=None, source_commit=None):
    """Build, self-validate and atomically publish the refreshed manifest at
    out. Returns (manifest, canonical view). Raises RefreshRefused or
    bfc.SourceStateError; then nothing is written and an existing out is
    untouched."""
    check_out_path(out, db_path, anchor_path)
    manifest, context = build_refreshed_manifest(db_path, anchor_path, source_commit)
    out = os.fspath(out)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(out)}.", suffix=".tmp",
                               dir=os.path.dirname(os.path.abspath(out)))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bfr.dumps(manifest).encode("ascii"))
            f.flush()
            os.fsync(f.fileno())
        cf = self_validate(manifest, tmp, context)
        check_out_path(out, context["db_path"], anchor_path)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise
    return manifest, cf


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, help="a frozen, quiescent neobdm.db")
    ap.add_argument("--out", required=True,
                    help="where the refreshed manifest goes; never the audited manifest")
    ap.add_argument("--anchor", default=None,
                    help="the audited manifest (default: the committed one); checked by content")
    ap.add_argument("--source-commit", default=None,
                    help="informational: the git commit the database was taken from")
    args = ap.parse_args(argv)
    try:
        m, cf = refresh(args.db, args.out, args.anchor, args.source_commit)
    except (RefreshRefused, bfc.SourceStateError, OSError) as e:
        print(f"REFUSED, nothing written: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"out": os.path.abspath(args.out),
                      "refresh": {k: m["refresh"][k] for k in ("contract", "anchor", "current_db")},
                      "new_live": m["refresh"]["inheritance"]["new_live"],
                      "backfill_changed": len(m["refresh"]["inheritance"]["backfill"]
                                              ["changed_since_anchor"]),
                      "summary": m["summary"]["by_date_class"],
                      "canonical": {"sessions": len(cf.sessions), "rows": len(cf.rows),
                                    "accounting": cf.accounting,
                                    "quarantined": [q.canonical_session_date
                                                    for q in cf.quarantined]}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
