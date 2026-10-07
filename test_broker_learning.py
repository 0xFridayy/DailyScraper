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

HERE = os.path.dirname(os.path.abspath(__file__))
H = bl.HORIZONS
# Stand-in ruleset: one bullish and one bearish rule, so direction handling is
# exercised without depending on broker_rules. DN's primary horizon is 60 (as
# R6's is) so the per-rule horizon is exercised too.
RULES_T = [{"id": "UP", "dir": +1}, {"id": "DN", "dir": -1}]
PH_T = {"UP": 10, "DN": 60}


# ── helpers ────────────────────────────────────────────────────────────────

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
    x = bl.attach_excess(rows, outs)
    elig = x["eligible"]
    assert x.loc[~elig, bl.X_COLS].isna().all().all(), "ineligible rows are never scored"
    assert x.loc[~elig, "rv20_q"].isna().all()

    thick = x[elig & x.date.isin(["2026-02-02", "2026-02-03"])]
    assert sorted(thick["rv20_q"].unique()) == [1, 2, 3, 4, 5]
    assert (thick.groupby(["date", "rv20_q"]).size() == 8).all()
    for _, g in thick.groupby("date"):         # buckets are ordered by rv20
        spans = g.groupby("rv20_q")["rv20"].agg(["min", "max"]).sort_index()
        assert (spans["max"].iloc[:-1].to_numpy() < spans["min"].iloc[1:].to_numpy()).all()
    thin = x[elig & (x.date == "2026-02-04")]
    assert (thin["rv20_q"] == bl.SINGLE_BUCKET).all(), "20 rows < 25: one bucket"

    for h in H:
        col = f"x_{h}"
        sums = x[elig].groupby(["date", "rv20_q"])[col].sum()
        assert sums.abs().max() < 1e-12, sums
        assert x.loc[elig & x[bl.RET_COL[h]].isna(), col].isna().all()
        # single bucket: excess vs the equal-weight eligible mean of that date
        f = thin[bl.RET_COL[h]]
        assert np.allclose(thin[col].dropna(), (f - f.mean()).dropna())

    # an ineligible row's outcome never leaks into anyone's excess
    outs2 = outs.copy()
    inel = rows.loc[~rows.eligible, ["date", "ticker"]].apply(tuple, axis=1)
    mask = outs2[["date", "ticker"]].apply(tuple, axis=1).isin(set(inel))
    outs2.loc[mask, "fwd_oo_10"] = 9.9
    x2 = bl.attach_excess(rows, outs2)
    pd.testing.assert_series_equal(x["x_10"], x2["x_10"])

    forced = bl.attach_excess(rows, outs, single_bucket=True)
    assert (forced.loc[forced.eligible, "rv20_q"] == bl.SINGLE_BUCKET).all()
    for d, g in forced[forced.eligible].groupby("date"):
        assert abs(g["x_10"].sum()) < 1e-12, d


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
    recs = [
        {"date": "d1", "ticker": "A", "fired": {"UP"}, "x": 0.02, "fwd": 0.03},
        {"date": "d1", "ticker": "B", "fired": {"UP"}, "x": -0.01, "fwd": -0.005},
        {"date": "d1", "ticker": "C", "x": -0.01, "fwd": 0.01},
        {"date": "d1", "ticker": "D", "x": 0.0, "fwd": -0.02},
        {"date": "d1", "ticker": "E", "eligible": False, "fired": {"UP"}, "fwd": 0.5},
        {"date": "d2", "ticker": "A", "fired": {"UP"}, "x": 0.04, "fwd": 0.05, "susp": 1.0},
        {"date": "d2", "ticker": "B", "x": -0.02, "fwd": -0.01, "susp": 1.0},
        {"date": "d2", "ticker": "C", "x": -0.02, "fwd": -0.01},
        {"date": "d3", "ticker": "A", "x": 0.01, "fwd": 0.02},   # not an event date
    ]
    stats = bl.rule_stats(rows_x_frame(recs), "2026-09-19", ("2025-10-01", "2026-09-19"),
                          rules=RULES_T)
    assert len(stats) == len(RULES_T) * len(H)
    cols = table_columns("rule_stats")
    for row in stats:
        assert list(row) == cols, (list(row), cols)

    up = next(r for r in stats if r["rule_id"] == "UP" and r["h"] == 10)
    assert up["n_events"] == 3 and up["n_dates"] == 2
    assert same(up["mean_excess"], (0.005 + 0.04) / 2)
    assert same(up["hit_rate"], 2 / 3)
    assert same(up["base_rate"], 3 / 7)
    assert same(up["hit_edge"], 2 / 3 - 3 / 7)
    assert same(up["daily_hit_edge"], (0.0 + (1 - 1 / 3)) / 2)
    assert up["big_rate"] == 0.0 and up["big_base_rate"] == 0.0, "no move reaches +20%"
    # two per-date values and a 10-date block: fewer dates than one block, no CI
    assert math.isnan(up["ci_lo"]) and math.isnan(up["ci_hi"])
    assert up["low_n"] == 1 and up["status"] == "LOW_N"
    assert (up["window_start"], up["window_end"]) == ("2025-10-01", "2026-09-19")
    # susp_rate: share of the h = 60 events whose window bridged a suspension
    # (1 of the 3 UP events; B's suspension is not an event); NULL elsewhere
    up60 = next(r for r in stats if r["rule_id"] == "UP" and r["h"] == 60)
    assert same(up60["susp_rate"], 1 / 3) and math.isnan(up["susp_rate"])
    assert all(math.isnan(r["susp_rate"]) for r in stats if r["h"] != 60)
    assert up60["low_n"] == 1 and up60["status"] == "LOW_N"

    dn = next(r for r in stats if r["rule_id"] == "DN" and r["h"] == 10)
    assert dn["n_events"] == 0 and dn["n_dates"] == 0 and dn["status"] == "LOW_N"
    for k in ("mean_excess", "ci_lo", "ci_hi", "hit_rate", "base_rate", "hit_edge",
              "daily_hit_edge", "big_rate", "big_base_rate", "susp_rate"):
        assert math.isnan(dn[k]), k

    # every row goes into the real table unchanged
    conn = sqlite3.connect(":memory:")
    db.ensure_schema(conn)
    assert db.insert_rows(conn, "rule_stats", stats) == len(stats)
    conn.close()


