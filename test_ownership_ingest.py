"""Tests for the ownership/custody capture-and-archival infrastructure.

Runs entirely against an in-memory SQLite DB and fixed fixture strings
pulled from real captures (discovery_batch2.json / pass-2 tickers) --
no network access, no Playwright, no live NeoBDM session required.

    py -3 -m pytest test_ownership_ingest.py -v
"""
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

def test_holder_count_parsed_and_flagged_approximate():
    conn = make_conn()
    capture = make_capture(traces=BP_TRACES, badge=BADGE_TPIA)
    ing.ingest_capture(conn, "TPIA", capture, "2026-08-30T10:00:00Z",
                        "https://neobdm.tech/stock_detail/TPIA/")
    row = conn.execute(
        "SELECT holder_count_raw, holder_count_approx, dq_rounded_holder_count "
        "FROM float_holder_snapshot WHERE ticker='TPIA'"
    ).fetchone()
    assert row == ("101K", 101000.0, 1)


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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
