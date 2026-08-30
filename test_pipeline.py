"""
Lightweight regression tests for walk_forward_backtest.py and kelly_sizing.py.

No test framework dependency (assert-based, run directly) — this repo has
none yet and these checks are simple enough not to need one. Focus is
specifically on leakage and formula correctness, since a silent bug here
(e.g. a "lagged" feature that actually sees today's data) would quietly
invalidate every backtest number in SYSTEM.md's log without ever raising an
exception. Run after touching build_panel(), signal_quality(), or
kelly_fraction() — before trusting a new backtest run's results.
"""

import pandas as pd
import numpy as np

from walk_forward_backtest import (
    _broker_day_aggregates, _broker_correlation_1d, _price_features_and_target, signal_quality,
    FEATURES,
)
from signal_metrics import spearman_ic, signal_stats, trade_stats
from strategy_variants import get_walk_forward_predictions
from kelly_sizing import kelly_fraction, kelly_from_trades
from price_audit import (add_forward_returns, add_lagged_returns,
                         series_signature, ticker_from_title, should_fail_run,
                         detect, bagholders_from_payload, date_offset_holds)


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


def test_spearman_ic_direction():
    x = pd.Series([1.0, 2, 3, 4, 5])
    assert abs(spearman_ic(x, x * 3 + 1) - 1.0) < 1e-9, "monotonic up must be +1"
    assert abs(spearman_ic(x, -x) + 1.0) < 1e-9, "monotonic down must be -1"
    # rank-based, so one wild outlier must NOT dominate the way it would Pearson
    assert abs(spearman_ic(x, pd.Series([1.0, 2, 3, 4, 500])) - 1.0) < 1e-9
    assert np.isnan(spearman_ic(x, pd.Series([7.0] * 5))), "constant -> undefined, not 0"
    assert np.isnan(spearman_ic(pd.Series([1.0]), pd.Series([1.0]))), "too few points"
    print("test_spearman_ic_direction passed")


def test_signal_stats_detects_a_useless_signal():
    # THE case this repo missed for months: hit_rate 42.8% was reported as a
    # result while the universe base rate was also 42.8%. A hit rate without its
    # base rate is unreadable, so hit_edge must come out ~0 here even though the
    # raw hit rate looks like a number worth quoting.
    rng = np.random.default_rng(0)
    actual = pd.Series(rng.normal(0.004, 0.03, 4000))
    pred = pd.Series(rng.normal(0, 0.01, 4000))          # independent of actual
    s = signal_stats(pred, actual)
    assert abs(s["ic"]) < 0.05, f"independent series should have ~0 IC, got {s['ic']}"
    assert abs(s["hit_edge"]) < 0.06, f"no edge expected, got {s['hit_edge']}"
    assert abs(s["edge"]) < 0.01
    assert s["base_rate"] > 0.5, "sanity: this generator has a positive drift"
    print("test_signal_stats_detects_a_useless_signal passed")


def test_signal_stats_reports_a_negative_edge_as_negative():
    # A signal that is actively WRONG must not be flattered into looking flat.
    actual = pd.Series(np.linspace(-0.1, 0.1, 200))
    pred = pd.Series(np.linspace(0.1, -0.1, 200))        # perfectly inverted
    s = signal_stats(pred, actual)
    assert s["ic"] < -0.99
    assert s["edge"] < 0, "top decile of an inverted signal must underperform"
    assert s["hit_edge"] < 0
    print("test_signal_stats_reports_a_negative_edge_as_negative passed")


def test_trade_stats_has_no_annualisation():
    # mean/std with NO sqrt(anything). If someone reintroduces a scale factor
    # this pins it down numerically.
    r = [0.02, -0.01, 0.03, -0.02, 0.01]
    s = trade_stats(r)
    arr = np.array(r)
    assert s["n_trades"] == 5
    assert abs(s["mean_ret"] - arr.mean()) < 1e-12
    assert abs(s["ret_per_risk"] - arr.mean() / pd.Series(r).std()) < 1e-12
    assert abs(s["hit_rate"] - 0.6) < 1e-12
    print("test_trade_stats_has_no_annualisation passed")


