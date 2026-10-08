"""Plain-script tests for broker_learning.py, broker_learning_db.py and
signal_metrics.date_balanced_hit_edge (run directly: py -3 test_broker_learning.py).
Covers the 2026-09-25 amendment too: h = 60, big-move rates, per-rule primary
horizons, the alpha case library and broker lift; and Amendment A2: hold_60 /
susp_60, the circular bootstrap, LOW_N = max(30, 3h), visible alpha cases.

Synthetic frames only, shaped like the broker_rules / broker_book contract
columns; neither of those modules is imported, so these tests pin the metric
layer on its own. No network, no scraper import, no committed database.
"""

import ast
import math
import os
import sqlite3
import sys
import tempfile

import numpy as np
import pandas as pd

import broker_learning as bl
import broker_learning_db as db
import price_audit
import signal_metrics
from price_contract import CONTRACT_VERSION, UnsupportedPriceContract

HERE = os.path.dirname(os.path.abspath(__file__))
H = bl.HORIZONS
# Stand-in ruleset: one bullish and one bearish rule, so direction handling is
# exercised without depending on broker_rules. DN's primary horizon is 60 (as
# R6's is) so the per-rule horizon is exercised too.
RULES_T = [{"id": "UP", "dir": +1}, {"id": "DN", "dir": -1}]
PH_T = {"UP": 10, "DN": 60}


# ── helpers ────────────────────────────────────────────────────────────────

def _assert_refusal(route, function, *args, **kwargs):
    frames = [(arg, arg.copy(deep=True)) for arg in args if isinstance(arg, pd.DataFrame)]
    try:
        function(*args, **kwargs)
    except UnsupportedPriceContract as exc:
        status = exc.as_dict()
        assert status["consumer"] == route and route in status["reason"]
        assert status["status"] == "UNSUPPORTED" and status["contract_version"] == CONTRACT_VERSION
    else:
        raise AssertionError(f"{route} accepted uncertified inputs")
    for frame, before in frames:
        pd.testing.assert_frame_equal(frame, before)


def weekdays(start, n):
    out, d = [], pd.Timestamp(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += pd.Timedelta(days=1)
    return out


def walk(ticker, dates, seed, start=1000.0):
    """A clean OHLC random walk: every close and open inside the ARA/ARB band,
    every open inside [low, high], so price_audit's guards pass everywhere."""
    rng = np.random.default_rng(seed)
    n = len(dates)
    close = start * np.cumprod(1 + rng.uniform(-0.02, 0.02, n))
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1] * (1 + rng.uniform(-0.01, 0.01, n - 1))
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    return pd.DataFrame({"date": dates, "ticker": ticker, "open": open_, "high": high,
                         "low": low, "close": close, "volume": 1000.0})


def same(a, b, tol=1e-12):
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return abs(a - b) <= tol


def fwd_of(outs, ticker, date, h):
    row = outs[(outs["ticker"] == ticker) & (outs["date"] == date)]
    assert len(row) == 1, (ticker, date)
    return float(row[f"fwd_oo_{h}"].iloc[0])


def rows_x_frame(records, rules=RULES_T):
    """rows_x as attach_excess would emit it, from compact dicts: date, ticker,
    eligible, fired (set of rule ids), x (x_h for every h), fwd (every h)."""
    out = []
    for r in records:
        row = {"date": r["date"], "ticker": r["ticker"], "eligible": r.get("eligible", True),
               "rv20": 0.03, "rv20_q": 0}
        for h in H:
            row[f"fwd_oo_{h}"] = r.get("fwd", np.nan)
            row[f"x_{h}"] = r.get("x", np.nan)
        row["hold_60"] = r.get("fwd", np.nan)          # h = 60 reads hold_60 (RET_COL)
        row["susp_60"] = r.get("susp", 0.0 if "fwd" in r else np.nan)
        for rule in rules:
            row[rule["id"]] = rule["id"] in r.get("fired", ())
        out.append(row)
    return pd.DataFrame(out)


def table_columns(table):
    conn = sqlite3.connect(":memory:")
    db.ensure_schema(conn)
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    conn.close()
    return cols


def _alpha_series(n, fwd_at, fired_at=None, ineligible=(), broker="XL", start="2025-06-02"):
    """n consecutive sessions of one ticker for alpha_cases: hold_60 is +10%
    except at the positions in fwd_at, rules fire at the positions in fired_at."""
    fired_at = fired_at or {}
    return [{"date": d, "fwd": fwd_at.get(i, 0.10), "fired": fired_at.get(i, set()),
             "eligible": i not in ineligible, "a_broker": broker}
            for i, d in enumerate(weekdays(start, n))]