def test_big_rate_vs_base_on_event_dates():
    # fwd is the same number at every h here, so each h applies its own bar:
    # BIG = {5: .15, 10: .20, 20: .30, 60: .50}
    recs = [
        {"date": "d1", "ticker": "A", "fired": {"UP"}, "x": 0.1, "fwd": 0.55},
        {"date": "d1", "ticker": "B", "fired": {"UP"}, "x": 0.0, "fwd": 0.18},
        {"date": "d1", "ticker": "C", "x": 0.0, "fwd": 0.25},
        {"date": "d1", "ticker": "D", "x": 0.0, "fwd": -0.1},
        {"date": "d2", "ticker": "A", "x": 0.0, "fwd": 0.9},     # not an event date
        {"date": "d1", "ticker": "E", "eligible": False, "fwd": 0.9},
    ]
    stats = {r["h"]: r for r in bl.rule_stats(rows_x_frame(recs), "a", ("s", "e"),
                                               rules=RULES_T) if r["rule_id"] == "UP"}
    assert sorted(stats) == [5, 10, 20, 60]
    # events A, B; base = A, B, C, D on d1 (d2 and the ineligible E are out)
    want = {5: (2 / 2, 3 / 4), 10: (1 / 2, 2 / 4), 20: (1 / 2, 1 / 4), 60: (1 / 2, 1 / 4)}
    for h, (big, base) in want.items():
        assert same(stats[h]["big_rate"], big), h
        assert same(stats[h]["big_base_rate"], base), h
    # the bar is inclusive: exactly +50% at h = 60 is a big move
    recs[1]["fwd"] = 0.50
    s60 = next(r for r in bl.rule_stats(rows_x_frame(recs), "a", ("s", "e"), rules=RULES_T)
               if r["rule_id"] == "UP" and r["h"] == 60)
    assert same(s60["big_rate"], 1.0)