def test_trade_stats_edges():
    assert trade_stats([])["n_trades"] == 0
    # zero variance must give NaN, never inf - an inf would rank top of any sort
    assert np.isnan(trade_stats([0.01, 0.01, 0.01])["ret_per_risk"])
    # base_rate is optional, but when given the edge must be computed
    s = trade_stats([0.01, -0.01, 0.01, 0.01], base_rate=0.5)
    assert abs(s["hit_edge"] - 0.25) < 1e-12
    assert np.isnan(trade_stats([0.01, -0.01])["hit_edge"]), "no base rate -> no edge claim"
    print("test_trade_stats_edges passed")


def test_signal_quality_scores_every_row_not_just_triggered():
    # The old sharpe_stats() computed its statistic over TRIGGERED rows only, so
    # every day the rule sat out vanished from the denominator. The signal half
    # must score all rows; the trade half only the triggered ones.
    pred = pd.Series([0.01, 0.02, -0.01, 0.006, 0.001])
    actual = pd.Series([0.02, -0.01, 0.03, 0.01, -0.05])
    q = signal_quality(pred, actual)
    assert q["n"] == 5, "signal half must see every row"
    assert q["n_trades"] == 3, "threshold >0.005 triggers indices 0,1,3"
    assert abs(q["base_rate"] - 0.6) < 1e-12, "3 of 5 actuals are positive"
    assert abs(q["trade_hit"] - (2 / 3)) < 1e-12
    assert "sharpe" not in q, "Sharpe must be gone, not renamed"
    print("test_signal_quality_scores_every_row_not_just_triggered passed")


def test_no_sqrt_252_anywhere():
    # The defect this whole change removes. AST-based so prose about it in
    # docstrings does not count as a reoccurrence.
    import ast as _ast, os as _os
    here = _os.path.dirname(_os.path.abspath(__file__))
    hits = []
    for fn in sorted(f for f in _os.listdir(here) if f.endswith(".py")):
        if fn == "check_ml_health.py":      # its own budget guard mentions it
            continue
        try:
            tree = _ast.parse(open(_os.path.join(here, fn), encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.Call) and len(node.args) == 1
                    and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "sqrt"
                    and isinstance(node.args[0], _ast.Constant) and node.args[0].value == 252):
                hits.append(f"{fn}:{node.lineno}")
    assert not hits, f"sqrt(252) is back at {hits}"
    print("test_no_sqrt_252_anywhere passed")


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


def test_forward_returns_never_bridge_a_removed_row():
    # The whole point of the gap guard. Quarantine removes d3, so d2's "next
    # row" is d4 - three days and a contamination-sized jump away. A plain
    # shift(-1) would report that as a real +354% next-day return and feed it
    # in as a training label.
    px = pd.DataFrame({
        "ticker": ["A"] * 4,
        "date": ["d1", "d2", "d4", "d5"],
        "close": [100.0, 110.0, 500.0, 505.0],
    })
    out = add_forward_returns(px, ["d1", "d2", "d3", "d4", "d5"], horizons=(1,))
    by_date = out.set_index("date")["fwd_1"]
    assert abs(by_date["d1"] - 0.10) < 1e-9, "contiguous row must still compute"
    assert np.isnan(by_date["d2"]), "d2->d4 spans the removed d3 and must be NaN"
    assert abs(by_date["d4"] - 0.01) < 1e-9, "contiguity resumes after the hole"
    assert np.isnan(by_date["d5"]), "last row has no next row"
    print("test_forward_returns_never_bridge_a_removed_row passed")


def test_lagged_returns_guarded_the_same_way():
    # Same hole, read backwards: d4's previous surviving row is d2, so a
    # momentum feature there would be fabricated too.
    px = pd.DataFrame({
        "ticker": ["A"] * 4,
        "date": ["d1", "d2", "d4", "d5"],
        "close": [100.0, 110.0, 500.0, 505.0],
    })
    out = add_lagged_returns(px, ["d1", "d2", "d3", "d4", "d5"], lags=(1,))
    by_date = out.set_index("date")["lag_1"]
    assert np.isnan(by_date["d1"]), "first row has no previous row"
    assert abs(by_date["d2"] - 0.10) < 1e-9
    assert np.isnan(by_date["d4"]), "d2->d4 spans the removed d3 and must be NaN"
    assert abs(by_date["d5"] - 0.01) < 1e-9
    print("test_lagged_returns_guarded_the_same_way passed")