def _alpha_rows(series):
    """rows_x with the columns alpha_cases / broker_lift read, from
    {ticker: _alpha_series(...)}. The explain values encode the position so a
    case can be traced back to its own row."""
    rows = []
    for ticker, recs in series.items():
        for i, r in enumerate(recs):
            row = {"date": r["date"], "ticker": ticker, "eligible": r["eligible"],
                   "hold_60": r["fwd"], "susp_60": r.get("susp", 0.0),
                   "a_broker": r["a_broker"],
                   "a_nl60_adv": 2.0 + i / 1000, "a_gap": 0.03, "range60": 0.2,
                   "val20": 1e9 + i}
            for rule in RULES_T:
                row[rule["id"]] = rule["id"] in r["fired"]
            rows.append(row)
    return pd.DataFrame(rows)


# ── outcomes ───────────────────────────────────────────────────────────────

def test_module_imports_neither_book_nor_rules():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def test_outcome_matches_hand_value_and_ignores_prices_after_exit():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def test_outcome_moves_when_prices_inside_window_change():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def test_entry_locked_at_limit_up_is_nan():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


# ── hold_60: the holder's h = 60 return (Amendment A2) ─────────────────────

def _hold_row(outs, ticker, date):
    row = outs[(outs["ticker"] == ticker) & (outs["date"] == date)]
    assert len(row) == 1, (ticker, date)
    return row.iloc[0]


def _suspended(n=200, gap=range(100, 105), seed=1):
    """AAAA misses the sessions in `gap` (a suspension); BBBB trades every
    session, so the calendar still has them."""
    dates = weekdays("2025-06-02", n)
    a, b = walk("AAAA", dates, seed), walk("BBBB", dates, seed + 1)
    return dates, a.drop(index=list(gap)), b


def test_hold_60_bridges_a_suspension():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def test_hold_60_still_rejects_a_split_like_step():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def test_hold_60_entry_must_trade_on_the_next_calendar_session():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def test_hold_60_reads_nothing_after_the_exit_open():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def test_live_outcome_rows_at_60_use_the_holder_return():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


# ── vol-neutral excess ─────────────────────────────────────────────────────

def _cross_section(seed=5):
    rng = np.random.default_rng(seed)
    rows, outs = [], []
    plan = [("2026-02-02", 40), ("2026-02-03", 40), ("2026-02-04", 20)]
    for d, n_elig in plan:
        for i in range(n_elig + 5):
            t = f"T{i:03d}"
            rows.append({"date": d, "ticker": t, "eligible": i < n_elig,
                         "rv20": float(rng.uniform(0.01, 0.08))})
            f = rng.normal(0.01, 0.05, len(H))
            if i == 3:
                f[:] = np.nan                  # an eligible row with no outcome
            outs.append({"date": d, "ticker": t, **{bl.RET_COL[h]: f[j] for j, h in enumerate(H)},
                         "susp_60": 0.0, "entry_blocked": False})
    return pd.DataFrame(rows), pd.DataFrame(outs)


def test_excess_sums_to_zero_within_date_and_bucket():
    rows, outs = _cross_section()
    _assert_refusal("broker_learning.attach_excess", bl.attach_excess, rows, outs)
    _assert_refusal("broker_learning.attach_excess", bl.attach_excess, rows, outs, single_bucket=True)
    changed = outs.copy()
    ineligible = set(rows.loc[~rows.eligible, ["date", "ticker"]].itertuples(index=False, name=None))
    changed.loc[changed[["date", "ticker"]].apply(tuple, axis=1).isin(ineligible), "fwd_oo_10"] = 9.9
    _assert_refusal("broker_learning.attach_excess", bl.attach_excess, rows, changed)
    # A partial schema must still refuse before any merge or source mutation.
    _assert_refusal("broker_learning.attach_excess", bl.attach_excess, rows.drop(columns="rv20"), outs)


# ── bootstrap and status ───────────────────────────────────────────────────

