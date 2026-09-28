"""Tests for the ownership/custody capture-and-archival infrastructure.

Runs entirely against an in-memory SQLite DB and fixed fixture strings
pulled from real captures (discovery_batch2.json / pass-2 tickers) --
no network access, no Playwright, no live NeoBDM session required.

    py -3 -m pytest test_ownership_ingest.py -v
"""
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ownership_parse as op          # noqa: E402
import ownership_ingest as ing        # noqa: E402
from ownership_schema import create_schema  # noqa: E402

# --- fixtures, verbatim from discovery_batch2.json captures ---------------

TPIA_KDA1 = (
    "Data per 31 jul 2026 XLSX\nTotal Kepemilikan: 79.3%\n"
    "Investor\tKepemilikan\tScrip\tScripless\n\n"
    "BARITO PACIFIC\nCorporate\n\t34.6%\t181M lot 21.0%\t118M lot 13.7%\n\n"
    "SCG CHEMICALS PUBLIC\nCorporate F\n\t15.7%\t136M lot 15.7%\t-\n\n"
    "PRAJOGO PANGESTU\nIndividual\n\t5.0%\t4.84M lot 0.6%\t38.7M lot 4.5%"
)

TPIA_KDA5 = (
    "\n                  Data per 27 aug 2026\n                \n"
    "                          PT BARITO PACIFIC TBK\n                        \n"
    "                        13.7%\n                        \n"
    "                          AF 60.8%\n                        \n"
    "                          DX 22.3%\n                        \n"
    "                          NI 16.9%\n                        \n"
)

GOTO_KDA5_TWO_HOLDERS = (
    "\n                  Data per 27 aug 2026\n                \n"
    "                          SVF GT SUBCO (SINGAPORE) PTE. LTD.\n                        \n"
    "                        7.6%\n                        \n"
    "                        F\n                        \n"
    "                          Deutsche Bank 100.0%\n                        \n"
    "                          TAOBAO CHINA HOLDING LIMITED\n                        \n"
    "                        7.4%\n                        \n"
    "                        F\n                        \n"
    "                          Citibank 100.0%\n                        \n"
)

BREN_PKDA1_FALSE_TURNOVER = (
    "Tanggal\nInvestor\nKepemilikan\nScrip\nScripless\nCatatan\n"
    "2026-05-29\nPRIME HILL FUND\nTrustee Bank\nF\n3.0%\n-\n+40.6M lot\nMasuk PKDA 1%\n"
    "2026-05-29\nZHAOCAI PRIME HILL FUND\nTrustee Bank\nF\n<1%\n-\n-41.4M lot\nKeluar PKDA 1%\n"
)

WIFI_PKDA5_SAME_DAY_OPPOSITE = (
    "Tanggal\nInvestor\nPerubahan\n"
    "2026-05-20\nINVESTASI SUKSES BERSAMA\n\nYB -600K lot\n"
    "2026-05-20\nINVESTASI SUKSES BERSAMA\n\nDR 600K lot\n"
)

GOTO_UBS_VARIANT_A = (
    "Data per 30 jun 2026 XLSX\nTotal Kepemilikan: 90.0%\n"
    "Investor\tKepemilikan\tScrip\tScripless\n\n"
    "UBS HONGKONG\nTrustee Bank F\n\t2.7%\t-\t36.0M lot 2.7%"
)
GOTO_UBS_VARIANT_B = (
    "Data per 31 jul 2026 XLSX\nTotal Kepemilikan: 90.0%\n"
    "Investor\tKepemilikan\tScrip\tScripless\n\n"
    "UBS HONG KONG\nTrustee Bank F\n\t2.7%\t-\t36.0M lot 2.7%"
)

BADGE_TPIA = (
    "TPIA Balance Position Chart [Scripless: 47.8%] [Free Float: 25.4%] "
    "[Holder: 101K] ChartCombination chart with 22 data series.The chart has "
    "1 X axis displaying Time. Data ranges from 2024-08-01 00:00:00 to "
    "2026-07-01 00:00:00."
)

BP_TRACES = [
    {"name": "Lokal individual", "x": ["2023-08-31", "2023-09-30"], "y": [1000.0, 1100.0]},
    {"name": "Foreign korporat", "x": ["2023-08-31", "2023-09-30"], "y": [500.0, 520.0]},
    {"name": "%Retail", "x": ["2023-08-31", "2023-09-30"], "y": [12.0, 12.5]},
    {"name": "%Institusi", "x": ["2023-08-31", "2023-09-30"], "y": [50.0, 50.5]},
    {"name": "%Foreign", "x": ["2023-08-31", "2023-09-30"], "y": [30.0, 30.5]},
    {"name": "scripless", "x": ["2023-08-31", "2023-09-30"], "y": [47.0, 47.5]},
]


def make_conn():
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    return conn


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def redirect_fragment_storage(tmp_path, monkeypatch):
    """store_pane_fragment() writes raw pane fragments to a path derived from
    ownership_ingest.HERE, since production capture always wants them beside
    the real DB. Tests use an in-memory DB but that module-level HERE is not
    otherwise redirected, so without this fixture every test that exercises a
    non-empty pane silently writes real .txt.gz files into the actual repo
    working tree. Point HERE at a throwaway tmp_path for every test instead."""
    monkeypatch.setattr(ing, "HERE", str(tmp_path))


def make_capture(kda1=None, kda5=None, pkda1=None, pkda5=None, badge=None, traces=None):
    return {
        "panes": {
            "insider-current": kda1, "insider-moves": pkda1,
            "insider5p-current": kda5, "insider5p-moves": pkda5,
        },
        "balance_position_traces": traces,
        "balance_position_badge": badge,
        "source_xlsx_url": None,
    }


# --- 1. idempotent re-ingestion ---------------------------------------------

def test_idempotent_reingestion_same_row_counts():
    conn = make_conn()
    capture = make_capture(kda1=TPIA_KDA1, kda5=TPIA_KDA5, traces=BP_TRACES, badge=BADGE_TPIA)
    c1 = ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                             "https://neobdm.tech/stock_detail/TPIA/")
    assert c1["ownership_snapshot"] > 0
    n_after_first = conn.execute("SELECT COUNT(*) FROM ownership_snapshot").fetchone()[0]

    c2 = ing.ingest_capture(conn, "TPIA", capture, "2026-08-31T10:00:00Z",
                             "https://neobdm.tech/stock_detail/TPIA/")
    n_after_second = conn.execute("SELECT COUNT(*) FROM ownership_snapshot").fetchone()[0]

    assert n_after_first == n_after_second, "re-ingesting the same capture must not duplicate rows"
    assert sum(c2.values()) == 0, "second identical ingestion must insert zero new rows"


# --- 2. captured_at / available_at anti-leakage semantics -------------------

def test_available_at_does_not_backdate_on_later_reingestion():
    conn = make_conn()
    capture = make_capture(kda1=TPIA_KDA1)
    first_captured_at = "2026-08-30T10:00:00Z"
    ing.ingest_capture(conn, "TPIA", capture, first_captured_at,
                        "https://neobdm.tech/stock_detail/TPIA/")
    row = conn.execute(
        "SELECT snapshot_date, captured_at, available_at, published_at "
        "FROM ownership_snapshot WHERE investor_name_raw='BARITO PACIFIC'"
    ).fetchone()
    snapshot_date, captured_at, available_at, published_at = row
    # snapshot_date (the economic date) is well before captured_at (today) --
    # available_at must equal captured_at, NOT the older snapshot_date.
    assert snapshot_date == "2026-07-31"
    assert captured_at == first_captured_at
    assert available_at == first_captured_at
    assert published_at is None

    # Re-ingesting later (simulating a resumed/rerun collector) must NOT
    # move available_at forward either -- first-seen wins.
    later_capture_at = "2026-09-15T10:00:00Z"
    ing.ingest_capture(conn, "TPIA", capture, later_capture_at,
                        "https://neobdm.tech/stock_detail/TPIA/")
    row2 = conn.execute(
        "SELECT captured_at, available_at FROM ownership_snapshot "
        "WHERE investor_name_raw='BARITO PACIFIC'"
    ).fetchone()
    assert row2 == (first_captured_at, first_captured_at), \
        "available_at/captured_at must not change on re-ingestion (no backdating, no forward-dating)"


def test_available_at_falls_back_to_captured_at_when_published_at_unknown():
    conn = make_conn()
    capture = make_capture(kda1=TPIA_KDA1)
    ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                        "https://neobdm.tech/stock_detail/TPIA/",
                        xlsx_verified_published_at=None)
    row = conn.execute(
        "SELECT published_at, available_at, dq_unknown_publication_time "
        "FROM ownership_snapshot LIMIT 1"
    ).fetchone()
    published_at, available_at, dq_flag = row
    assert published_at is None
    assert available_at == "2026-08-30T10:00:00Z"
    assert dq_flag == 1


def test_available_at_uses_published_at_when_independently_verified():
    conn = make_conn()
    capture = make_capture(kda1=TPIA_KDA1)
    ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                        "https://neobdm.tech/stock_detail/TPIA/",
                        xlsx_verified_published_at="2026-08-05T00:00:00Z")
    row = conn.execute(
        "SELECT published_at, available_at, dq_unknown_publication_time "
        "FROM ownership_snapshot LIMIT 1"
    ).fetchone()
    published_at, available_at, dq_flag = row
    assert published_at == "2026-08-05T00:00:00Z"
    assert available_at == "2026-08-05T00:00:00Z"
    assert dq_flag == 0


