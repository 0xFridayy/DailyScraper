"""check_signal_integrity recomputes trust from raw observations (F16).

Every published label below comes from a forged or stale producer frame. The
monitor must reject it from the stored price_history and quarantine alone,
never from the producer's reason strings, admission flags or certificate.
"""

from datetime import date, timedelta
import sqlite3

import pandas as pd
import pytest

import check_signal_integrity as integrity
import price_audit as pa
import price_contract as pc
from price_contract_frame import _seal_price_frame
from test_price_contract import document

TICKER = "AAAA"


def sessions(start, count):
    out, day = [], date.fromisoformat(start)
    while len(out) < count:
        if pc.is_idx_session(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


DATES = sessions("2026-03-02", 45)
T = 30                                   # decision row, well after the restart window


def registry():
    doc = document()
    doc["registry_version"] = "synthetic-monitor-fixture.v1"
    doc["events"] = []
    doc["reviewed_coverage"] = [{"venue": "IDX", "market_scope": ["REGULAR"], "tickers": [TICKER, "BBBB"],
                                 "from": DATES[0], "through": DATES[-1],
                                 "evidence_refs": ["SYNTHETIC_MONITOR_FIXTURE_ONLY"]}]
    return pc.parse_registry(doc)


def bars(ticker=TICKER, base=1000.0):
    closes = [base + i for i in range(len(DATES))]
    return pd.DataFrame({"date": DATES, "ticker": ticker, "open": [c * 0.998 for c in closes],
                         "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                         "close": closes, "volume": [1000.0] * len(DATES)})


def database(px):
    conn = sqlite3.connect(":memory:")
    px.to_sql("price_history", conn, index=False)
    bars("BBBB", 3000.0).to_sql("price_history", conn, index=False, if_exists="append")
    conn.execute("CREATE TABLE price_quarantine(date,ticker,reasons)")
    return conn


def published(conn):
    return pa.clean_panel(conn, horizons=(1, 2), lags=(1, 2), extremes=True, open_anchored=True,
                          registry=registry(), representation=pc.RAW_ACTUAL)


def monitor(conn, frame, monkeypatch):
    monkeypatch.setattr(pa, "clean_panel", lambda *a, **k: frame)
    problems, notes, stats = [], [], {}
    integrity.check_price_contract(conn, problems, notes, stats, registry=registry(),
                                   representation=pc.RAW_ACTUAL)
    return problems


def forge(frame, column, value, day=None, **admission):
    """Write a label and matching admission flags, then reseal the certificate."""
    out = frame.copy()
    out.attrs = dict(frame.attrs)
    where = out.ticker.eq(TICKER) & out.date.eq(day or DATES[T])
    out.loc[where, column] = value
    if column + "_reason" in out:
        out.loc[where, column + "_reason"] = ""
    for name, flag in admission.items():
        out.loc[out.ticker.eq(TICKER) & out.date.eq(name.split("@")[1]), name.split("@")[0]] = flag
    return _seal_price_frame(out)


def test_valid_published_labels_are_not_flagged(monkeypatch):
    with database(bars()) as conn:
        frame = published(conn)
        row = frame.set_index(["ticker", "date"]).loc[(TICKER, DATES[T])]
        assert row.fwd_1 == pytest.approx(1031.0 / 1030.0 - 1) and row.max_2 == pytest.approx(1032 * 1.01 / 1030 - 1)
        assert row.lag_2 == pytest.approx(1030.0 / 1028.0 - 1) and row.gap_1 == pytest.approx(1031 * 0.998 / 1030 - 1)
        assert not monitor(conn, frame, monkeypatch)


def test_forward_and_lag_labels_bridging_a_missing_ticker_session_are_rejected(monkeypatch):
    px = bars()
    gap = DATES[T + 1]
    with database(px[px.date.ne(gap)].reset_index(drop=True)) as conn:
        frame = published(conn)                       # BBBB keeps the date on the session axis
        assert pd.isna(frame.set_index(["ticker", "date"]).loc[(TICKER, DATES[T]), "fwd_1"])
        forged = forge(frame, "fwd_1", 1032.0 / 1030.0 - 1)
        assert any("fwd_1" in p and "MISSING_TICKER_SESSION" in p for p in monitor(conn, forged, monkeypatch))
        forged = forge(frame, "lag_1", 1032.0 / 1030.0 - 1, day=DATES[T + 2])
        assert any("lag_1" in p for p in monitor(conn, forged, monkeypatch))


def test_label_ending_in_an_out_of_band_observation_is_rejected(monkeypatch):
    px = bars()
    px.loc[T + 1, ["open", "high", "low", "close"]] = [1850.0, 1860.0, 1840.0, 1855.0]
    with database(px) as conn:
        frame = published(conn)
        forged = forge(frame, "fwd_1", 1855.0 / 1030.0 - 1, **{f"price_step_admissible@{DATES[T + 1]}": True})
        assert any("fwd_1" in p and "INADMISSIBLE_PATH" in p for p in monitor(conn, forged, monkeypatch))


def test_label_from_a_quarantined_anchor_is_rejected(monkeypatch):
    with database(bars()) as conn:
        frame = published(conn)                       # computed before the quarantine record existed
        conn.execute("INSERT INTO price_quarantine VALUES(?, ?, 'cross_ticker_dup')", (DATES[T], TICKER))
        problems = monitor(conn, frame, monkeypatch)
        assert any("fwd_1" in p and "UNTRUSTED_ANCHOR" in p for p in problems)
        assert any("lag_1" in p for p in problems), "its successor's lag uses the same anchor"


@pytest.mark.parametrize("field,value", [("high", None), ("high", 1020.0), ("low", 500.0)])
def test_extrema_using_missing_or_untrusted_high_low_are_rejected(field, value, monkeypatch):
    px = bars()
    with database(px) as conn:
        frame = published(conn)
        conn.execute(f"UPDATE price_history SET {field}=? WHERE ticker=? AND date=?", (value, TICKER, DATES[T + 2]))
        problems = monitor(conn, frame, monkeypatch)
        assert any(p.startswith("max_2") or p.startswith("mdd_2") for p in problems), problems


def test_forged_current_metadata_over_invalid_underlying_data_is_rejected(monkeypatch):
    with database(bars()) as conn:
        frame = published(conn)                       # valid, current identity and certificates
        conn.execute("UPDATE price_history SET close=close*1.8, high=high*1.8, open=open*1.8, low=low*1.8 "
                     "WHERE ticker=? AND date=?", (TICKER, DATES[T + 1]))
        problems = monitor(conn, frame, monkeypatch)
        assert any("fwd_1" in p for p in problems)
        assert any("differs from stored price_history" in p for p in problems)


def test_forged_label_value_over_valid_data_is_rejected(monkeypatch):
    with database(bars()) as conn:
        forged = forge(published(conn), "fwd_1", 0.25)
        assert any("fwd_1" in p and "VALUE_MISMATCH" in p for p in monitor(conn, forged, monkeypatch))


def test_restart_anchor_label_needs_only_its_own_window(monkeypatch):
    # An old quarantine does not stop the monitor accepting a valid current label.
    with database(bars()) as conn:
        conn.execute("INSERT INTO price_quarantine VALUES(?, ?, 'limit_violation')", (DATES[5], TICKER))
        frame = published(conn)
        assert not pd.isna(frame.set_index(["ticker", "date"]).loc[(TICKER, DATES[T]), "fwd_1"])
        assert not monitor(conn, frame, monkeypatch)