def test_block_bootstrap_is_deterministic_and_sane():
    rng = np.random.default_rng(3)
    idx = [f"2026-{m:02d}-{d:02d}" for m in range(1, 7) for d in range(1, 21)]
    s = pd.Series(rng.normal(0.01, 0.03, len(idx)), index=idx)
    first = bl.block_bootstrap_ci(s, block=10)
    assert first == bl.block_bootstrap_ci(s, block=10), "same inputs, same CI"
    lo, hi = first
    assert lo < s.mean() < hi and hi - lo > 0
    # blocks follow date order, not the order the series arrived in
    assert bl.block_bootstrap_ci(s.sample(frac=1, random_state=7), block=10) == first
    assert bl.block_bootstrap_ci(s, block=10, seed=18) != first, "the seed is used"
    for block in (1, 5, 20, 40):
        lo, hi = bl.block_bootstrap_ci(s, block=block)
        assert lo <= s.mean() <= hi, block

    lo, hi = bl.block_bootstrap_ci(pd.Series(0.02, index=idx), block=10)
    assert lo == hi and abs(lo - 0.02) < 1e-12, "a constant series has no spread"
    for tiny in (pd.Series([0.01], index=["2026-01-01"]), pd.Series([], dtype=float),
                 pd.Series([0.01, np.nan], index=["a", "b"])):
        assert all(math.isnan(v) for v in bl.block_bootstrap_ci(tiny, block=5))
    # Fewer than three blocks has no CI (§10): 119 dates at h = 60 is under
    # two blocks, and a CI from that is a bootstrap artifact. A missing CI is
    # LOW_N, never NEUTRAL or CONSISTENT, whatever the mean.
    short = s.iloc[:119] + 0.05
    ci = bl.block_bootstrap_ci(short, block=60)
    assert all(math.isnan(v) for v in ci), ci
    assert bl.status_of(+1, float(short.mean()), *ci, len(short), 60) == "LOW_N"
    assert all(math.isnan(v) for v in bl.block_bootstrap_ci(s.iloc[:29], block=10))
    assert all(math.isfinite(v) for v in bl.block_bootstrap_ci(s.iloc[:30], block=10))


def test_block_bootstrap_is_circular():
    # With circular blocks every date is drawn equally often, so the bootstrap
    # mean of a series is the series mean up to resampling noise; a moving
    # block that does not wrap under-draws both ends. Put all the signal at
    # the ends to make that visible.
    n, block = 90, 30
    v = np.zeros(n)
    v[:5] = v[-5:] = 1.0
    s = pd.Series(v, index=[f"d{i:03d}" for i in range(n)])
    lo, hi = bl.block_bootstrap_ci(s, block=block, B=4000)
    assert lo <= s.mean() <= hi
    # replicate the draw by hand: circular starts over all n, wrapped indices
    rng = np.random.default_rng(bl.BOOT_SEED)
    starts = rng.integers(0, n, size=(bl.BOOT_B, 3))
    idx = ((starts[:, :, None] + np.arange(block)) % n).reshape(bl.BOOT_B, 3 * block)
    means = v[idx].mean(axis=1)
    want = tuple(float(x) for x in np.percentile(means, [2.5, 97.5]))
    assert bl.block_bootstrap_ci(s, block=block) == want
    assert abs(means.mean() - s.mean()) < 0.01, "every date equally likely"
    # exactly three blocks is enough, one date fewer is not
    assert all(math.isfinite(x) for x in bl.block_bootstrap_ci(s, block=30))
    assert all(math.isnan(x) for x in bl.block_bootstrap_ci(s.iloc[:89], block=30))


def test_status_truth_table():
    cases = [
        # dir, mean,   lo,     hi,    n,  expected
        (+1, 0.010, 0.002, 0.020, 29, "LOW_N"),
        (+1, 0.010, 0.002, 0.020, 30, "CONSISTENT"),
        (+1, -0.010, -0.020, -0.002, 30, "CONTRARY"),
        (+1, 0.010, -0.005, 0.020, 30, "DIRECTIONAL"),
        (+1, 0.010, 0.000, 0.020, 30, "DIRECTIONAL"),   # touching 0 is crossing
        (+1, -0.010, -0.020, 0.005, 30, "NEUTRAL"),
        (+1, 0.000, -0.010, 0.010, 30, "NEUTRAL"),
        (-1, -0.010, -0.020, -0.002, 30, "CONSISTENT"),   # bearish and it fell
        (-1, 0.010, 0.002, 0.020, 30, "CONTRARY"),        # bearish and it rose
        (-1, -0.010, -0.020, 0.005, 30, "DIRECTIONAL"),
        (-1, 0.010, -0.005, 0.020, 30, "NEUTRAL"),
        (-1, -0.010, -0.020, -0.002, 12, "LOW_N"),
        (+1, 0.010, np.nan, np.nan, 40, "LOW_N"),        # no CI is no verdict
        (+1, -0.010, np.nan, 0.020, 40, "LOW_N"),
    ]
    for direction, mean, lo, hi, n, want in cases:
        got = bl.status_of(direction, mean, lo, hi, n, 10)
        assert got == want, (direction, mean, lo, hi, n, got, want)
    # LOW_N = n_dates < max(30, 3h): 30 at h = 5/10, 60 at h = 20, 180 at h = 60
    assert [bl.low_n_min(h) for h in H] == [30, 30, 60, 180]
    for h, n_min in ((5, 30), (20, 60), (60, 180)):
        assert bl.status_of(+1, 0.01, 0.002, 0.02, n_min - 1, h) == "LOW_N", h
        assert bl.status_of(+1, 0.01, 0.002, 0.02, n_min, h) == "CONSISTENT", h
    # the pre-A2 h = 60 run had ~99 dates: that is LOW_N now, CI or not
    assert bl.status_of(+1, 0.02, 0.01, 0.03, 99, 60) == "LOW_N"
    assert set(bl.STATUS_LABELS) == {"LOW_N", "CONSISTENT", "CONTRARY", "DIRECTIONAL", "NEUTRAL"}