def test_primary_status_reads_each_rules_own_horizon():
    stats = [
        {"rule_id": "UP", "h": 5, "status": "NEUTRAL"},
        {"rule_id": "UP", "h": 10, "status": "DIRECTIONAL"},
        {"rule_id": "DN", "h": 10, "status": "CONSISTENT"},
        {"rule_id": "DN", "h": 60, "status": "LOW_N"},
        {"rule_id": "XX", "h": 10, "status": "CONSISTENT"},     # not in the ruleset
    ]
    want = {"UP": "DIRECTIONAL", "DN": "LOW_N"}
    assert bl.primary_status(stats, primary_h=PH_T) == want
    assert bl.primary_status(pd.DataFrame(stats), primary_h=PH_T) == want
    assert bl.primary_status(stats[:1], primary_h=PH_T) == {}, "no primary row, no guess"


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
    def w(rows, rule, ph=PH_T):
        return next(r for r in bl.rule_weights(rows, "as", primary_h=ph, rules=RULES_T)
                    if r["rule_id"] == rule)

    none = w(rows_x_frame([{"date": "d1", "ticker": "A", "x": 0.03, "fwd": 0.03}]), "UP")
    assert none["weight"] == 1.0 and none["n_dates"] == 0
    assert math.isnan(none["avg_excess_pct"]), "nothing measured is not 0"

    # 100 dates of +5% excess: n = 10, avg_pct = 5 -> 1 + 0.5 * 10/30 * 5
    up = w(_weight_rows(100, 0.05, "UP"), "UP")
    assert up["n_dates"] == 100 and same(up["avg_excess_pct"], 5.0, 1e-9)
    assert same(up["weight"], 1 + 0.5 * (10 / 30) * 5, 1e-12) and up["weight"] > 1

    # the same move under a bearish rule is evidence AGAINST it; DN is judged
    # at h = 60, so 100 dates are only n = 100/60 independent looks
    dn = w(_weight_rows(100, 0.01, "DN"), "DN")
    assert same(dn["avg_excess_pct"], -1.0, 1e-9)
    n60 = 100 / 60
    assert same(dn["weight"], 1 - 0.5 * (n60 / (n60 + 20)) * 1, 1e-12) and dn["weight"] < 1
    # at h = 10 the same rows would count as n = 10
    dn10 = w(_weight_rows(100, 0.01, "DN"), "DN", ph={"UP": 10, "DN": 10})
    assert same(dn10["weight"], 1 - 0.5 * (10 / 30) * 1, 1e-12)
    # the horizon picks the column: only x_60 counts for DN
    rows = _weight_rows(100, 0.01, "DN")
    rows["x_60"] = np.nan
    assert w(rows, "DN")["weight"] == 1.0 and w(rows, "DN")["n_dates"] == 0
    try:
        w(rows, "DN", ph={"UP": 10})
    except ValueError:
        pass
    else:
        raise AssertionError("a rule without a primary horizon must be refused")
    # and a falling name under it is evidence for it
    assert w(_weight_rows(100, -0.01, "DN"), "DN")["weight"] > 1

    # clipping at both ends (the +50% is also capped to 15% first)
    assert w(_weight_rows(100, 0.50, "UP"), "UP")["weight"] == bl.WEIGHT_MAX
    assert same(w(_weight_rows(100, 0.50, "UP"), "UP")["avg_excess_pct"], 15.0, 1e-9)
    assert w(_weight_rows(400, 0.10, "DN"), "DN")["weight"] == bl.WEIGHT_MIN

    # the cap applies per event BEFORE the date mean: [0.50, -0.10] -> 0.025
    recs = [{"date": "d1", "ticker": "A", "fired": {"UP"}, "x": 0.50, "fwd": 0.5},
            {"date": "d1", "ticker": "B", "fired": {"UP"}, "x": -0.10, "fwd": -0.1}]
    one = w(rows_x_frame(recs), "UP")
    assert same(one["avg_excess_pct"], 2.5, 1e-9)
    n = 1 / 10
    assert same(one["weight"], 1 + 0.5 * (n / (n + 20)) * 2.5, 1e-12)


