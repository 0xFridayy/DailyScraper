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
import hashlib
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
                    xlsx_verified_published_at=None):
    """capture: dict with keys 'panes' (the 4 pane innerText strings),
    'balance_position_traces' (list of {'name','x','y'}),
    'balance_position_badge' (badge text), 'source_xlsx_url' (str|None).

    Returns a dict of per-table inserted-row counts (rows actually new,
    not rows attempted -- OR IGNORE hits are not counted).
    """
    counts = {
        "ownership_snapshot": 0, "ownership_change": 0,
        "custody_breakdown_snapshot": 0, "custody_participants": 0,
        "balance_position_monthly": 0, "balance_position_summary_monthly": 0,
        "float_holder_snapshot": 0, "entity_alias_candidate": 0,
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
