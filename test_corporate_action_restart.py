"""Evidence-based trust restart: bounded, calendar-defined and slice invariant.

Synthetic bars are reconstructed inputs. The historical witnesses read only the
reviewed read-only fixture (sha256 pinned by verify_corporate_action_contract)
and mutate private copies of it; they never open a production database.
"""

from datetime import date, timedelta
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sqlite3

import pandas as pd
import pytest

import price_audit as pa
import price_contract as pc
from price_contract_frame import annotate_prices
from test_price_contract import document

K = getattr(pc, "RESTART_SESSIONS", 10)
CONTEXT = getattr(pc, "SERIES_CONTEXT_ROWS", 10)
FIRST_RESTART = CONTEXT + K - 1          # zero-based row of the first restart anchor
DEPENDENCY = getattr(pc, "DEPENDENCY_ROWS", CONTEXT + K + 1)   # earlier rows that pin a session
FIXTURE_SHA256 = "6fc475e6db6be597a539a8cc30f6a0c44f05a5c14b263367c07a3aa417389be5"
TRUST_COLUMNS = ["close_anchor_admissible", "price_step_admissible", "entry_open_admissible",
                 "limit_reference_status", "limit_reference_price", "limit_admission_status",
                 "limit_unresolved_reason", "limit_violation", "series_break", "cross_ticker_dup"]