def test_extreme_windows_share_the_contiguity_mask():
    # max_h / mdd_h roll over the same h rows the endpoint spans, so a window
    # that bridges a hole must be dropped, not just the endpoint return.
    px = pd.DataFrame({
        "ticker": ["A"] * 4,
        "date": ["d1", "d2", "d4", "d5"],
        "close": [100.0, 110.0, 500.0, 505.0],
        "high": [105.0, 115.0, 520.0, 515.0],
        "low": [95.0, 105.0, 480.0, 495.0],
    })
    out = add_forward_returns(px, ["d1", "d2", "d3", "d4", "d5"],
                              horizons=(1,), extremes=True)
    by_date = out.set_index("date")
    assert np.isnan(by_date.loc["d2", "max_1"]), "bridged window must be NaN"
    assert np.isnan(by_date.loc["d2", "mdd_1"]), "bridged window must be NaN"
    assert abs(by_date.loc["d1", "max_1"] - 0.15) < 1e-9   # high 115 vs close 100
    assert abs(by_date.loc["d1", "mdd_1"] - 0.05) < 1e-9   # low 105 vs close 100
    print("test_extreme_windows_share_the_contiguity_mask passed")



def test_series_signature_distinguishes_two_stocks():
    # The scraper's last-line-of-defence guard. Two different stocks must never
    # produce the same signature; the same series must always produce the same
    # one, or the stale-chart check either never fires or fires constantly.
    cdia = {"x": ["2026-01-01", "2026-01-02"], "close": [700.0, 715.0]}
    coin = {"x": ["2026-01-01", "2026-01-02"], "close": [2560.0, 2600.0]}
    assert series_signature(cdia) == series_signature(dict(cdia))
    assert series_signature(cdia) != series_signature(coin)
    # A stale chart hands back the PREVIOUS ticker's series verbatim - which is
    # exactly the equality the guard trips on.
    assert series_signature(cdia) == series_signature({"x": cdia["x"], "close": cdia["close"]})
    print("test_series_signature_distinguishes_two_stocks passed")


def test_series_signature_none_when_empty():
    # No series means "nothing to compare", not "identical to the last one" -
    # returning a truthy constant here would abort every empty scrape as a
    # duplicate.
    assert series_signature(None) is None
    assert series_signature({}) is None
    assert series_signature({"x": [], "close": []}) is None
    print("test_series_signature_none_when_empty passed")


def test_ticker_from_title_only_asserts_when_it_can():
    # Present and authoritative.
    assert ticker_from_title("CDIA - Chandra Daya Investasi") == "CDIA"
    assert ticker_from_title("Inventory chart: COIN") == "COIN"
    # Absent must be None, NOT a guess: the title format is not guaranteed, and
    # treating "no code found" as a mismatch would abort every valid scrape.
    assert ticker_from_title("") is None
    assert ticker_from_title(None) is None
    assert ticker_from_title("Broker inventory") is None
    # Lowercase and wrong-length words are not codes.
    assert ticker_from_title("cdia daily chart") is None
    assert ticker_from_title("PT ABC") is None
    print("test_ticker_from_title_only_asserts_when_it_can passed")



def _price_frame(rows):
    """Minimal price_history-shaped frame for the detectors."""
    df = pd.DataFrame(rows, columns=["date", "ticker", "open", "high", "low",
                                     "close", "volume"])
    df.insert(0, "rid", range(1, len(df) + 1))
    return df.sort_values(["ticker", "date"]).reset_index(drop=True)


def _steady(ticker, start, days, price):
    # A flat, unremarkable series — no detector should ever flag these.
    return [(f"2026-08-{start + i:02d}", ticker, price, price, price, price, 1000)
            for i in range(days)]