# ── rule stats and weights ─────────────────────────────────────────────────

def test_rule_stats_hand_computed_and_schema_keys():
    rows = rows_x_frame([
        {"date": "d1", "ticker": "A", "fired": {"UP"}, "x": .02, "fwd": .03},
        {"date": "d1", "ticker": "B", "fired": {"UP"}, "x": -.01, "fwd": -.005},
        {"date": "d1", "ticker": "C", "x": -.01, "fwd": .01},
        {"date": "d1", "ticker": "D", "x": 0., "fwd": -.02},
        {"date": "d1", "ticker": "E", "eligible": False, "fired": {"UP"}, "fwd": .5},
        {"date": "d2", "ticker": "A", "fired": {"UP"}, "x": .04, "fwd": .05, "susp": 1.},
        {"date": "d2", "ticker": "B", "x": -.02, "fwd": -.01, "susp": 1.},
        {"date": "d2", "ticker": "C", "x": -.02, "fwd": -.01},
        {"date": "d3", "ticker": "A", "x": .01, "fwd": .02},
    ])
    _assert_refusal("broker_learning.rule_stats", bl.rule_stats, rows, "2026-09-19",
                    ("2025-10-01", "2026-09-19"), rules=RULES_T)
    # Retain the anonymous statistical operator's independent hand calculation.
    events = rows.eligible & rows.UP & rows.x_10.notna()
    universe = rows.eligible & rows.x_10.notna()
    up = bl._event_stats(rows.x_10, rows.fwd_oo_10, rows.date, events, universe, 10)
    assert up["n_events"] == 3 and up["n_dates"] == 2
    assert same(up["mean_excess"], (.005 + .04) / 2)
    assert same(up["hit_rate"], 2 / 3) and same(up["base_rate"], 3 / 7)
    assert same(up["hit_edge"], 2 / 3 - 3 / 7)
    assert same(up["daily_hit_edge"], (0.0 + (1 - 1 / 3)) / 2)
    assert up["big_rate"] == up["big_base_rate"] == 0.
    assert math.isnan(up["ci_lo"]) and math.isnan(up["ci_hi"]) and math.isnan(up["susp_rate"])
    assert bl.status_of(1, up["mean_excess"], up["ci_lo"], up["ci_hi"], up["n_dates"], 10) == "LOW_N"
    up60 = bl._event_stats(rows.x_60, rows.hold_60, rows.date, events, universe, 60, rows.susp_60)
    assert same(up60["susp_rate"], 1 / 3)
    dn = bl._event_stats(rows.x_10, rows.fwd_oo_10, rows.date, universe & rows.DN, universe, 10)
    assert dn["n_events"] == dn["n_dates"] == 0
    assert all(math.isnan(dn[k]) for k in dn if k not in {"n_events", "n_dates"})
    assert set(up) <= set(table_columns("rule_stats"))
    with sqlite3.connect(":memory:") as conn:
        db.ensure_schema(conn)
        before = conn.total_changes
        _assert_refusal("broker_learning_db.insert_rows", db.insert_rows, conn, "rule_stats", [up])
        assert conn.total_changes == before and conn.execute("SELECT COUNT(*) FROM rule_stats").fetchone()[0] == 0