# --- 3. KDA5 custodian percentages sum to ~100% -----------------------------

def test_kda5_custodian_breakdown_sums_to_100pct():
    parsed = op.parse_kda5_current(TPIA_KDA5)
    assert len(parsed["holders"]) == 1
    holder = parsed["holders"][0]
    total = sum(pct for _, pct in holder["breakdown"])
    assert abs(total - 100.0) < 0.5

    conn = make_conn()
    capture = make_capture(kda5=TPIA_KDA5)
    ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                        "https://neobdm.tech/stock_detail/TPIA/")
    rows = conn.execute(
        "SELECT custodian_pct_of_holder FROM custody_breakdown_snapshot WHERE ticker='TPIA'"
    ).fetchall()
    assert abs(sum(r[0] for r in rows) - 100.0) < 0.5

    labels = conn.execute(
        "SELECT label, participant_type FROM custody_participants"
    ).fetchall()
    assert {"AF", "DX", "NI"} == {l for l, _ in labels}
    assert all(t == "unknown" for _, t in labels), \
        "custody_participants.participant_type must never be auto-resolved"


def test_kda5_two_holder_breakdown_stays_separated():
    parsed = op.parse_kda5_current(GOTO_KDA5_TWO_HOLDERS)
    assert len(parsed["holders"]) == 2
    names = {h["investor_name_raw"] for h in parsed["holders"]}
    assert names == {"SVF GT SUBCO (SINGAPORE) PTE. LTD.", "TAOBAO CHINA HOLDING LIMITED"}
    for h in parsed["holders"]:
        assert h["is_foreign"] is True
        assert h["breakdown"][0][1] == 100.0


# --- 4. holder-count approximation handling ---------------------------------

def test_holder_count_kept_as_abbreviated_raw_string():
    """The page only ever shows an abbreviated holder count ("101K"). The badge
    observation keeps that string verbatim -- never an unrounded integer the
    page never showed -- and parse_holder_count() is the one place it becomes
    an approximate number."""
    conn = make_conn()
    capture = make_capture(traces=BP_TRACES, badge=BADGE_TPIA)
    ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                        "https://neobdm.tech/stock_detail/TPIA/")
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM ownership_observation "
        "WHERE source_family='float_holder_badge'"
    ).fetchone()[0])
    assert payload["holder_count_raw"] == "101K"
    assert op.parse_holder_count(payload["holder_count_raw"]) == 101000.0


def test_parse_holder_count_values():
    assert op.parse_holder_count("101K") == 101000.0
    assert op.parse_holder_count("43.6K") == 43600.0
    assert op.parse_holder_count("783K") == 783000.0
    assert op.parse_holder_count("1.2M") == 1_200_000.0


# --- 5. PKDA scrip/scripless sign preservation ------------------------------

def test_pkda1_sign_preserved_for_both_sides_of_false_turnover():
    parsed = op.parse_pkda1_moves(BREN_PKDA1_FALSE_TURNOVER)
    assert len(parsed["rows"]) == 2
    prime_hill = next(r for r in parsed["rows"] if r["investor_name_raw"] == "PRIME HILL FUND")
    zhaocai = next(r for r in parsed["rows"] if r["investor_name_raw"] == "ZHAOCAI PRIME HILL FUND")
    assert prime_hill["scripless_lot_change"] == 40_600_000.0
    assert zhaocai["scripless_lot_change"] == -41_400_000.0
    assert prime_hill["note"] == "Masuk PKDA 1%"
    assert zhaocai["note"] == "Keluar PKDA 1%"
    # resulting_ownership_pct: '<1%' must not be coerced to a number
    assert zhaocai["resulting_ownership_pct"] is None
    assert zhaocai["resulting_ownership_pct_raw"] == "<1%"


def test_pkda5_same_day_opposite_sign_rows_both_kept():
    parsed = op.parse_pkda5_moves(WIFI_PKDA5_SAME_DAY_OPPOSITE)
    assert len(parsed["rows"]) == 2
    signs = sorted(r["lot_change"] for r in parsed["rows"])
    assert signs == [-600_000.0, 600_000.0]

    conn = make_conn()
    capture = make_capture(pkda5=WIFI_PKDA5_SAME_DAY_OPPOSITE)
    ing.ingest_capture(conn, "WIFI", capture, "2026-08-30T10:00:00Z",
                        "https://neobdm.tech/stock_detail/WIFI/")
    rows = conn.execute(
        "SELECT lot_change, row_ordinal FROM ownership_change "
        "WHERE ticker='WIFI' AND change_date='2026-05-20'"
    ).fetchall()
    assert len(rows) == 2, "same-day opposite-sign custodian moves must both survive, disambiguated by row_ordinal"
    assert sorted(r[0] for r in rows) == [-600_000.0, 600_000.0]


# --- 6. Balance Position month uniqueness -----------------------------------

def test_balance_position_month_uniqueness_enforced_and_idempotent():
    conn = make_conn()
    capture = make_capture(traces=BP_TRACES, badge=BADGE_TPIA)
    c1 = ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                             "https://neobdm.tech/stock_detail/TPIA/")
    assert c1["balance_position_monthly"] == 4    # 2 categories x 2 months
    assert c1["balance_position_summary_monthly"] == 2

    # direct duplicate insert attempt must be rejected by the PK, not just
    # skipped by the ingester's own logic
    import sqlite3 as sq
    try:
        conn.execute(
            "INSERT INTO balance_position_monthly "
            "(ticker, period_date, category, lots, captured_at, available_at, "
            " source_url, source_family, extraction_version, raw_hash) "
            "VALUES ('TPIA','2023-08-31','local_individual',9999,'x','x','u','f','v','h')"
        )
        assert False, "duplicate (ticker, period_date, category) must violate the PK"
    except sq.IntegrityError:
        pass

    c2 = ing.ingest_capture(conn, "TPIA", capture, "2026-08-31T10:00:00Z",
                             "https://neobdm.tech/stock_detail/TPIA/")
    assert c2["balance_position_monthly"] == 0
    assert c2["balance_position_summary_monthly"] == 0
    n = conn.execute("SELECT COUNT(*) FROM balance_position_monthly").fetchone()[0]
    assert n == 4


def test_balance_position_earliest_point_flagged_incomplete_depth():
    conn = make_conn()
    capture = make_capture(traces=BP_TRACES, badge=BADGE_TPIA)
    ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                        "https://neobdm.tech/stock_detail/TPIA/")
    flags = dict(conn.execute(
        "SELECT period_date, dq_incomplete_historical_depth FROM balance_position_summary_monthly "
        "WHERE ticker='TPIA' ORDER BY period_date"
    ).fetchall())
    assert flags == {"2023-08-31": 1, "2023-09-30": 0}


# --- entity alias candidate flagging (collected, never merged) -------------

def test_entity_alias_candidate_flagged_not_merged():
    conn = make_conn()
    ing.ingest_capture(conn, "GOTO", make_capture(kda1=GOTO_UBS_VARIANT_A),
                        "2026-06-30T10:00:00Z", "https://neobdm.tech/stock_detail/GOTO/")
    counts = ing.ingest_capture(conn, "GOTO", make_capture(kda1=GOTO_UBS_VARIANT_B),
                                 "2026-07-31T10:00:00Z", "https://neobdm.tech/stock_detail/GOTO/")
    assert counts["entity_alias_candidate"] == 1

    names = {r[0] for r in conn.execute(
        "SELECT investor_name_raw FROM ownership_snapshot WHERE ticker='GOTO'")}
    assert names == {"UBS HONGKONG", "UBS HONG KONG"}, \
        "both raw names must survive untouched -- no auto-merge"

    cand = conn.execute(
        "SELECT name_a, name_b FROM entity_alias_candidate WHERE ticker='GOTO'"
    ).fetchone()
    assert set(cand) == {"UBS HONGKONG", "UBS HONG KONG"}


def test_normalize_entity_name_does_not_overreach():
    # the one confirmed noise case: must match
    assert op.normalize_entity_name("UBS HONGKONG") == op.normalize_entity_name("UBS HONG KONG")
    # a merely-similar but NOT confirmed-identical pair: must NOT match
    assert op.normalize_entity_name("PRIME HILL FUND") != \
        op.normalize_entity_name("ZHAOCAI PRIME HILL FUND")