def test_commit_gate_ignores_legitimate_volatility():
    # The gate the topup workflow runs counts cross_ticker_dup ONLY. A correct
    # scrape adds real IDX volatility — a +30% ARA day, a corporate action — that
    # trips limit_violation (and, on a big enough jump, series_break). Those must
    # NOT count against the gate, or every honest nightly scrape reddens it and
    # price_history stops advancing. This is the bug that froze it at 2026-08-21.
    base = _price_frame(_steady("AAA", 1, 6, 1000))
    d0 = detect(base)

    # Same series plus one legitimate +30% jump on a fresh day (prev close 1000 ->
    # ara bound 25%, so this is outside the band and trips limit_violation).
    after = _price_frame(_steady("AAA", 1, 6, 1000) + [("2026-08-07", "AAA",
                         1300, 1300, 1300, 1300, 1000)])
    d1 = detect(after)

    assert int(d1["limit_violation"].sum()) > int(d0["limit_violation"].sum()), \
        "a +30% day should register as a limit_violation"
    # ...but the metric the gate actually reads did not move.
    assert int(d1["cross_ticker_dup"].sum()) == int(d0["cross_ticker_dup"].sum()) == 0, \
        "legitimate volatility must not raise the cross-ticker-dup gate"
    print("test_commit_gate_ignores_legitimate_volatility passed")


def test_commit_gate_catches_a_recontaminated_scrape():
    # The regression the gate exists for: the scraper writes one ticker's OHLCV
    # under another's name. Two different real stocks cannot share byte-identical
    # open/high/low/close/volume, so this is exactly what cross_ticker_dup means,
    # and the gate must see it rise.
    clean = _price_frame(_steady("AAA", 1, 4, 1000) + _steady("BBB", 1, 4, 500))
    d0 = detect(clean)
    assert int(d0["cross_ticker_dup"].sum()) == 0

    # BBB's 08-04 row now carries AAA's exact OHLCV — the contamination signature.
    contaminated = _price_frame(
        _steady("AAA", 1, 4, 1000)
        + _steady("BBB", 1, 3, 500)
        + [("2026-08-04", "BBB", 1000, 1000, 1000, 1000, 1000)])
    d1 = detect(contaminated)
    assert int(d1["cross_ticker_dup"].sum()) > 0, \
        "identical OHLCV under two tickers on one date must raise cross_ticker_dup"
    print("test_commit_gate_catches_a_recontaminated_scrape passed")


def _inventory_payload(nlot, nval):
    # Shape confirmed against a real /api/inventory response (HANDOFF Appendix N).
    return {"success": True, "data": {"nlot": nlot, "nval": nval},
            "meta": {"symbol": "SINI"}}



def test_predictions_carry_the_columns_the_scorer_reads():
    # run_ml_reports.py died with KeyError: 'target' every night for a week
    # (2026-08-23 onward). score_all() started computing the base rate from
    # preds["target"], but get_walk_forward_predictions() returned only
    # ticker/date/pred. Nothing caught it: check_ml_health imports the module
    # and runs these tests, neither of which executed the report path.
    rng = np.random.default_rng(0)
    dates = [f"2026-{m:02d}-{d:02d}" for m in (1, 2) for d in range(1, 21)][:40]
    rows = []
    for d in dates:
        for t in ("AAA", "BBB", "CCC"):
            row = {"date": d, "ticker": t}
            for f in FEATURES:
                row[f] = float(rng.normal())
            row["target"] = float(rng.normal(0, 0.02))
            rows.append(row)
    panel = pd.DataFrame(rows)

    preds = get_walk_forward_predictions(panel)
    assert len(preds), "the synthetic panel should span enough dates to produce folds"
    for col in ("ticker", "date", "pred", "target"):
        assert col in preds.columns, f"score_all() reads {col!r}; it must survive"
    # The exact expression that used to raise.
    base_rate = float((preds["target"] > 0).mean())
    assert 0.0 <= base_rate <= 1.0
    print("test_predictions_carry_the_columns_the_scorer_reads passed")


def test_date_offset_only_holds_before_the_open():
    # The invariant every date join rests on (Appendix E) is "scrape date - 1 ==
    # data date", and it is true ONLY because the scheduled run beats the open.
    # The screener serves the last COMPLETED session, so after the open it hands
    # back today's close instead.
    from datetime import datetime as _dt
    sched = _dt(2026, 8, 27, 7, 0)            # 07:00, the cron's own slot
    assert date_offset_holds(sched)
    assert date_offset_holds(_dt(2026, 8, 27, 0, 30))
    assert date_offset_holds(_dt(2026, 8, 27, 9, 59))

    # 22:09 local == the 21:09 WIB manual dispatch that overwrote 2026-08-27's
    # correctly-offset rows with same-day closes and dropped cross-source
    # agreement from 100% to 85%.
    assert not date_offset_holds(_dt(2026, 8, 27, 22, 9))
    # IDX opens 09:00 WIB (UTC+7) == 10:00 in the UTC+8 scrape timezone.
    assert not date_offset_holds(_dt(2026, 8, 27, 10, 0)), "the open itself is already unsafe"
    assert not date_offset_holds(_dt(2026, 8, 27, 16, 30))
    print("test_date_offset_only_holds_before_the_open passed")


