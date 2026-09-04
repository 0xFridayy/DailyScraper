"""Idempotent ingestion of a Stock Detail capture into the ownership DB.

One call to ingest_capture() takes the raw text/JSON captured for ONE
ticker's page load (see ownership_capture.py) plus a single captured_at
timestamp for that whole page load, parses it via ownership_parse, and
writes rows into every table via INSERT OR IGNORE against each table's
UNIQUE/PRIMARY KEY.

Idempotency + anti-leakage policy (deliberate, see STOCK_DETAIL_OWNERSHIP_
DISCOVERY.md section 3 and this turn's instructions):

  - INSERT OR IGNORE, first-write-wins. Re-running the collector against
    the same underlying fact (same natural key) is a silent no-op: it
    does NOT refresh captured_at/available_at, does NOT rewrite values.
    This is what makes "historical rows discovered today keep today's
    available_at, and are never backdated" true by construction -- the
    row's available_at is set once, at first sight, and nothing after
    that can move it earlier OR later.
  - published_at stays NULL (and available_at falls back to captured_at)
    unless the caller supplies independently-verified evidence (currently
    none exists for any of these sources -- see discovery doc section 4/5.2).
  - No entity resolution. investor_name_canonical is always written NULL.
    normalize_entity_name() is used only to raise entity_alias_candidate
    rows for human review.
  - No broker-code resolution. custody_participants.participant_type is
    always written 'unknown'; overlaps_broker_code only records a
    coincidence with the existing broker-flow taxonomy.
"""
import gzip
import hashlib
import json
import re
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ownership_parse as op  # noqa: E402

try:
    from neobdm_scraper import (
        BANDAR_GROUPS, ALGO_BIG_PLAYERS_T1, ALGO_BIG_PLAYERS_T2,
        SMART_MONEY, BROKER_FLOW_CODES,
    )
    _KNOWN_BROKER_CODES = set(BROKER_FLOW_CODES) | set(ALGO_BIG_PLAYERS_T1) | \
        set(ALGO_BIG_PLAYERS_T2) | set(SMART_MONEY) | \
        {c for codes in BANDAR_GROUPS.values() for c in codes}
except Exception:
    # Read-only best-effort import; never block ingestion on it.
    _KNOWN_BROKER_CODES = set()

EXTRACTION_VERSION = "stock_detail_v1"


def sha256_hex(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def resolve_available_at(published_at, captured_at):
    return published_at if published_at else captured_at


# ---------------------------------------------------------------------------
# Bitemporal layer (capture_run / ownership_observation / observation_state).
#
# Everything below is ADDITIVE. The eight INSERT OR IGNORE statements further
# down are untouched, so the legacy tables keep meaning exactly what they have
# always meant -- the state as FIRST observed -- and Experiment #2A0's frozen
# ownership_change cohort is unaffected.
# ---------------------------------------------------------------------------

PAYLOAD_HASH_VERSION = "payload_v1"

#: Panes recorded in capture_run. The four DOM panes plus the two derived
#: sources that come from the Dash callback rather than a pane.
CAPTURE_PANES = (
    "insider-current", "insider-moves", "insider5p-current", "insider5p-moves",
    "balance_position", "badge",
)


def payload_hash_hex(s):
    """FULL-WIDTH sha256 over a canonical payload serialisation.

    Deliberately NOT sha256_hex(). That one is truncated to 16 hex chars and,
    worse, is fed the row key plus a 200-byte prefix of the surrounding pane --
    so it cannot detect a change in the value it is attached to. A 3.1% -> 9.9%
    ownership revision produces a byte-identical legacy raw_hash. This hash
    covers the row's own economically relevant fields and nothing else.

    Timestamps are excluded on purpose: captured_at changes every run, and
    including it would make every capture look like a revision.
    """
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def canonical_json(obj):
    """Stable serialisation: sorted keys, no incidental whitespace, unicode
    preserved. Python's float repr is round-trippable and deterministic, so
    numbers parsed from the same text always render identically."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def business_key(source_family, *key_parts):
    """Canonical serialisation of the SAME natural key the legacy table uses.

    JSON-encoded rather than delimiter-joined because investor names are free
    text and may contain any separator we might pick. None stays null, which
    is distinct from the empty string.
    """
    return canonical_json([source_family, *key_parts])


#: Raw pane fragments live OUTSIDE the committed DB, as immutable per-hash
#: gzip files. A SQLite file re-committed on every capture run costs roughly
#: its full size per commit in new git objects (page shifts defeat delta
#: compression); write-once content-addressed files add only their own bytes,
#: once, regardless of how many runs or tickers reference the same content.
FRAGMENT_DIR_NAME = "ownership_raw_fragments"


def fragment_rel_path(pane_hash):
    """Relative path (POSIX separators, portable in git) for a fragment.
    Two-char hash prefix as a subdirectory keeps any one directory from
    holding tens of thousands of files as captures accumulate."""
    return f"{FRAGMENT_DIR_NAME}/{pane_hash[:2]}/{pane_hash}.txt.gz"


def store_pane_fragment(conn, pane_hash, text, run_id, observed_at):
    """Write the raw pane text as an immutable, content-addressed gzip file
    and index it in capture_payload. A pane byte-identical to one already
    stored costs zero additional bytes AND zero additional rows -- this is
    what makes daily balance_position_traces retention affordable: an
    unchanged monthly chart re-observed daily hashes to the same pane_hash
    and is never written twice.

    Idempotent and safe to call from a read-only-checked context: if the
    hash is already indexed, this is a no-op (both the DB check and the
    on-disk `if not exists` guard the write).
    """
    if not text:
        return False
    already = conn.execute(
        "SELECT 1 FROM capture_payload WHERE pane_hash = ?", (pane_hash,)
    ).fetchone()
    if already:
        return False

    rel_path = fragment_rel_path(pane_hash)
    abs_path = os.path.join(HERE, *rel_path.split("/"))
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    raw_bytes = text.encode("utf-8")
    if not os.path.exists(abs_path):
        with gzip.open(abs_path, "wb") as f:
            f.write(raw_bytes)
    gz_len = os.path.getsize(abs_path)

    conn.execute(
        """INSERT OR IGNORE INTO capture_payload
           (pane_hash, fragment_path, byte_len, gz_byte_len, first_run_id, first_seen_at)
           VALUES (?,?,?,?,?,?)""",
        (pane_hash, rel_path, len(raw_bytes), gz_len, run_id, observed_at),
    )
    return True


def record_capture_run(conn, run_id, ticker, pane, captured_at, status,
                       snapshot_date_seen=None, row_count=None, pane_hash=None,
                       error_detail=None, evidence_class="STRICT",
                       extraction_version=EXTRACTION_VERSION):
    """Write one manifest row. Append-only ACROSS runs; last-attempt-wins
    WITHIN a run, so a resumed capture's successful retry supersedes the
    earlier failure for that same (run_id, ticker, pane).

    row_count=0 and row_count=None are different facts and must stay that way:
    0 means the pane rendered and held no rows, None means it was never parsed.
    """
    conn.execute(
        """INSERT OR REPLACE INTO capture_run
           (run_id, ticker, pane, captured_at, status, snapshot_date_seen,
            row_count, pane_hash, error_detail, extraction_version, evidence_class)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, ticker, pane, captured_at, status, snapshot_date_seen,
         row_count, pane_hash, error_detail, extraction_version, evidence_class),
    )
    return 1


