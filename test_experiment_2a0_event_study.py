import io

import numpy as np
import pandas as pd
import pytest

import experiment_2a0_event_study as study


def tiny_prices():
    dates = pd.date_range("2026-01-02", periods=30, freq="B")
    px = study.PriceSeries(
        dates=dates.to_numpy(dtype="datetime64[ns]"),
        closes=np.arange(100.0, 130.0),
        index_by_date={d: i for i, d in enumerate(dates)},
    )
    ihsg = pd.Series(np.arange(200.0, 230.0), index=dates)
    return dates, {"AAA": px}, ihsg


def test_event_return_starts_at_first_session_strictly_after_date():
    dates, prices, ihsg = tiny_prices()
    result = study.event_return("AAA", dates[0], 1, prices, ihsg)
    assert result["t0"] == dates[1]
    assert result["end_date"] == dates[2]
    assert result["raw"] == pytest.approx(102 / 101 - 1)


def test_return_coverage_is_horizon_specific():
    dates, prices, ihsg = tiny_prices()
    event_date = dates[-3]
    assert study.event_return("AAA", event_date, 1, prices, ihsg) is not None
    assert study.event_return("AAA", event_date, 5, prices, ihsg) is None


def test_observability_fails_closed_for_capture_only_backfill():
    rows = pd.DataFrame({
        "ticker": ["AAA"], "change_date": ["2025-01-02"],
        "published_at": [None], "available_at": ["2026-01-02T00:00:00Z"],
        "captured_at": ["2026-01-02T00:00:00Z"],
    })
    with pytest.raises(study.ObservabilityGateError):
        study.establish_observability(rows)


def test_complete_evidence_manifest_passes_observability():
    rows = pd.DataFrame({
        "ticker": ["AAA"], "change_date": ["2025-01-02"],
        "published_at": [None], "available_at": ["2026-01-02T00:00:00Z"],
        "captured_at": ["2026-01-02T00:00:00Z"],
    })
    manifest = io.StringIO()
    pd.DataFrame({"ticker": ["AAA"], "change_date": ["2025-01-02"],
                  "observable_date": ["2025-01-07"],
                  "evidence": ["independently timestamped publication"]}).to_csv(
                      manifest, index=False)
    manifest.seek(0)
    result = study.establish_observability(rows, manifest)
    assert result.loc[0, "observable_date"] == pd.Timestamp("2025-01-07")


def test_observable_date_cannot_precede_change_date():
    rows = pd.DataFrame({
        "ticker": ["AAA"], "change_date": ["2025-01-02"],
        "published_at": [None], "available_at": ["2026-01-02T00:00:00Z"],
        "captured_at": ["2026-01-02T00:00:00Z"],
    })
    manifest = io.StringIO(
        "ticker,change_date,observable_date,evidence\n"
        "AAA,2025-01-02,2025-01-01,publication\n"
    )
    with pytest.raises(study.ObservabilityGateError):
        study.establish_observability(rows, manifest)


def test_placebo_eligibility_is_horizon_specific_and_month_matched():
    dates, prices, ihsg = tiny_prices()
    real_date = pd.Timestamp("2026-01-15")
    one_day, _ = study._eligible_pseudo_dates(
        "AAA", real_date, 1, "raw", prices, ihsg, dates, excluded=set())
    twenty_day, _ = study._eligible_pseudo_dates(
        "AAA", real_date, 20, "raw", prices, ihsg, dates, excluded=set())
    assert one_day
    assert all(d.year == 2026 and d.month == 1 for d in one_day)
    assert len(one_day) > len(twenty_day)


def test_holm_adjustment_is_monotone_in_rank():
    adjusted = study.holm_adjust([0.01, 0.04, 0.03])
    assert adjusted == pytest.approx([0.03, 0.06, 0.06])


def test_full_pipeline_on_synthetic_observable_events(monkeypatch):
    dates = pd.date_range("2026-01-02", periods=70, freq="B")
    frame_parts = []
    price_map = {}
    for ticker, offset in (("AAA", 0.0), ("BBB", 20.0)):
        closes = np.arange(100.0 + offset, 170.0 + offset)
        frame_parts.append(pd.DataFrame({"date": dates, "ticker": ticker,
                                         "close": closes}))
        price_map[ticker] = study.PriceSeries(
            dates=dates.to_numpy(dtype="datetime64[ns]"), closes=closes,
            index_by_date={d: i for i, d in enumerate(dates)},
        )
    price_frame = pd.concat(frame_parts, ignore_index=True)
    ihsg = pd.Series(np.arange(200.0, 270.0), index=dates)
    events = pd.DataFrame({
        "ticker": ["AAA", "BBB"],
        "change_date": ["2026-01-05", "2026-01-06"],
        "observable_date": pd.to_datetime(["2026-01-07", "2026-01-08"]),
    })
    monkeypatch.setattr(study, "N_RESAMPLES", 5)
    monkeypatch.setattr(study, "load_prices", lambda _: (price_frame, price_map))
    monkeypatch.setattr(study, "load_ihsg", lambda _: ihsg)
    result = study.run_study(events, None, None)
    assert result["samples"]["full"]["raw_20d"]["n_events"] == 2
    assert "holm_p_eight_tests" in result["samples"]["full"]["raw_1d"]
    assert set(result["leave_one_ticker_out"]["raw_1d"]["estimates"]) == {"AAA", "BBB"}
