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