def record_observation(conn, run_id, ticker, source_family, key, payload,
                       observed_at, evidence_class="STRICT",
                       extraction_version=EXTRACTION_VERSION):
    """Append-on-change. Returns 'new', 'unchanged' or 'revised'.

        no prior observation      -> append, revision_number = 0
        payload_hash unchanged    -> append NOTHING, bump last_seen/times_seen
        payload_hash differs      -> append, revision_number = prev + 1

    This is the whole point of the remediation: a business key seen again with
    a different payload becomes a retrievable revision instead of vanishing
    into INSERT OR IGNORE.

    first_seen_at/last_seen_at use MIN/MAX rather than blind assignment so
    replaying runs out of order (as the legacy migration does) cannot move
    first_seen forward or last_seen backward.
    """
    payload_json = canonical_json(payload)
    phash = payload_hash_hex(payload_json)

    state = conn.execute(
        "SELECT current_payload_hash, revision_count FROM observation_state "
        "WHERE business_key = ?", (key,),
    ).fetchone()

    if state is not None and state[0] == phash:
        conn.execute(
            "UPDATE observation_state SET last_seen_at = MAX(last_seen_at, ?), "
            "times_seen = times_seen + 1 WHERE business_key = ?",
            (observed_at, key),
        )
        return "unchanged"

    revision_number = 0 if state is None else state[1] + 1
    cur = conn.execute(
        """INSERT INTO ownership_observation
           (run_id, ticker, source_family, business_key, payload_json,
            payload_hash, observed_at, revision_number, evidence_class,
            extraction_version)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (run_id, ticker, source_family, key, payload_json, phash,
         observed_at, revision_number, evidence_class, extraction_version),
    )
    obs_id = cur.lastrowid

    if state is None:
        conn.execute(
            """INSERT INTO observation_state
               (business_key, ticker, source_family, first_seen_at, last_seen_at,
                times_seen, current_payload_hash, current_obs_id, revision_count,
                evidence_class)
               VALUES (?,?,?,?,?,1,?,?,0,?)""",
            (key, ticker, source_family, observed_at, observed_at,
             phash, obs_id, evidence_class),
        )
        return "new"

    conn.execute(
        """UPDATE observation_state
           SET first_seen_at = MIN(first_seen_at, ?),
               last_seen_at = MAX(last_seen_at, ?),
               times_seen = times_seen + 1,
               current_payload_hash = ?, current_obs_id = ?, revision_count = ?
           WHERE business_key = ?""",
        (observed_at, observed_at, phash, obs_id, revision_number, key),
    )
    return "revised"


#: source_family -> the capture_run.pane it comes from. Two business_key_kinds
#: share a pane on purpose: ownership_snapshot(5pct) and custody_breakdown_snapshot
#: both come from insider5p-current; balance_position_monthly and
#: balance_position_summary_monthly both come from the balance_position pane.
PANE_FOR_SOURCE_FAMILY = {
    "stock_detail_kda1_current": "insider-current",
    "stock_detail_pkda1_moves": "insider-moves",
    "stock_detail_kda5_current": "insider5p-current",
    "stock_detail_pkda5_moves": "insider5p-moves",
    "balance_position_chart": "balance_position",
    "float_holder_badge": "badge",
}


def rebuild_observation_state(conn):
    """Recompute observation_state entirely from ownership_observation +
    capture_run. NOT from ownership_observation alone -- an unchanged-payload
    re-observation deliberately writes no new log row (that is the whole
    point of content-addressing), so the log alone has no record that a
    state was reconfirmed on some later date. The only trace of that
    reconfirmation is capture_run: any later 'ok' run of the same
    (ticker, pane) whose pane_hash matches the hash tied to a key's current
    revision is evidence the key was still there, unchanged, on that date.

    first_seen_at, current_payload_hash and revision_count are EXACT: a
    key's first appearance and every genuine revision always write a real
    observation row, so MIN(observed_at) and the highest-revision_number row
    are authoritative from the log alone, unconditionally.

    last_seen_at and times_seen are reconstructions, not re-derivations of a
    stored fact, and are DELIBERATELY CONSERVATIVE (a safe lower bound, never
    an overstatement): pane_hash is a whole-pane hash, so a run only
    "reconfirms" a key when its ENTIRE pane matches the hash tied to that
    key's current revision. If a sibling row elsewhere in the same pane
    changed, the pane's hash changes too even though this key's own row did
    not -- rebuild then cannot see that reconfirmation and last_seen_at stays
    at the key's own observed_at, understating true freshness. The stated
    invariant is therefore: rebuilt last_seen_at <= incrementally-tracked
    last_seen_at, always, and == whenever nothing else in the same pane
    changed between the two dates (the common case -- most panes are
    unchanged on most days). Recovering the exact value in the partial-change
    case would require re-parsing and diffing every later run's retained
    fragment (capture_payload) row by row; not implemented here.
    """
    latest = {}       # business_key -> observation row (highest revision_number)
    first_seen = {}   # business_key -> earliest observed_at in the log

    for row in conn.execute(
        "SELECT business_key, ticker, source_family, run_id, observed_at, "
        "payload_hash, obs_id, revision_number, evidence_class "
        "FROM ownership_observation ORDER BY business_key, revision_number"
    ):
        key = row[0]
        if key not in first_seen:
            first_seen[key] = row[4]
        cur = latest.get(key)
        if cur is None or row[7] >= cur[7]:
            latest[key] = row

    # (ticker, pane, pane_hash) -> sorted captured_at values of every 'ok' run
    # that produced that exact hash, i.e. every run that reconfirmed it.
    confirmations = {}
    for run_id, ticker, pane, pane_hash, captured_at in conn.execute(
        "SELECT run_id, ticker, pane, pane_hash, captured_at "
        "FROM capture_run WHERE status = 'ok' AND pane_hash IS NOT NULL"
    ):
        confirmations.setdefault((ticker, pane, pane_hash), []).append(captured_at)
    # per-(run_id,ticker,pane) hash lookup, to find the hash tied to a key's
    # CURRENT revision specifically (not just any run of that pane)
    run_hash = {}
    for run_id, ticker, pane, pane_hash in conn.execute(
        "SELECT run_id, ticker, pane, pane_hash FROM capture_run "
        "WHERE status = 'ok' AND pane_hash IS NOT NULL"
    ):
        run_hash[(run_id, ticker, pane)] = pane_hash

    conn.execute("DELETE FROM observation_state")
    for key, row in latest.items():
        (_, ticker, source_family, run_id, observed_at, payload_hash, obs_id,
         revision_number, evidence_class) = row
        pane = PANE_FOR_SOURCE_FAMILY.get(source_family)
        current_hash = run_hash.get((run_id, ticker, pane))
        reconfirming = confirmations.get((ticker, pane, current_hash), []) if current_hash else []
        last_seen_at = max([observed_at, *reconfirming])
        times_seen = len({observed_at, *reconfirming})
        conn.execute(
            """INSERT INTO observation_state
               (business_key, ticker, source_family, first_seen_at, last_seen_at,
                times_seen, current_payload_hash, current_obs_id, revision_count,
                evidence_class)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (key, ticker, source_family, first_seen[key], last_seen_at,
             times_seen, payload_hash, obs_id, revision_number, evidence_class),
        )