def test_net_trade_stats_ship_with_base_rate():
    recs = [
        {"date": "d1", "ticker": "A", "fired": {"UP", "DN"}, "x": 0.02, "fwd": 0.03},
        {"date": "d1", "ticker": "B", "fired": {"UP"}, "x": -0.01, "fwd": 0.005},
        {"date": "d1", "ticker": "C", "x": -0.01, "fwd": 0.01},
        {"date": "d1", "ticker": "D", "x": 0.0, "fwd": -0.02},
    ]
    s = bl.net_trade_stats(rows_x_frame(recs), primary_h=PH_T, rules=RULES_T)
    assert list(s) == ["UP"], "bearish rules get no long-trade stats"
    up = s["UP"]
    assert up["n_trades"] == 2 and same(up["hit_rate"], 0.5)         # 0.03 and 0.005 net of 0.73%
    assert same(up["mean_ret"], (0.03 + 0.005) / 2 - bl.ROUND_TRIP_COST)
    assert same(up["base_rate"], 2 / 4) and same(up["hit_edge"], 0.0)


# ── broker scores and profitability ────────────────────────────────────────

def test_broker_scores_are_date_balanced_and_shrunk():
    ev = pd.DataFrame([
        # date, ticker, broker, side, x (all h)
        ("d1", "AAAA", "XL", "buy", 0.04), ("d1", "BBBB", "XL", "buy", 0.00),
        ("d2", "AAAA", "XL", "buy", -0.01),
        ("d1", "CCCC", "XL", "sell", -0.03),
        ("d3", "AAAA", "AK", "buy", 0.02),
    ], columns=["date", "ticker", "broker", "side", "x"])
    ev["nl5"], ev["adv20"] = 100.0, 50.0
    for h in H:
        ev[f"x_{h}"] = ev["x"]
    ev.loc[ev.broker == "AK", "x_20"] = np.nan         # AK not yet scorable at h=20
    scores = bl.broker_scores(ev.drop(columns="x"), "2026-09-19")

    keys = [(r["broker"], r["side"], r["h"]) for r in scores]
    assert keys == sorted(keys)
    assert ("AK", "buy", 20) not in keys, "no scored event, no row (not zeros)"
    xl = next(r for r in scores if (r["broker"], r["side"], r["h"]) == ("XL", "buy", 10))
    assert xl["n_events"] == 3 and xl["n_dates"] == 2 and xl["n_tickers"] == 2
    assert same(xl["mean_excess"], (0.02 + -0.01) / 2)            # d1 averaged first
    n = 2 / 10
    assert same(xl["shrunk"], n / (n + 20) * xl["mean_excess"])
    assert xl["low_n"] == 1
    sell = next(r for r in scores if (r["broker"], r["side"], r["h"]) == ("XL", "sell", 5))
    assert same(sell["mean_excess"], -0.03), "raw sign: the dashboard applies dir"
    assert math.isnan(sell["ci_lo"]), "one date cannot give a CI"

    # LOW_N = n_dates < max(30, 3h): 60 dates is enough at h = 20, not at 60
    days = weekdays("2025-10-01", 60)
    many = pd.DataFrame({"date": days, "ticker": "AAAA", "broker": "ZZ", "side": "buy",
                         "nl5": 100.0, "adv20": 50.0})
    for h in H:
        many[f"x_{h}"] = np.linspace(-0.01, 0.03, len(days))
    low = {r["h"]: r for r in bl.broker_scores(many, "2026-09-19")}
    assert [low[h]["low_n"] for h in H] == [0, 0, 0, 1], low
    assert math.isnan(low[60]["ci_lo"]) and not math.isnan(low[20]["ci_lo"])

    bad = ev.drop(columns="x").assign(side="hold")
    try:
        bl.broker_scores(bad, "2026-09-19")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown side must be refused")


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
    a = _alpha_series(
        200,
        fwd_at={90: 0.49, 95: 0.80, 96: np.nan, 100: 0.70, 101: 0.90, 130: 0.60,
                160: 0.60, 161: 0.50, 170: 1.20},
        fired_at={79: {"UP"}, 80: {"DN"}, 95: {"UP"}, 100: {"UP"},
                  140: {"DN"}, 141: {"DN", "UP"}},
        ineligible={95},
    )
    a[161]["a_broker"] = None
    b = _alpha_series(200, fwd_at={130: 0.55}, broker="AK")
    rows = _alpha_rows({"BBBB": b, "AAAA": a}).sample(frac=1, random_state=3)
    cases = bl.alpha_cases(rows, "2026-09-19", rules=RULES_T)

    got = [(c["ticker"], c["session_date"]) for c in cases]
    da, db_ = weekdays("2025-06-02", 200), weekdays("2025-06-02", 200)
    # AAAA: 90 is below the bar, 95 is ineligible, 96 has no outcome; 100 is
    # taken, 101..160 fall in its 60-session shadow, 161 (exactly +50%) starts
    # a new episode, 170 is in 161's shadow. BBBB's 130 is its own ticker.
    assert got == [("AAAA", da[100]), ("AAAA", da[161]), ("BBBB", db_[130])], got

    c100, c161, cb = cases
    assert c100["as_of"] == "2026-09-19" and same(c100["hold_60"], 0.70)
    # sessions before T in the data, and whether the run-up's accumulation is
    # inside the data at all (>= 80 sessions of history)
    assert (c100["sessions_before"], c100["visible"]) == (100, 1)
    assert (c161["sessions_before"], cb["sessions_before"]) == (161, 130)
    assert c100["susp_60"] == 0 and isinstance(c100["susp_60"], int)
    # at T: UP. prior 80..99: DN at 80; UP at 79 is 21 sessions back, and the
    # UP at 95 was on an ineligible row, so neither counts
    assert c100["rules_at_t"] == "UP" and c100["rules_prior20"] == "DN"
    # nothing at T is "", not NULL; prior 141..160 has both, in ruleset order
    assert c161["rules_at_t"] == "" and c161["rules_prior20"] == "UP,DN"
    assert c161["top_broker"] is None, "a missing broker stays missing"
    assert cb["rules_at_t"] == "" and cb["rules_prior20"] == "" and cb["top_broker"] == "AK"
    # the snapshot is the case row's own, as of T
    assert same(c100["top_nl60_adv"], 2.1) and same(c100["val20"], 1e9 + 100)
    assert same(c100["top_cost_gap"], 0.03) and same(c100["range60"], 0.2)

    # the result does not depend on the order rows arrive in
    again = bl.alpha_cases(rows.sort_values(["date", "ticker"]), "2026-09-19", rules=RULES_T)
    assert [(c["ticker"], c["session_date"]) for c in again] == got

    assert bl.alpha_cases(rows.assign(hold_60=0.2), "x", rules=RULES_T) == []
    # the guarded fwd_oo_60 is not what picks a case any more: hold_60 is
    assert bl.alpha_cases(rows.assign(fwd_oo_60=9.9, hold_60=0.2), "x", rules=RULES_T) == []

    # a case in the first 80 sessions: its accumulation predates the data
    early = _alpha_series(120, fwd_at={79: 0.9, 80: 0.9})
    early[79]["susp"] = 1.0
    c = bl.alpha_cases(_alpha_rows({"CCCC": early}), "x", rules=RULES_T)
    assert [(r["sessions_before"], r["visible"], r["susp_60"]) for r in c] == [(79, 0, 1)]
    late = _alpha_series(120, fwd_at={80: 0.9})
    c = bl.alpha_cases(_alpha_rows({"CCCC": late}), "x", rules=RULES_T)
    assert [(r["sessions_before"], r["visible"]) for r in c] == [(80, 1)]
    for bad in (rows.drop(columns="a_gap"), pd.concat([rows, rows.iloc[:1]])):
        try:
            bl.alpha_cases(bad, "x", rules=RULES_T)
        except ValueError:
            continue
        raise AssertionError("missing columns / duplicate rows must be refused")


