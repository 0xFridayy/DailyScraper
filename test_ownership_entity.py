"""Tests for deterministic ownership entity canonicalization.

In-memory SQLite only -- no network, no Playwright, no live DB.

    py -3 -m pytest test_ownership_entity.py -v

The must-not-merge tests are the important half of this file. Entity IDs are
global, so a false-positive merge is expensive and hard to undo once anything
joins on it. Each case below is a real pair from the captured data where one
name contains the other but legal identity may genuinely differ.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ownership_entity as oe             # noqa: E402
from ownership_schema import create_schema  # noqa: E402

NOW = "2026-09-01T00:00:00Z"

# Pairs that MUST stay separate entities. Suffix/qualifier/containment
# differences that may be a real difference in legal person.
MUST_NOT_MERGE = [
    ("EFG BANK", "EFG BANK AG"),
    ("PRIME HILL FUND", "ZHAOCAI PRIME HILL FUND"),
    ("GREEN ERA ENERGY", "GREEN ERA ENERGY PTE. LTD."),
    ("GOLDWAVE CAPITAL", "GOLDWAVE CAPITAL LIMITED"),
    ("SCG CHEMICALS PUBLIC", "SCG CHEMICALS PUBLIC COMPANY"),
    ("DBS BANK", "DBS BANK S/A ALBIZIA ASEAN OPPORTUNITIES FUND"),
    ("MAYBANK SEKURITAS INDONESIA", "MAYBANK SEKURITAS INDONESIA - REGISTRAR"),
    ("CGS INTERNATIONAL SECURITIES HONG KONG",
     "CGS INTERNATIONAL SECURITIES HONG KONG LIMITED"),
    ("GLAS TRUST (S) LTD.", "GLAS TRUST SINGAPORE"),
    ("UBS AG", "UBS AG SINGAPORE BRANCH"),
    ("CITIBANK", "CITIBANK NOMINEES"),
    ("HSBC BANK", "HSBC BANK CUSTODY"),
]


def make_conn():
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    return conn


def add_names(conn, names, ticker="TEST"):
    """Insert raw names as ownership_snapshot rows."""
    for i, name in enumerate(names):
        conn.execute(
            """INSERT OR IGNORE INTO ownership_snapshot
                 (ticker, threshold, snapshot_date, investor_name_raw,
                  captured_at, available_at, source_url, source_family,
                  extraction_version, raw_hash)
               VALUES (?, '1pct', '2026-08-31', ?, ?, ?, 'u', 'f', 'v', ?)""",
            (ticker, name, NOW, NOW, f"h{i}"),
        )
    conn.commit()


def add_candidate(conn, name_a, name_b, ticker="TEST"):
    conn.execute(
        """INSERT OR IGNORE INTO entity_alias_candidate
             (ticker, source_table, name_a, name_b, normalized_key, captured_at)
           VALUES (?, 'ownership_snapshot', ?, ?, ?, ?)""",
        (ticker, name_a, name_b, "k", NOW),
    )
    conn.commit()


def eid(conn, name):
    return conn.execute(
        "SELECT entity_id FROM entity_alias WHERE name_raw = ?", (name,)
    ).fetchone()[0]


def alias_row(conn, name):
    return conn.execute(
        """SELECT match_rule, match_status, confidence, evidence, rule_version
             FROM entity_alias WHERE name_raw = ?""",
        (name,),
    ).fetchone()


# --- rule classification --------------------------------------------------

def test_r0_byte_identical():
    assert oe.classify_pair("PT ABC", "PT ABC")[0] == "R0"


def test_r1_case_punctuation_whitespace_only():
    for a, b in [
        ("PT. PRAMBANAN INVESTASI SUKSES", "PT PRAMBANAN INVESTASI SUKSES"),
        ("Barito Pacific", "BARITO PACIFIC"),
        ("ABC   CAPITAL", "ABC CAPITAL"),
        ("ABC, CAPITAL", "ABC CAPITAL"),
        ("(ABC) CAPITAL", "ABC CAPITAL"),
    ]:
        assert oe.classify_pair(a, b)[0] == "R1", (a, b)


def test_r2_pt_tbk_removal_any_position():
    for a, b in [
        ("PT TIRTA ORISA YASA", "TIRTA ORISA YASA"),           # leading
        ("SIRIUS SURYA SENTOSA, PT", "SIRIUS SURYA SENTOSA"),  # trailing
        ("PT SIRIUS SURYA SENTOSA", "SIRIUS SURYA SENTOSA, PT"),  # both, moved
        ("ELANG MAHKOTA TEKNOLOGI", "ELANG MAHKOTA TEKNOLOGI TBK, PT"),
        ("PT ABC TBK", "ABC"),
    ]:
        assert oe.classify_pair(a, b)[0] == "R2", (a, b)


def test_r3_curated_spacing_alias():
    assert oe.classify_pair("UBS HONGKONG", "UBS HONG KONG")[0] == "R3"


def test_r4_everything_else():
    assert oe.classify_pair("ALPHA CAPITAL", "BETA CAPITAL")[0] == "R4"


def test_rules_are_symmetric():
    for a, b in MUST_NOT_MERGE + [("PT TIRTA ORISA YASA", "TIRTA ORISA YASA")]:
        assert oe.classify_pair(a, b)[0] == oe.classify_pair(b, a)[0], (a, b)


# --- ordered-token semantics ----------------------------------------------

def test_token_order_is_preserved_not_a_multiset():
    """An unordered multiset would merge these; ordered comparison must not."""
    assert oe.classify_pair("ALPHA BETA PT", "PT BETA ALPHA")[0] == "R4"
    assert oe.classify_pair("A B C", "C B A")[0] == "R4"
    assert oe.canonical_key("ALPHA BETA") != oe.canonical_key("BETA ALPHA")


def test_normalize_tokens_returns_ordered_sequence():
    assert oe.normalize_tokens("PT. Alpha, Beta") == ["PT", "ALPHA", "BETA"]


# --- must-not-merge -------------------------------------------------------

def test_must_not_merge_pairs_classify_r4():
    for a, b in MUST_NOT_MERGE:
        assert oe.classify_pair(a, b)[0] == "R4", (a, b)


def test_must_not_merge_pairs_get_distinct_entity_ids():
    conn = make_conn()
    flat = [n for pair in MUST_NOT_MERGE for n in pair]
    add_names(conn, flat)
    for a, b in MUST_NOT_MERGE:
        add_candidate(conn, a, b)
    oe.backfill(conn, NOW)
    for a, b in MUST_NOT_MERGE:
        assert eid(conn, a) != eid(conn, b), (a, b)


def test_unconfirmed_candidate_pair_is_flagged_for_review():
    conn = make_conn()
    add_names(conn, ["EFG BANK", "EFG BANK AG"])
    add_candidate(conn, "EFG BANK", "EFG BANK AG")
    oe.backfill(conn, NOW)
    for name in ("EFG BANK", "EFG BANK AG"):
        rule, status, confidence, _, _ = alias_row(conn, name)
        assert (rule, status, confidence) == ("R4", "review", "unresolved")


def test_ambiguity_defaults_to_separate_ids_plus_review():
    """The stated default: when uncertain, separate IDs AND a review flag."""
    conn = make_conn()
    add_names(conn, ["PRIME HILL FUND", "ZHAOCAI PRIME HILL FUND"])
    add_candidate(conn, "PRIME HILL FUND", "ZHAOCAI PRIME HILL FUND")
    oe.backfill(conn, NOW)
    assert eid(conn, "PRIME HILL FUND") != eid(conn, "ZHAOCAI PRIME HILL FUND")
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_alias WHERE match_status = 'review'"
    ).fetchone()[0] == 2


# --- merging via the DB ---------------------------------------------------

def test_pt_variants_share_one_entity_id():
    conn = make_conn()
    variants = ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA", "TIRTA ORISA YASA, PT"]
    add_names(conn, variants)
    oe.backfill(conn, NOW)
    assert len({eid(conn, n) for n in variants}) == 1


def test_ubs_spacing_alias_shares_one_entity_id():
    conn = make_conn()
    add_names(conn, ["UBS HONGKONG", "UBS HONG KONG"])
    oe.backfill(conn, NOW)
    assert eid(conn, "UBS HONGKONG") == eid(conn, "UBS HONG KONG")


def test_case_and_punctuation_variants_share_one_entity_id():
    conn = make_conn()
    variants = ["PT. Prambanan Investasi Sukses", "PT PRAMBANAN INVESTASI SUKSES"]
    add_names(conn, variants)
    oe.backfill(conn, NOW)
    assert eid(conn, variants[0]) == eid(conn, variants[1])


# --- raw data preservation ------------------------------------------------

def test_raw_names_are_never_rewritten():
    conn = make_conn()
    originals = ["PT. Prambanan Investasi Sukses", "TIRTA ORISA YASA, PT"]
    add_names(conn, originals)
    oe.backfill(conn, NOW)
    stored = {r[0] for r in conn.execute(
        "SELECT investor_name_raw FROM ownership_snapshot")}
    assert stored == set(originals)


def test_backfill_does_not_populate_ml_or_canonical_columns():
    """Identity is served by joining entity_alias; source columns stay NULL."""
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA"])
    oe.backfill(conn, NOW)
    assert conn.execute(
        "SELECT COUNT(*) FROM ownership_snapshot "
        "WHERE investor_name_canonical IS NOT NULL"
    ).fetchone()[0] == 0


# --- cross-ticker identity ------------------------------------------------

def test_same_holder_across_tickers_gets_one_global_id():
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA"], ticker="AAAA")
    add_names(conn, ["TIRTA ORISA YASA"], ticker="BBBB")
    add_names(conn, ["TIRTA ORISA YASA, PT"], ticker="CCCC")
    oe.backfill(conn, NOW)
    ids = {eid(conn, n) for n in
           ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA", "TIRTA ORISA YASA, PT"]}
    assert len(ids) == 1
    rows = oe.cross_ticker_entities(conn)
    assert rows and rows[0][2] == 3


def test_entity_id_is_independent_of_ticker_and_insertion_order():
    a, b = make_conn(), make_conn()
    add_names(a, ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA"], ticker="AAAA")
    add_names(b, ["TIRTA ORISA YASA", "PT TIRTA ORISA YASA"], ticker="ZZZZ")
    oe.backfill(a, NOW)
    oe.backfill(b, "2030-01-01T00:00:00Z")
    assert eid(a, "TIRTA ORISA YASA") == eid(b, "TIRTA ORISA YASA")


def test_names_from_all_three_tables_are_canonicalized():
    conn = make_conn()
    conn.execute(
        """INSERT INTO ownership_change
             (ticker, threshold, change_date, investor_name_raw, captured_at,
              available_at, source_url, source_family, extraction_version, raw_hash)
           VALUES ('T', '1pct', '2026-08-31', 'PT ALPHA CHANGE', ?, ?, 'u','f','v','h')""",
        (NOW, NOW),
    )
    conn.execute(
        """INSERT INTO custody_breakdown_snapshot
             (ticker, snapshot_date, investor_name_raw, custodian_label,
              captured_at, available_at, source_url, source_family,
              extraction_version, raw_hash)
           VALUES ('T', '2026-08-31', 'PT BETA CUSTODY', 'X', ?, ?, 'u','f','v','h')""",
        (NOW, NOW),
    )
    conn.commit()
    add_names(conn, ["PT GAMMA SNAP"])
    oe.backfill(conn, NOW)
    for n in ("PT ALPHA CHANGE", "PT BETA CUSTODY", "PT GAMMA SNAP"):
        assert alias_row(conn, n) is not None, n


# --- idempotency ----------------------------------------------------------

def test_backfill_is_idempotent():
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA", "EFG BANK",
                     "EFG BANK AG", "UBS HONGKONG", "UBS HONG KONG"])
    add_candidate(conn, "EFG BANK", "EFG BANK AG")

    first = oe.backfill(conn, NOW)
    snap1 = conn.execute(
        "SELECT * FROM entity_alias ORDER BY name_raw").fetchall()
    canon1 = conn.execute(
        "SELECT * FROM entity_canonical ORDER BY entity_id").fetchall()

    second = oe.backfill(conn, NOW)
    snap2 = conn.execute(
        "SELECT * FROM entity_alias ORDER BY name_raw").fetchall()
    canon2 = conn.execute(
        "SELECT * FROM entity_canonical ORDER BY entity_id").fetchall()

    assert first == second
    assert snap1 == snap2
    assert canon1 == canon2


def test_rerun_at_a_later_time_is_a_byte_for_byte_noop():
    """The real-world case: same data, later clock.

    A naive upsert restamps updated_at on every run, which would dirty the
    git-committed DB daily and hide whether anything actually changed.
    """
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA", "EFG BANK",
                     "EFG BANK AG", "UBS HONGKONG", "UBS HONG KONG"])
    add_candidate(conn, "EFG BANK", "EFG BANK AG")
    oe.backfill(conn, NOW)
    before = (conn.execute("SELECT * FROM entity_alias ORDER BY name_raw").fetchall(),
              conn.execute("SELECT * FROM entity_canonical ORDER BY entity_id").fetchall())

    oe.backfill(conn, "2031-12-25T09:30:00Z")
    after = (conn.execute("SELECT * FROM entity_alias ORDER BY name_raw").fetchall(),
             conn.execute("SELECT * FROM entity_canonical ORDER BY entity_id").fetchall())
    assert before == after


def test_genuine_change_does_update_updated_at():
    """The guard must suppress no-op writes without freezing real updates."""
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA"])
    oe.backfill(conn, NOW)

    later = "2031-12-25T09:30:00Z"
    add_names(conn, ["TIRTA ORISA YASA, PT"])   # member_count 1 -> 2
    oe.backfill(conn, later)
    assert conn.execute(
        "SELECT updated_at FROM entity_canonical").fetchone()[0] == later


def test_rerun_preserves_first_seen_at_and_entity_ids():
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA"])
    oe.backfill(conn, NOW)
    before = conn.execute(
        "SELECT name_raw, entity_id, first_seen_at FROM entity_alias "
        "ORDER BY name_raw").fetchall()

    oe.backfill(conn, "2030-06-06T00:00:00Z")
    after = conn.execute(
        "SELECT name_raw, entity_id, first_seen_at FROM entity_alias "
        "ORDER BY name_raw").fetchall()
    assert before == after


def test_new_name_joins_existing_group_without_renumbering():
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA"])
    oe.backfill(conn, NOW)
    original = eid(conn, "PT TIRTA ORISA YASA")

    add_names(conn, ["TIRTA ORISA YASA, PT"])
    oe.backfill(conn, NOW)
    assert eid(conn, "PT TIRTA ORISA YASA") == original
    assert eid(conn, "TIRTA ORISA YASA, PT") == original


# --- provenance -----------------------------------------------------------

def test_every_alias_has_complete_provenance():
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA", "UBS HONGKONG",
                     "UBS HONG KONG", "EFG BANK", "EFG BANK AG", "ALPHA"])
    add_candidate(conn, "EFG BANK", "EFG BANK AG")
    oe.backfill(conn, NOW)
    rows = conn.execute(
        """SELECT name_raw, match_rule, match_status, confidence, evidence,
                  rule_version, first_seen_at, updated_at FROM entity_alias"""
    ).fetchall()
    assert rows
    for row in rows:
        for i, field in enumerate(row):
            assert field not in (None, ""), (row[0], i)
        assert row[1] in ("R0", "R1", "R2", "R3", "R4")
        assert row[2] in ("auto", "review")
        assert row[5] == oe.RULE_VERSION


def test_review_status_matches_r4_rule():
    conn = make_conn()
    add_names(conn, ["EFG BANK", "EFG BANK AG", "PT ALPHA", "ALPHA"])
    add_candidate(conn, "EFG BANK", "EFG BANK AG")
    oe.backfill(conn, NOW)
    for rule, status in conn.execute(
            "SELECT match_rule, match_status FROM entity_alias"):
        assert (rule == "R4") == (status == "review")


def test_every_alias_points_at_an_existing_canonical_entity():
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA", "EFG BANK",
                     "EFG BANK AG"])
    add_candidate(conn, "EFG BANK", "EFG BANK AG")
    oe.backfill(conn, NOW)
    assert conn.execute(
        """SELECT COUNT(*) FROM entity_alias a
             LEFT JOIN entity_canonical c ON c.entity_id = a.entity_id
            WHERE c.entity_id IS NULL"""
    ).fetchone()[0] == 0


def test_member_count_matches_actual_members():
    conn = make_conn()
    add_names(conn, ["PT TIRTA ORISA YASA", "TIRTA ORISA YASA",
                     "TIRTA ORISA YASA, PT", "ALPHA"])
    oe.backfill(conn, NOW)
    for entity_id, member_count in conn.execute(
            "SELECT entity_id, member_count FROM entity_canonical"):
        actual = conn.execute(
            "SELECT COUNT(*) FROM entity_alias WHERE entity_id = ?", (entity_id,)
        ).fetchone()[0]
        assert actual == member_count


# --- degenerate input -----------------------------------------------------

def test_form_token_only_names_do_not_collapse_together():
    """'PT' and 'TBK' reduce to nothing; they must not become one entity."""
    conn = make_conn()
    add_names(conn, ["PT", "TBK"])
    oe.backfill(conn, NOW)
    assert eid(conn, "PT") != eid(conn, "TBK")