def _observe(conn, counts, run_id, ticker, source_family, key, payload,
             observed_at, evidence_class, extraction_version=EXTRACTION_VERSION):
    """record_observation() plus count bookkeeping.

    'unchanged' increments nothing, so an exact replay still returns all-zero
    counts and the existing idempotency tests keep passing unmodified.
    """
    outcome = record_observation(conn, run_id, ticker, source_family, key,
                                 payload, observed_at, evidence_class,
                                 extraction_version)
    if outcome == "new":
        counts["ownership_observation"] += 1
    elif outcome == "revised":
        counts["ownership_observation"] += 1
        counts["ownership_revision"] += 1
    return outcome


def overlaps_broker_code(label):
    """Record-only coincidence check -- never asserts identity."""
    if label and re.fullmatch(r"[A-Z]{1,3}", label) and label in _KNOWN_BROKER_CODES:
        return label
    return None


def _upsert_custody_participant(conn, label, ticker, source_family, captured_at):
    if not label:
        return 0
    cur = conn.execute(
        """INSERT OR IGNORE INTO custody_participants
           (label, participant_type, overlaps_broker_code, notes,
            first_seen_ticker, first_seen_source_family, first_seen_at,
            extraction_version, dq_unresolved_participant)
           VALUES (?, 'unknown', ?, NULL, ?, ?, ?, ?, 1)""",
        (label, overlaps_broker_code(label), ticker, source_family,
         captured_at, EXTRACTION_VERSION),
    )
    return 1 if cur.rowcount > 0 else 0


