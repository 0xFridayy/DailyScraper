"""
Lightweight regression tests for walk_forward_backtest.py and kelly_sizing.py.

No test framework dependency (assert-based, run directly) — this repo has
none yet and these checks are simple enough not to need one. Focus is
specifically on leakage and formula correctness, since a silent bug here
(e.g. a "lagged" feature that actually sees today's data) would quietly
invalidate every backtest number in SYSTEM.md's log without ever raising an
exception. Run after touching build_panel(), sharpe_stats(), or
kelly_fraction() — before trusting a new backtest run's results.
"""

import pandas as pd
import numpy as np

from walk_forward_backtest import (
    _broker_day_aggregates, _broker_correlation_1d, _price_features_and_target, sharpe_stats,
)
from kelly_sizing import kelly_fraction, kelly_from_trades


def test_broker_day_aggregates_basic():
    bf = pd.DataFrame([
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "X", "netval": 10.0},
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "Y", "netval": -5.0},
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "Z", "netval": 2.0},
    ])
    agg = _broker_day_aggregates(bf)
    row = agg.iloc[0]
    assert row["n_brokers"] == 3
    assert abs(row["net_flow_total"] - 7.0) < 1e-9
    # top-3 of 3 brokers = all of them -> concentration should be exactly 1.0
    assert abs(row["broker_concentration"] - 1.0) < 1e-9
    # 2 of 3 brokers net positive
    assert abs(row["net_buy_ratio"] - (2 / 3)) < 1e-9
    print("test_broker_day_aggregates_basic passed")


def test_broker_correlation_first_day_is_nan():
    # a ticker's first day has no prior day to correlate against -> must be NaN,
    # never silently 0 or 1 (which would be a real, easy-to-miss leakage bug)
    bf = pd.DataFrame([
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "X", "netval": 10.0},
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "Y", "netval": -5.0},
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "Z", "netval": 3.0},
        {"date": "2026-01-02", "ticker": "AAA", "broker_code": "X", "netval": 8.0},
        {"date": "2026-01-02", "ticker": "AAA", "broker_code": "Y", "netval": -4.0},
        {"date": "2026-01-02", "ticker": "AAA", "broker_code": "Z", "netval": 1.0},
    ])
    corr = _broker_correlation_1d(bf)
    day1 = corr[corr["date"] == "2026-01-01"]
    day2 = corr[corr["date"] == "2026-01-02"]
    assert len(day1) == 0, "first day should produce no correlation row at all"
    assert len(day2) == 1
    # X,Y,Z moved 10->8, -5->-4, 3->1: same signs, strongly correlated -> should be near +1
    assert corr.iloc[0]["broker_correlation_1d"] > 0.9
    print("test_broker_correlation_first_day_is_nan passed")


def test_price_features_no_leakage():
    # construct a price series where today's close is a dead giveaway of a "cheat"
    # value, and confirm neither momentum_1d nor volume_ratio for day d can see it
    px = pd.DataFrame({
        "date": [f"2026-01-{d:02d}" for d in range(1, 11)],
        "ticker": ["AAA"] * 10,
        "close": [100, 101, 99, 105, 98, 110, 90, 120, 80, 130],
        "volume": [1000] * 10,
    })
    out = _price_features_and_target(px)

    # momentum_1d on day d must equal (close[d]-close[d-1])/close[d-1], using
    # ONLY past+current-close info (both known as of day d's close) — not
    # tomorrow's close, which is what `target` (not momentum_1d) should hold
    expected_mom = (101 - 100) / 100
    row = out[out["date"] == "2026-01-02"].iloc[0]
    assert abs(row["momentum_1d"] - expected_mom) < 1e-9

    # target on day d must be tomorrow's return, not today's
    expected_target = (99 - 101) / 101
    assert abs(row["target"] - expected_target) < 1e-9

    # volume_ratio must use a rolling mean that EXCLUDES today (shift(1) before
    # rolling) — with constant volume=1000 this is trivially 1.0 everywhere it's
    # defined, but the point is it must be defined starting only once 5 PRIOR
    # days exist, not 5 total days including today
    defined = out.dropna(subset=["volume_ratio"])
    assert defined["date"].min() == "2026-01-06", (
        "volume_ratio should first be defined on day 6, using days 1-5 as the "
        "trailing window (shift(1) before rolling(5) excludes day 6's own "
        "volume) — if this drifts to day 5 or earlier, today's own volume is "
        "leaking into its own trailing average"
    )
    print("test_price_features_no_leakage passed")


def test_sharpe_stats_formula():
    pred = pd.Series([0.01, 0.02, -0.01, 0.006, 0.001])
    actual = pd.Series([0.02, -0.01, 0.03, 0.01, -0.05])
    stats = sharpe_stats(pred, actual)
    # only pred > 0.005 trigger a position: indices 0,1,3 (0.01, 0.02, 0.006)
    assert stats["n_trades"] == 3
    traded_actual = actual.iloc[[0, 1, 3]]
    expected_hit_rate = (traded_actual > 0).mean()
    assert abs(stats["hit_rate"] - expected_hit_rate) < 1e-9
    print("test_sharpe_stats_formula passed")


def test_sharpe_stats_no_trades():
    pred = pd.Series([0.001, -0.01, 0.002])
    actual = pd.Series([0.01, 0.02, -0.01])
    stats = sharpe_stats(pred, actual)
    assert stats["n_trades"] == 0
    assert np.isnan(stats["sharpe"])
    assert np.isnan(stats["hit_rate"])
    print("test_sharpe_stats_no_trades passed")


def test_kelly_fraction_known_example():
    # textbook case: 60% win rate, 1:1 payoff -> full Kelly = 0.2
    f = kelly_fraction(win_rate=0.6, avg_win=1.0, avg_loss=1.0, fraction_cap=1.0)
    assert abs(f - 0.2) < 1e-9
    print("test_kelly_fraction_known_example passed")


def test_kelly_fraction_negative_edge_returns_zero():
    # losing edge (40% win rate, 1:1 payoff) must return exactly 0, not a
    # negative number — "bet negative" is meaningless for a long-only sizer
    f = kelly_fraction(win_rate=0.4, avg_win=1.0, avg_loss=1.0, fraction_cap=1.0)
    assert f == 0.0
    print("test_kelly_fraction_negative_edge_returns_zero passed")


def test_kelly_from_trades_matches_manual_calc():
    returns = [0.05, -0.02, 0.03, -0.01, 0.04, -0.02]
    f = kelly_from_trades(returns, fraction_cap=1.0)
    wins = [0.05, 0.03, 0.04]
    losses = [0.02, 0.01, 0.02]
    win_rate = 3 / 6
    payoff = (sum(wins) / 3) / (sum(losses) / 3)
    expected = win_rate - (1 - win_rate) / payoff
    assert abs(f - expected) < 1e-9
    print("test_kelly_from_trades_matches_manual_calc passed")


if __name__ == "__main__":
    test_broker_day_aggregates_basic()
    test_broker_correlation_first_day_is_nan()
    test_price_features_no_leakage()
    test_sharpe_stats_formula()
    test_sharpe_stats_no_trades()
    test_kelly_fraction_known_example()
    test_kelly_fraction_negative_edge_returns_zero()
    test_kelly_from_trades_matches_manual_calc()
    print("\nAll tests passed.")