def test_alias_candidate_order_independence_regression():
    """Verify that alias candidates SCG CHEMICALS PUBLIC and SCG CHEMICALS PUBLIC COMPANY
    are flagged during the first clean ingestion, regardless of source-table processing order,
    and verify that their dq_suspected_entity_name_variant columns are set to 1."""
    conn = make_conn()
    # KDA 1% contains SCG CHEMICALS PUBLIC
    kda1 = "Data per 31 jul 2026 XLSX\nTotal Kepemilikan: 79.3%\nInvestor\tKepemilikan\tScrip\tScripless\n\nSCG CHEMICALS PUBLIC\nCorporate F\n\t15.7%\t136M lot\t-"
    # PKDA 5% contains SCG CHEMICALS PUBLIC COMPANY
    pkda5 = "Tanggal\nInvestor\nPerubahan\n2026-06-05\nSCG CHEMICALS PUBLIC COMPANY\n\nF\n\nYU 23M lot"

    capture = make_capture(kda1=kda1, pkda5=pkda5)
    counts = ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")

    # Assert that it is flagged in the first run!
    assert counts["entity_alias_candidate"] == 1

    # Verify both names exist in tables
    names_snapshot = [r[0] for r in conn.execute("SELECT investor_name_raw FROM ownership_snapshot WHERE ticker='TPIA'")]
    names_change = [r[0] for r in conn.execute("SELECT investor_name_raw FROM ownership_change WHERE ticker='TPIA'")]
    assert "SCG CHEMICALS PUBLIC" in names_snapshot
    assert "SCG CHEMICALS PUBLIC COMPANY" in names_change

    # Verify dq_suspected_entity_name_variant is updated to 1 for both
    dq_snapshot = conn.execute("SELECT dq_suspected_entity_name_variant FROM ownership_snapshot WHERE investor_name_raw='SCG CHEMICALS PUBLIC'").fetchone()[0]
    dq_change = conn.execute("SELECT dq_suspected_entity_name_variant FROM ownership_change WHERE investor_name_raw='SCG CHEMICALS PUBLIC COMPANY'").fetchone()[0]
    assert dq_snapshot == 1
    assert dq_change == 1

    # Verify rerun inserts exactly 0 rows across all tables
    counts_rerun = ing.ingest_capture(conn, "TPIA", capture, "2026-08-31T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    assert sum(counts_rerun.values()) == 0

    conn.close()


# --- 7. dry-run transaction: ingest then rollback leaves DB unchanged ----------

def test_dry_run_rollback_leaves_db_unchanged():
    """Simulates ownership_capture.py --dry-run behaviour:
    ingest_capture() no longer commits; the caller rolls back.
    Row counts must be identical before and after.
    """
    conn = make_conn()

    def row_counts():
        tables = [
            "ownership_snapshot", "ownership_change",
            "custody_breakdown_snapshot", "custody_participants",
            "balance_position_monthly", "balance_position_summary_monthly",
            "float_holder_snapshot", "entity_alias_candidate",
        ]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in tables}

    before = row_counts()
    assert all(v == 0 for v in before.values()), "fresh in-memory DB must start empty"

    capture = make_capture(kda1=TPIA_KDA1, kda5=TPIA_KDA5, traces=BP_TRACES, badge=BADGE_TPIA)
    counts = ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                                 "https://neobdm.tech/stock_detail/TPIA/")
    # Confirm inserts were attempted (rows exist inside uncommitted transaction)
    assert counts["ownership_snapshot"] > 0
    assert counts["balance_position_monthly"] > 0

    # Caller rolls back -- simulating --dry-run
    conn.rollback()

    after = row_counts()
    assert after == before, (
        f"dry-run rollback must leave all table counts at 0; got {after}"
    )


# --- 8. balance position bdata decoder ----------------------------------------

def test_decode_y_plain_list():
    """Plain list y-values pass through unchanged."""
    assert op._decode_y([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0]
    assert op._decode_y([]) == []


def test_decode_y_bdata_f8():
    """f8 (little-endian float64) bdata round-trips correctly."""
    import struct, base64
    values = [1000.0, 1100.0, 0.0, -500.5]
    raw = struct.pack(f"<{len(values)}d", *values)
    bdata = base64.b64encode(raw).decode()
    decoded = op._decode_y({"dtype": "f8", "bdata": bdata})
    assert len(decoded) == len(values)
    for got, want in zip(decoded, values):
        assert abs(got - want) < 1e-9


def test_decode_y_unknown_dtype_returns_empty():
    """An unrecognised dtype must not crash -- returns empty list."""
    import base64
    result = op._decode_y({"dtype": "u2", "bdata": base64.b64encode(b"\x00" * 8).decode()})
    assert result == []


def test_decode_y_non_list_non_dict_returns_empty():
    assert op._decode_y(None) == []
    assert op._decode_y(42) == []


def test_parse_balance_position_with_bdata():
    """parse_balance_position decodes bdata traces and produces correct row counts."""
    import struct, base64

    def make_bdata(values):
        raw = struct.pack(f"<{len(values)}d", *values)
        return {"dtype": "f8", "bdata": base64.b64encode(raw).decode()}

    xs = ["2023-08-31", "2023-09-30", "2023-10-31"]
    traces = [
        {"name": "Lokal individual", "x": xs, "y": make_bdata([100.0, 110.0, 120.0])},
        {"name": "Foreign korporat", "x": xs, "y": make_bdata([200.0, 210.0, 220.0])},
        {"name": "%Retail",          "x": xs, "y": make_bdata([10.0,  10.5,  11.0])},
        {"name": "%Foreign",         "x": xs, "y": make_bdata([30.0,  30.5,  31.0])},
    ]
    parsed = op.parse_balance_position(traces)
    # 2 category traces × 3 months = 6 monthly rows
    assert len(parsed["monthly"]) == 6, f"expected 6, got {len(parsed['monthly'])}"
    # 3 unique months in summary (only %Retail + %Foreign merged per date)
    assert len(parsed["summary"]) == 3, f"expected 3, got {len(parsed['summary'])}"
    # Values decoded correctly
    lots = {(r["period_date"], r["category"]): r["lots"] for r in parsed["monthly"]}
    assert lots[("2023-08-31", "local_individual")] == 100.0
    assert lots[("2023-10-31", "foreign_korporat")] == 220.0
    # Summary pct fields populated
    assert parsed["summary"][0]["pct_retail"] == 10.0
    assert parsed["summary"][2]["pct_foreign"] == 31.0

    # Plug into ingester and verify counts
    conn = make_conn()
    capture = make_capture(traces=traces)
    c = ing.ingest_capture(conn, "BREN", capture, "2026-08-30T10:00:00Z",
                           "https://neobdm.tech/stock_detail/BREN/")
    conn.commit()  # test owns the transaction
    assert c["balance_position_monthly"] == 6
    assert c["balance_position_summary_monthly"] == 3
    n = conn.execute("SELECT COUNT(*) FROM balance_position_monthly").fetchone()[0]
    assert n == 6
    conn.close()


def test_per_ticker_savepoint_atomicity_and_recovery():
    """Verify savepoint rollback isolates a failed ticker's partial writes and recovers."""
    conn = make_conn()
    conn.execute("BEGIN")

    # Ticker A: partially writes, then encounters an exception and rolls back to savepoint
    conn.execute("SAVEPOINT ticker_savepoint")
    try:
        # Perform some writes for TICKER_A
        capture_a = make_capture(kda1="Data per 31 jul 2026\n\nHOLDER A\nCorporate\n1.5%\t10M lot\t5M lot")
        ing.ingest_capture(conn, "TICKER_A", capture_a, "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TICKER_A/")

        # Verify that TICKER_A writes are visible inside the active transaction
        n_a = conn.execute("SELECT COUNT(*) FROM ownership_snapshot WHERE ticker = 'TICKER_A'").fetchone()[0]
        assert n_a == 1

        # Simulate exception (e.g. raised during subsequent parsing/processing of the same ticker)
        raise ValueError("Simulated parsing/ingestion failure for TICKER_A")

        conn.execute("RELEASE ticker_savepoint")
        conn.commit()
        conn.execute("BEGIN")
    except Exception:
        conn.execute("ROLLBACK TO ticker_savepoint")
        conn.execute("RELEASE ticker_savepoint")

    # Verify TICKER_A left exactly 0 rows in all tables
    for tbl in ["ownership_snapshot", "ownership_change", "custody_breakdown_snapshot", "balance_position_monthly"]:
        cnt = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE ticker = 'TICKER_A'").fetchone()[0]
        assert cnt == 0

    # Ticker B: successful capture immediately after on same connection
    conn.execute("SAVEPOINT ticker_savepoint")
    try:
        capture_b = make_capture(kda1="Data per 31 jul 2026\n\nHOLDER B\nCorporate\n2.5%\t20M lot\t10M lot")
        ing.ingest_capture(conn, "TICKER_B", capture_b, "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TICKER_B/")
        conn.execute("RELEASE ticker_savepoint")
        conn.commit()
        conn.execute("BEGIN")
    except Exception as e:
        conn.execute("ROLLBACK TO ticker_savepoint")
        conn.execute("RELEASE ticker_savepoint")
        raise e

    # Verify TICKER_B rows are successfully committed
    n_b = conn.execute("SELECT COUNT(*) FROM ownership_snapshot WHERE ticker = 'TICKER_B'").fetchone()[0]
    assert n_b == 1

    # Verify TICKER_A remains at 0 rows
    n_a = conn.execute("SELECT COUNT(*) FROM ownership_snapshot WHERE ticker = 'TICKER_A'").fetchone()[0]
    assert n_a == 0

    conn.close()


# =============================================================================
# Bitemporal layer: capture_run / ownership_observation / observation_state /
# capture_payload. The invariant under test throughout: exact replay is
# idempotent; factual revision is append-only and detectable. Contrast with
# the legacy INSERT OR IGNORE tests above, which prove the OPPOSITE property
# is deliberately preserved on the eight original tables.
# =============================================================================

def test_payload_hash_detects_every_relevant_field_change():
    """The regression test for the original defect: legacy raw_hash covers a
    row key plus a 200-byte PANE PREFIX, not the row's own payload, so most
    fields could change without the hash moving. payload_hash_hex() must
    react to every field in the payload, not just whichever happen to appear
    early in the source pane."""
    base = {"investor_category": "Corporate", "is_foreign": 0,
            "ownership_pct_raw": "34.6%", "ownership_pct": 34.6,
            "scrip_lot": 181000000.0, "scrip_pct": 21.0, "scrip_raw": "181M lot 21.0%",
            "scripless_lot": 118000000.0, "scripless_pct": 13.7,
            "scripless_raw": "118M lot 13.7%"}
    base_hash = ing.payload_hash_hex(ing.canonical_json(base))
    for field, new_value in [
        ("investor_category", "Individual"), ("is_foreign", 1),
        ("ownership_pct", 40.0), ("scrip_lot", 999.0), ("scrip_pct", 1.0),
        ("scripless_lot", 1.0), ("scripless_pct", 1.0),
    ]:
        mutated = dict(base)
        mutated[field] = new_value
        mutated_hash = ing.payload_hash_hex(ing.canonical_json(mutated))
        assert mutated_hash != base_hash, \
            f"payload_hash_hex did not react to a change in '{field}'"


def test_exact_replay_is_idempotent_at_observation_layer():
    conn = make_conn()
    capture = make_capture(kda1=TPIA_KDA1)
    c1 = ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                             "https://neobdm.tech/stock_detail/TPIA/")
    assert c1["ownership_observation"] > 0
    assert c1["ownership_revision"] == 0
    n_obs = conn.execute("SELECT COUNT(*) FROM ownership_observation").fetchone()[0]

    c2 = ing.ingest_capture(conn, "TPIA", capture, "2026-08-31T10:00:00Z",
                             "https://neobdm.tech/stock_detail/TPIA/")
    assert c2["ownership_observation"] == 0, "identical replay must add no new observation rows"
    assert c2["ownership_revision"] == 0
    n_obs2 = conn.execute("SELECT COUNT(*) FROM ownership_observation").fetchone()[0]
    assert n_obs == n_obs2

    state = conn.execute(
        "SELECT first_seen_at, last_seen_at, times_seen FROM observation_state"
    ).fetchall()
    assert all(row[2] == 2 for row in state), "times_seen must advance on an unchanged replay"
    assert all(row[0] == "2026-08-30T10:00:00Z" for row in state), "first_seen_at must not move"
    assert all(row[1] == "2026-08-31T10:00:00Z" for row in state), "last_seen_at must advance"
    conn.close()


