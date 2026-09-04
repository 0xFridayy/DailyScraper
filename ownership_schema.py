"""SQLite schema for the point-in-time ownership/custody dataset.

Implements the seven tables reviewed in STOCK_DETAIL_OWNERSHIP_DISCOVERY.md
section 2 (ownership_snapshot, ownership_change, custody_breakdown_snapshot,
custody_participants, balance_position_monthly, balance_position_summary_monthly,
float_holder_snapshot), plus:

  - four-field point-in-time semantics on every date-bearing table
    (snapshot_date/change_date, published_at, captured_at, available_at)
  - provenance columns (source_url, source_family, extraction_version,
    raw_hash) requested for Phase 2A, on every table including the
    custody_participants dimension table
  - data-quality flag columns requested for Phase 2A
  - ONE additional table, `entity_alias_candidate`, beyond the seven
    reviewed ones -- a QA-only bookkeeping table for flagged possible
    name variants (e.g. UBS HONGKONG / UBS HONG KONG). It never merges
    anything; it only records a candidate pair for human review. Called
    out separately in reports so "seven tables" claims stay accurate.

available_at = published_at when independently verifiable, else captured_at.
Nothing here backdates: a row's available_at is fixed at first insert and
never moved earlier or later by re-ingestion (see ownership_ingest.py).

Plus a FOUR-TABLE BITEMPORAL LAYER added alongside (capture_run,
capture_payload, ownership_observation, observation_state). The eight tables
above record only the state as FIRST observed -- a natural key seen again with
a different payload is discarded by INSERT OR IGNORE, so revisions, last_seen_at
and the difference between "holder gone" and "pane failed" are all unavailable
from them. The new layer records that history additively: no existing table's
DDL, key, semantics or rows change, which is what keeps Experiment #2A0's
frozen ownership_change cohort byte-for-byte intact.
"""