def test_big_rate_vs_base_on_event_dates():
    rows = rows_x_frame([
        {"date": "d1", "ticker": "A", "fired": {"UP"}, "x": .1, "fwd": .55},
        {"date": "d1", "ticker": "B", "fired": {"UP"}, "x": 0., "fwd": .18},
        {"date": "d1", "ticker": "C", "x": 0., "fwd": .25},
        {"date": "d1", "ticker": "D", "x": 0., "fwd": -.1},
        {"date": "d2", "ticker": "A", "x": 0., "fwd": .9},
        {"date": "d1", "ticker": "E", "eligible": False, "fwd": .9},
    ])
    _assert_refusal("broker_learning.rule_stats", bl.rule_stats, rows, "a", ("s", "e"), rules=RULES_T)
    events, universe = rows.eligible & rows.UP, rows.eligible
    want = {5: (1., 3 / 4), 10: (1 / 2, 2 / 4), 20: (1 / 2, 1 / 4), 60: (1 / 2, 1 / 4)}
    for h, (big, base) in want.items():
        stats = bl._event_stats(rows[f"x_{h}"], rows[bl.RET_COL[h]], rows.date, events, universe, h)
        assert same(stats["big_rate"], big) and same(stats["big_base_rate"], base), h
    values = rows.hold_60.copy()
    values.iloc[1] = .50
    assert bl._big(values[events], 60) == 1., "the +50% threshold is inclusive"


def test_primary_status_reads_each_rules_own_horizon():
    from corporate_action_test_support import assert_unmigrated, frozen_reviewed_numeric_function
    assert_unmigrated("broker_learning.primary_status")
    legacy = frozen_reviewed_numeric_function("broker_learning", "primary_status")
    stats = [
        {"rule_id": "UP", "h": 5, "status": "NEUTRAL"},
        {"rule_id": "UP", "h": 10, "status": "DIRECTIONAL"},
        {"rule_id": "DN", "h": 10, "status": "CONSISTENT"},
        {"rule_id": "DN", "h": 60, "status": "LOW_N"},
        {"rule_id": "XX", "h": 10, "status": "CONSISTENT"},     # not in the ruleset
    ]
    want = {"UP": "DIRECTIONAL", "DN": "LOW_N"}
    assert legacy(stats, primary_h=PH_T) == want
    assert legacy(pd.DataFrame(stats), primary_h=PH_T) == want
    assert legacy(stats[:1], primary_h=PH_T) == {}, "no primary row, no guess"


def test_other_writers_match_their_table_columns():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def _weight_rows(n_dates, x, rule, per_date=1):
    recs = []
    for d in weekdays("2025-10-01", n_dates):
        for j in range(per_date):
            recs.append({"date": d, "ticker": f"T{j:02d}", "fired": {rule}, "x": x, "fwd": x})
        recs.append({"date": d, "ticker": "ZZ", "x": 0.0, "fwd": 0.0})   # non-firing
    return rows_x_frame(recs)


def test_weights_mirror_learn_weights():
    for n, x, rule in [(100, .05, "UP"), (100, .01, "DN"), (100, -.01, "DN"),
                       (100, .50, "UP"), (400, .10, "DN")]:
        rows = _weight_rows(n, x, rule)
        _assert_refusal("broker_learning.rule_weights", bl.rule_weights, rows, "as", primary_h=PH_T, rules=RULES_T)
    rows = _weight_rows(100, .01, "DN").assign(x_60=np.nan)
    _assert_refusal("broker_learning.rule_weights", bl.rule_weights, rows, "as", primary_h={"UP": 10}, rules=RULES_T)
    # Clipping before the date mean is still tested on the numerical operator.
    clipped = pd.Series([.50, -.10]).clip(-bl.EXCESS_CAP, bl.EXCESS_CAP)
    per_date = bl._per_date_mean(clipped, pd.Series(["d1", "d1"]))
    assert same(per_date.iloc[0] * 100, 2.5)
    assert bl._per_date_mean(pd.Series(dtype=float), pd.Series(dtype=str)).empty


def test_net_trade_stats_ship_with_base_rate():
    rows = rows_x_frame([
        {"date": "d1", "ticker": "A", "fired": {"UP", "DN"}, "x": .02, "fwd": .03},
        {"date": "d1", "ticker": "B", "fired": {"UP"}, "x": -.01, "fwd": .005},
        {"date": "d1", "ticker": "C", "x": -.01, "fwd": .01},
        {"date": "d1", "ticker": "D", "x": 0., "fwd": -.02},
    ])
    _assert_refusal("broker_learning.net_trade_stats", bl.net_trade_stats, rows, primary_h=PH_T, rules=RULES_T)
    net = rows.fwd_oo_10 - bl.ROUND_TRIP_COST
    up = signal_metrics.trade_stats(net[rows.UP], base_rate=bl._hit(net))
    assert up["n_trades"] == 2 and same(up["hit_rate"], .5)
    assert same(up["mean_ret"], (.03 + .005) / 2 - bl.ROUND_TRIP_COST)
    assert same(up["base_rate"], .5) and same(up["hit_edge"], 0.)