def sessions(start, count):
    out, day = [], date.fromisoformat(start)
    while len(out) < count:
        if pc.is_idx_session(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def weekdays_2025(start, end):
    out, day = [], date.fromisoformat(start)
    while day <= date.fromisoformat(end):
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def bars(ticker, dates, closes=None, base=1000.0):
    closes = list(closes) if closes is not None else [base + i for i in range(len(dates))]
    return pd.DataFrame({"date": dates, "ticker": ticker, "open": [c * 0.998 for c in closes],
                         "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                         "close": closes, "volume": [1000.0] * len(dates)})


def registry(tickers=("AAAA", "BBBB"), start="2026-01-02", end="2026-12-30", events=()):
    doc = document()
    doc["registry_version"] = "synthetic-restart-fixture.v1"
    doc["events"] = list(events)
    doc["reviewed_coverage"] = [{"venue": "IDX", "market_scope": ["REGULAR"],
                                 "tickers": list(tickers), "from": start, "through": end,
                                 "evidence_refs": ["SYNTHETIC_RESTART_FIXTURE_ONLY"]}]
    return pc.parse_registry(doc)


def audit(px, reg=None, trusted=None, representation=pc.RAW_ACTUAL):
    return pa.detect(px.reset_index(drop=True), trusted=trusted, registry=reg or registry(),
                     representation=representation)


def trusted_dates(out, ticker="AAAA"):
    rows = out[out.ticker.eq(ticker)]
    return rows.loc[rows.close_anchor_admissible.astype(bool), "date"].tolist()


# ── W1: unsupported 2025 history, certified 2026 restart, ordinary recovery ──

def test_w1_unsupported_history_restarts_only_on_a_complete_2026_window():
    old = weekdays_2025("2025-12-01", "2025-12-30")
    new = sessions("2026-01-02", 45)
    px = pd.concat([bars("AAAA", old, base=900.0), bars("AAAA", new)], ignore_index=True)
    out = audit(px)
    assert not out.loc[out.date.lt("2026"), "close_anchor_admissible"].any()
    assert out.loc[out.date.lt("2026"), "limit_unresolved_reason"].eq("UNSUPPORTED_CALENDAR").all()
    assert trusted_dates(out) == new[FIRST_RESTART:], "restart must need a full evidence window"
    later = out[out.date.gt(new[FIRST_RESTART])]
    assert later.limit_reference_status.eq("RESOLVED").all()
    assert later.price_step_admissible.all() and later.entry_open_admissible.all()
    statuses = out.set_index("date")["anchor_trust_status"]
    assert statuses[new[FIRST_RESTART]] == "TRUSTED_RESTART_ANCHOR"
    assert statuses[new[FIRST_RESTART + 1]] == "TRUSTED_CHAIN"
    assert statuses[new[FIRST_RESTART - 1]] == "RESTART_PENDING"
    assert statuses[old[-1]] == "INADMISSIBLE"
    # The restart anchor closes a window; its own entry step is not certified.
    assert not out.set_index("date").loc[new[FIRST_RESTART], "price_step_admissible"]


def test_w1_detection_recovers_after_unsupported_history():
    old = weekdays_2025("2025-12-01", "2025-12-30")
    new = sessions("2026-01-02", 45)
    closes = [1000.0 + i for i in range(len(new))]
    closes[40] = closes[39] * 1.8
    px = pd.concat([bars("AAAA", old, base=900.0), bars("AAAA", new, closes)], ignore_index=True)
    out = audit(px).set_index("date")
    assert out.loc[new[40], "limit_violation"] and out.loc[new[40], "suspect"]
    assert out.loc[new[40], "limit_reference_status"] == "RESOLVED"
    assert not out.loc[new[40], "close_anchor_admissible"]


# ── W2: an old quarantined row does not block a clean current dependency window ──

def test_w2_old_quarantined_row_does_not_block_a_current_certified_trade():
    import strategy_variants as sv
    dates = sessions("2026-03-02", 75)
    px = bars("AAAA", dates)
    reg = registry(("AAAA",), dates[0], dates[-1])
    with sqlite3.connect(":memory:") as conn:
        px.to_sql("price_history", conn, index=False)
        conn.execute("CREATE TABLE price_quarantine(date,ticker,reasons)")
        conn.execute("INSERT INTO price_quarantine VALUES(?, 'AAAA', 'cross_ticker_dup')", (dates[20],))
        panel = pa.clean_panel(conn, horizons=(1,), open_anchored=True, registry=reg,
                               representation=pc.RAW_ACTUAL)
    decision = dates[60]                       # eight weeks (40 sessions) after the quarantine
    assert dates[20] not in panel.date.tolist()
    row = panel.set_index("date").loc[decision]
    assert row.close_anchor_admissible and row.fwd_1 == pytest.approx(1061.0 / 1060.0 - 1)
    assert row.fwd_oo_1 == pytest.approx(1062.0 / 1061.0 - 1)
    ix, by_date = sv._index_price_history(panel, registry=reg)
    assert sv.simulate_trade(ix, by_date, "AAAA", decision, 1, None, None, registry=reg) \
        == pytest.approx(1 / 0.998 - 1)
    # The quarantine itself is never reused and its direct successors wait for evidence.
    nxt = panel.set_index("date")
    assert not nxt.loc[dates[21], "close_anchor_admissible"]
    assert nxt.loc[dates[21], "limit_unresolved_reason"] == "UNTRUSTED_PREDECESSOR"
    assert not nxt.loc[dates[21 + K - 2], "close_anchor_admissible"]
    assert nxt.loc[dates[21 + K - 1], "close_anchor_admissible"]
    assert nxt.loc[dates[21 + K - 1], "anchor_trust_status"] == "TRUSTED_RESTART_ANCHOR"


# ── W3/W4: contaminated runs cannot bootstrap; slicing cannot launder them ──

def contaminated_pair(start=30, length=10):
    dates = sessions("2026-03-02", 70)
    a, b = bars("AAAA", dates), bars("BBBB", dates, base=1800.0)
    for i in range(start, start + length):       # AAAA stored BBBB's bar: the race signature
        a.loc[i, ["open", "high", "low", "close", "volume"]] = b.loc[i, ["open", "high", "low", "close", "volume"]].values
    return dates, pd.concat([a, b], ignore_index=True)


def test_w3_cross_ticker_contaminated_run_cannot_bootstrap_trust():
    dates, px = contaminated_pair(length=K + 5)
    out = audit(px, registry(("AAAA", "BBBB"), dates[0], dates[-1]))
    run = dates[30:30 + K + 5]
    aaaa = out[out.ticker.eq("AAAA")].set_index("date")
    assert aaaa.loc[run, "cross_ticker_dup"].all()
    assert not aaaa.loc[run, "close_anchor_admissible"].any()
    assert aaaa.loc[run, "anchor_trust_status"].eq("INADMISSIBLE").all()
    after = dates[30 + K + 5:]
    assert not aaaa.loc[after[:K - 1], "close_anchor_admissible"].any()
    assert aaaa.loc[after[K - 1:], "close_anchor_admissible"].all(), "recovery after the run"


def test_w3_evidence_free_short_run_cannot_bootstrap_trust():
    dates = sessions("2026-03-02", 60)
    closes = [1000.0 + i for i in range(60)]
    for i in range(30, 30 + K - 1):               # consistent with itself, +80% vs history
        closes[i] = 1800.0 + i
    out = audit(bars("AAAA", dates, closes), registry(("AAAA",), dates[0], dates[-1])).set_index("date")
    run = dates[30:30 + K - 1]
    assert out.loc[dates[30], "limit_violation"]
    assert not out.loc[run, "close_anchor_admissible"].any()
    exit_bar = dates[30 + K - 1]
    assert out.loc[exit_bar, "limit_violation"], "the return discontinuity is flagged too"
    assert not out.loc[dates[30 + K - 1:30 + 2 * K - 1], "close_anchor_admissible"].any()
    assert out.loc[dates[30 + 2 * K - 1], "close_anchor_admissible"]


def test_w3_quarantined_long_run_cannot_bootstrap_trust():
    dates = sessions("2026-03-02", 70)
    closes = [1000.0 + i for i in range(70)]
    run = range(30, 30 + 2 * K)
    for i in run:
        closes[i] = 1800.0 + i
    px = bars("AAAA", dates, closes)
    reg = registry(("AAAA",), dates[0], dates[-1])
    with sqlite3.connect(":memory:") as conn:
        px.to_sql("price_history", conn, index=False)
        conn.execute("CREATE TABLE price_quarantine(date,ticker,reasons)")
        conn.executemany("INSERT INTO price_quarantine VALUES(?, 'AAAA', 'limit_violation')",
                         [(dates[i],) for i in run])
        out, bad, _ = pa.adjudicate_quarantine(conn, registry=reg, representation=pc.RAW_ACTUAL)
    out = out.set_index("date")
    assert not out.loc[[dates[i] for i in run], "close_anchor_admissible"].any()
    assert out.loc[[dates[i] for i in run], "anchor_trust_reason"].eq("EXTERNALLY_UNTRUSTED").all()
    assert out.loc[dates[30 + 2 * K + K - 1], "close_anchor_admissible"]


@pytest.mark.parametrize("cut", [31, 33, 35])
def test_w4_frame_starting_mid_cross_ticker_run_does_not_admit_it(cut):
    dates, px = contaminated_pair(length=K + 5)
    sliced = px[px.date.ge(dates[cut])]
    out = audit(sliced, registry(("AAAA", "BBBB"), dates[0], dates[-1]))
    aaaa = out[out.ticker.eq("AAAA")].set_index("date")
    run = [d for d in dates[30:30 + K + 5] if d >= dates[cut]]
    assert not aaaa.loc[run, "close_anchor_admissible"].any()
    assert not aaaa.loc[run, "price_step_admissible"].any()


@pytest.mark.parametrize("cut", [31, 34, 37])
def test_w4_frame_starting_mid_evidence_free_run_does_not_admit_it(cut):
    dates = sessions("2026-03-02", 60)
    closes = [1000.0 + i for i in range(60)]
    for i in range(30, 30 + K - 1):
        closes[i] = 1800.0 + i
    px = bars("AAAA", dates, closes)
    out = audit(px[px.date.ge(dates[cut])], registry(("AAAA",), dates[0], dates[-1])).set_index("date")
    run = [d for d in dates[30:30 + K - 1] if d >= dates[cut]]
    assert not out.loc[run, "close_anchor_admissible"].any(), "no positional first-row trust"
    assert not out.loc[run, "price_step_admissible"].any()
    assert not out.loc[run, "entry_open_admissible"].any()


def test_w4_explicit_quarantine_mask_survives_slicing():
    dates = sessions("2026-03-02", 60)
    closes = [1000.0 + i for i in range(60)]
    for i in range(30, 30 + 2 * K):
        closes[i] = 1800.0 + i
    px = bars("AAAA", dates, closes)
    for cut in (31, 40, 45):
        sliced = px[px.date.ge(dates[cut])].reset_index(drop=True)
        mask = [not (dates[30] <= d < dates[30 + 2 * K]) for d in sliced.date]
        out = audit(sliced, registry(("AAAA",), dates[0], dates[-1]), trusted=mask).set_index("date")
        assert not out.loc[[d for d in dates[30:30 + 2 * K] if d >= dates[cut]], "close_anchor_admissible"].any()


def test_first_row_of_any_slice_is_never_positionally_trusted():
    dates = sessions("2026-03-02", 40)
    px = bars("AAAA", dates)
    for cut in range(0, 40, 7):
        out = audit(px[px.date.ge(dates[cut])], registry(("AAAA",), dates[0], dates[-1]))
        first = out.iloc[0]
        assert not first.close_anchor_admissible
        assert first.anchor_trust_status in {"RESTART_PENDING", "INADMISSIBLE"}


# ── W6: a missing exchange session is never an immediate predecessor ──

def test_w6_missing_session_is_not_an_immediate_predecessor():
    dates = sessions("2026-03-02", 50)
    gap = dates[30]
    px = bars("AAAA", [d for d in dates if d != gap])
    out = audit(px, registry(("AAAA",), dates[0], dates[-1])).set_index("date")
    after = out.loc[dates[31]]
    assert after.limit_reference_status == "UNRESOLVED"
    assert after.limit_unresolved_reason == "MISSING_IMMEDIATE_PREDECESSOR"
    assert not after.close_anchor_admissible and not after.price_step_admissible
    assert after.consistency_status == "UNAVAILABLE"
    assert not out.loc[dates[31:31 + K - 1], "close_anchor_admissible"].any()
    assert out.loc[dates[31 + K - 1], "close_anchor_admissible"]
    labeled = pa.add_forward_returns(px, sorted(px.date), (1, 2), open_anchored=True,
                                     registry=registry(("AAAA",), dates[0], dates[-1]),
                                     representation=pc.RAW_ACTUAL).set_index("date")
    assert pd.isna(labeled.loc[dates[29], "fwd_1"]) and pd.isna(labeled.loc[dates[29], "fwd_2"])
    assert pd.isna(labeled.loc[dates[29], "fwd_oo_1"]) and pd.isna(labeled.loc[dates[28], "fwd_2"])


def test_w6_writer_stores_post_gap_bar_without_bridging():
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    dates = sessions("2026-03-02", 25)
    with price_db() as conn:
        conn.executemany("INSERT INTO price_history VALUES (?,?,?,?,?,?,?)",
                         [(d, "BBBB", 100, 100, 100, 100, 1000) for d in dates[:20]])
        before = conn.total_changes
        # +80% versus the last stored close, but the immediate session is missing.
        body = price_payload([price_bar(dates[21], 180)], "BBBB")
        bf.insert_inventory(conn, "BBBB", body)
        assert conn.total_changes > before
        out = pa.detect(pa.load(conn)).set_index("date")
        assert out.loc[dates[21], "limit_unresolved_reason"] == "MISSING_IMMEDIATE_PREDECESSOR"
        assert not out.loc[dates[21], "limit_violation"]
        assert not out.loc[dates[21], "close_anchor_admissible"]


# ── W7: identical adjudication for any frame holding the dependency window ──

def assert_slice_invariant(full, sliced):
    full = full.set_index(["ticker", "date"])
    part = sliced.set_index(["ticker", "date"])
    group = sliced.assign(_supported=sliced.date.ge("2026-01-01"))
    rank = group.groupby(["ticker", "price_segment_id", "_supported"]).cumcount()
    pinned = sliced.assign(_rank=rank.values).set_index(["ticker", "date"])["_rank"].ge(DEPENDENCY)
    keys = pinned[pinned].index
    assert len(keys)
    left = full.loc[keys, TRUST_COLUMNS + ["anchor_trust_status"]]
    right = part.loc[keys, TRUST_COLUMNS + ["anchor_trust_status"]]
    pd.testing.assert_frame_equal(left, right, check_dtype=False)
    unpinned = pinned[~pinned].index
    widened = part.loc[unpinned, "close_anchor_admissible"].astype(bool) & \
        ~full.loc[unpinned, "close_anchor_admissible"].astype(bool)
    assert not widened.any(), "a truncated frame can only fail closed"
    return len(keys)


def test_w7_truncated_series_break_context_cannot_widen_trust():
    # Ten self-consistent bars at 6x the ticker's own level. With history they
    # are series breaks; a frame starting on them lacks that backward median.
    dates = sessions("2026-03-02", 70)
    closes = [1000.0 + i for i in range(70)]
    for i in range(30, 30 + K):
        closes[i] = 6000.0 + i
    px = bars("AAAA", dates, closes)
    reg = registry(("AAAA",), dates[0], dates[-1])
    full = audit(px, reg)
    assert full.set_index("date").loc[dates[31:30 + K], "series_break"].all()
    for cut in (30, 31, 33):
        sliced = audit(px[px.date.ge(dates[cut])], reg)
        assert_slice_invariant(full, sliced)
        assert not sliced.set_index("date").loc[dates[cut:30 + K], "close_anchor_admissible"].any()


def test_w7_alternating_discontinuities_keep_a_bounded_dependency():
    # Every step of a long sawtooth is out of band. A chained admissibility
    # rule would make each verdict depend on the parity of the frame start.
    dates = sessions("2026-03-02", 90)
    closes = [1000.0 + i for i in range(90)]
    for i in range(30, 75):
        closes[i] = 1000.0 if i % 2 == 0 else 1600.0
    px = bars("AAAA", dates, closes)
    reg = registry(("AAAA",), dates[0], dates[-1])
    full = audit(px, reg)
    assert full.set_index("date").loc[dates[31], "limit_violation"]
    assert not full.set_index("date").loc[dates[31:75], "close_anchor_admissible"].any()
    for cut in (31, 32, 35, 36):
        assert_slice_invariant(full, audit(px[px.date.ge(dates[cut])], reg))


def test_w7_synthetic_results_are_invariant_to_slice_start():
    dates, px = contaminated_pair(length=4)
    gap = px.date.eq(dates[50]) & px.ticker.eq("BBBB")
    px = px[~gap].reset_index(drop=True)
    reg = registry(("AAAA", "BBBB"), dates[0], dates[-1])
    full = audit(px, reg)
    for cut in (1, 5, 17, 33, 41):
        assert_slice_invariant(full, audit(px[px.date.ge(dates[cut])], reg))


# ── Historical fixture witnesses ──

def fixture_path():
    for candidate in (os.environ.get("CA_HISTORICAL_FIXTURE"), Path(__file__).with_name("neobdm.db")):
        if candidate and Path(candidate).is_file():
            if sha256(Path(candidate).read_bytes()).hexdigest() == FIXTURE_SHA256:
                return Path(candidate)
    pytest.skip("UNAVAILABLE: reviewed historical fixture (sha256 6fc475e6...) is not present")


def fixture_connect(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


@pytest.fixture(scope="module")
def fixture_frames():
    path = fixture_path()
    with fixture_connect(path) as conn:
        px = pa.load(conn)
        quarantine = set(conn.execute("SELECT date, ticker FROM price_quarantine"))
    return path, px, quarantine


def fixture_audit(px, quarantine):
    px = px.reset_index(drop=True)
    mask = [(d, t) not in quarantine for d, t in zip(px.date, px.ticker)]
    return pa.detect(px, trusted=mask)


def test_w7_fixture_results_are_invariant_to_slice_start(fixture_frames):
    _, px, quarantine = fixture_frames
    full = fixture_audit(px, quarantine)
    from_2026 = fixture_audit(px[px.date.ge("2026-01-01")], quarantine)
    narrower = fixture_audit(px[px.date.ge("2026-06-01")], quarantine)
    assert assert_slice_invariant(full, from_2026) > 6000
    assert assert_slice_invariant(full, narrower) > 2000
    y26 = full[full.date.ge("2026-01-01")]
    # Exactly the same 2026 decisions with or without unsupported 2025 history.
    pd.testing.assert_frame_equal(
        y26.set_index(["ticker", "date"])[TRUST_COLUMNS + ["anchor_trust_status"]],
        from_2026.set_index(["ticker", "date"])[TRUST_COLUMNS + ["anchor_trust_status"]],
        check_dtype=False)


def test_w1_fixture_tickers_recover_after_unsupported_2025_history(fixture_frames):
    _, px, quarantine = fixture_frames
    full = fixture_audit(px, quarantine)
    y26 = full[full.date.ge("2026-01-01")]
    recovered = y26.groupby("ticker").close_anchor_admissible.any()
    assert recovered.sum() >= 40, recovered[~recovered]
    assert y26.close_anchor_admissible.sum() > 5000
    assert full.loc[full.date.lt("2026-01-01"), "close_anchor_admissible"].sum() == 0


def plus80_copy(source, tmp_path):
    target = tmp_path / "neobdm.db"
    shutil.copyfile(source, target)
    os.chmod(target, 0o644)
    with sqlite3.connect(target) as conn:
        ticker = "BBHI"
        prev = conn.execute("SELECT close FROM price_history WHERE ticker=? AND date='2026-10-02'",
                            (ticker,)).fetchone()[0]
        bad = round(prev * 1.8, 2)
        conn.execute("UPDATE price_history SET open=?, high=?, low=?, close=? "
                     "WHERE ticker=? AND date='2026-10-05'", (bad, bad, bad, bad, ticker))
    return target, ticker


def test_plus80_after_recovery_is_caught_by_the_audit(fixture_frames, tmp_path):
    path, _, _ = fixture_frames
    target, ticker = plus80_copy(path, tmp_path)
    with sqlite3.connect(target) as conn:
        audited, _, _ = pa.adjudicate_quarantine(conn)
    row = audited[audited.ticker.eq(ticker) & audited.date.eq("2026-10-05")].iloc[0]
    assert row.limit_violation and row.suspect
    assert row.limit_reference_status == "RESOLVED", "the predecessor recovered before the injection"
    assert row.limit_change == pytest.approx(0.8, abs=0.01)


def test_plus80_after_recovery_is_caught_by_the_monitor(fixture_frames, tmp_path, monkeypatch):
    import check_signal_integrity as integrity

    class ReviewDay(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 6)

    monkeypatch.setattr(integrity, "date", ReviewDay)
    path, _, _ = fixture_frames
    target, ticker = plus80_copy(path, tmp_path)
    with sqlite3.connect(target) as conn:
        problems, notes, stats = [], [], {}
        integrity.check_new_contamination(conn, problems, notes, stats)
    assert stats["fresh_suspects"] >= 1
    assert any("NEW contaminated" in p and f"2026-10-05 {ticker}" in p for p in problems), problems
    with fixture_connect(path) as conn:
        problems, notes, stats = [], [], {}
        integrity.check_new_contamination(conn, problems, notes, stats)
    assert not any(f"2026-10-05 {ticker}" in p for p in problems)