def test_broker_lift_hand_example():
    brokers = ["XL"] * 4 + ["AK"] * 4 + [None, "CC", "XL", "XL"]
    rows = pd.DataFrame({
        "date": [f"d{i}" for i in range(12)], "ticker": "T",
        "eligible": [True] * 10 + [False, True],
        "hold_60": [0.1] * 11 + [np.nan],
        "fwd_oo_60": [np.nan] * 12,          # guarded return: not the base any more
        "a_broker": brokers,
    })
    # base: the first 10 rows (row 10 is ineligible, row 11 has no outcome).
    # Only visible cases count: the AK and CC ones are invisible (and one has
    # no flag at all, which is not "visible").
    cases = [{"top_broker": "XL", "visible": 1}, {"top_broker": "XL", "visible": True},
             {"top_broker": None, "visible": 1}, {"top_broker": "AK", "visible": 0},
             {"top_broker": "CC", "visible": np.nan}, {"top_broker": "CC"}]
    lift = {r["broker"]: r for r in bl.broker_lift(rows, cases, "2026-09-19")}
    assert list(lift) == ["AK", "CC", "XL"], "a broker needs a code: None gives no row"
    xl = lift["XL"]
    assert (xl["n_cases_top"], xl["n_rows_top"]) == (2, 4)
    assert same(xl["case_share"], 2 / 3) and same(xl["row_share"], 4 / 10)
    assert same(xl["lift"], (2 / 3) / (4 / 10))
    assert lift["AK"]["n_cases_top"] == 0 and lift["AK"]["lift"] == 0.0
    assert same(lift["CC"]["row_share"], 0.1)
    assert all(r["as_of"] == "2026-09-19" for r in lift.values())
    # a DataFrame of cases (as loaded back from the db) reads the same
    assert bl.broker_lift(rows, pd.DataFrame(cases), "2026-09-19") ==         bl.broker_lift(rows, cases, "2026-09-19")
    assert bl.broker_lift(rows, [], "x") == [], "no cases: nothing to divide"
    assert bl.broker_lift(rows, cases[3:], "x") == [], "no visible case: nothing to divide"


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
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert names == set(db.TABLES) and len(db.TABLES) == 9
            assert set(db.AS_OF_TABLES) == set(db.TABLES) - {"runs", "live_signals",
                                                              "live_outcomes"}

            row = {"as_of": "2026-09-19", "ruleset": "v1", "rule_id": "R1",
                   "weight": 1.5, "n_dates": 40, "avg_excess_pct": 2.0}
            assert db.insert_rows(conn, "rule_weights", [row]) == 1
            assert db.insert_rows(conn, "rule_weights", [dict(row, weight=0.5)]) == 0
            assert db.load_weights(conn) == {"R1": 1.5}, "the first write is kept"

            nan_row = dict(row, rule_id="R2", avg_excess_pct=np.nan, n_dates=np.int64(0))
            assert db.insert_rows(conn, "rule_weights", [nan_row]) == 1
            assert conn.execute("SELECT avg_excess_pct IS NULL, n_dates FROM rule_weights "
                                "WHERE rule_id = 'R2'").fetchone() == (1, 0)

            for bad in (lambda: db.insert_rows(conn, "sqlite_master", [row]),
                        lambda: db.insert_rows(conn, "rule_weights", [dict(row, wieght=1)]),
                        lambda: db.insert_rows(conn, "rule_weights", [dict(row, as_of=None)]),
                        lambda: db.latest_as_of(conn, "live_signals")):
                try:
                    bad()
                except ValueError:
                    continue
                raise AssertionError("bad table/column/key must be refused")

            run_id = db.start_run(conn, "weekly", "2026-09-19T09:00:00Z")
            assert run_id == "weekly-2026-09-19T09:00:00Z"
            db.finish_run(conn, run_id, status="ok", tickers_ok=1000, tickers_fail=np.int64(3),
                          finished_utc="2026-09-19T09:45:00Z", data_through="2026-09-18")
            assert conn.execute("SELECT status, tickers_ok, tickers_fail FROM runs").fetchone() \
                == ("ok", 1000, 3)
        finally:
            conn.close()