def test_revision_is_preserved_not_dropped():
    """The core defect this layer exists to fix: under the legacy tables, a
    same-key different-payload re-observation is silently discarded by
    INSERT OR IGNORE with cur.rowcount thrown away. Here it must become a
    retrievable, numbered revision."""
    conn = make_conn()
    c1 = ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                             "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    assert c1["ownership_revision"] == 0

    revised = TPIA_KDA1.replace("34.6%\t181M lot 21.0%", "40.0%\t210M lot 24.3%")
    c2 = ing.ingest_capture(conn, "TPIA", make_capture(kda1=revised),
                             "2026-08-31T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    assert c2["ownership_observation"] == 1
    assert c2["ownership_revision"] == 1, "a changed payload at the same key must be a detected revision"

    rows = conn.execute(
        "SELECT revision_number, observed_at, payload_hash FROM ownership_observation "
        "WHERE business_key LIKE '%BARITO PACIFIC%' ORDER BY revision_number"
    ).fetchall()
    assert [r[0] for r in rows] == [0, 1]
    assert rows[0][2] != rows[1][2], "both revisions must be independently retrievable"

    # The legacy table, meanwhile, kept only the first-seen value (34.6%) --
    # this is the existing, deliberately-preserved contract, not a bug.
    legacy_pct = conn.execute(
        "SELECT ownership_pct FROM ownership_snapshot WHERE investor_name_raw='BARITO PACIFIC'"
    ).fetchone()[0]
    assert legacy_pct == 34.6

    state = conn.execute(
        "SELECT current_payload_hash, revision_count FROM observation_state "
        "WHERE business_key LIKE '%BARITO PACIFIC%'"
    ).fetchone()
    assert state[1] == 1
    assert state[0] == rows[1][2], "observation_state must point at the LATEST revision"
    conn.close()


def test_no_business_key_is_ever_silently_dropped():
    """Counterpart of the legacy tables' discarded cur.rowcount: every
    re-presented business key must resolve to exactly one outcome
    (new/unchanged/revised), never vanish without a trace."""
    conn = make_conn()
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    keys_after_1 = {r[0] for r in conn.execute("SELECT business_key FROM observation_state")}

    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-31T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    keys_after_2 = {r[0] for r in conn.execute("SELECT business_key FROM observation_state")}
    assert keys_after_1 == keys_after_2, "a re-presented key must never disappear from observation_state"
    conn.close()


def test_absence_vs_capture_failure_are_distinguishable():
    """status='ok' with a key absent means genuine disappearance. Any other
    status means unknown -- absence must not be inferred from it."""
    conn = make_conn()
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    ok_row = conn.execute(
        "SELECT status, row_count FROM capture_run WHERE ticker='TPIA' AND pane='insider-current'"
    ).fetchone()
    assert ok_row == ("ok", 3)  # TPIA_KDA1 fixture has 3 investor rows

    # A capture where the pane failed to load entirely (None, not empty text)
    conn2 = make_conn()
    failed_capture = make_capture(kda1=None)
    ing.ingest_capture(conn2, "XYZQ", failed_capture,
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/XYZQ/")
    fail_row = conn2.execute(
        "SELECT status, row_count, error_detail FROM capture_run "
        "WHERE ticker='XYZQ' AND pane='insider-current'"
    ).fetchone()
    assert fail_row[0] != "ok"
    assert fail_row[2] is not None, "a non-ok status must carry an explanation"
    # No observation_state row exists for XYZQ's insider-current -- correctly
    # UNKNOWN, not incorrectly inferred as "holder never existed".
    n = conn2.execute(
        "SELECT COUNT(*) FROM observation_state WHERE ticker='XYZQ'"
    ).fetchone()[0]
    assert n == 0
    conn.close(); conn2.close()


def test_row_count_zero_is_not_row_count_null():
    """A pane that rendered with zero rows and a pane that was never parsed
    are different facts and must stay distinguishable."""
    conn = make_conn()
    empty_but_present = "Data per 31 jul 2026 XLSX\nTotal Kepemilikan: 0.0%\n"
    ing.ingest_capture(conn, "EMPT", make_capture(kda1=empty_but_present),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/EMPT/")
    row = conn.execute(
        "SELECT status, row_count FROM capture_run WHERE ticker='EMPT' AND pane='insider-current'"
    ).fetchone()
    assert row == ("ok", 0)

    ing.ingest_capture(conn, "MISS", make_capture(kda1=None),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/MISS/")
    row2 = conn.execute(
        "SELECT status, row_count FROM capture_run WHERE ticker='MISS' AND pane='insider-current'"
    ).fetchone()
    assert row2[0] != "ok"
    assert row2[1] is None
    conn.close()


def test_first_and_last_seen_derivation_across_runs():
    conn = make_conn()
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    # unchanged middle run
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-31T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    revised = TPIA_KDA1.replace("34.6%\t181M lot 21.0%", "36.0%\t190M lot 22.0%")
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=revised),
                       "2026-09-01T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")

    row = conn.execute(
        "SELECT first_seen_at, last_seen_at, times_seen, revision_count FROM observation_state "
        "WHERE business_key LIKE '%BARITO PACIFIC%'"
    ).fetchone()
    assert row == ("2026-08-30T10:00:00Z", "2026-09-01T10:00:00Z", 3, 1)
    conn.close()


def test_observation_state_first_seen_and_current_state_are_exactly_rebuildable():
    """first_seen_at, current_payload_hash and revision_count always match
    the log exactly -- unlike last_seen_at (see the next test), these never
    depend on capture_run reconfirmation."""
    conn = make_conn()
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    revised = TPIA_KDA1.replace("34.6%\t181M lot 21.0%", "36.0%\t190M lot 22.0%")
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=revised),
                       "2026-08-31T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")

    before = sorted(conn.execute(
        "SELECT business_key, first_seen_at, current_payload_hash, revision_count "
        "FROM observation_state"
    ).fetchall())
    ing.rebuild_observation_state(conn)
    after = sorted(conn.execute(
        "SELECT business_key, first_seen_at, current_payload_hash, revision_count "
        "FROM observation_state"
    ).fetchall())
    assert before == after
    conn.close()