SCHEMA_SQL = """
-- KDA 1% / KDA 5% "current" tables -- point-in-time composition of one
-- holder's stake. threshold='5pct' rows come from the insider5p-current
-- pane and do NOT carry scrip/scripless detail (that pane doesn't expose
-- it) -- those columns stay NULL for 5pct rows by design, not by omission.
CREATE TABLE IF NOT EXISTS ownership_snapshot (
    id                      INTEGER PRIMARY KEY,
    ticker                  TEXT NOT NULL,
    threshold               TEXT NOT NULL CHECK (threshold IN ('1pct', '5pct')),
    snapshot_date           TEXT NOT NULL,
    investor_name_raw       TEXT NOT NULL,
    investor_name_canonical TEXT,
    investor_category       TEXT,
    is_foreign              INTEGER,
    ownership_pct_raw       TEXT,
    ownership_pct           REAL,
    scrip_lot               REAL,
    scrip_pct               REAL,
    scrip_raw               TEXT,
    scripless_lot           REAL,
    scripless_pct           REAL,
    scripless_raw           TEXT,
    source_xlsx_url         TEXT,
    published_at            TEXT,
    captured_at             TEXT NOT NULL,
    available_at            TEXT NOT NULL,
    source_url              TEXT NOT NULL,
    source_family           TEXT NOT NULL,
    extraction_version      TEXT NOT NULL,
    raw_hash                TEXT NOT NULL,
    dq_unknown_publication_time INTEGER NOT NULL DEFAULT 0,
    dq_suspected_entity_name_variant INTEGER NOT NULL DEFAULT 0,
    UNIQUE (ticker, threshold, snapshot_date, investor_name_raw)
);

-- PKDA 1% / PKDA 5% "moves" tables -- change log.
-- threshold='1pct': change_date is month-end, category/resulting_pct/
--   scrip_lot_change/scripless_lot_change/note are populated,
--   is_custodian_move/custodian_or_code are NULL (not applicable).
-- threshold='5pct': change_date is daily, is_custodian_move/
--   custodian_or_code/lot_change are populated, the 1pct-only columns
--   are NULL. row_ordinal disambiguates same-day/same-investor rows
--   (confirmed to occur, e.g. two opposite-sign custodian transfers on
--   the same date -- see STOCK_DETAIL_OWNERSHIP_DISCOVERY.md 1.3).
CREATE TABLE IF NOT EXISTS ownership_change (
    id                      INTEGER PRIMARY KEY,
    ticker                  TEXT NOT NULL,
    threshold               TEXT NOT NULL CHECK (threshold IN ('1pct', '5pct')),
    change_date             TEXT NOT NULL,
    row_ordinal             INTEGER NOT NULL DEFAULT 0,
    investor_name_raw       TEXT NOT NULL,
    investor_name_canonical TEXT,
    investor_category       TEXT,
    is_foreign              INTEGER,
    resulting_ownership_pct_raw TEXT,
    resulting_ownership_pct REAL,
    scrip_lot_change        REAL,
    scripless_lot_change    REAL,
    lot_change              REAL,
    is_custodian_move       INTEGER,
    custodian_or_code       TEXT,
    note                    TEXT,
    source_xlsx_url         TEXT,
    published_at            TEXT,
    captured_at             TEXT NOT NULL,
    available_at            TEXT NOT NULL,
    source_url              TEXT NOT NULL,
    source_family           TEXT NOT NULL,
    extraction_version      TEXT NOT NULL,
    raw_hash                TEXT NOT NULL,
    dq_unknown_publication_time INTEGER NOT NULL DEFAULT 0,
    dq_incomplete_historical_depth INTEGER NOT NULL DEFAULT 0,
    dq_unresolved_participant INTEGER NOT NULL DEFAULT 0,
    dq_suspected_entity_name_variant INTEGER NOT NULL DEFAULT 0,
    UNIQUE (ticker, threshold, change_date, investor_name_raw, row_ordinal)
);

-- KDA 5% "current" per-custodian split of one holder's (scripless) stake.
-- custodian_pct_of_holder is confirmed (pass 2, section 5.3, 7/7 examples
-- summing to ~100%) to be % of the HOLDER's own position, never of
-- shares outstanding or free float. row_ordinal because the same label
-- can repeat for one holder (distinct KSEI sub-accounts, e.g. two
-- separate "Bank HSBC" rows for one holder).
CREATE TABLE IF NOT EXISTS custody_breakdown_snapshot (
    id                        INTEGER PRIMARY KEY,
    ticker                    TEXT NOT NULL,
    snapshot_date             TEXT NOT NULL,
    investor_name_raw         TEXT NOT NULL,
    row_ordinal               INTEGER NOT NULL DEFAULT 0,
    investor_total_pct        REAL,
    is_foreign                INTEGER,
    custodian_label           TEXT NOT NULL,
    custodian_pct_of_holder   REAL,
    published_at              TEXT,
    captured_at               TEXT NOT NULL,
    available_at              TEXT NOT NULL,
    source_url                TEXT NOT NULL,
    source_family             TEXT NOT NULL,
    extraction_version        TEXT NOT NULL,
    raw_hash                  TEXT NOT NULL,
    dq_unknown_publication_time INTEGER NOT NULL DEFAULT 0,
    dq_unresolved_participant  INTEGER NOT NULL DEFAULT 1,
    UNIQUE (ticker, snapshot_date, investor_name_raw, row_ordinal)
);

-- Dimension table: every distinct label seen in custodian_label /
-- custodian_or_code. participant_type is deliberately left 'unknown'
-- by the ingester -- never auto-derived. overlaps_broker_code records a
-- coincidence with neobdm_scraper.py's existing broker-flow taxonomy
-- (BANDAR_GROUPS / ALGO_BIG_PLAYERS_T1/T2 / SMART_MONEY / BROKER_FLOW_CODES)
-- WITHOUT asserting the custody participant and the execution broker are
-- the same entity.
CREATE TABLE IF NOT EXISTS custody_participants (
    label                TEXT PRIMARY KEY,
    participant_type     TEXT NOT NULL DEFAULT 'unknown',
    overlaps_broker_code TEXT,
    notes                TEXT,
    first_seen_ticker    TEXT,
    first_seen_source_family TEXT,
    first_seen_at        TEXT NOT NULL,
    extraction_version   TEXT NOT NULL,
    dq_unresolved_participant INTEGER NOT NULL DEFAULT 1
);

-- Balance Position Chart, monthly, per investor-category ABSOLUTE lots
-- (18 canonical local_*/foreign_* categories -- see STOCK_DETAIL_
-- OWNERSHIP_DISCOVERY.md 1.5). One point per ticker/month/category;
-- PRIMARY KEY is the natural key and doubles as the idempotency guard.
CREATE TABLE IF NOT EXISTS balance_position_monthly (
    ticker       TEXT NOT NULL,
    period_date  TEXT NOT NULL,
    category     TEXT NOT NULL,
    lots         REAL,
    published_at TEXT,
    captured_at  TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source_url   TEXT NOT NULL,
    source_family TEXT NOT NULL,
    extraction_version TEXT NOT NULL,
    raw_hash     TEXT NOT NULL,
    dq_unknown_publication_time INTEGER NOT NULL DEFAULT 0,
    dq_incomplete_historical_depth INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, period_date, category)
);

-- Balance Position Chart, monthly, the 4 percentage line-series.
CREATE TABLE IF NOT EXISTS balance_position_summary_monthly (
    ticker         TEXT NOT NULL,
    period_date    TEXT NOT NULL,
    pct_retail     REAL,
    pct_institusi  REAL,
    pct_foreign    REAL,
    pct_scripless  REAL,
    published_at   TEXT,
    captured_at    TEXT NOT NULL,
    available_at   TEXT NOT NULL,
    source_url     TEXT NOT NULL,
    source_family  TEXT NOT NULL,
    extraction_version TEXT NOT NULL,
    raw_hash       TEXT NOT NULL,
    dq_unknown_publication_time INTEGER NOT NULL DEFAULT 0,
    dq_incomplete_historical_depth INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, period_date)
);

-- Free float / scripless / holder-count badges. snapshot_date is INFERRED
-- (= latest balance_position_monthly.period_date at capture time); the
-- page never timestamps these badges independently (still open per
-- STOCK_DETAIL_OWNERSHIP_DISCOVERY.md 5.8). holder_count is a parsed
-- approximation of an already-abbreviated display value ("101K") -- the
-- raw string is kept alongside it and dq_rounded_holder_count is always 1.
CREATE TABLE IF NOT EXISTS float_holder_snapshot (
    ticker             TEXT NOT NULL,
    snapshot_date      TEXT NOT NULL,
    free_float_pct     REAL,
    scripless_pct      REAL,
    holder_count_raw   TEXT,
    holder_count_approx REAL,
    published_at       TEXT,
    captured_at        TEXT NOT NULL,
    available_at       TEXT NOT NULL,
    source_url         TEXT NOT NULL,
    source_family      TEXT NOT NULL,
    extraction_version TEXT NOT NULL,
    raw_hash           TEXT NOT NULL,
    dq_unknown_publication_time INTEGER NOT NULL DEFAULT 0,
    dq_rounded_holder_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ticker, snapshot_date)
);

-- QA-ONLY. Not one of the seven reviewed tables. Records a candidate
-- same-entity pair (e.g. "UBS HONGKONG" / "UBS HONG KONG") flagged by a
-- conservative normalization heuristic (punctuation/whitespace/legal-
-- suffix noise only -- see ownership_parse.normalize_entity_name). NEVER
-- auto-merges. name_a/name_b are stored in a stable (sorted) order so a
-- pair is recorded once regardless of which side is seen first.
CREATE TABLE IF NOT EXISTS entity_alias_candidate (
    id              INTEGER PRIMARY KEY,
    ticker          TEXT NOT NULL,
    source_table    TEXT NOT NULL,
    name_a          TEXT NOT NULL,
    name_b          TEXT NOT NULL,
    normalized_key  TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    UNIQUE (ticker, source_table, name_a, name_b)
);

-- Canonical ownership entity identity. GLOBAL (deliberately NOT scoped by
-- ticker, unlike entity_alias_candidate above): the same holder appears under
-- several tickers -- TIRTA ORISA YASA under four, ELANG MAHKOTA TEKNOLOGI
-- under three -- and a per-ticker identity would issue that one holder several
-- unrelated IDs.
--
-- entity_id is DERIVED, not assigned: 'ENT-' + sha256(canonical_key)[:16].
-- It is a pure function of canonical_key, so it is stable across reruns,
-- machines and insertion order, and a newly captured alias joining a group
-- never renumbers the group. canonical_key is the fully-reduced ordered token
-- sequence (see ownership_entity.py); ORDERED, so 'ALPHA BETA' and
-- 'BETA ALPHA' are different entities.
--
-- canonical_name is a representative RAW name chosen from the members. It is a
-- display label only. No source row is ever rewritten to it, and
-- investor_name_raw everywhere remains exactly the captured bytes.
CREATE TABLE IF NOT EXISTS entity_canonical (
    entity_id       TEXT PRIMARY KEY,
    canonical_key   TEXT NOT NULL UNIQUE,
    canonical_name  TEXT NOT NULL,
    member_count    INTEGER NOT NULL,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    rule_version    TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- One row per DISTINCT raw investor name ever captured, mapping it to its
-- global entity_id, with the deterministic rule that produced the mapping.
--
--   R0  raw name needed no transformation at all
--   R1  case / punctuation / whitespace only, token ORDER preserved
--   R2  R1 plus removal of the PT and TBK entity-form tokens only
--   R3  R1/R2 plus an explicitly curated spacing substitution (HONGKONG)
--   R4  refused -- a candidate pair proposed this name as an alias of another
--       name and the rules above did not confirm it, so it KEEPS ITS OWN
--       global entity_id and is flagged for a human. Ambiguity never merges.
--
-- match_status='review' marks any name a human still has to adjudicate. That
-- includes every R4 name and also any confidently-merged name that separately
-- has an unconfirmed candidate partner outstanding.
CREATE TABLE IF NOT EXISTS entity_alias (
    name_raw      TEXT PRIMARY KEY,
    entity_id     TEXT NOT NULL REFERENCES entity_canonical(entity_id),
    match_rule    TEXT NOT NULL CHECK (match_rule IN ('R0','R1','R2','R3','R4')),
    match_status  TEXT NOT NULL CHECK (match_status IN ('auto','review')),
    confidence    TEXT NOT NULL CHECK (confidence IN ('exact','high','confirmed','unresolved')),
    evidence      TEXT NOT NULL,
    rule_version  TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entity_alias_entity ON entity_alias (entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_alias_status ON entity_alias (match_status);

-- ---------------------------------------------------------------------------
-- BITEMPORAL LAYER (added after the capture-durability review).
--
-- The eight tables above are first-write-wins: a natural key seen again with a
-- DIFFERENT payload is silently discarded by INSERT OR IGNORE, and captured_at
-- never moves. That makes them a faithful record of "the state as FIRST
-- observed" and nothing else -- no last_seen_at, no revisions, and no way to
-- tell a holder that genuinely disappeared from a pane that failed to load.
--
-- These four tables add that missing history ALONGSIDE the originals. Nothing
-- above changes: same DDL, same keys, same semantics, same rows. Experiment
-- #2A0's frozen cohort queries ownership_change and must keep returning
-- exactly what it returns today, which is why this layer is additive rather
-- than a widening of the existing keys.
-- ---------------------------------------------------------------------------

-- Append-only capture manifest: one row per run x ticker x pane, written
-- whether the pane succeeded or not. This is the table that separates
-- "the holder is gone" from "we never saw the pane".
--
--   status='ok' AND the key absent from that run's observations -> genuine
--     disappearance.
--   any other status -> UNKNOWN. No exit may be inferred, ever.
--
-- row_count=0 (pane rendered, contained no rows) and row_count IS NULL (pane
-- never parsed) are deliberately different values, not interchangeable nulls.
--
-- evidence_class='STRICT' marks a run captured under this bitemporal ingest.
-- 'LEGACY_PARTIAL' marks the three pre-remediation runs (2026-08-31/09-01/
-- 09-03), whose revision history was already lost and cannot be reconstructed.
-- The strict capture clock counts STRICT runs only.
CREATE TABLE IF NOT EXISTS capture_run (
    run_id              TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    pane                TEXT NOT NULL,
    captured_at         TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN (
                            'ok', 'empty_pane', 'parse_error', 'fetch_error',
                            'skipped', 'legacy_reconstructed')),
    snapshot_date_seen  TEXT,
    row_count           INTEGER,
    pane_hash           TEXT,
    error_detail        TEXT,
    extraction_version  TEXT NOT NULL,
    evidence_class      TEXT NOT NULL CHECK (evidence_class IN ('STRICT', 'LEGACY_PARTIAL')),
    PRIMARY KEY (run_id, ticker, pane)
);

CREATE INDEX IF NOT EXISTS idx_capture_run_evidence ON capture_run (evidence_class, status);
CREATE INDEX IF NOT EXISTS idx_capture_run_hash ON capture_run (pane_hash);

-- Content-addressed index of retained raw pane fragments. Keyed by content,
-- so a pane that did not change between runs is recorded once and referenced
-- by every later capture_run row -- an unchanged pane costs no extra storage.
--
-- The BYTES live on disk as write-once gzipped files, NOT in this DB. The DB
-- is committed on every capture run, and a mutating SQLite file costs roughly
-- its full size per commit in new git objects (page shifts defeat delta
-- compression); .git is already ~475 MB with neobdm.db committed 91 times.
-- Immutable per-run files add only their own bytes, once. fragment_path is
-- relative to the repo root.
CREATE TABLE IF NOT EXISTS capture_payload (
    pane_hash      TEXT PRIMARY KEY,
    fragment_path  TEXT NOT NULL,
    byte_len       INTEGER NOT NULL,
    gz_byte_len    INTEGER,
    first_run_id   TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL
);

-- Append-on-change canonical observations. The bitemporal record.
--
--   no prior observation for business_key  -> append, revision_number = 0
--   payload_hash == latest payload_hash    -> append NOTHING (exact replay is
--                                             idempotent); bump observation_state
--   payload_hash differs                   -> append, revision_number = prev + 1
--
-- payload_hash is a FULL-WIDTH sha256 over a canonical serialisation of every
-- economically relevant field for that source family. It is emphatically not
-- the legacy raw_hash, which hashes the row key plus a 200-byte prefix of the
-- surrounding pane and therefore cannot detect a change in the value it is
-- attached to (a 3.1% -> 9.9% ownership revision produces a byte-identical
-- legacy raw_hash).
--
-- business_key is the canonical serialisation of the SAME natural key the
-- corresponding legacy table uses, so the two layers stay joinable.
CREATE TABLE IF NOT EXISTS ownership_observation (
    obs_id             INTEGER PRIMARY KEY,
    run_id             TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    source_family      TEXT NOT NULL,
    business_key       TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    payload_hash       TEXT NOT NULL,
    observed_at        TEXT NOT NULL,
    revision_number    INTEGER NOT NULL,
    evidence_class     TEXT NOT NULL CHECK (evidence_class IN ('STRICT', 'LEGACY_PARTIAL')),
    extraction_version TEXT NOT NULL,
    UNIQUE (business_key, payload_hash, run_id)
);

CREATE INDEX IF NOT EXISTS idx_obs_key_time ON ownership_observation (business_key, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_run ON ownership_observation (run_id);

-- Derived projection over ownership_observation + capture_run. The ONLY
-- mutable table in this layer, and the only place last_seen_at lives.
--
-- Maintained incrementally for query speed, but never the sole copy of the
-- truth for first_seen_at / current state: those are exactly rebuildable
-- from ownership_observation alone. last_seen_at is rebuildable from
-- ownership_observation + capture_run as a conservative lower bound (never
-- an overstatement, exact whenever nothing else in the same pane changed
-- meanwhile) -- pane_hash is whole-pane, so it cannot alone reconfirm one
-- unchanged row inside a pane where a sibling row changed. See
-- ownership_ingest.rebuild_observation_state() for the exact contract.
CREATE TABLE IF NOT EXISTS observation_state (
    business_key         TEXT PRIMARY KEY,
    ticker               TEXT NOT NULL,
    source_family        TEXT NOT NULL,
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    times_seen           INTEGER NOT NULL,
    current_payload_hash TEXT NOT NULL,
    current_obs_id       INTEGER NOT NULL REFERENCES ownership_observation(obs_id),
    revision_count       INTEGER NOT NULL,
    evidence_class       TEXT NOT NULL CHECK (evidence_class IN ('STRICT', 'LEGACY_PARTIAL'))
);

CREATE INDEX IF NOT EXISTS idx_obs_state_ticker ON observation_state (ticker, source_family);
"""


def create_schema(conn):
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def connect(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    create_schema(conn)
    return conn