def test_quarantined_row_is_not_a_baseline_for_the_next_row():
    # The 2026-08-27 false alarm: MDIA's 08-13 close was KIOS's price (95), was
    # flagged and quarantined weeks earlier, and its real price is ~250. The next
    # clean row then read as +171% and got reported as the scraper "writing bad
    # rows again". A known-bad close must never be the baseline a good row is
    # judged against.
    rows = [(d, "AAA", 250, 250, 250, 250, 1000)
            for d in ("2026-08-01", "2026-08-02", "2026-08-03")]
    rows.append(("2026-08-04", "AAA", 95, 95, 95, 95, 1000))    # contaminated
    rows.append(("2026-08-05", "AAA", 252, 252, 252, 252, 1000))  # clean
    px = _price_frame(rows)

    naive = detect(px)
    bad = naive[naive["date"] == "2026-08-05"].iloc[0]
    assert bool(bad["limit_violation"]), "95 -> 252 must look like a violation untrusted"

    # Same data, with the contaminated row marked untrusted.
    trusted = [d != "2026-08-04" for d in px["date"]]
    guarded = detect(px, trusted=trusted)
    row = guarded[guarded["date"] == "2026-08-05"].iloc[0]
    assert not bool(row["limit_violation"]), \
        "a clean row after a quarantined one must not be flagged"
    # The baseline is DROPPED, not bridged to the last good close: a multi-day
    # move cannot be judged against a one-day ARA/ARB band either.
    assert pd.isna(row["prev_close"])
    print("test_quarantined_row_is_not_a_baseline_for_the_next_row passed")


def test_trusted_mask_leaves_real_contamination_detectable():
    # The mask must only relax the limit_violation BASELINE. cross_ticker_dup is
    # what actually proves the scraper regressed, and it must survive untouched —
    # otherwise this fix would blind the check it is meant to keep credible.
    rows = [("2026-08-01", "AAA", 100, 100, 100, 100, 500),
            ("2026-08-02", "AAA", 100, 100, 100, 100, 500),
            ("2026-08-01", "BBB", 100, 100, 100, 100, 500),
            ("2026-08-02", "BBB", 100, 100, 100, 100, 500)]
    px = _price_frame(rows)
    trusted = [False] * len(px)           # even with everything distrusted
    guarded = detect(px, trusted=trusted)
    assert int(guarded["cross_ticker_dup"].sum()) == 4, \
        "identical OHLCV across tickers must still be caught"
    assert bool(guarded["suspect"].any())
    # And the default path is unchanged for every existing caller.
    assert int(detect(px)["cross_ticker_dup"].sum()) == 4
    print("test_trusted_mask_leaves_real_contamination_detectable passed")


def test_bagholders_sum_per_day_lots_not_last_value():
    # nlot is PER-DAY net lot, not a cumulative series, so the position is its
    # sum. Reading the last element (what a cumulative series would need) would
    # report 10 lot for AK here instead of 300.
    payload = _inventory_payload(
        nlot={"AK": [100, 200, 10], "BK": [50, 25, 25]},
        nval={"AK": [1e6, 2e6, 1e5], "BK": [5e5, 2.5e5, 2.5e5]},
    )
    holders = bagholders_from_payload(payload, n=2)
    assert [h["code"] for h in holders] == ["AK", "BK"], "must rank by cumulative net lot"
    assert holders[0]["cum"] == 310
    assert holders[1]["cum"] == 100
    # avg = sum(nval) / (sum(nlot) * 100 shares) = 3.1e6 / 31000
    assert abs(holders[0]["avg"] - 100.0) < 1e-9
    print("test_bagholders_sum_per_day_lots_not_last_value passed")