# ── broker scores and profitability ────────────────────────────────────────

def test_broker_scores_are_date_balanced_and_shrunk():
    events = pd.DataFrame([
        ("d1", "AAAA", "XL", "buy", .04), ("d1", "BBBB", "XL", "buy", .00),
        ("d2", "AAAA", "XL", "buy", -.01), ("d1", "CCCC", "XL", "sell", -.03),
        ("d3", "AAAA", "AK", "buy", .02),
    ], columns=["date", "ticker", "broker", "side", "x"])
    events["nl5"], events["adv20"] = 100., 50.
    for h in H:
        events[f"x_{h}"] = events.x
    events.loc[events.broker.eq("AK"), "x_20"] = np.nan
    _assert_refusal("broker_learning.broker_scores", bl.broker_scores, events.drop(columns="x"), "2026-09-19")
    _assert_refusal("broker_learning.broker_scores", bl.broker_scores, events.assign(side="hold"), "2026-09-19")
    supplied = events[events.broker.eq("XL") & events.side.eq("buy")]
    per_date = bl._per_date_mean(supplied.x, supplied.date)
    assert per_date.tolist() == [.02, -.01]
    assert same(per_date.mean(), (.02 - .01) / 2)
    assert [60 < bl.low_n_min(h) for h in H] == [False, False, False, True]
    sell = events[events.side.eq("sell")]
    assert bl._per_date_mean(sell.x, sell.date).iloc[0] == -.03
    assert all(math.isnan(v) for v in bl.block_bootstrap_ci(per_date, 10))


def test_broker_profitability_sums_books():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def test_live_outcome_rows_record_the_return_and_exit_date():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


def test_live_excess_uses_every_outcome_recorded_for_the_session():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


# ── alpha case library ─────────────────────────────────────────────────────

def test_alpha_cases_keep_the_first_t_of_each_episode():
    series = _alpha_series(200, {90: .49, 100: .70, 101: .90, 161: .50, 170: 1.20},
                           fired_at={79: {"UP"}, 80: {"DN"}, 100: {"UP"}}, ineligible={95})
    rows = _alpha_rows({"AAAA": series}).sample(frac=1, random_state=3)
    for fixture in (rows, rows.sort_values(["date", "ticker"]), rows.assign(hold_60=.2),
                    rows.assign(fwd_oo_60=9.9, hold_60=.2), rows.drop(columns="a_gap"),
                    pd.concat([rows, rows.iloc[:1]])):
        _assert_refusal("broker_learning.alpha_cases", bl.alpha_cases, fixture, "2026-09-19", rules=RULES_T)
    for value in (True, 1):
        assert bl._visible({"visible": value})
    for value in (False, 0, np.nan, None):
        assert not bl._visible({"visible": value})
    assert not bl._visible({})


def test_broker_lift_hand_example():
    rows = pd.DataFrame({"date": [f"d{i}" for i in range(12)], "ticker": "T",
        "eligible": [True] * 10 + [False, True], "hold_60": [.1] * 11 + [np.nan],
        "fwd_oo_60": [np.nan] * 12, "a_broker": ["XL"] * 4 + ["AK"] * 4 + [None, "CC", "XL", "XL"]})
    cases = [{"top_broker": "XL", "visible": 1}, {"top_broker": "XL", "visible": True},
             {"top_broker": None, "visible": 1}, {"top_broker": "AK", "visible": 0},
             {"top_broker": "CC", "visible": np.nan}, {"top_broker": "CC"}]
    for fixture in (cases, pd.DataFrame(cases), [], cases[3:]):
        _assert_refusal("broker_learning.broker_lift", bl.broker_lift, rows, fixture, "2026-09-19")
    assert [bl._visible(c) for c in cases] == [True, True, True, False, False, False]


# ── signal_metrics.date_balanced_hit_edge ──────────────────────────────────