def test_db_as_of_versions():
    conn = sqlite3.connect(":memory:")
    db.ensure_schema(conn)
    assert db.latest_as_of(conn, "rule_weights") is None
    assert db.previous_as_of(conn, "rule_weights") is None
    assert db.load_weights(conn) == {}
    empty = db.load_rule_stats(conn)
    assert empty.empty and list(empty.columns) == table_columns("rule_stats")

    for as_of, w in (("2026-09-12", 1.1), ("2026-09-19", 1.3)):
        db.insert_rows(conn, "rule_weights", [
            {"as_of": as_of, "ruleset": "v1", "rule_id": "R1", "weight": w, "n_dates": 30,
             "avg_excess_pct": 1.0}])
        if as_of == "2026-09-12":
            assert db.previous_as_of(conn, "rule_weights") is None
    assert db.latest_as_of(conn, "rule_weights") == "2026-09-19"
    assert db.previous_as_of(conn, "rule_weights") == "2026-09-12"
    assert db.load_weights(conn) == {"R1": 1.3}
    assert db.load_weights(conn, "2026-09-12") == {"R1": 1.1}

    db.insert_rows(conn, "broker_scores", [
        {"as_of": "2026-09-19", "broker": b, "side": "buy", "h": 10, "n_events": 1,
         "n_dates": 1, "n_tickers": 1, "mean_excess": 0.01, "ci_lo": None, "ci_hi": None,
         "shrunk": 0.0, "low_n": 1} for b in ("XL", "AK")])
    assert list(db.load_broker_scores(conn)["broker"]) == ["AK", "XL"]
    assert db.load_profitability(conn).empty

    # the alpha library and broker lift are versioned the same way
    assert db.load_alpha_cases(conn).empty and db.load_broker_lift(conn).empty
    assert list(db.load_alpha_cases(conn).columns) == table_columns("alpha_cases")
    case = {"ticker": "AAAA", "session_date": "2026-03-02", "hold_60": 0.6, "susp_60": 1,
            "sessions_before": 120, "visible": 1, "rules_at_t": "R6", "rules_prior20": "", "top_broker": "XL",
            "top_nl60_adv": 2.4, "top_cost_gap": 0.02, "range60": 0.18, "val20": 1.5e9}
    db.insert_rows(conn, "alpha_cases", [
        dict(case, as_of="2026-09-12"),
        dict(case, as_of="2026-09-19"),
        dict(case, as_of="2026-09-19", ticker="BBBB", hold_60=1.4, top_broker=None)])
    assert db.insert_rows(conn, "alpha_cases", [dict(case, as_of="2026-09-19",
                                                     hold_60=9.9)]) == 0
    latest = db.load_alpha_cases(conn)
    assert list(latest["ticker"]) == ["BBBB", "AAAA"], "largest move first"
    assert list(latest["hold_60"]) == [1.4, 0.6], "the first write is kept"
    assert list(latest["susp_60"]) == [1, 1] and list(latest["visible"]) == [1, 1]
    assert pd.isna(latest["top_broker"].iloc[0]) and latest["rules_prior20"].iloc[1] == ""
    assert len(db.load_alpha_cases(conn, "2026-09-12")) == 1
    assert db.previous_as_of(conn, "alpha_cases") == "2026-09-12"

    lift = {"as_of": "2026-09-19", "broker": "XL", "n_cases_top": 3, "n_rows_top": 40,
            "case_share": 0.3, "row_share": 0.1, "lift": 3.0}
    assert db.insert_rows(conn, "broker_lift", [lift, dict(lift, broker="AK", lift=np.nan)]) == 2
    got = db.load_broker_lift(conn)
    assert list(got["broker"]) == ["AK", "XL"] and pd.isna(got["lift"].iloc[0])
    assert db.latest_as_of(conn, "broker_lift") == "2026-09-19"
    assert db.previous_as_of(conn, "broker_lift") is None
    conn.close()


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
    # With no rules=/primary_h=, the frozen lists are read from broker_rules at
    # call time. A stand-in module proves that path without the real one.
    import types
    fake = types.ModuleType("broker_rules")
    fake.RULES, fake.PRIMARY_H = RULES_T, PH_T
    saved = sys.modules.get("broker_rules")
    sys.modules["broker_rules"] = fake
    try:
        rows = _weight_rows(100, 0.01, "DN")
        w_default = [r["weight"] for r in bl.rule_weights(rows, "as")]
        w_given = [r["weight"] for r in bl.rule_weights(rows, "as", primary_h=PH_T,
                                                         rules=RULES_T)]
        assert w_default == w_given and w_default[1] < 1
        assert bl.primary_status([{"rule_id": "DN", "h": 60, "status": "LOW_N"}]) ==             {"DN": "LOW_N"}
        assert len(bl.rule_stats(rows, "as", ("s", "e"))) == len(RULES_T) * len(H)
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