def test_observation_state_last_seen_rebuild_is_exact_when_whole_pane_unchanged():
    """When nothing else in the pane changes between two captures, rebuilt
    last_seen_at exactly matches the incrementally-tracked value (this is the
    common production case: most panes are unchanged on most days)."""
    conn = make_conn()
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),  # byte-identical pane
                       "2026-08-31T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")

    before = sorted(conn.execute(
        "SELECT business_key, last_seen_at FROM observation_state"
    ).fetchall())
    ing.rebuild_observation_state(conn)
    after = sorted(conn.execute(
        "SELECT business_key, last_seen_at FROM observation_state"
    ).fetchall())
    assert before == after
    assert all(v == "2026-08-31T10:00:00Z" for _, v in after)
    conn.close()


def test_observation_state_last_seen_rebuild_never_overstates_when_pane_partially_changes():
    """When ONE row in a pane changes, rebuild cannot recover the fact that
    an UNCHANGED sibling row was reconfirmed the same day (pane_hash is
    whole-pane, so the sibling's reconfirming run no longer hash-matches).
    The rebuilt value must therefore be <= the true incrementally-tracked
    value -- it may understate freshness, it must never overstate it."""
    conn = make_conn()
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    revised = TPIA_KDA1.replace("34.6%\t181M lot 21.0%", "36.0%\t190M lot 22.0%")
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=revised),  # only BARITO PACIFIC changed
                       "2026-08-31T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")

    true_last_seen = dict(conn.execute(
        "SELECT business_key, last_seen_at FROM observation_state"
    ).fetchall())
    ing.rebuild_observation_state(conn)
    rebuilt_last_seen = dict(conn.execute(
        "SELECT business_key, last_seen_at FROM observation_state"
    ).fetchall())

    for key in true_last_seen:
        assert rebuilt_last_seen[key] <= true_last_seen[key], \
            f"rebuild overstated freshness for {key}"

    prajogo_key = next(k for k in true_last_seen if "PRAJOGO" in k)
    assert true_last_seen[prajogo_key] == "2026-08-31T10:00:00Z", \
        "incremental tracking correctly reconfirms the unchanged row"
    assert rebuilt_last_seen[prajogo_key] == "2026-08-30T10:00:00Z", \
        "rebuild cannot see that reconfirmation once the whole pane's hash moved -- documented limitation"
    conn.close()


def test_legacy_isolation_strict_clock_starts_at_zero():
    """LEGACY_PARTIAL runs must never count toward the strict capture clock."""
    conn = make_conn()
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/",
                       run_id="2026-08-30", evidence_class="LEGACY_PARTIAL")
    strict_days = conn.execute(
        "SELECT COUNT(DISTINCT run_id) FROM capture_run WHERE evidence_class='STRICT' AND status='ok'"
    ).fetchone()[0]
    assert strict_days == 0

    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-09-05T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/",
                       run_id="2026-09-05", evidence_class="STRICT")
    strict_days2 = conn.execute(
        "SELECT COUNT(DISTINCT run_id) FROM capture_run WHERE evidence_class='STRICT' AND status='ok'"
    ).fetchone()[0]
    assert strict_days2 == 1
    conn.close()


def test_legacy_table_equals_revision_zero_projection():
    """The eight original tables must always equal the FIRST-observed
    (revision_number=0) slice of the log for shared keys -- this is the
    invariant that lets both layers coexist without diverging."""
    conn = make_conn()
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    revised = TPIA_KDA1.replace("34.6%\t181M lot 21.0%", "36.0%\t190M lot 22.0%")
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=revised),
                       "2026-08-31T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")

    legacy_pct = conn.execute(
        "SELECT ownership_pct FROM ownership_snapshot WHERE investor_name_raw='BARITO PACIFIC'"
    ).fetchone()[0]
    rev0_payload = conn.execute(
        "SELECT payload_json FROM ownership_observation "
        "WHERE business_key LIKE '%BARITO PACIFIC%' AND revision_number = 0"
    ).fetchone()[0]
    assert json.loads(rev0_payload)["ownership_pct"] == legacy_pct
    conn.close()


def test_fragment_replay_fidelity():
    """A stored gzipped fragment must re-parse to exactly the same rows
    recorded at ingest time."""
    conn = make_conn()
    ing.ingest_capture(conn, "TPIA", make_capture(kda1=TPIA_KDA1),
                       "2026-08-30T10:00:00Z", "https://neobdm.tech/stock_detail/TPIA/")
    frag = conn.execute(
        "SELECT fragment_path, byte_len FROM capture_payload"
    ).fetchone()
    assert frag is not None
    fragment_path, byte_len = frag
    abs_path = os.path.join(ing.HERE, *fragment_path.split("/"))
    import gzip
    with gzip.open(abs_path, "rb") as f:
        replayed_text = f.read().decode("utf-8")
    assert len(replayed_text.encode("utf-8")) == byte_len
    assert op.parse_kda1_current(replayed_text) == op.parse_kda1_current(TPIA_KDA1)
    conn.close()


# =============================================================================
# Balance Position badge temporal semantics.
#
# The badge ([Scripless] [Free Float] [Holder]) carries no as-of date of its
# own. Live captures showed Holder and Free Float changing mid-month while the
# page chart's latest month stayed fixed, so the chart month is NOT their
# effective date. A badge reading is therefore observed under one UNDATED key
# per ticker, and its only timing is the observation history itself.
# =============================================================================

# Byte-for-byte the retained ownership_raw_fragments/ badge fragments for BREN
# on three real capture runs. 2026-08-31 -> 2026-09-05: the page chart rolled
# from July to August while Holder stayed 43.6K. 2026-09-05 -> 2026-09-10:
# Holder moved to 44.3K while the chart month stayed August.
BADGE_BREN_2026_08_31 = (
    "BREN Balance Position Chart [Scripless: 36.7%] [Free Float: 12.6%] "
    "[Holder: 43.6K] ChartCombination chart with 22 data series.The chart has "
    "1 X axis displaying Time. Data ranges from 2024-09-01 00:00:00 to "
    "2026-07-01 00:00:00.The chart has 2 Y axes displaying Kepemilikan saham "
    "(lot) and Persentase.Created with Highcharts 10.1.0Kepemilikan saham "
    "(lot)PersentaseForeign LainnyaForeign YayasanForeign "
)
BADGE_BREN_2026_09_05 = (
    "BREN Balance Position Chart [Scripless: 36.7%] [Free Float: 12.6%] "
    "[Holder: 43.6K] ChartCombination chart with 22 data series.The chart has "
    "1 X axis displaying Time. Data ranges from 2024-09-01 00:00:00 to "
    "2026-08-01 00:00:00.The chart has 2 Y axes displaying Kepemilikan saham "
    "(lot) and Persentase.Created with Highcharts 10.1.0Kepemilikan saham "
    "(lot)PersentaseForeign LainnyaForeign YayasanForeign "
)
BADGE_BREN_2026_09_10 = (
    "BREN Balance Position Chart [Scripless: 36.7%] [Free Float: 12.6%] "
    "[Holder: 44.3K] ChartCombination chart with 22 data series.The chart has "
    "1 X axis displaying Time. Data ranges from 2024-09-01 00:00:00 to "
    "2026-08-01 00:00:00.The chart has 2 Y axes displaying Kepemilikan saham "
    "(lot) and Persentase.Created with Highcharts 10.1.0Kepemilikan saham "
    "(lot)PersentaseForeign LainnyaForeign YayasanForeign "
)
# The same 2026-09-05 reading with the chart's "Data ranges ..." sentence
# missing, e.g. cut off by the capture's 400-character badge slice.
BADGE_BREN_NO_RANGE = (
    "BREN Balance Position Chart [Scripless: 36.7%] [Free Float: 12.6%] "
    "[Holder: 43.6K]"
)

# Dash traces use month-END x values; the page chart labels the same month by
# its first day.
BREN_TRACES_TO_JUL = [
    {"name": "Lokal individual", "x": ["2026-06-30", "2026-07-31"], "y": [100.0, 110.0]},
    {"name": "scripless", "x": ["2026-06-30", "2026-07-31"],
     "y": [0.36739264577472924, 0.36739264577472924]},
]
BREN_TRACES_TO_AUG = [
    {"name": "Lokal individual", "x": ["2026-07-31", "2026-08-31"], "y": [110.0, 120.0]},
    {"name": "scripless", "x": ["2026-07-31", "2026-08-31"],
     "y": [0.36739264577472924, 0.36739264577472924]},
]

BREN_BADGE_KEY = '["float_holder_badge","BREN"]'
BREN_43_6K = {"free_float_pct": 12.6, "holder_count_raw": "43.6K", "scripless_pct": 36.7}
BREN_44_3K = {"free_float_pct": 12.6, "holder_count_raw": "44.3K", "scripless_pct": 36.7}
BREN_URL = "https://neobdm.tech/stock_detail/BREN/"


def badge_observations(conn):
    return [(key, rev, observed_at, json.loads(payload)) for key, rev, observed_at, payload
            in conn.execute(
                "SELECT business_key, revision_number, observed_at, payload_json "
                "FROM ownership_observation WHERE source_family='float_holder_badge' "
                "ORDER BY obs_id")]


def badge_state(conn):
    return conn.execute(
        "SELECT business_key, first_seen_at, last_seen_at, times_seen, revision_count "
        "FROM observation_state WHERE source_family='float_holder_badge' "
        "ORDER BY business_key").fetchall()