def test_bagholders_exclude_net_sellers():
    # The term means ACCUMULATOR. The retired implementation sorted by cumulative
    # net and took the top n unconditionally, so on a ticker everyone was dumping
    # it would print a net seller as the "bag holder".
    payload = _inventory_payload(
        nlot={"AK": [-500, -200], "BK": [-10, -5], "CC": [40, 60]},
        nval={"AK": [-5e6, -2e6], "BK": [-1e5, -5e4], "CC": [4e5, 6e5]},
    )
    holders = bagholders_from_payload(payload, n=2)
    assert [h["code"] for h in holders] == ["CC"], "only net accumulators qualify"
    # Everyone selling must yield an EMPTY list, not a least-bad seller.
    all_selling = _inventory_payload(nlot={"AK": [-5], "BK": [-1]},
                                     nval={"AK": [-5e4], "BK": [-1e4]})
    assert bagholders_from_payload(all_selling, n=2) == []
    print("test_bagholders_exclude_net_sellers passed")


def test_bagholders_survive_a_malformed_payload():
    # A retired endpoint / changed schema must degrade to "no holders", never
    # raise into the daily signal. None entries appear in real series.
    assert bagholders_from_payload(None) == []
    assert bagholders_from_payload({}) == []
    assert bagholders_from_payload({"success": True, "data": {}}) == []
    holed = _inventory_payload(nlot={"AK": [100, None, 50]}, nval={"AK": [1e6, None, 5e5]})
    assert bagholders_from_payload(holed)[0]["cum"] == 150
    # A broker present in nlot but absent from nval must not divide by nothing.
    no_val = _inventory_payload(nlot={"AK": [100]}, nval={})
    assert no_val and bagholders_from_payload(no_val)[0]["avg"] == 0
    print("test_bagholders_survive_a_malformed_payload passed")


def test_should_fail_run_tolerates_a_few_failures():
    # A handful of tickers legitimately fail (suspended, no chart). Reddening the
    # workflow for those would train everyone to ignore it.
    assert should_fail_run(0, 45) is False
    assert should_fail_run(5, 45) is False       # 11%
    assert should_fail_run(13, 45) is False      # 29%, just under the 30% limit
    print("test_should_fail_run_tolerates_a_few_failures passed")


def test_should_fail_run_catches_a_broken_scrape():
    # The case this exists for: every ticker failed, price_history is unchanged,
    # the contamination gate sees no growth and passes, and nothing is committed.
    # Without this the run would go GREEN having done nothing at all.
    assert should_fail_run(45, 45) is True
    assert should_fail_run(14, 45) is True       # 31%, just over
    assert should_fail_run(0, 0) is True         # nothing attempted is a failure
    print("test_should_fail_run_catches_a_broken_scrape passed")


if __name__ == "__main__":
    test_commit_gate_ignores_legitimate_volatility()
    test_commit_gate_catches_a_recontaminated_scrape()
    test_predictions_carry_the_columns_the_scorer_reads()
    test_date_offset_only_holds_before_the_open()
    test_quarantined_row_is_not_a_baseline_for_the_next_row()
    test_trusted_mask_leaves_real_contamination_detectable()
    test_bagholders_sum_per_day_lots_not_last_value()
    test_bagholders_exclude_net_sellers()
    test_bagholders_survive_a_malformed_payload()
    test_should_fail_run_tolerates_a_few_failures()
    test_should_fail_run_catches_a_broken_scrape()
    test_series_signature_distinguishes_two_stocks()
    test_series_signature_none_when_empty()
    test_ticker_from_title_only_asserts_when_it_can()
    test_forward_returns_never_bridge_a_removed_row()
    test_lagged_returns_guarded_the_same_way()
    test_extreme_windows_share_the_contiguity_mask()
    test_broker_day_aggregates_basic()
    test_broker_correlation_first_day_is_nan()
    test_price_features_no_leakage()
    test_spearman_ic_direction()
    test_signal_stats_detects_a_useless_signal()
    test_signal_stats_reports_a_negative_edge_as_negative()
    test_trade_stats_has_no_annualisation()
    test_trade_stats_edges()
    test_signal_quality_scores_every_row_not_just_triggered()
    test_no_sqrt_252_anywhere()
    test_kelly_fraction_known_example()
    test_kelly_fraction_negative_edge_returns_zero()
    test_kelly_from_trades_matches_manual_calc()
    print("\nAll tests passed.")