def test_date_balanced_hit_edge_hand_example():
    # d1: hits 1/2 vs base 2/4 -> 0 ; d2: hits 1/1 vs base 1/4 -> +0.75
    # d3: the only signal return is NaN -> skipped ; d4: no universe return -> skipped
    sig_r = pd.Series([0.02, -0.01, 0.03, np.nan, 0.01], index=[9, 8, 7, 6, 5])
    sig_d = ["d1", "d1", "d2", "d3", "d4"]
    uni_r = [0.01, -0.02, -0.01, 0.03, 0.02, -0.01, -0.03, -0.02, 0.04, np.nan]
    uni_d = ["d1", "d1", "d1", "d1", "d2", "d2", "d2", "d2", "d3", "d4"]
    got = signal_metrics.date_balanced_hit_edge(sig_r, sig_d, uni_r, uni_d)
    assert got["n_dates"] == 2 and same(got["daily_hit_edge"], 0.375)
    # a zero return is not a hit on either side
    got = signal_metrics.date_balanced_hit_edge([0.0], ["d"], [0.0, 0.01], ["d", "d"])
    assert same(got["daily_hit_edge"], -0.5)
    empty = signal_metrics.date_balanced_hit_edge([], [], [0.01], ["d"])
    assert empty["n_dates"] == 0 and math.isnan(empty["daily_hit_edge"])


# ── database ───────────────────────────────────────────────────────────────

def test_db_schema_idempotent_and_insert_or_ignore():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bl.db")
        conn = db.connect(path)
        db.ensure_schema(conn)
        conn.close()
        conn = db.connect(path)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert names == set(db.TABLES) and len(db.TABLES) == 9
            assert set(db.AS_OF_TABLES) == set(db.TABLES) - {"runs", "live_signals", "live_outcomes"}
            before = conn.total_changes
            for table in set(db.TABLES) - {"runs"}:
                _assert_refusal("broker_learning_db.insert_rows", db.insert_rows, conn, table, [{"as_of": "2026-09-19"}])
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            assert conn.total_changes == before
            row = {"run_id": "first", "kind": "weekly", "status": "ok", "tickers_ok": np.int64(40)}
            assert db.insert_rows(conn, "runs", [row]) == 1
            assert db.insert_rows(conn, "runs", [dict(row, tickers_ok=3)]) == 0
            assert conn.execute("SELECT tickers_ok FROM runs WHERE run_id='first'").fetchone()[0] == 40
            assert db.insert_rows(conn, "runs", [dict(row, run_id="missing", tickers_ok=np.nan, tickers_fail=np.int64(0))]) == 1
            assert conn.execute("SELECT tickers_ok IS NULL,tickers_fail FROM runs WHERE run_id='missing'").fetchone() == (1, 0)
            for table, bad in (("sqlite_master", row), ("runs", dict(row, typo=1)), ("runs", dict(row, run_id=None))):
                try:
                    db.insert_rows(conn, table, [bad])
                except ValueError:
                    pass
                else:
                    raise AssertionError("bad table/column/key accepted")
            run_id = db.start_run(conn, "weekly", "2026-09-19T09:00:00Z")
            assert run_id == "weekly-2026-09-19T09:00:00Z"
            db.finish_run(conn, run_id, status="ok", tickers_ok=1000, tickers_fail=np.int64(3),
                          finished_utc="2026-09-19T09:45:00Z", data_through="2026-09-18")
            assert conn.execute("SELECT status,tickers_ok,tickers_fail FROM runs WHERE run_id=?", (run_id,)).fetchone() == ("ok",1000,3)
        finally:
            conn.close()


def test_db_as_of_versions():
    with sqlite3.connect(":memory:") as conn:
        db.ensure_schema(conn)
        assert db.latest_as_of(conn, "rule_weights") is None
        assert db.previous_as_of(conn, "rule_weights") is None
        # Stored v0 records are fixture history, never produced by a guarded v1 writer.
        conn.executemany("INSERT INTO rule_weights VALUES (?,?,?,?,?,?)", [
            ("2026-09-12", "v1", "R1", 1.1, 30, 1.),
            ("2026-09-19", "v1", "R1", 1.3, 30, 1.)])
        assert db.latest_as_of(conn, "rule_weights") == "2026-09-19"
        assert db.previous_as_of(conn, "rule_weights") == "2026-09-12"
        before = conn.total_changes
        for name in ("load_weights", "load_rule_stats", "load_broker_scores", "load_profitability", "load_alpha_cases", "load_broker_lift"):
            fn = getattr(db, name)
            _assert_refusal("broker_learning_db." + name, fn, conn)
            _assert_refusal("broker_learning_db." + name, fn, conn, "2026-09-12")
        assert conn.total_changes == before
        assert conn.execute("SELECT weight FROM rule_weights ORDER BY as_of").fetchall() == [(1.1,), (1.3,)]
        try:
            db.latest_as_of(conn, "live_signals")
        except ValueError:
            pass
        else:
            raise AssertionError("non-versioned table accepted by latest_as_of")