def test_badge_month_rollover_unchanged_reading_keeps_one_undated_key():
    """A. The chart rolling July -> August must not give an unchanged badge
    reading a second, month-dated identity: same key, times_seen goes to 2."""
    conn = make_conn()
    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_08_31, traces=BREN_TRACES_TO_JUL),
                       "2026-08-31T14:27:20Z", BREN_URL)
    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_05, traces=BREN_TRACES_TO_AUG),
                       "2026-09-05T07:04:14Z", BREN_URL)

    assert badge_state(conn) == [
        (BREN_BADGE_KEY, "2026-08-31T14:27:20Z", "2026-09-05T07:04:14Z", 2, 0)]
    assert badge_observations(conn) == [
        (BREN_BADGE_KEY, 0, "2026-08-31T14:27:20Z", BREN_43_6K)]


def test_badge_mid_month_update_is_one_revision_under_same_key():
    """B. 43.6K -> 44.3K under an unchanged chart range is one revision of the
    same key, stamped with the second capture's observed_at -- not dropped."""
    conn = make_conn()
    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_05, traces=BREN_TRACES_TO_AUG),
                       "2026-09-05T07:04:14Z", BREN_URL)
    c2 = ing.ingest_capture(conn, "BREN",
                            make_capture(badge=BADGE_BREN_2026_09_10, traces=BREN_TRACES_TO_AUG),
                            "2026-09-10T07:27:31Z", BREN_URL)

    assert badge_observations(conn) == [
        (BREN_BADGE_KEY, 0, "2026-09-05T07:04:14Z", BREN_43_6K),
        (BREN_BADGE_KEY, 1, "2026-09-10T07:27:31Z", BREN_44_3K),
    ]
    assert c2["ownership_revision"] == 1


def test_badge_key_identical_with_or_without_data_ranges_sentence():
    """C. The badge key must not depend on whether the chart's "Data ranges"
    sentence was captured. Previously its absence silently switched the key's
    date from the chart's month-start label to the Dash month-end date."""
    conn = make_conn()
    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_NO_RANGE, traces=BREN_TRACES_TO_AUG),
                       "2026-09-05T07:04:14Z", BREN_URL)
    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_05, traces=BREN_TRACES_TO_AUG),
                       "2026-09-06T07:15:31Z", BREN_URL)

    assert badge_state(conn) == [
        (BREN_BADGE_KEY, "2026-09-05T07:04:14Z", "2026-09-06T07:15:31Z", 2, 0)]


def test_badge_capture_run_claims_no_snapshot_date():
    """D. The page never dates the badge, so its manifest row claims no date."""
    conn = make_conn()
    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_05, traces=BREN_TRACES_TO_AUG),
                       "2026-09-05T07:04:14Z", BREN_URL)
    row = conn.execute(
        "SELECT status, snapshot_date_seen, row_count FROM capture_run "
        "WHERE ticker='BREN' AND pane='badge'"
    ).fetchone()
    assert row == ("ok", None, 1)


def test_badge_replay_of_retained_bren_sequence():
    """E. The retained BREN sequence 43.6K, 43.6K, 44.3K is one undated
    history: revision 0 first seen 08-31, reconfirmed on 09-05 across the chart
    rollover, revised to 44.3K on 09-10. No legacy dated row is written."""
    conn = make_conn()
    runs = [
        (BADGE_BREN_2026_08_31, BREN_TRACES_TO_JUL, "2026-08-31T14:27:20Z"),
        (BADGE_BREN_2026_09_05, BREN_TRACES_TO_AUG, "2026-09-05T07:04:14Z"),
        (BADGE_BREN_2026_09_10, BREN_TRACES_TO_AUG, "2026-09-10T07:27:31Z"),
    ]
    revisions = []
    for badge, traces, captured_at in runs:
        c = ing.ingest_capture(conn, "BREN", make_capture(badge=badge, traces=traces),
                               captured_at, BREN_URL)
        revisions.append(c["ownership_revision"])
        assert c["float_holder_snapshot"] == 0

    assert revisions == [0, 0, 1]
    assert badge_observations(conn) == [
        (BREN_BADGE_KEY, 0, "2026-08-31T14:27:20Z", BREN_43_6K),
        (BREN_BADGE_KEY, 1, "2026-09-10T07:27:31Z", BREN_44_3K),
    ]
    assert badge_state(conn) == [
        (BREN_BADGE_KEY, "2026-08-31T14:27:20Z", "2026-09-10T07:27:31Z", 3, 1)]
    assert conn.execute("SELECT COUNT(*) FROM float_holder_snapshot").fetchone()[0] == 0


def test_legacy_float_holder_snapshot_is_frozen_existing_rows_untouched():
    """Legacy float_holder_snapshot is no longer written: a pre-existing
    historical row survives byte-for-byte and no new dated row is added."""
    conn = make_conn()
    legacy_row = (
        "BREN", "2026-07-01", 12.6, 36.7, "43.6K", 43600.0, None,
        "2026-08-31T14:27:20Z", "2026-08-31T14:27:20Z", BREN_URL,
        "float_holder_badge", "stock_detail_v1", "3ca9cd4a78c0efd8", 1, 1,
    )
    conn.execute(
        "INSERT INTO float_holder_snapshot (ticker, snapshot_date, free_float_pct, "
        "scripless_pct, holder_count_raw, holder_count_approx, published_at, captured_at, "
        "available_at, source_url, source_family, extraction_version, raw_hash, "
        "dq_unknown_publication_time, dq_rounded_holder_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", legacy_row)

    for badge, captured_at in [(BADGE_BREN_2026_09_05, "2026-09-05T07:04:14Z"),
                               (BADGE_BREN_2026_09_10, "2026-09-10T07:27:31Z")]:
        ing.ingest_capture(conn, "BREN",
                           make_capture(badge=badge, traces=BREN_TRACES_TO_AUG),
                           captured_at, BREN_URL)

    rows = conn.execute(
        "SELECT ticker, snapshot_date, free_float_pct, scripless_pct, holder_count_raw, "
        "holder_count_approx, published_at, captured_at, available_at, source_url, "
        "source_family, extraction_version, raw_hash, dq_unknown_publication_time, "
        "dq_rounded_holder_count FROM float_holder_snapshot"
    ).fetchall()
    assert rows == [legacy_row]


def test_badge_observed_without_balance_position_traces():
    """The badge used to be skipped whenever the Dash traces were missing,
    only because the traces supplied its inferred date. With no date to
    anchor, a captured badge is observed on its own."""
    conn = make_conn()
    ing.ingest_capture(conn, "BREN", make_capture(badge=BADGE_BREN_2026_09_05),
                       "2026-09-05T07:04:14Z", BREN_URL)
    manifest = {pane: (status, seen) for pane, status, seen in conn.execute(
        "SELECT pane, status, snapshot_date_seen FROM capture_run WHERE ticker='BREN' "
        "AND pane IN ('badge', 'balance_position')")}
    assert manifest == {"badge": ("ok", None), "balance_position": ("empty_pane", None)}
    assert badge_observations(conn) == [
        (BREN_BADGE_KEY, 0, "2026-09-05T07:04:14Z", BREN_43_6K)]


def test_badge_present_but_empty_is_not_an_observation():
    """An empty badge string is a rendered-but-empty pane: manifest says so,
    and no reading is observed."""
    conn = make_conn()
    ing.ingest_capture(conn, "BREN", make_capture(badge="", traces=BREN_TRACES_TO_AUG),
                       "2026-09-05T07:04:14Z", BREN_URL)
    row = conn.execute(
        "SELECT status, snapshot_date_seen, row_count, error_detail FROM capture_run "
        "WHERE ticker='BREN' AND pane='badge'").fetchone()
    assert row == ("empty_pane", None, None, "badge present but empty")
    assert badge_observations(conn) == []


def test_old_month_dated_badge_observations_are_left_untouched():
    """History written under the old month-dated key is never rewritten or
    bumped: a new capture of the very same pane lands on the undated key only."""
    conn = make_conn()
    old_key = '["float_holder_snapshot","BREN","2026-08-01"]'
    ing.record_observation(conn, "2026-09-05", "BREN", "float_holder_badge", old_key,
                           BREN_43_6K, "2026-09-05T07:04:14Z")

    def old_rows():
        return (conn.execute("SELECT * FROM ownership_observation WHERE business_key=?",
                             (old_key,)).fetchall(),
                conn.execute("SELECT * FROM observation_state WHERE business_key=?",
                             (old_key,)).fetchall())

    before = old_rows()
    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_05, traces=BREN_TRACES_TO_AUG),
                       "2026-09-29T08:00:00Z", BREN_URL)
    assert old_rows() == before
    assert [k for k, *_ in badge_state(conn)] == [BREN_BADGE_KEY, old_key]