def _detect_and_insert_alias_candidates(conn, ticker, captured_at):
    """Compare normalized forms of all distinct investor names for the current
    ticker across the complete ticker state and flag candidate variants in
    entity_alias_candidate, updating dq_suspected_entity_name_variant to 1
    for those names."""
    # 1. Fetch all distinct raw investor names from the three name-bearing tables
    names_snapshot = {r[0] for r in conn.execute(
        "SELECT DISTINCT investor_name_raw FROM ownership_snapshot WHERE ticker = ?", (ticker,)
    )}
    names_change = {r[0] for r in conn.execute(
        "SELECT DISTINCT investor_name_raw FROM ownership_change WHERE ticker = ?", (ticker,)
    )}
    names_custody = {r[0] for r in conn.execute(
        "SELECT DISTINCT investor_name_raw FROM custody_breakdown_snapshot WHERE ticker = ?", (ticker,)
    )}

    all_names = names_snapshot | names_change | names_custody

    # 2. Group by normalized key
    groups = {}
    for name in all_names:
        key = op.normalize_entity_name(name)
        if key:
            groups.setdefault(key, set()).add(name)

    # 3. Process each group with size > 1
    inserted = 0
    for key, raw_names in groups.items():
        if len(raw_names) > 1:
            raw_names_list = sorted(list(raw_names))
            # Generate all unique pairs
            for i in range(len(raw_names_list)):
                for j in range(i + 1, len(raw_names_list)):
                    a = raw_names_list[i]
                    b = raw_names_list[j]

                    # Determine source table prioritizing snapshot -> change -> custody
                    if a in names_snapshot:
                        src_tbl = "ownership_snapshot"
                    elif a in names_change:
                        src_tbl = "ownership_change"
                    else:
                        src_tbl = "custody_breakdown_snapshot"

                    cur = conn.execute(
                        """INSERT OR IGNORE INTO entity_alias_candidate
                           (ticker, source_table, name_a, name_b, normalized_key, captured_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (ticker, src_tbl, a, b, key, captured_at),
                    )
                    if cur.rowcount > 0:
                        inserted += 1

            # 4. Set dq_suspected_entity_name_variant = 1 for the variants in snapshot and change tables
            for name in raw_names:
                conn.execute(
                    "UPDATE ownership_snapshot SET dq_suspected_entity_name_variant = 1 WHERE ticker = ? AND investor_name_raw = ?",
                    (ticker, name)
                )
                conn.execute(
                    "UPDATE ownership_change SET dq_suspected_entity_name_variant = 1 WHERE ticker = ? AND investor_name_raw = ?",
                    (ticker, name)
                )

    return inserted


def ingest_capture(conn, ticker, capture, captured_at, source_url,
                    xlsx_verified_published_at=None, run_id=None,
                    evidence_class="STRICT"):
    """capture: dict with keys 'panes' (the 4 pane innerText strings),
    'balance_position_traces' (list of {'name','x','y'}),
    'balance_position_badge' (badge text), 'source_xlsx_url' (str|None).

    Returns a dict of per-table inserted-row counts (rows actually new,
    not rows attempted -- OR IGNORE hits are not counted).

    run_id defaults to captured_at's date, which keeps every existing caller
    and test working unchanged.

    capture_run rows are deliberately NOT counted in the returned dict: a
    manifest row is written on every run, including one that changes nothing,
    so counting them would break the "an identical re-ingestion inserts zero
    rows" contract that ownership_capture.py and the tests rely on. The
    observation counts DO belong there, because an exact replay produces none.
    """
    run_id = run_id or (captured_at or "")[:10]
    counts = {
        "ownership_snapshot": 0, "ownership_change": 0,
        "custody_breakdown_snapshot": 0, "custody_participants": 0,
        "balance_position_monthly": 0, "balance_position_summary_monthly": 0,
        "float_holder_snapshot": 0, "entity_alias_candidate": 0,
        "ownership_observation": 0, "ownership_revision": 0,
    }
    panes = capture.get("panes") or {}
    xlsx_url = capture.get("source_xlsx_url")
    published_at = xlsx_verified_published_at  # None unless caller verified it
    available_at = resolve_available_at(published_at, captured_at)
    dq_unknown_pub = 0 if published_at else 1

    # -- KDA 1% (ownership_snapshot, threshold='1pct') -----------------
    kda1_text = panes.get("insider-current")
    if kda1_text:
        parsed = op.parse_kda1_current(kda1_text)
        snap_date = parsed["snapshot_date"]
        src_family = "stock_detail_kda1_current"
        for row in parsed["rows"]:
            raw_hash = sha256_hex(f"{ticker}|{snap_date}|{row['investor_name_raw']}|{kda1_text[:200]}")
            cur = conn.execute(
                """INSERT OR IGNORE INTO ownership_snapshot
                   (ticker, threshold, snapshot_date, investor_name_raw,
                    investor_name_canonical, investor_category, is_foreign,
                    ownership_pct_raw, ownership_pct, scrip_lot, scrip_pct, scrip_raw,
                    scripless_lot, scripless_pct, scripless_raw, source_xlsx_url,
                    published_at, captured_at, available_at,
                    source_url, source_family, extraction_version, raw_hash,
                    dq_unknown_publication_time, dq_suspected_entity_name_variant)
                   VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (ticker, "1pct", snap_date, row["investor_name_raw"],
                 row["investor_category"], int(row["is_foreign"]),
                 row["ownership_pct_raw"], row["ownership_pct"],
                 row["scrip_lot"], row["scrip_pct"], row["scrip_raw"],
                 row["scripless_lot"], row["scripless_pct"], row["scripless_raw"],
                 xlsx_url, published_at, captured_at, available_at,
                 source_url, src_family, EXTRACTION_VERSION, raw_hash,
                 dq_unknown_pub),
            )
            counts["ownership_snapshot"] += cur.rowcount if cur.rowcount > 0 else 0

            _observe(conn, counts, run_id, ticker, src_family,
                     business_key("ownership_snapshot", ticker, "1pct", snap_date,
                                  row["investor_name_raw"]),
                     {"investor_category": row["investor_category"],
                      "is_foreign": int(row["is_foreign"]),
                      "ownership_pct_raw": row["ownership_pct_raw"],
                      "ownership_pct": row["ownership_pct"],
                      "scrip_lot": row["scrip_lot"], "scrip_pct": row["scrip_pct"],
                      "scrip_raw": row["scrip_raw"],
                      "scripless_lot": row["scripless_lot"],
                      "scripless_pct": row["scripless_pct"],
                      "scripless_raw": row["scripless_raw"]},
                     captured_at, evidence_class)

        kda1_hash = payload_hash_hex(kda1_text)
        store_pane_fragment(conn, kda1_hash, kda1_text, run_id, captured_at)
        record_capture_run(conn, run_id, ticker, "insider-current", captured_at,
                           "ok", snapshot_date_seen=snap_date,
                           row_count=len(parsed["rows"]),
                           pane_hash=kda1_hash,
                           evidence_class=evidence_class)
    else:
        record_capture_run(conn, run_id, ticker, "insider-current", captured_at,
                           "empty_pane", evidence_class=evidence_class,
                           error_detail=("pane absent from DOM" if kda1_text is None
                                         else "pane present but empty"))

    # -- PKDA 1% (ownership_change, threshold='1pct') ------------------
    pkda1_text = panes.get("insider-moves")
    if pkda1_text:
        parsed = op.parse_pkda1_moves(pkda1_text)
        src_family = "stock_detail_pkda1_moves"
        by_key = {}
        for row in parsed["rows"]:
            k = (row["change_date"], row["investor_name_raw"])
            ordinal = by_key.get(k, 0)
            by_key[k] = ordinal + 1
            raw_hash = sha256_hex(
                f"{ticker}|{row['change_date']}|{row['investor_name_raw']}|{ordinal}|{pkda1_text[:200]}")
            cur = conn.execute(
                """INSERT OR IGNORE INTO ownership_change
                   (ticker, threshold, change_date, row_ordinal, investor_name_raw,
                    investor_name_canonical, investor_category, is_foreign,
                    resulting_ownership_pct_raw, resulting_ownership_pct,
                    scrip_lot_change, scripless_lot_change, lot_change,
                    is_custodian_move, custodian_or_code, note, source_xlsx_url,
                    published_at, captured_at, available_at,
                    source_url, source_family, extraction_version, raw_hash,
                    dq_unknown_publication_time, dq_incomplete_historical_depth,
                    dq_unresolved_participant, dq_suspected_entity_name_variant)
                   VALUES (?,?,?,?,?,NULL,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?,?,1,0,?)""",
                (ticker, "1pct", row["change_date"], ordinal, row["investor_name_raw"],
                 row["investor_category"], int(row["is_foreign"]),
                 row["resulting_ownership_pct_raw"], row["resulting_ownership_pct"],
                 row["scrip_lot_change"], row["scripless_lot_change"],
                 row["note"], xlsx_url,
                 published_at, captured_at, available_at,
                 source_url, src_family, EXTRACTION_VERSION, raw_hash,
                 dq_unknown_pub, 0),
            )
            counts["ownership_change"] += cur.rowcount if cur.rowcount > 0 else 0

            _observe(conn, counts, run_id, ticker, src_family,
                     business_key("ownership_change", ticker, "1pct",
                                  row["change_date"], row["investor_name_raw"], ordinal),
                     {"investor_category": row["investor_category"],
                      "is_foreign": int(row["is_foreign"]),
                      "resulting_ownership_pct_raw": row["resulting_ownership_pct_raw"],
                      "resulting_ownership_pct": row["resulting_ownership_pct"],
                      "scrip_lot_change": row["scrip_lot_change"],
                      "scripless_lot_change": row["scripless_lot_change"],
                      "note": row["note"]},
                     captured_at, evidence_class)

        pkda1_hash = payload_hash_hex(pkda1_text)
        store_pane_fragment(conn, pkda1_hash, pkda1_text, run_id, captured_at)
        record_capture_run(conn, run_id, ticker, "insider-moves", captured_at,
                           "ok", row_count=len(parsed["rows"]),
                           pane_hash=pkda1_hash,
                           evidence_class=evidence_class)
    else:
        record_capture_run(conn, run_id, ticker, "insider-moves", captured_at,
                           "empty_pane", evidence_class=evidence_class,
                           error_detail=("pane absent from DOM" if pkda1_text is None
                                         else "pane present but empty"))

    # -- KDA 5% (ownership_snapshot threshold='5pct' + custody_breakdown) --
    kda5_text = panes.get("insider5p-current")
    if kda5_text:
        parsed = op.parse_kda5_current(kda5_text)
        snap_date = parsed["snapshot_date"]
        src_family = "stock_detail_kda5_current"
        for holder in parsed["holders"]:
            raw_hash = sha256_hex(f"{ticker}|{snap_date}|{holder['investor_name_raw']}|{kda5_text[:200]}")
            cur = conn.execute(
                """INSERT OR IGNORE INTO ownership_snapshot
                   (ticker, threshold, snapshot_date, investor_name_raw,
                    investor_name_canonical, investor_category, is_foreign,
                    ownership_pct_raw, ownership_pct, scrip_lot, scrip_pct, scrip_raw,
                    scripless_lot, scripless_pct, scripless_raw, source_xlsx_url,
                    published_at, captured_at, available_at,
                    source_url, source_family, extraction_version, raw_hash,
                    dq_unknown_publication_time, dq_suspected_entity_name_variant)
                   VALUES (?,?,?,?,NULL,NULL,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                           ?,?,?,?,?,?,?,?,0)""",
                (ticker, "5pct", snap_date, holder["investor_name_raw"],
                 int(holder["is_foreign"]),
                 f"{holder['total_pct']}%" if holder["total_pct"] is not None else None,
                 holder["total_pct"],
                 published_at, captured_at, available_at,
                 source_url, src_family, EXTRACTION_VERSION, raw_hash,
                 dq_unknown_pub),
            )
            counts["ownership_snapshot"] += cur.rowcount if cur.rowcount > 0 else 0

            _observe(conn, counts, run_id, ticker, src_family,
                     business_key("ownership_snapshot", ticker, "5pct", snap_date,
                                  holder["investor_name_raw"]),
                     {"is_foreign": int(holder["is_foreign"]),
                      "ownership_pct": holder["total_pct"]},
                     captured_at, evidence_class)

            for ordinal, (label, pct) in enumerate(holder["breakdown"]):
                counts["custody_participants"] += _upsert_custody_participant(
                    conn, label, ticker, src_family, captured_at)
                raw_hash2 = sha256_hex(
                    f"{ticker}|{snap_date}|{holder['investor_name_raw']}|{ordinal}|{label}|{pct}")
                cur2 = conn.execute(
                    """INSERT OR IGNORE INTO custody_breakdown_snapshot
                       (ticker, snapshot_date, investor_name_raw, row_ordinal,
                        investor_total_pct, is_foreign, custodian_label,
                        custodian_pct_of_holder, published_at, captured_at, available_at,
                        source_url, source_family, extraction_version, raw_hash,
                        dq_unknown_publication_time, dq_unresolved_participant)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (ticker, snap_date, holder["investor_name_raw"], ordinal,
                     holder["total_pct"], int(holder["is_foreign"]), label, pct,
                     published_at, captured_at, available_at,
                     source_url, src_family, EXTRACTION_VERSION, raw_hash2,
                     dq_unknown_pub),
                )
                counts["custody_breakdown_snapshot"] += cur2.rowcount if cur2.rowcount > 0 else 0

                _observe(conn, counts, run_id, ticker, src_family,
                         business_key("custody_breakdown_snapshot", ticker, snap_date,
                                      holder["investor_name_raw"], ordinal),
                         {"investor_total_pct": holder["total_pct"],
                          "is_foreign": int(holder["is_foreign"]),
                          "custodian_label": label,
                          "custodian_pct_of_holder": pct},
                         captured_at, evidence_class)

        kda5_hash = payload_hash_hex(kda5_text)
        store_pane_fragment(conn, kda5_hash, kda5_text, run_id, captured_at)
        record_capture_run(conn, run_id, ticker, "insider5p-current", captured_at,
                           "ok", snapshot_date_seen=snap_date,
                           row_count=len(parsed["holders"]),
                           pane_hash=kda5_hash,
                           evidence_class=evidence_class)
    else:
        record_capture_run(conn, run_id, ticker, "insider5p-current", captured_at,
                           "empty_pane", evidence_class=evidence_class,
                           error_detail=("pane absent from DOM" if kda5_text is None
                                         else "pane present but empty"))

    # -- PKDA 5% (ownership_change, threshold='5pct') ------------------
    pkda5_text = panes.get("insider5p-moves")
    if pkda5_text:
        parsed = op.parse_pkda5_moves(pkda5_text)
        src_family = "stock_detail_pkda5_moves"
        by_key = {}
        for row in parsed["rows"]:
            counts["custody_participants"] += _upsert_custody_participant(
                conn, row["custodian_or_code"], ticker, src_family, captured_at)
            k = (row["change_date"], row["investor_name_raw"])
            ordinal = by_key.get(k, 0)
            by_key[k] = ordinal + 1
            raw_hash = sha256_hex(
                f"{ticker}|{row['change_date']}|{row['investor_name_raw']}|{ordinal}|{pkda5_text[:200]}")
            cur = conn.execute(
                """INSERT OR IGNORE INTO ownership_change
                   (ticker, threshold, change_date, row_ordinal, investor_name_raw,
                    investor_name_canonical, investor_category, is_foreign,
                    resulting_ownership_pct_raw, resulting_ownership_pct,
                    scrip_lot_change, scripless_lot_change, lot_change,
                    is_custodian_move, custodian_or_code, note, source_xlsx_url,
                    published_at, captured_at, available_at,
                    source_url, source_family, extraction_version, raw_hash,
                    dq_unknown_publication_time, dq_incomplete_historical_depth,
                    dq_unresolved_participant, dq_suspected_entity_name_variant)
                   VALUES (?,?,?,?,?,NULL,NULL,?,NULL,NULL,NULL,NULL,?,?,?,NULL,NULL,
                           ?,?,?,?,?,?,?,?,0,1,0)""",
                (ticker, "5pct", row["change_date"], ordinal, row["investor_name_raw"],
                 int(row["is_foreign"]), row["lot_change"],
                 int(row["is_custodian_move"]), row["custodian_or_code"],
                 published_at, captured_at, available_at,
                 source_url, src_family, EXTRACTION_VERSION, raw_hash,
                 dq_unknown_pub),
            )
            counts["ownership_change"] += cur.rowcount if cur.rowcount > 0 else 0

            _observe(conn, counts, run_id, ticker, src_family,
                     business_key("ownership_change", ticker, "5pct",
                                  row["change_date"], row["investor_name_raw"], ordinal),
                     {"is_foreign": int(row["is_foreign"]),
                      "lot_change": row["lot_change"],
                      "is_custodian_move": int(row["is_custodian_move"]),
                      "custodian_or_code": row["custodian_or_code"]},
                     captured_at, evidence_class)

        pkda5_hash = payload_hash_hex(pkda5_text)
        store_pane_fragment(conn, pkda5_hash, pkda5_text, run_id, captured_at)
        record_capture_run(conn, run_id, ticker, "insider5p-moves", captured_at,
                           "ok", row_count=len(parsed["rows"]),
                           pane_hash=pkda5_hash,
                           evidence_class=evidence_class)
    else:
        record_capture_run(conn, run_id, ticker, "insider5p-moves", captured_at,
                           "empty_pane", evidence_class=evidence_class,
                           error_detail=("pane absent from DOM" if pkda5_text is None
                                         else "pane present but empty"))

    # -- Balance Position Chart (monthly + summary) --------------------
    traces = capture.get("balance_position_traces")
    if traces:
        parsed = op.parse_balance_position(traces)
        src_family = "balance_position_chart"
        dates = sorted({r["period_date"] for r in parsed["monthly"]} |
                       {r["period_date"] for r in parsed["summary"]})
        earliest = dates[0] if dates else None
        for row in parsed["monthly"]:
            is_earliest = int(row["period_date"] == earliest)
            raw_hash = sha256_hex(f"{ticker}|{row['period_date']}|{row['category']}|{row['lots']}")
            cur = conn.execute(
                """INSERT OR IGNORE INTO balance_position_monthly
                   (ticker, period_date, category, lots, published_at, captured_at,
                    available_at, source_url, source_family, extraction_version, raw_hash,
                    dq_unknown_publication_time, dq_incomplete_historical_depth)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ticker, row["period_date"], row["category"], row["lots"],
                 published_at, captured_at, available_at,
                 source_url, src_family, EXTRACTION_VERSION, raw_hash,
                 dq_unknown_pub, is_earliest),
            )
            counts["balance_position_monthly"] += cur.rowcount if cur.rowcount > 0 else 0

            _observe(conn, counts, run_id, ticker, src_family,
                     business_key("balance_position_monthly", ticker,
                                  row["period_date"], row["category"]),
                     {"lots": row["lots"]}, captured_at, evidence_class)
        for row in parsed["summary"]:
            is_earliest = int(row["period_date"] == earliest)
            raw_hash = sha256_hex(f"{ticker}|{row['period_date']}|summary")
            cur = conn.execute(
                """INSERT OR IGNORE INTO balance_position_summary_monthly
                   (ticker, period_date, pct_retail, pct_institusi, pct_foreign,
                    pct_scripless, published_at, captured_at, available_at,
                    source_url, source_family, extraction_version, raw_hash,
                    dq_unknown_publication_time, dq_incomplete_historical_depth)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ticker, row["period_date"], row.get("pct_retail"), row.get("pct_institusi"),
                 row.get("pct_foreign"), row.get("pct_scripless"),
                 published_at, captured_at, available_at,
                 source_url, src_family, EXTRACTION_VERSION, raw_hash,
                 dq_unknown_pub, is_earliest),
            )
            counts["balance_position_summary_monthly"] += cur.rowcount if cur.rowcount > 0 else 0

            _observe(conn, counts, run_id, ticker, src_family,
                     business_key("balance_position_summary_monthly", ticker,
                                  row["period_date"]),
                     {"pct_retail": row.get("pct_retail"),
                      "pct_institusi": row.get("pct_institusi"),
                      "pct_foreign": row.get("pct_foreign"),
                      "pct_scripless": row.get("pct_scripless")},
                     captured_at, evidence_class)

        bp_hash = payload_hash_hex(canonical_json(traces))
        store_pane_fragment(conn, bp_hash, canonical_json(traces), run_id, captured_at)
        record_capture_run(conn, run_id, ticker, "balance_position", captured_at,
                           "ok", row_count=len(parsed["monthly"]) + len(parsed["summary"]),
                           pane_hash=bp_hash, evidence_class=evidence_class)

        # -- float/holder badge (snapshot_date inferred = latest bp month) --
        badge_text = capture.get("balance_position_badge")
        if badge_text and earliest:
            badge = op.parse_badge(badge_text)
            snap_date = badge["date_range_end"] or dates[-1]
            raw_hash = sha256_hex(f"{ticker}|{snap_date}|{badge_text[:200]}")
            cur = conn.execute(
                """INSERT OR IGNORE INTO float_holder_snapshot
                   (ticker, snapshot_date, free_float_pct, scripless_pct,
                    holder_count_raw, holder_count_approx, published_at, captured_at,
                    available_at, source_url, source_family, extraction_version, raw_hash,
                    dq_unknown_publication_time, dq_rounded_holder_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (ticker, snap_date, badge["free_float_pct"], badge["scripless_pct"],
                 badge["holder_count_raw"], badge["holder_count_approx"],
                 published_at, captured_at, available_at,
                 source_url, "float_holder_badge", EXTRACTION_VERSION, raw_hash,
                 dq_unknown_pub),
            )
            counts["float_holder_snapshot"] += cur.rowcount if cur.rowcount > 0 else 0

            _observe(conn, counts, run_id, ticker, "float_holder_badge",
                     business_key("float_holder_snapshot", ticker, snap_date),
                     {"free_float_pct": badge["free_float_pct"],
                      "scripless_pct": badge["scripless_pct"],
                      "holder_count_raw": badge["holder_count_raw"]},
                     captured_at, evidence_class)

            badge_hash = payload_hash_hex(badge_text)
            store_pane_fragment(conn, badge_hash, badge_text, run_id, captured_at)
            record_capture_run(conn, run_id, ticker, "badge", captured_at, "ok",
                               snapshot_date_seen=snap_date, row_count=1,
                               pane_hash=badge_hash, evidence_class=evidence_class)
        else:
            record_capture_run(conn, run_id, ticker, "badge", captured_at,
                               "empty_pane", evidence_class=evidence_class,
                               error_detail=("badge absent from DOM" if not badge_text
                                             else "no balance-position dates to anchor snapshot_date"))
    else:
        record_capture_run(conn, run_id, ticker, "balance_position", captured_at,
                           "empty_pane", evidence_class=evidence_class,
                           error_detail="no balance_position_traces in this capture")
        record_capture_run(conn, run_id, ticker, "badge", captured_at,
                           "skipped", evidence_class=evidence_class,
                           error_detail="badge snapshot_date depends on balance_position_traces")

    # Execute alias candidate detection once across the complete ticker state
    counts["entity_alias_candidate"] = _detect_and_insert_alias_candidates(conn, ticker, captured_at)

    # Deliberately no conn.commit() here.
    # Commit/rollback semantics belong to the caller:
    #   - ownership_capture.py calls conn.commit() after each successful ticker
    #     in normal mode, or conn.rollback() at the end in --dry-run mode.
    #   - Tests use an in-memory DB and manage their own transactions.
    # Committing inside ingest_capture() would make --dry-run's rollback a no-op
    # because sqlite3 cannot roll back an already-committed transaction.
    return counts