def _seed_live(conn):
    sig = []
    for d, t, fired in (("d1", "AAAA", {"R1"}), ("d1", "BBBB", set()), ("d1", "CCCC", {"R1"}),
                        ("d2", "AAAA", {"R1"}), ("d2", "BBBB", {"R2"})):
        for rule in ("R1", "R2"):
            sig.append({"session_date": d, "ticker": t, "ruleset": "v1", "rule_id": rule,
                        "fired": int(rule in fired), "score": 0.0,
                        "captured_utc": "2026-09-25T10:40Z", "features": "{}"})
    db.insert_rows(conn, "live_signals", sig)
    outs = [("d1", "AAAA", 10, 0.04), ("d1", "BBBB", 10, -0.01),
            ("d1", "CCCC", 10, 0.03), ("d2", "AAAA", 10, -0.02),
            ("d1", "AAAA", 5, 0.01), ("d1", "AAAA", 60, 0.9),
            ("d1", "CCCC", 60, 0.1), ("d1", "BBBB", 60, 0.2)]
    db.insert_rows(conn, "live_outcomes", [
        {"session_date": d, "ticker": t, "h": h, "fwd_oo": f,
         "susp": (1 if t == "AAAA" else 0) if h == 60 else None,
         "exit_date": "dx", "recorded_utc": "2026-10-10T10:40Z"} for d, t, h, f in outs])


def test_db_pending_live_and_live_summary():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_learning.outcomes")


# ── guards ─────────────────────────────────────────────────────────────────

def _sqrt_252_sites(source, filename):
    """Same matcher as test_pipeline._sqrt_252_call_sites: any sqrt(...) whose
    argument holds the constant 252 anywhere. AST-based, so prose is ignored."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or len(node.args) != 1:
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name != "sqrt":
            continue
        for sub in ast.walk(node.args[0]):
            if (isinstance(sub, ast.Constant) and not isinstance(sub.value, bool)
                    and sub.value == 252):
                hits.append(f"{filename}:{node.lineno}")
                break
    return hits


def test_defaults_come_from_broker_rules_lazily():
    import types
    fake = types.ModuleType("broker_rules")
    fake.RULES, fake.PRIMARY_H = RULES_T, PH_T
    saved = sys.modules.get("broker_rules")
    sys.modules["broker_rules"] = fake
    try:
        assert bl._rule_defs() == [("UP", 1), ("DN", -1)]
        assert bl._primary_h() == PH_T
        from corporate_action_test_support import assert_unmigrated, frozen_reviewed_numeric_function
        assert_unmigrated("broker_learning.primary_status")
        legacy = frozen_reviewed_numeric_function("broker_learning", "primary_status")
        assert legacy([{"rule_id": "DN", "h": 60, "status": "LOW_N"}]) == {"DN": "LOW_N"}
        rows = _weight_rows(100, .01, "DN")
        _assert_refusal("broker_learning.rule_weights", bl.rule_weights, rows, "as")
        _assert_refusal("broker_learning.rule_weights", bl.rule_weights, rows, "as", primary_h=PH_T, rules=RULES_T)
        _assert_refusal("broker_learning.rule_stats", bl.rule_stats, rows, "as", ("s", "e"))
    finally:
        if saved is None:
            sys.modules.pop("broker_rules", None)
        else:
            sys.modules["broker_rules"] = saved


def test_no_annualisation_in_these_files():
    assert _sqrt_252_sites("r = m / s * np.sqrt(252 / h)", "x.py"), "matcher is live"
    for fn in ("broker_learning.py", "broker_learning_db.py", "test_broker_learning.py",
               "signal_metrics.py"):
        with open(os.path.join(HERE, fn), encoding="utf-8") as f:
            src = f.read()
        assert not _sqrt_252_sites(src, fn), fn
    for fn in ("broker_learning.py", "broker_learning_db.py"):
        with open(os.path.join(HERE, fn), encoding="utf-8") as f:
            assert "sharpe" not in f.read().lower(), fn


ALL = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    print(f"broker learning: {len(ALL)} tests\n")
    for fn in ALL:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"\nAll {len(ALL)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