def test_source_dated_ownership_products_unchanged():
    """F. Only the badge lost its inferred date. Every source-dated product
    keeps its source-provided date in both its legacy key and its observation
    business key, and its payload shape (so its payload_hash) is unchanged."""
    conn = make_conn()
    capture = make_capture(kda1=TPIA_KDA1, kda5=TPIA_KDA5, pkda1=BREN_PKDA1_FALSE_TURNOVER,
                           pkda5=WIFI_PKDA5_SAME_DAY_OPPOSITE, traces=BP_TRACES,
                           badge=BADGE_TPIA)
    ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                       "https://neobdm.tech/stock_detail/TPIA/")

    keys = {}
    for family, key in conn.execute(
            "SELECT source_family, business_key FROM ownership_observation "
            "WHERE source_family != 'float_holder_badge'"):
        keys.setdefault(family, set()).add(key)
    assert keys == {
        "stock_detail_kda1_current": {
            '["ownership_snapshot","TPIA","1pct","2026-07-31","BARITO PACIFIC"]',
            '["ownership_snapshot","TPIA","1pct","2026-07-31","SCG CHEMICALS PUBLIC"]',
            '["ownership_snapshot","TPIA","1pct","2026-07-31","PRAJOGO PANGESTU"]',
        },
        "stock_detail_kda5_current": {
            '["ownership_snapshot","TPIA","5pct","2026-08-27","PT BARITO PACIFIC TBK"]',
            '["custody_breakdown_snapshot","TPIA","2026-08-27","PT BARITO PACIFIC TBK",0]',
            '["custody_breakdown_snapshot","TPIA","2026-08-27","PT BARITO PACIFIC TBK",1]',
            '["custody_breakdown_snapshot","TPIA","2026-08-27","PT BARITO PACIFIC TBK",2]',
        },
        "stock_detail_pkda1_moves": {
            '["ownership_change","TPIA","1pct","2026-05-29","PRIME HILL FUND",0]',
            '["ownership_change","TPIA","1pct","2026-05-29","ZHAOCAI PRIME HILL FUND",0]',
        },
        "stock_detail_pkda5_moves": {
            '["ownership_change","TPIA","5pct","2026-05-20","INVESTASI SUKSES BERSAMA",0]',
            '["ownership_change","TPIA","5pct","2026-05-20","INVESTASI SUKSES BERSAMA",1]',
        },
        "balance_position_chart": {
            '["balance_position_monthly","TPIA","2023-08-31","local_individual"]',
            '["balance_position_monthly","TPIA","2023-09-30","local_individual"]',
            '["balance_position_monthly","TPIA","2023-08-31","foreign_korporat"]',
            '["balance_position_monthly","TPIA","2023-09-30","foreign_korporat"]',
            '["balance_position_summary_monthly","TPIA","2023-08-31"]',
            '["balance_position_summary_monthly","TPIA","2023-09-30"]',
        },
    }

    def payload(key):
        return json.loads(conn.execute(
            "SELECT payload_json FROM ownership_observation WHERE business_key=?",
            (key,)).fetchone()[0])

    # One row per source-dated family: a payload-shape change here would make
    # every existing key look revised on the next production run.
    assert payload('["ownership_snapshot","TPIA","1pct","2026-07-31","BARITO PACIFIC"]') == {
        "investor_category": "Corporate", "is_foreign": 0,
        "ownership_pct_raw": "34.6%", "ownership_pct": 34.6,
        "scrip_lot": 181000000.0, "scrip_pct": 21.0, "scrip_raw": "181M lot 21.0%",
        "scripless_lot": 118000000.0, "scripless_pct": 13.7,
        "scripless_raw": "118M lot 13.7%",
    }
    assert payload('["ownership_snapshot","TPIA","5pct","2026-08-27","PT BARITO PACIFIC TBK"]') == {
        "is_foreign": 0, "ownership_pct": 13.7}
    assert payload('["custody_breakdown_snapshot","TPIA","2026-08-27","PT BARITO PACIFIC TBK",0]') == {
        "investor_total_pct": 13.7, "is_foreign": 0,
        "custodian_label": "AF", "custodian_pct_of_holder": 60.8}
    assert payload('["ownership_change","TPIA","1pct","2026-05-29","PRIME HILL FUND",0]') == {
        "investor_category": "Trustee Bank", "is_foreign": 1,
        "resulting_ownership_pct_raw": "3.0%", "resulting_ownership_pct": 3.0,
        "scrip_lot_change": None, "scripless_lot_change": 40600000.0,
        "note": "Masuk PKDA 1%"}
    assert payload('["ownership_change","TPIA","5pct","2026-05-20","INVESTASI SUKSES BERSAMA",0]') == {
        "is_foreign": 0, "lot_change": -600000.0, "is_custodian_move": 0,
        "custodian_or_code": "YB"}
    assert payload('["balance_position_monthly","TPIA","2023-08-31","local_individual"]') == {
        "lots": 1000.0}
    assert payload('["balance_position_summary_monthly","TPIA","2023-08-31"]') == {
        "pct_retail": 12.0, "pct_institusi": 50.0, "pct_foreign": 30.0, "pct_scripless": 47.0}

    legacy_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
        "ownership_snapshot", "ownership_change", "custody_breakdown_snapshot",
        "balance_position_monthly", "balance_position_summary_monthly")}
    assert legacy_counts == {
        "ownership_snapshot": 4, "ownership_change": 4, "custody_breakdown_snapshot": 3,
        "balance_position_monthly": 4, "balance_position_summary_monthly": 2,
    }
    assert set(conn.execute(
        "SELECT threshold, snapshot_date FROM ownership_snapshot").fetchall()) == {
        ("1pct", "2026-07-31"), ("5pct", "2026-08-27")}
    assert {pane: (status, seen) for pane, status, seen in conn.execute(
        "SELECT pane, status, snapshot_date_seen FROM capture_run WHERE ticker='TPIA' "
        "AND pane != 'badge'")} == {
        "insider-current": ("ok", "2026-07-31"), "insider-moves": ("ok", None),
        "insider5p-current": ("ok", "2026-08-27"), "insider5p-moves": ("ok", None),
        "balance_position": ("ok", None)}


# --- badge cutover / replay safety -------------------------------------------
#
# ownership_capture.py resumes from cached raw captures with their ORIGINAL
# captured_at, run_id defaults to that date, and capture_run is INSERT OR
# REPLACE. A replay must therefore never backfill the undated key, overwrite a
# run's badge manifest row, or revise the badge state backwards.

def seed_legacy_badge_run(conn, badge_text, captured_at, month, ticker="BREN"):
    """Write exactly what the pre-fix ingest (b4fd79e) wrote for one badge
    capture: a float_holder_snapshot row under the inferred month, an
    observation under the month-dated key, and a manifest row naming that
    month in snapshot_date_seen."""
    run_id = captured_at[:10]
    badge = op.parse_badge(badge_text)
    conn.execute(
        "INSERT OR IGNORE INTO float_holder_snapshot (ticker, snapshot_date, free_float_pct, "
        "scripless_pct, holder_count_raw, holder_count_approx, published_at, captured_at, "
        "available_at, source_url, source_family, extraction_version, raw_hash, "
        "dq_unknown_publication_time, dq_rounded_holder_count) "
        "VALUES (?,?,?,?,?,?,NULL,?,?,?,'float_holder_badge','stock_detail_v1','legacy',1,1)",
        (ticker, month, badge["free_float_pct"], badge["scripless_pct"],
         badge["holder_count_raw"], badge["holder_count_approx"],
         captured_at, captured_at, f"https://neobdm.tech/stock_detail/{ticker}/"))
    ing.record_observation(
        conn, run_id, ticker, "float_holder_badge",
        f'["float_holder_snapshot","{ticker}","{month}"]',
        {"free_float_pct": badge["free_float_pct"], "scripless_pct": badge["scripless_pct"],
         "holder_count_raw": badge["holder_count_raw"]},
        captured_at)
    ing.record_capture_run(conn, run_id, ticker, "badge", captured_at, "ok",
                           snapshot_date_seen=month, row_count=1,
                           pane_hash=ing.payload_hash_hex(badge_text))


def badge_footprint(conn):
    """Every row the badge can touch, in a stable order."""
    return {
        "capture_run": conn.execute(
            "SELECT * FROM capture_run WHERE pane='badge' ORDER BY run_id, ticker").fetchall(),
        "observation": conn.execute(
            "SELECT * FROM ownership_observation WHERE source_family='float_holder_badge' "
            "ORDER BY obs_id").fetchall(),
        "state": conn.execute(
            "SELECT * FROM observation_state WHERE source_family='float_holder_badge' "
            "ORDER BY business_key").fetchall(),
        "float_holder_snapshot": conn.execute(
            "SELECT * FROM float_holder_snapshot ORDER BY ticker, snapshot_date").fetchall(),
    }


def test_replaying_legacy_recorded_badge_runs_touches_nothing():
    """Blocker 1. Re-ingesting cached pre-fix captures -- runs the legacy
    scheme already recorded, and a legacy-era run it never recorded -- must not
    create the undated key, rewrite any badge manifest row (and its
    snapshot_date_seen), touch the month-dated observations, or touch
    float_holder_snapshot."""
    conn = make_conn()
    seed_legacy_badge_run(conn, BADGE_BREN_2026_08_31, "2026-08-31T14:27:20Z", "2026-07-01")
    seed_legacy_badge_run(conn, BADGE_BREN_2026_09_05, "2026-09-05T07:04:14Z", "2026-08-01")
    seed_legacy_badge_run(conn, BADGE_BREN_2026_09_10, "2026-09-10T07:27:31Z", "2026-08-01")
    before = badge_footprint(conn)

    for badge, traces, captured_at in [
        (BADGE_BREN_2026_08_31, BREN_TRACES_TO_JUL, "2026-08-31T14:27:20Z"),
        (BADGE_BREN_2026_09_05, BREN_TRACES_TO_AUG, "2026-09-05T07:04:14Z"),
        (BADGE_BREN_2026_09_10, BREN_TRACES_TO_AUG, "2026-09-10T07:27:31Z"),
        (BADGE_BREN_2026_09_05, BREN_TRACES_TO_AUG, "2026-09-03T07:19:05Z"),  # never recorded
    ]:
        ing.ingest_capture(conn, "BREN", make_capture(badge=badge, traces=traces),
                           captured_at, BREN_URL)

    assert badge_footprint(conn) == before
    assert [k for k, *_ in badge_state(conn)] == [
        '["float_holder_snapshot","BREN","2026-07-01"]',
        '["float_holder_snapshot","BREN","2026-08-01"]']


def test_replaying_an_older_capture_never_revises_undated_state_backwards():
    """Blocker 2. Once 44.3K is the undated key's current state, replaying an
    older 43.6K capture -- one already ingested, or one never ingested -- must
    leave the state (and every badge row) exactly as it was."""
    conn = make_conn()
    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_05, traces=BREN_TRACES_TO_AUG),
                       "2026-09-05T07:04:14Z", BREN_URL)
    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_10, traces=BREN_TRACES_TO_AUG),
                       "2026-09-10T07:27:31Z", BREN_URL)
    before = badge_footprint(conn)

    for captured_at in ("2026-09-05T07:04:14Z", "2026-09-07T07:28:40Z"):
        ing.ingest_capture(conn, "BREN",
                           make_capture(badge=BADGE_BREN_2026_09_05, traces=BREN_TRACES_TO_AUG),
                           captured_at, BREN_URL)

    assert badge_footprint(conn) == before
    assert badge_state(conn) == [
        (BREN_BADGE_KEY, "2026-09-05T07:04:14Z", "2026-09-10T07:27:31Z", 2, 1)]
    assert badge_observations(conn)[-1] == (
        BREN_BADGE_KEY, 1, "2026-09-10T07:27:31Z", BREN_44_3K)


def test_genuinely_later_capture_after_legacy_runs_creates_and_updates_undated_key():
    """Blocker 3. A capture newer than every recorded badge run starts the
    undated key normally and keeps revising it, while the legacy run's
    manifest row and month-dated key stay exactly as they were."""
    conn = make_conn()
    seed_legacy_badge_run(conn, BADGE_BREN_2026_09_10, "2026-09-10T07:27:31Z", "2026-08-01")
    legacy_manifest = conn.execute(
        "SELECT * FROM capture_run WHERE run_id='2026-09-10' AND pane='badge'").fetchall()
    old_state = conn.execute(
        "SELECT * FROM observation_state WHERE business_key="
        "'[\"float_holder_snapshot\",\"BREN\",\"2026-08-01\"]'").fetchall()

    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_10, traces=BREN_TRACES_TO_AUG),
                       "2026-09-29T08:00:00Z", BREN_URL)
    c2 = ing.ingest_capture(conn, "BREN",
                            make_capture(badge=BADGE_BREN_2026_09_05, traces=BREN_TRACES_TO_AUG),
                            "2026-09-30T08:00:00Z", BREN_URL)

    assert [o for o in badge_observations(conn) if o[0] == BREN_BADGE_KEY] == [
        (BREN_BADGE_KEY, 0, "2026-09-29T08:00:00Z", BREN_44_3K),
        (BREN_BADGE_KEY, 1, "2026-09-30T08:00:00Z", BREN_43_6K),
    ]
    assert c2["ownership_revision"] == 1
    assert conn.execute(
        "SELECT run_id, status, snapshot_date_seen FROM capture_run WHERE pane='badge' "
        "ORDER BY run_id").fetchall() == [
        ("2026-09-10", "ok", "2026-08-01"), ("2026-09-29", "ok", None),
        ("2026-09-30", "ok", None)]
    assert conn.execute(
        "SELECT * FROM capture_run WHERE run_id='2026-09-10' AND pane='badge'"
    ).fetchall() == legacy_manifest
    assert conn.execute(
        "SELECT * FROM observation_state WHERE business_key="
        "'[\"float_holder_snapshot\",\"BREN\",\"2026-08-01\"]'").fetchall() == old_state


def test_later_capture_in_a_run_the_legacy_scheme_recorded_leaves_that_run_legacy():
    """A run the legacy scheme already recorded stays legacy for the badge: a
    later capture that shares its run_id (same UTC day) must not overwrite that
    run's manifest row. The next run is processed normally."""
    conn = make_conn()
    seed_legacy_badge_run(conn, BADGE_BREN_2026_09_10, "2026-09-28T08:53:21Z", "2026-08-01")
    before = badge_footprint(conn)

    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_10, traces=BREN_TRACES_TO_AUG),
                       "2026-09-28T15:00:00Z", BREN_URL)
    assert badge_footprint(conn) == before

    ing.ingest_capture(conn, "BREN",
                       make_capture(badge=BADGE_BREN_2026_09_10, traces=BREN_TRACES_TO_AUG),
                       "2026-09-29T08:00:00Z", BREN_URL)
    assert [o for o in badge_observations(conn) if o[0] == BREN_BADGE_KEY] == [
        (BREN_BADGE_KEY, 0, "2026-09-29T08:00:00Z", BREN_44_3K)]


def test_same_run_exact_resume_is_idempotent_for_badge():
    """Blocker 4. Resuming a run re-ingests its cached capture with the same
    captured_at: nothing about the badge may change, not even times_seen."""
    conn = make_conn()
    capture = make_capture(badge=BADGE_BREN_2026_09_05, traces=BREN_TRACES_TO_AUG)
    ing.ingest_capture(conn, "BREN", capture, "2026-09-05T07:04:14Z", BREN_URL)
    before = badge_footprint(conn)

    counts = ing.ingest_capture(conn, "BREN", capture, "2026-09-05T07:04:14Z", BREN_URL)

    assert badge_footprint(conn) == before
    assert sum(counts.values()) == 0


# --- rebuild_observation_state across the badge key change ---------------------

def test_rebuild_does_not_cross_confirm_badge_keys_across_the_key_change():
    """Rebuild A+B. The old month-dated key and the new undated key share one
    pane and, here, byte-identical pane text. A run only reconfirms the key its
    own scheme produced: the old key gains no post-cutover confirmations, the
    new key counts no pre-cutover runs, and rebuild never exceeds the
    incrementally tracked values."""
    conn = make_conn()
    for captured_at in ("2026-09-27T08:27:47Z", "2026-09-28T08:53:21Z"):
        seed_legacy_badge_run(conn, BADGE_BREN_2026_09_10, captured_at, "2026-08-01")
    for captured_at in ("2026-09-29T08:00:00Z", "2026-09-30T08:00:00Z"):
        ing.ingest_capture(conn, "BREN",
                           make_capture(badge=BADGE_BREN_2026_09_10, traces=BREN_TRACES_TO_AUG),
                           captured_at, BREN_URL)
    incremental = badge_state(conn)

    ing.rebuild_observation_state(conn)

    expected = [
        (BREN_BADGE_KEY, "2026-09-29T08:00:00Z", "2026-09-30T08:00:00Z", 2, 0),
        ('["float_holder_snapshot","BREN","2026-08-01"]',
         "2026-09-27T08:27:47Z", "2026-09-28T08:53:21Z", 2, 0),
    ]
    assert incremental == expected
    assert badge_state(conn) == expected


def test_rebuild_unchanged_for_non_badge_families():
    """Rebuild C. Every other source family still reconfirms on whole-pane hash
    alone: an identical capture seen twice rebuilds to last_seen = second
    capture, times_seen = 2, for every key of every family."""
    conn = make_conn()
    capture = make_capture(kda1=TPIA_KDA1, kda5=TPIA_KDA5, pkda1=BREN_PKDA1_FALSE_TURNOVER,
                           pkda5=WIFI_PKDA5_SAME_DAY_OPPOSITE, traces=BP_TRACES,
                           badge=BADGE_TPIA)
    for captured_at in ("2026-08-30T10:00:00Z", "2026-08-31T10:00:00Z"):
        ing.ingest_capture(conn, "TPIA", capture, captured_at,
                           "https://neobdm.tech/stock_detail/TPIA/")

    ing.rebuild_observation_state(conn)

    rows = conn.execute(
        "SELECT source_family, first_seen_at, last_seen_at, times_seen FROM observation_state"
    ).fetchall()
    assert {r[0] for r in rows} == {
        "stock_detail_kda1_current", "stock_detail_kda5_current", "stock_detail_pkda1_moves",
        "stock_detail_pkda5_moves", "balance_position_chart", "float_holder_badge"}
    assert {r[1:] for r in rows} == {("2026-08-30T10:00:00Z", "2026-08-31T10:00:00Z", 2)}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
