"""Experiment #1F Gate-B preparation tests. No model is fitted and nothing is predicted.

Synthetic fixtures pin exact semantics; `test_real_*` tests check the immutable
Gate-A export and the pinned preparation ledgers (skipped if the export is absent).
"""

import copy
import json
import os
import re
import shutil
import sys
import tempfile

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import experiment_1f_evaluation as ev  # noqa: E402
import experiment_1f_features as f  # noqa: E402
import experiment_1f_gate_b_contract as contract  # noqa: E402
from experiment_1f_universe_gate import frame_digest  # noqa: E402

CAL = tuple(f"2026-04-{d:02d}" for d in range(1, 31))
SPEC = {"AAA": {"missing": set(), "invalid": set(), "price_bad": set()},
        "BBB": {"missing": {12}, "invalid": set(), "price_bad": set()},
        "CCC": {"missing": set(), "invalid": {8}, "price_bad": {15}}}


def lots(i):
    """code -> (blot, slot) on session i; PD trades on even sessions only."""
    out = {"KZ": (i + 1, 0), "BB": (0, i % 3 + 1), "XL": (2 + i % 4, 1)}
    if i % 2 == 0:
        out["PD"] = (2, 0)
    return out


def synthetic_inputs():
    axis, broker = [], []
    for ticker, spec in SPEC.items():
        for i, date in enumerate(CAL):
            if i in spec["missing"]:
                continue
            valid = i not in spec["invalid"]
            day = lots(i)
            price = 1000.0 + i
            axis.append({"date": date, "ticker": ticker, "open": price, "high": price * 1.01, "low": price * 0.99,
                         "close": price, "volume": float(100 * sum(b for b, _ in day.values())),
                         "price_valid": i not in spec["price_bad"], "broker_basis_valid": valid})
            if valid:
                for code, (b, s) in day.items():
                    bval, sval = b * 100 * price / 1e9, s * 100 * price / 1e9
                    broker.append({"date": date, "ticker": ticker, "broker_code": code, "nlot": b - s, "blot": b,
                                   "slot": s, "netval": bval - sval, "bval": bval, "sval": sval,
                                   "bavg": price if b else np.nan, "savg": price if s else np.nan})
    axis = pd.DataFrame(axis)
    broker = pd.DataFrame(broker).astype({"nlot": "int64", "blot": "int64", "slot": "int64"})
    panel = axis[axis["price_valid"]].drop(columns=["price_valid", "broker_basis_valid"]).reset_index(drop=True)
    calpos = {d: i for i, d in enumerate(CAL)}
    for k in (1, 3, 5, 10, 20):
        vals = []
        for t, g in panel.groupby("ticker", sort=False):
            p = g["date"].map(calpos).to_numpy()
            present = set(p)
            vals.extend(0.001 * k if all((q - j) in present for j in range(k + 1)) else np.nan for q in p)
        panel[f"lag_{k}"] = vals
    for h in (1, 2, 3, 4, 5):
        panel[f"fwd_oo_{h}"] = 0.002
    panel["gap_1"] = 0.0
    return {"panel": panel, "broker": broker, "axis": axis, "calendar": CAL}


def row(frame, ticker, i):
    hit = frame[(frame["ticker"] == ticker) & (frame["date"] == CAL[i])]
    assert len(hit) == 1, (ticker, i, len(hit))
    return hit.iloc[0]


def total_blot(i):
    return sum(b for b, _ in lots(i).values())


# ── feature identity / validity ────────────────────────────────────────────
def test_absent_code_on_valid_session_is_zero_and_flow_is_exact():
    frame, _ = f.build_features(synthetic_inputs(), "lag0")
    odd, even = row(frame, "AAA", 5), row(frame, "AAA", 4)
    assert odd[f.flow_column("PD", 1)] == 0.0
    assert odd[f.flow_column("KZ", 1)] == 6 / total_blot(5)
    assert even[f.flow_column("PD", 1)] == 2 / total_blot(4)
    assert odd[f.flow_column("BB", 1)] == -(5 % 3 + 1) / total_blot(5)


def test_invalid_session_is_unavailable_not_zero_and_recovers_after_full_lookback():
    frame, _ = f.build_features(synthetic_inputs(), "lag0")
    col1, col3 = f.flow_column("KZ", 1), f.flow_column("KZ", 3)
    assert np.isnan(row(frame, "CCC", 8)[col1])
    for i in (8, 9, 10):
        assert np.isnan(row(frame, "CCC", i)[col3]), i
    assert not np.isnan(row(frame, "CCC", 9)[col1])
    assert not np.isnan(row(frame, "CCC", 11)[col3])
    for name in f.B_ADDED:
        assert np.isnan(row(frame, "CCC", 8)[name]), name


def test_windows_count_global_market_sessions_not_ticker_rows():
    frame, _ = f.build_features(synthetic_inputs(), "lag0")
    col3 = f.flow_column("KZ", 3)
    assert not np.isnan(row(frame, "BBB", 13)[f.flow_column("KZ", 1)])
    assert np.isnan(row(frame, "BBB", 13)[col3])     # ticker rows 10, 11, 13 would be three rows
    assert np.isnan(row(frame, "BBB", 14)[col3])
    assert row(frame, "BBB", 15)[col3] == sum(i + 1 for i in (13, 14, 15)) / sum(total_blot(i) for i in (13, 14, 15))


def test_price_quarantine_keeps_broker_observation_for_later_windows():
    inputs = synthetic_inputs()
    frame, _ = f.build_features(inputs, "lag0")
    assert frame[(frame["ticker"] == "CCC") & (frame["date"] == CAL[15])].empty
    sessions = (14, 15, 16)
    expected = sum(i + 1 for i in sessions) / sum(total_blot(i) for i in sessions)
    assert row(frame, "CCC", 16)[f.flow_column("KZ", 3)] == expected


def test_lag1_cutoff_applies_to_observations_before_any_window():
    base = synthetic_inputs()
    mutated = copy.deepcopy(base)
    hit = mutated["broker"]["date"] == CAL[20]
    mutated["broker"].loc[hit, ["nlot", "blot"]] += 1000
    mutated["broker"].loc[hit, "netval"] += 7.0
    lag1_a, _ = f.build_features(base, "lag1")
    lag1_b, _ = f.build_features(mutated, "lag1")
    lag0_a, _ = f.build_features(base, "lag0")
    lag0_b, _ = f.build_features(mutated, "lag0")
    cols = [c for c in lag1_a.columns if c not in f.KEY and c not in f.A_COLUMNS]
    at20 = lag1_a["date"] == CAL[20]
    pd.testing.assert_frame_equal(lag1_a.loc[at20, cols], lag1_b.loc[at20, cols])
    assert not lag0_a.loc[at20, cols].equals(lag0_b.loc[at20, cols])
    at21 = lag1_a["date"] == CAL[21]
    assert not lag1_a.loc[at21, cols].equals(lag1_b.loc[at21, cols])


def test_lag1_anchor_is_previous_market_session_shared_by_b_c_d():
    frame, info = f.build_features(synthetic_inputs(), "lag1")
    prov = info["provenance"].set_index(f.KEY)
    calpos = {d: i for i, d in enumerate(CAL)}
    anchored = prov["broker_anchor_date"].notna()
    assert all(calpos[a] == calpos[d] - 1 for (_, d), a in prov.loc[anchored, "broker_anchor_date"].items())
    missing_prev = row(frame, "BBB", 13)
    for col in list(f.B_ADDED) + [f.flow_column("KZ", 1)]:
        assert np.isnan(missing_prev[col]), col
    lag0, _ = f.build_features(synthetic_inputs(), "lag0")
    for col in list(f.B_ADDED) + [f.flow_column("KZ", w) for w in f.FLOW_WINDOWS]:
        a, b = row(frame, "AAA", 11)[col], row(lag0, "AAA", 10)[col]
        assert (np.isnan(a) and np.isnan(b)) or a == b, col


def test_correlation_uses_previous_market_session_and_common_codes():
    inputs = synthetic_inputs()
    frame, _ = f.build_features(inputs, "lag0")
    b = inputs["broker"]
    x = b[(b["ticker"] == "AAA") & (b["date"] == CAL[7])].set_index("broker_code")["netval"]
    y = b[(b["ticker"] == "AAA") & (b["date"] == CAL[6])].set_index("broker_code")["netval"]
    common = x.index.intersection(y.index)
    assert abs(row(frame, "AAA", 7)["broker_correlation_1d"] - np.corrcoef(x[common], y[common])[0, 1]) < 1e-12
    assert np.isnan(row(frame, "CCC", 9)["broker_correlation_1d"])   # previous session invalid


def test_family_nesting_counts_and_forbidden_columns():
    codes = ("AA", "BB")
    fam = f.family_columns(f.feature_ledger(codes))
    assert set(fam["A"]) < set(fam["B"]) < set(fam["C"]) < set(fam["D"])
    assert "net_flow_total" not in fam["D"]
    assert len(fam["C"]) - len(fam["B"]) == len(codes) * len(f.FLOW_WINDOWS)
    assert len(fam["D"]) - len(fam["C"]) == len(codes) and f.ACCUM_WINDOW == 60
    for bad in ("fwd_oo_1", "gap_1", "target"):
        try:
            f.assert_no_label_features(fam["A"] + [bad])
        except ValueError:
            continue
        raise AssertionError(bad)


def test_d_is_adv60_units_over_exactly_60_market_sessions():
    cal = tuple(pd.bdate_range("2025-01-01", periods=70).strftime("%Y-%m-%d"))
    axis, broker = [], []
    for i, d in enumerate(cal):
        axis.append({"date": d, "ticker": "ZZZ", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
                     "price_valid": True, "broker_basis_valid": True})
        broker.append({"date": d, "ticker": "ZZZ", "broker_code": "KZ", "nlot": i + 1, "blot": i + 1, "slot": 0,
                       "netval": 0.0, "bval": 0.0, "sval": 0.0, "bavg": 1.0, "savg": np.nan})
        broker.append({"date": d, "ticker": "ZZZ", "broker_code": "BB", "nlot": -1, "blot": 0, "slot": 1,
                       "netval": 0.0, "bval": 0.0, "sval": 0.0, "bavg": np.nan, "savg": 1.0})
    daily = f.broker_daily(pd.DataFrame(axis), pd.DataFrame(broker), cal)
    ok, num, den = f.window_state(daily, f.ACCUM_WINDOW)
    assert not ok[58] and ok[59] and ok[69]
    kz = daily["codes"].index("KZ")
    assert num[69, kz] == sum(range(11, 71)) and den[69] == sum(range(11, 71))


def test_exact_nlot_is_carried_without_reconstruction():
    inputs = synthetic_inputs()
    daily = f.broker_daily(inputs["axis"], inputs["broker"], CAL)
    b = inputs["broker"]
    rows = daily["axis"].reset_index().set_index(f.KEY)["index"].reindex(pd.MultiIndex.from_arrays([b["ticker"], b["date"]]))
    cols = b["broker_code"].map({c: i for i, c in enumerate(daily["codes"])})
    assert (daily["nlot"][rows.to_numpy(), cols.to_numpy()] == b["nlot"].to_numpy()).all()
    assert daily["nlot"].dtype == np.int64


def test_lag1_validity_is_observation_based_on_basis_invalid_decision_sessions():
    inputs = synthetic_inputs()
    lag1, _ = f.build_features(inputs, "lag1")
    lag0, _ = f.build_features(inputs, "lag0")
    c1, c3 = f.flow_column("KZ", 1), f.flow_column("KZ", 3)
    own_invalid = row(lag1, "CCC", 8)          # CCC session 8 is basis-invalid; lag1 consumes only 5..7
    assert own_invalid[c1] == row(lag0, "CCC", 7)[c1] and not np.isnan(own_invalid[c3])
    assert not np.isnan(own_invalid["broker_concentration"])
    assert np.isnan(row(lag1, "CCC", 9)[c1])     # would consume session 8
    for i in (9, 10, 11):
        assert np.isnan(row(lag1, "CCC", i)[c3]), i
    assert not np.isnan(row(lag1, "CCC", 12)[c3])
    assert np.isnan(row(lag1, "CCC", 10)["broker_correlation_1d"])
    assert row(lag1, "CCC", 16)[c1] == 16 / total_blot(15)    # price-quarantined session 15 consumed


def test_b_alignment_placebo_permutes_the_b_vector_jointly_within_date():
    rng = np.random.default_rng(4)
    frame = pd.DataFrame([{"ticker": f"T{j:02d}", "date": d} for d in CAL[:3] for j in range(10)])
    for col in list(f.A_COLUMNS) + list(f.B_ADDED):
        frame[col] = rng.normal(size=len(frame))
    mask = np.ones(len(frame), bool)
    mask[[0, 15]] = False
    a, a2, b = (f.b_alignment_placebo(frame, mask, d) for d in (1, 1, 2))
    pd.testing.assert_frame_equal(a, a2)
    other = [c for c in frame.columns if c not in f.B_ADDED]
    pd.testing.assert_frame_equal(a[other], frame[other])
    cols = list(f.B_ADDED)
    for d in frame.loc[mask, "date"].unique():
        m = mask & (frame["date"] == d).to_numpy()
        assert sorted(map(tuple, frame.loc[m, cols].to_numpy())) == sorted(map(tuple, a.loc[m, cols].to_numpy()))
        rows_before = {tuple(v) for v in frame.loc[m, cols].to_numpy()}
        assert all(tuple(v) in rows_before for v in a.loc[m, cols].to_numpy())   # vectors move whole
    pd.testing.assert_frame_equal(a.loc[~mask, cols], frame.loc[~mask, cols])
    assert not a.loc[mask, cols].equals(b.loc[mask, cols])


def test_all_variant_intersection_divergence_is_detected():
    frame = pd.DataFrame({"ticker": ["T1", "T2"], "date": [CAL[0]] * 2, "a": [1.0, 1.0], "b": [1.0, 1.0],
                          "c": [np.nan, 1.0], "d": [1.0, 1.0]})
    original = f.family_columns
    f.family_columns = lambda ledger: {"A": ["a"], "B": ["a", "b"], "C": ["a", "b", "c"], "D": ["a", "d"]}
    try:
        f.sample_masks(frame, None)
    except AssertionError:
        return
    finally:
        f.family_columns = original
    raise AssertionError("broken nesting / intersection equality was not detected")


# ── samples and splits ─────────────────────────────────────────────────────
def test_sample_masks_are_nested_and_common_equals_d_complete():
    frame, info = f.build_features(synthetic_inputs(), "lag0")
    masks = f.sample_masks(frame, info["ledger"])
    assert (masks["common_ABCD"] == masks["CD"]).all()
    assert not (masks["BC"] & ~masks["AB"]).any() and not (masks["AB"] & ~masks["A_full"]).any()


def test_horizon_binding_and_split_dates():
    for label, split_h, h in (("fwd_oo_2", 1, 1), ("fwd_oo_1", 2, 1)):
        try:
            f.assert_horizon_binding(label, split_h, h)
        except AssertionError:
            continue
        raise AssertionError((label, split_h, h))
    dates = f.split_dates(CAL[3:], CAL, horizon=2)
    assert dates[0] == CAL[3] and dates[-1] == CAL[len(CAL) - 1 - 3]


def test_split_ledger_is_deterministic_and_purges_equal_horizon():
    cal = tuple(pd.bdate_range("2025-01-01", periods=120).strftime("%Y-%m-%d"))
    for h in (1, 5):
        rec1, splits = f.split_ledger(cal, cal, h)
        rec2, _ = f.split_ledger(cal, cal, h)
        assert rec1 == rec2 and rec1["folds"] > 0
        pos = {d: i for i, d in enumerate(cal)}
        for s in splits:
            assert pos[s["eval"][0]] - pos[s["fit"][-1]] - 1 >= h
            assert pos[s["test"][0]] - pos[s["eval"][-1]] - 1 >= h


# ── placebos ───────────────────────────────────────────────────────────────
def test_c_identity_placebo_preserves_declared_marginals_and_destroys_identity():
    daily = f.broker_daily(synthetic_inputs()["axis"], synthetic_inputs()["broker"], CAL)
    p1, p1b, p2 = (f.c_identity_placebo(daily, d) for d in (1, 1, 2))
    valid = daily["valid"]
    assert (p1 == p1b).all() and not (p1 == p2).all()
    assert (np.sort(p1[valid], axis=1) == np.sort(daily["nlot"][valid], axis=1)).all()
    assert (p1.sum(axis=1) == daily["nlot"].sum(axis=1)).all()
    assert (p1[~valid] == daily["nlot"][~valid]).all()
    assert (p1[valid] != daily["nlot"][valid]).any(axis=1).mean() > 0.5
    inputs = synthetic_inputs()
    real, _ = f.build_features(inputs, "lag0")
    placebo, _ = f.build_features(inputs, "lag0", c_placebo_draw=1)
    keep = list(f.A_COLUMNS) + list(f.B_ADDED)
    pd.testing.assert_frame_equal(real[keep], placebo[keep])


def test_d_state_placebo_preserves_same_date_vectors_and_destroys_ticker_link():
    rng = np.random.default_rng(0)
    rows = [{"ticker": f"T{j:02d}", "date": d, "lag_1": float(j)} for d in ("2026-01-01", "2026-01-02") for j in range(12)]
    frame = pd.DataFrame(rows)
    d_cols = [f.accum_column(c) for c in ("AA", "BB", "CC")]
    frame[d_cols] = rng.normal(size=(len(frame), 3))
    mask = np.ones(len(frame), bool)
    a, a2, b = (f.d_state_placebo(frame, mask, d) for d in (5, 5, 6))
    pd.testing.assert_frame_equal(a, a2)
    assert not a[d_cols].equals(b[d_cols])
    pd.testing.assert_frame_equal(a[["ticker", "date", "lag_1"]], frame[["ticker", "date", "lag_1"]])
    for d in frame["date"].unique():
        m = frame["date"] == d
        before = sorted(map(tuple, frame.loc[m, d_cols].to_numpy()))
        after = sorted(map(tuple, a.loc[m, d_cols].to_numpy()))
        assert before == after
    assert (a[d_cols].to_numpy() == frame[d_cols].to_numpy()).all(axis=1).mean() < 0.5


# ── executable policy ──────────────────────────────────────────────────────
def _bar(ticker, i, o, h=None, l=None, c=None):
    return {"ticker": ticker, "date": CAL[i], "open": o, "high": o + 10 if h is None else h,
            "low": o - 10 if l is None else l, "close": o if c is None else c, "volume": 1000.0}


def _single(ticker, i, price):
    return _bar(ticker, i, price, price, price, price)


def _execution_panel():
    """OHLC scenarios; gap_1 / fwd_oo_1 come from Gate A's own add_forward_returns."""
    from price_audit import add_forward_returns
    rows = [_bar("NRM", i, 1000.0 + i) for i in range(6)]
    rows += [_bar("SPU", 0, 1000.0), _single("SPU", 1, 1050.0), _bar("SPU", 2, 1050.0), _bar("SPU", 3, 1050.0)]
    rows += [_bar("SPD", 0, 1000.0), _bar("SPD", 1, 1000.0), _single("SPD", 2, 950.0), _bar("SPD", 3, 960.0),
             _bar("SPD", 4, 960.0)]
    rows += [_bar("SPD2", 0, 1000.0), _bar("SPD2", 1, 1000.0), _single("SPD2", 2, 950.0),
             _single("SPD2", 3, 910.0), _bar("SPD2", 4, 915.0), _bar("SPD2", 5, 915.0)]
    rows += [_bar("GAP", 0, 1000.0), _bar("GAP", 1, 1000.0), _bar("GAP", 3, 1005.0), _bar("GAP", 4, 1005.0)]
    rows += [_bar("BAD", 0, 1000.0), _bar("BAD", 1, 1000.0), _bar("BAD", 2, 1400.0, 1400.0, 990.0, 1000.0),
             _bar("BAD", 3, 1002.0), _bar("BAD", 4, 1002.0)]
    rows += [_bar("MIX", 0, 1000.0), _bar("MIX", 1, 1000.0), _single("MIX", 2, 950.0),
             _bar("MIX", 3, 1400.0, 1400.0, 940.0, 950.0), _single("MIX", 4, 920.0), _bar("MIX", 6, 925.0),
             _bar("MIX", 7, 925.0)]
    rows += [_bar("NOX", 0, 1000.0), _bar("NOX", 1, 1000.0), _single("NOX", 2, 950.0), _single("NOX", 3, 910.0)]
    rows += [_bar("BRK", 0, 1000.0), _bar("BRK", 1, 1000.0), _single("BRK", 2, 950.0),
             _bar("BRK", 3, 2000.0, 2010.0, 1990.0, 2000.0), _bar("BRK", 4, 2000.0)]
    return add_forward_returns(pd.DataFrame(rows), CAL, horizons=(1,), open_anchored=True)


def _outcomes():
    return ev.slot_outcomes(_execution_panel(), CAL).set_index(f.KEY)


def test_entry_single_price_up_is_unfilled_cash_and_only_the_optimistic_view_fills_it():
    out = _outcomes()
    spu = out.loc[("SPU", CAL[0])]
    assert spu["status"] == "UNFILLED_SINGLE_PRICE_UP" and bool(spu["entry_single_price_up"])
    assert spu["return_hold_through"] == 0.0 and spu["return_cash"] == 0.0 and np.isnan(spu["holding_sessions"])
    assert spu["status_ohlc_optimistic"] == "EXIT_H1" and spu["return_ohlc_optimistic"] == 0.0   # 1050 -> 1050
    # a single-price bar BELOW close(T) at entry is buyable and does not block the entry
    assert out.loc[("SPD", CAL[1]), "status"] != "UNFILLED_SINGLE_PRICE_UP"


def test_single_price_diagnostics_never_claim_board_membership():
    out = _outcomes().reset_index()
    names = " ".join(out.columns).lower() + " " + " ".join(ev.STATUSES).lower()
    assert not any(word in names for word in ("locked_up", "limit_lock", "board", "cap_", "special"))
    assert [c for c in out.columns if "near" in c] == [c for c in out.columns if c.startswith("diag_")]
    statement = contract.EXECUTABLE["diagnostics_price_level_proximity_only"]["statement"]
    assert "not evidence of board membership" in statement
    assert "no rule claims" in contract.EXECUTABLE["board_regime"]
    assert "not a legal price-limit determination" in " ".join(contract.LIMITATIONS)


def test_unfilled_pick_is_not_backfilled_by_rank_four():
    out = _outcomes().reset_index()
    day = out[out["date"] == CAL[0]].copy()
    day["score"] = day["ticker"].map({"SPU": 9.0, "NRM": 8.0, "SPD": 7.0}).fillna(0.0)
    picked = ev.select_top_k(day, "score")
    assert list(picked["ticker"]) == ["SPU", "NRM", "SPD"]
    nrm, spd = (day.set_index("ticker").loc[t, "return_hold_through"] for t in ("NRM", "SPD"))
    assert abs(ev.portfolio_return(picked, "HOLD_THROUGH") - (0.0 + nrm + spd) / 3) < 1e-15


def test_ordinary_t2_exit_equals_gate_a_fwd_oo_1():
    panel = _execution_panel()
    out = ev.slot_outcomes(panel, CAL).set_index(f.KEY)
    labels = panel.set_index(f.KEY)
    nrm = out.loc[("NRM", CAL[0])]
    assert nrm["status"] == "EXIT_H1" and nrm["holding_sessions"] == 1 and nrm["exit_date"] == CAL[2]
    assert nrm["return_hold_through"] == labels.loc[("NRM", CAL[0]), "fwd_oo_1"]
    exit_h1 = out["status"] == "EXIT_H1"
    assert (out.loc[exit_h1, "return_hold_through"] == labels.loc[out.index[exit_h1], "fwd_oo_1"]).all()
    entered = labels["gap_1"].notna()
    usable_t2 = labels["fwd_oo_1"].notna()
    assert not (exit_h1 & ~usable_t2.reindex(out.index)).any()
    assert set(out.loc[entered.reindex(out.index) & usable_t2.reindex(out.index), "status"]) <= {
        "EXIT_H1", "EXIT_HOLD_THROUGH", "UNFILLED_SINGLE_PRICE_UP", "UNRESOLVED"}   # blocked T+2 may never clear


def test_open_usability_is_gate_a_open_anchor_validity():
    from price_audit import _open_anchor_valid
    px = _execution_panel().sort_values(f.KEY, kind="mergesort").reset_index(drop=True)
    expected = _open_anchor_valid(px, px.groupby("ticker", sort=False)).fillna(False).to_numpy(bool)
    assert np.array_equal(ev.open_usable(px), expected)
    with open(os.path.join(HERE, "experiment_1f_evaluation.py"), encoding="utf-8") as fh:
        source = fh.read()
    assert not re.search(r"0\.35|0\.25|0\.20|-0\.15|0\.005", source), "evaluation must not redefine Gate-A bands"


def test_blocked_missing_and_unusable_exits_hold_through_to_the_first_usable_open():
    out = _outcomes()
    cases = {  # ticker: (exit session, holding sessions, delay reason, exit open, blocked, unusable)
        "SPD": (3, 2, "t2_single_price_down", 960.0, 1, 0),
        "SPD2": (4, 3, "t2_single_price_down", 915.0, 2, 0),
        "GAP": (3, 2, "t2_missing", 1005.0, 0, 0),
        "BAD": (3, 2, "t2_open_unusable", 1002.0, 0, 1),
        "MIX": (6, 5, "t2_single_price_down", 925.0, 2, 1)}
    for ticker, (exit_i, holding, reason, exit_open, blocked, unusable) in cases.items():
        rec = out.loc[(ticker, CAL[0])]
        assert rec["status"] == "EXIT_HOLD_THROUGH", (ticker, rec["status"])
        assert rec["exit_date"] == CAL[exit_i] and rec["holding_sessions"] == holding, ticker
        assert rec["exit_delay_reason"] == reason, (ticker, rec["exit_delay_reason"])
        assert (rec["blocked_down_exit_attempts"], rec["unusable_open_exit_attempts"]) == (blocked, unusable), ticker
        assert abs(rec["return_hold_through"] - (exit_open / 1000.0 - 1)) < 1e-12, ticker
        assert rec["return_cash"] == 0.0 and np.isnan(rec["return_excluded"]), ticker
    assert out.loc[("SPD", CAL[0]), "status_ohlc_optimistic"] == "EXIT_H1"
    assert out.loc[("SPD2", CAL[0]), "return_ohlc_optimistic"] == 950.0 / 1000.0 - 1


def test_no_usable_exit_or_broken_path_is_unresolved():
    out = _outcomes()
    nox, brk = out.loc[("NOX", CAL[0])], out.loc[("BRK", CAL[0])]
    assert nox["status"] == "UNRESOLVED" and nox["unresolved_reason"] == "no_later_usable_open"
    assert nox["blocked_down_exit_attempts"] == 2
    assert brk["status"] == "UNRESOLVED" and brk["unresolved_reason"] == "path_break"
    for rec in (nox, brk):
        assert rec["return_hold_through"] == 0.0 and rec["return_cash"] == 0.0 and np.isnan(rec["return_excluded"])
        assert np.isnan(rec["holding_sessions"]) and pd.isna(rec["exit_date"])


def test_execution_diagnostics_holding_period_distribution():
    out = ev.slot_outcomes(_execution_panel(), CAL)
    diag = ev.execution_diagnostics(out[out["date"] == CAL[0]], CAL)
    held = diag["holding_sessions_hold_through"]
    assert held["n"] == 5 and held["distribution"] == {"2": 3, "3": 1, "5": 1} and held["max"] == 5.0
    assert diag["hold_through_delay_reason"] == {"t2_missing": 1, "t2_open_unusable": 1, "t2_single_price_down": 3}
    assert diag["unresolved_reason"] == {"path_break": 1, "no_later_usable_open": 1}
    assert diag["status"]["UNFILLED_SINGLE_PRICE_UP"] == 1
    assert sum(diag["status"].values()) == diag["rows"]


def test_execution_rejects_labels_inconsistent_with_gate_a():
    panel = _execution_panel()
    panel.loc[(panel["ticker"] == "SPD") & (panel["date"] == CAL[0]), "fwd_oo_1"] = 0.123
    try:
        ev.slot_outcomes(panel, CAL)
    except AssertionError:
        return
    raise AssertionError("an inconsistent fwd_oo_1 was not rejected")


def test_top3_no_backfill_benchmark_keeps_unfilled_weight_and_pairwise_delta():
    day = pd.DataFrame({"ticker": list("ABCDE"), "score": [5, 4, 3, 2, 1],
                        "return_hold_through": [0.03, 0.0, 0.01, 0.50, -0.02],
                        "return_cash": [0.03, 0.0, 0.0, 0.50, -0.02],
                        "return_excluded": [0.03, 0.0, np.nan, 0.50, -0.02],
                        "status": ["EXIT_H1", "UNFILLED_SINGLE_PRICE_UP", "EXIT_HOLD_THROUGH", "EXIT_H1", "EXIT_H1"]})
    picked = ev.select_top_k(day, "score")
    assert list(picked["ticker"]) == ["A", "B", "C"]
    assert abs(ev.portfolio_return(picked, "HOLD_THROUGH") - 0.04 / 3) < 1e-15
    assert abs(ev.portfolio_return(picked, "CASH") - 0.03 / 3) < 1e-15
    assert abs(ev.benchmark_return(day, "HOLD_THROUGH") - 0.52 / 5) < 1e-15      # unfilled B keeps 1/5 in cash
    assert abs(ev.benchmark_return(day, "EXCLUDED") - 0.51 / 4) < 1e-15          # diagnostic renormalises
    assert abs(ev.daily_top3_excess(day, "score") - (0.04 / 3 - 0.52 / 5)) < 1e-15
    assert abs(ev.portfolio_return(day.head(2), "HOLD_THROUGH") - 0.03 / 3) < 1e-15
    dates = [CAL[0], CAL[1]]
    large = {17: pd.Series([0.02, 0.00], index=dates), 19: pd.Series([0.04, 0.02], index=dates)}
    small = {17: pd.Series([0.01, 0.01], index=dates), 19: pd.Series([0.01, 0.01], index=dates)}
    delta, daily = ev.pairwise_top3_delta(large, small)
    assert np.allclose(daily.to_numpy(), [0.02, 0.0]) and abs(delta - 0.01) < 1e-15
    first, second = ev.period_halves(list(CAL[:7]))
    assert len(first) == 4 and len(second) == 3


# ── labels and statistics ──────────────────────────────────────────────────
def test_rank_label_average_ties_scaling_min_names_and_missing():
    values = [0.01, 0.02, 0.02, 0.03, np.nan, -0.01, 0.0, 0.05, 0.05, 0.05, 0.04]
    rows = [{"ticker": f"T{j:02d}", "date": CAL[0], "fwd_oo_1": v} for j, v in enumerate(values)]
    rows += [{"ticker": f"T{j:02d}", "date": CAL[1], "fwd_oo_1": 0.01 * j} for j in range(5)]
    panel = pd.DataFrame(rows)
    label = ev.rank_label(panel, 1)
    valid = (panel["date"] == CAL[0]) & panel["fwd_oo_1"].notna()
    expected = (panel.loc[valid, "fwd_oo_1"].rank(method="average") - 0.5) / int(valid.sum())
    assert np.allclose(label[valid], expected) and abs(label[valid].mean() - 0.5) < 1e-12
    assert label[valid].between(0, 1, inclusive="neither").all()
    assert label[panel["fwd_oo_1"] == 0.05].nunique() == 1
    assert label[(panel["date"] == CAL[0]) & panel["fwd_oo_1"].isna()].isna().all()
    assert label[panel["date"] == CAL[1]].isna().all()


def test_daily_ic_on_raw_return_equals_ic_on_rank_label():
    rng = np.random.default_rng(1)
    n = 40
    frame = pd.DataFrame({"ticker": [f"T{j}" for j in range(n)] * 3, "date": [CAL[0]] * n + [CAL[1]] * n + [CAL[2]] * n})
    frame["fwd_oo_1"] = np.round(rng.normal(size=3 * n), 1)
    frame.loc[3, "fwd_oo_1"] = np.nan
    frame["score"] = rng.normal(size=3 * n)
    frame.loc[frame["date"] == CAL[2], "score"] = 1.0
    frame.loc[[10, 11, 50], "score"] = np.nan
    frame["rank_label"] = ev.rank_label(frame, 1)
    raw = ev.daily_spearman_ic(frame, "score", "fwd_oo_1")
    ranked = ev.daily_spearman_ic(frame, "score", "rank_label")
    assert np.allclose(raw.to_numpy(), ranked.to_numpy(), atol=1e-12)
    day = frame[(frame["date"] == CAL[1])].dropna(subset=["score", "fwd_oo_1"])
    assert abs(raw[CAL[1]] - day["score"].rank().corr(day["fwd_oo_1"].rank())) < 1e-12
    assert raw[CAL[2]] == 0.0
    small = frame[frame["date"] == CAL[0]].head(9)
    assert ev.daily_spearman_ic(small, "score", "fwd_oo_1").empty


def test_bootstrap_deterministic_and_basic_interval_agrees_with_pvalue():
    results = {}
    for name in ("positive", "null"):
        x = np.random.default_rng(5).normal(0.02, 0.05, size=180)
        if name == "null":
            x = x - x.mean() + 1e-4
        b1 = ev.circular_block_bootstrap_means(x, 10, 4000, ev.bootstrap_rng(0, 10))
        b2 = ev.circular_block_bootstrap_means(x, 10, 4000, ev.bootstrap_rng(0, 10))
        assert np.array_equal(b1, b2)
        theta = float(x.mean())
        lo, hi = ev.basic_bootstrap_ci(theta, b1)
        p = ev.basic_bootstrap_pvalue(theta, b1)
        assert (lo > 0 or hi < 0) == (p <= 0.05), (name, lo, hi, p)
        results[name] = p
    assert results["positive"] <= 0.05 < results["null"]


def test_bootstrap_pvalue_is_symmetric_for_negative_effects():
    # PR #41 review (P1): with signed thresholds a negative estimate counted the
    # centre of the bootstrap distribution on both sides and returned p = 1 even
    # when the basic interval excluded zero.
    x = np.random.default_rng(5).normal(0.02, 0.05, size=180)
    boot = ev.circular_block_bootstrap_means(x, 10, 4000, ev.bootstrap_rng(0, 10))
    theta = float(x.mean())
    neg_x, neg_boot, neg_theta = -x, -boot, -theta
    lo, hi = ev.basic_bootstrap_ci(neg_theta, neg_boot)
    p_neg = ev.basic_bootstrap_pvalue(neg_theta, neg_boot)
    assert hi < 0 and p_neg <= 0.05, (lo, hi, p_neg)
    assert p_neg == ev.basic_bootstrap_pvalue(theta, boot)        # mirror image, same evidence
    null = neg_x - neg_x.mean() - 1e-4
    null_boot = ev.circular_block_bootstrap_means(null, 10, 4000, ev.bootstrap_rng(0, 10))
    null_theta = float(null.mean())
    lo, hi = ev.basic_bootstrap_ci(null_theta, null_boot)
    p_null = ev.basic_bootstrap_pvalue(null_theta, null_boot)
    assert lo < 0 < hi and p_null > 0.05 and p_null < 1.0, (lo, hi, p_null)


def test_holm_step_down():
    out = ev.holm({"B-A": 0.01, "C-B": 0.04, "D-C": 0.03})
    assert out["B-A"][1] and not out["D-C"][1] and not out["C-B"][1]
    assert abs(out["B-A"][0] - 0.03) < 1e-12 and abs(out["D-C"][0] - 0.06) < 1e-12 and abs(out["C-B"][0] - 0.06) < 1e-12
    assert all(v[1] for v in ev.holm({"B-A": 0.01, "C-B": 0.02, "D-C": 0.03}).values())


def test_placebo_statistics_and_mutually_exclusive_outcomes():
    null = np.arange(50) / 50.0
    stats = ev.placebo_statistics(0.95, null)
    assert abs(stats["q95"] - np.quantile(null, 0.95)) < 1e-15 and stats["exceeds_q95"]
    assert abs(stats["p_empirical"] - 3 / 51) < 1e-15
    rng = np.random.default_rng(9)
    for _ in range(3000):
        primary, obs = rng.normal(0, 1), rng.normal(0, 1)
        placebo = ev.placebo_statistics(obs, rng.normal(0, 1, 50))
        seeds, halves = list(rng.normal(0.5, 1, 5)), list(rng.normal(0.5, 1, 2))
        category, cond = ev.classify_increment(primary, bool(rng.random() < 0.5), obs, placebo, seeds, halves,
                                               rng.normal(), rng.normal())
        reject = primary <= 0 or obs <= placebo["median"]
        if reject:
            assert category == "REJECT_FOR_NOW"
        elif all(cond.values()):
            assert category == "GRADUATE_TOWARD_SPECTRA"
        else:
            assert category == "SENSITIVITY_ONLY"
    good = ev.placebo_statistics(1.0, null)
    assert ev.classify_increment(0.01, True, 1.0, good, [.1] * 5, [.1, .1], 0.0, 0.0)[0] == "GRADUATE_TOWARD_SPECTRA"
    assert ev.classify_increment(0.01, True, 1.0, good, [.1] * 4 + [-.1], [.1, .1], 0.0, 0.0)[0] == "SENSITIVITY_ONLY"
    assert ev.classify_increment(0.01, True, 1.0, good, [.1] * 5, [.1, .1], 0.0, -0.01)[0] == "SENSITIVITY_ONLY"
    assert ev.classify_increment(0.01, True, 0.2, good, [.1] * 5, [.1, .1], 0.0, 0.0)[0] == "REJECT_FOR_NOW"
    assert ev.classify_price_core(0.01, (0.001, 0.02), 0.01, (0.001, 0.02), [.1] * 5, [.1, .1], [.1] * 5, [.1, .1]) \
        == "GRADUATE_TOWARD_SPECTRA"
    assert ev.classify_price_core(-0.01, (-0.02, 0.0), 0.01, (0.001, 0.02), [.1] * 5, [.1, .1], [.1] * 5, [.1, .1]) \
        == "REJECT_FOR_NOW"
    assert abs(ev.minimum_detectable_effect(0.01, 0.05) - (1.959963984540054 + 0.8416212335729143) * 0.01) < 1e-12


# ── contract ───────────────────────────────────────────────────────────────
def test_contract_status_follows_pending_decisions_and_assert_frozen_refuses_drafts():
    body = contract.contract_body()
    assert body["contract_version"] == "gate_b_contract_v2"
    assert body["status"] == (contract.DRAFT if body["pending_decisions"] else contract.READY)
    assert contract.contract_sha256(body) == contract.contract_sha256(copy.deepcopy(body))
    ready = dict(copy.deepcopy(body), pending_decisions={}, status=contract.READY)
    assert contract.assert_frozen(ready) == contract.contract_sha256(ready)
    draft = dict(copy.deepcopy(body), pending_decisions={"x": "y"}, status=contract.DRAFT)
    try:
        contract.assert_frozen(draft)
    except contract.ContractNotFrozen:
        pass
    else:
        raise AssertionError("a contract with pending decisions passed assert_frozen")


def test_v1_contract_is_preserved_as_provenance_and_documents_are_immutable():
    v1 = contract.PROVENANCE["supersedes"]
    path = os.path.join(HERE, v1["document"])
    if os.path.exists(path):
        assert contract.file_sha256(path) == v1["document_sha256"]
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)["contract_sha256"] == v1["canonical_sha256"]
    assert contract.PROVENANCE["model_results_observed_before_revision"] is False
    assert v1["canonical_sha256"] != contract.contract_sha256()
    tmp = tempfile.mkdtemp()
    try:
        written = contract.write_contract(tmp)
        digest = contract.file_sha256(written)
        assert contract.write_contract(tmp) == written and contract.file_sha256(written) == digest
        with open(written, "a", encoding="utf-8") as fh:
            fh.write(" ")
        try:
            contract.write_contract(tmp)
        except contract.ContractNotFrozen:
            pass
        else:
            raise AssertionError("an existing contract document was overwritten")
    finally:
        shutil.rmtree(tmp)


def test_contract_pins_decisions_and_references_gate_a():
    assert contract.GATE_A["frozen_commit"] == "b31c2b6eb80ea3ea0c469de0460086a66f974856"
    assert contract.EXPORT["digests"]["panel_semantic"]["value"] == contract.GATE_A["panel_digest"]
    assert contract.EXPORT["digests"]["broker_semantic"]["value"] == contract.GATE_A["broker_digest"]
    assert contract.SEEDS["model"] == list(f.MODEL_SEEDS) == [17, 19, 23, 29, 31]
    assert contract.BROKER_TIMING_ARMS["graduation_arm"] == "lag1"
    assert "not historically PIT-proven" in contract.BROKER_TIMING_ARMS["lag1"]["interpretation"]
    assert contract.FAMILIES["counts"] == {"A": 7, "B": 12, "C": 477, "D": 570}
    assert contract.MODEL["objective"] == "reg:squarederror on rank_label_h"
    assert contract.LABEL["ties"] == "average rank" and contract.LABEL["min_names_per_date"] == ev.MIN_LABEL_NAMES
    assert contract.STATISTICS["bootstrap"]["replicates"] == ev.BOOTSTRAP["replicates"] == 10000
    assert set(contract.PLACEBOS) >= {"B_alignment", "C_identity", "D_state"} and contract.PLACEBOS["draws_per_family"] == 50
    assert contract.EXECUTABLE["views"]["EXCLUDED"].startswith("DIAGNOSTIC ONLY")
    assert contract.EXECUTABLE["views"]["HOLD_THROUGH"].startswith("PRIMARY")
    assert set(contract.EXECUTABLE["views"]) == set(ev.VIEWS) and set(contract.EXECUTABLE["statuses"]) == set(ev.STATUSES)
    assert "none" in contract.PIT_ELIGIBILITY_SENSITIVITY["liquidity_floor"]


def test_every_export_digest_has_exactly_one_meaning():
    digests = contract.EXPORT["digests"]
    values = [spec["value"] for spec in digests.values()]
    assert len(values) == len(set(values))
    assert digests["lot_content_canonical"]["value"] == "de23c817da1e080c"
    assert digests["basis_invalid_sessions"]["count"] == 1425
    assert digests["basis_quarantined_broker_rows"]["count"] == 55404
    assert all("meaning" in spec for spec in digests.values())


def test_file_and_logical_pins_detect_tampering():
    tmp = tempfile.mkdtemp()
    try:
        frame = pd.DataFrame({"date": ["2026-01-02"] * 2, "ticker": ["AAAA"] * 2, "broker_code": ["BB", "KZ"],
                              "nlot": [5, -5], "blot": [5, 0], "slot": [0, 5],
                              "netval": [0.1, -0.1], "bval": [0.1, 0.0], "sval": [0.0, 0.1]})
        path = os.path.join(tmp, "broker.parquet")
        frame.to_parquet(path, index=False)
        lots_cols = ["date", "ticker", "broker_code", "nlot", "blot", "slot"]
        spec = {"lot": {"file": "broker.parquet", "columns": lots_cols, "value": frame_digest(frame, lots_cols)}}
        pin = contract.file_sha256(path)
        assert contract.verify_file_pins({"broker.parquet": pin}, tmp) == []
        assert contract.verify_logical_digests(spec, tmp, frame_digest)[0] == []
        frame.assign(nlot=[6, -5], blot=[6, 0]).to_parquet(path, index=False)
        assert contract.verify_file_pins({"broker.parquet": pin}, tmp) == ["sha256 mismatch broker.parquet"]
        assert contract.verify_logical_digests(spec, tmp, frame_digest)[0][0].startswith("lot mismatch")
    finally:
        shutil.rmtree(tmp)


def test_no_training_or_prediction_in_preparation_modules():
    pattern = re.compile(r"^\s*(import xgboost|from xgboost)|\.fit\(|\.predict\(|XGBRegressor\(", re.M)
    for name in ("experiment_1f_features.py", "experiment_1f_evaluation.py", "experiment_1f_gate_b_contract.py"):
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            assert not pattern.search(fh.read()), name


def test_retail_codes_match_frozen_definition():
    from walk_forward_backtest import RETAIL_BROKERS
    assert set(RETAIL_BROKERS) == set(f.RETAIL_BROKERS)


# ── real export (immutable Gate-A frames) ──────────────────────────────────
_REAL = {}


def _real():
    if not os.path.exists(os.path.join(f.EXPORT_DIR, "export_manifest.json")):
        return None
    if not _REAL:
        inputs = f.load_inputs()
        daily = f.broker_daily(inputs["axis"], inputs["broker"], inputs["calendar"])
        _REAL.update(inputs=inputs, daily=daily,
                     lag0=f.build_features(inputs, "lag0", daily=daily),
                     lag1=f.build_features(inputs, "lag1", daily=daily))
    return _REAL


def test_real_export_reproduces_gate_a_identities():
    if _real() is None:
        return
    assert contract.verify_inputs(recompute_logical=True) == []


def test_real_preparation_ledgers_match_pins_and_are_deterministic():
    real = _real()
    if real is None:
        return
    first = f.preparation_ledgers(real["inputs"])
    assert first == contract.load_pins()
    assert f.preparation_ledgers(real["inputs"])["arms"]["lag1"]["feature_value_digest"] == first["arms"]["lag1"]["feature_value_digest"]


def test_real_exact_nlot_windows_equal_source_sums():
    real = _real()
    if real is None:
        return
    inputs, daily = real["inputs"], real["daily"]
    frame = real["lag0"][0]
    by_ticker = {t: g.set_index(["date", "broker_code"]) for t, g in inputs["broker"].groupby("ticker")}
    rid = daily["axis"].reset_index().set_index(f.KEY)["index"]
    rng = np.random.default_rng(17)
    col = f.flow_column("KZ", 20)
    candidates = np.flatnonzero(frame[col].notna().to_numpy())
    for r in rng.choice(candidates, size=150, replace=False):
        t, d = frame.at[r, "ticker"], frame.at[r, "date"]
        p = daily["pos"][rid[(t, d)]]
        dates = daily["calendar"][p - 19:p + 1]
        g = by_ticker[t]
        window = g[g.index.get_level_values("date").isin(dates)]
        num = int(window.xs("KZ", level="broker_code")["nlot"].sum()) if "KZ" in window.index.get_level_values(1) else 0
        den = int(window["blot"].sum())
        assert frame.at[r, col] == num / den, (t, d)


def test_real_no_broker_feature_consumes_an_invalid_observation():
    real = _real()
    if real is None:
        return
    inputs, daily = real["inputs"], real["daily"]
    axis = inputs["axis"]
    invalid = set(zip(axis.loc[~axis["broker_basis_valid"], "ticker"], axis.loc[~axis["broker_basis_valid"], "date"]))
    present = set(zip(axis["ticker"], axis["date"]))
    counts = {}
    for arm in ("lag0", "lag1"):
        frame, info = real[arm]
        on_invalid = np.array([(t, d) in invalid for t, d in zip(frame["ticker"], frame["date"])])
        broker_cols = list(f.B_ADDED) + [f.flow_column(c, 1) for c in daily["codes"]]
        counts[arm] = int(frame.loc[on_invalid, broker_cols].notna().any(axis=1).sum())
        prov = info["provenance"]
        rng = np.random.default_rng(3)
        d_col = f.accum_column("KZ")
        rows = np.flatnonzero(frame[d_col].notna().to_numpy())
        calpos = {d: i for i, d in enumerate(daily["calendar"])}
        for r in rng.choice(rows, size=200, replace=False):
            t = frame.at[r, "ticker"]
            q = calpos[prov.at[r, "broker_anchor_date"]]
            assert q <= calpos[frame.at[r, "date"]] - f.TIMING_ARMS[arm]
            for s in daily["calendar"][q - 59:q + 1]:
                assert (t, s) in present and (t, s) not in invalid, (arm, t, s)
    assert counts == {"lag0": 0, "lag1": 7}, counts
    frame, info = real["lag1"]
    calpos = {d: i for i, d in enumerate(daily["calendar"])}
    on_invalid = np.array([(t, d) in invalid for t, d in zip(frame["ticker"], frame["date"])])
    rows = np.flatnonzero(on_invalid & frame[list(f.B_ADDED) + [f.flow_column(c, 1) for c in daily["codes"]]]
                          .notna().any(axis=1).to_numpy())
    assert len(rows) == 7
    for r in rows:
        t, anchor = frame.at[r, "ticker"], info["provenance"].at[r, "broker_anchor_date"]
        q = calpos[anchor]
        assert q == calpos[frame.at[r, "date"]] - 1
        for w in f.FLOW_WINDOWS + (f.ACCUM_WINDOW,):
            col = f.flow_column("KZ", w) if w != f.ACCUM_WINDOW else f.accum_column("KZ")
            if not np.isnan(frame.at[r, col]):
                for s in daily["calendar"][q - w + 1:q + 1]:
                    assert (t, s) in present and (t, s) not in invalid, (t, w, s)


def test_real_b_aggregates_match_frozen_definitions():
    real = _real()
    if real is None:
        return
    from walk_forward_backtest import _broker_correlation_1d, _broker_day_aggregates
    inputs = real["inputs"]
    frame = real["lag0"][0].set_index(f.KEY)
    tickers = ["BBCA", "BUMI", "TINS"]
    bf = inputs["broker"][inputs["broker"]["ticker"].isin(tickers)]
    frozen = _broker_day_aggregates(bf).set_index(f.KEY)
    common = frozen.index.intersection(frame.index)
    for name in ("broker_concentration", "n_brokers", "net_buy_ratio", "retail_presence_pct"):
        a, b = frame.loc[common, name].to_numpy(float), frozen.loc[common, name].to_numpy(float)
        both = ~np.isnan(a) & ~np.isnan(b)
        assert (np.isnan(a) == np.isnan(b)).all() and np.allclose(a[both], b[both], rtol=0, atol=1e-12), name
    corr = _broker_correlation_1d(bf).set_index(f.KEY)["broker_correlation_1d"]
    ours = frame.loc[frame.index.intersection(corr.index), "broker_correlation_1d"]
    mask = ours.notna()
    assert mask.sum() > 100
    assert np.allclose(ours[mask].to_numpy(), corr.loc[ours[mask].index].to_numpy(), rtol=0, atol=1e-9)


def test_real_price_features_follow_their_definitions():
    real = _real()
    if real is None:
        return
    panel = real["inputs"]["panel"]
    frame = real["lag0"][0]
    g = panel.groupby("ticker", sort=False)
    rng = np.random.default_rng(11)
    for r in rng.choice(np.flatnonzero(frame["log_adv20"].notna().to_numpy()), size=100, replace=False):
        t, d = frame.at[r, "ticker"], frame.at[r, "date"]
        hist = g.get_group(t).reset_index(drop=True)
        i = int(np.flatnonzero(hist["date"].to_numpy() == d)[0])
        assert abs(frame.at[r, "log_adv20"] - np.log(hist["volume"].iloc[i - 19:i + 1].mean())) < 1e-12
        if not np.isnan(frame.at[r, "volume_ratio"]):
            assert abs(frame.at[r, "volume_ratio"] - hist["volume"].iloc[i] / hist["volume"].iloc[i - 5:i].mean()) < 1e-12


def test_real_execution_diagnostics_match_contract():
    real = _real()
    if real is None:
        return
    inputs = real["inputs"]
    out = ev.slot_outcomes(inputs["panel"], inputs["calendar"])
    diag = ev.execution_diagnostics(out, inputs["calendar"])
    pinned = contract.EXECUTABLE["counts_h1_evaluable"]
    assert diag["rows"] == pinned["rows"]
    assert diag["status"] == {k: pinned[k] for k in ev.STATUSES}, diag["status"]
    assert diag["hold_through_delay_reason"] == pinned["hold_through_delay_reason"]
    assert diag["unresolved_reason"] == pinned["unresolved_reason"]
    for key in ("blocked_single_price_down_exit_attempts", "positions_with_a_blocked_exit_attempt",
                "unusable_open_exit_attempts"):
        assert diag[key] == pinned[key], key
    assert diag["ohlc_optimistic_status"] == pinned["ohlc_optimistic"]
    held, pinned_held = diag["holding_sessions_hold_through"], contract.EXECUTABLE["holding_sessions_hold_through"]
    assert held["distribution"] == pinned_held["distribution"] and held["n"] == pinned_held["n"]
    assert all(abs(held[k] - pinned_held[k]) < 1e-9 for k in ("p50", "p90", "p99", "max"))
    prox = contract.EXECUTABLE["diagnostics_price_level_proximity_only"]["entry_single_price_up"]
    got = diag["diagnostics_price_level_proximity_only"]
    assert got["entry_single_price_up"] == sum(prox.values()) == pinned["UNFILLED_SINGLE_PRICE_UP"]
    assert (got["entry_single_price_up_near_10pct_level"], got["entry_single_price_up_near_ara_level"]) == (
        prox["near_10pct_level"], prox["near_ara_level"])
    assert (out["status"] == "EXIT_HOLD_THROUGH").sum() >= pinned["EXIT_HOLD_THROUGH"]


def test_real_timing_intersection_and_label_ledgers():
    real = _real()
    if real is None:
        return
    pins = contract.load_pins()
    ledger = real["lag0"][1]["ledger"]
    m0 = f.sample_masks(real["lag0"][0], ledger)
    m1 = f.sample_masks(real["lag1"][0], ledger)
    both = f.timing_intersection_masks(m0, m1, real["lag0"][0], real["lag1"][0])
    for name, mask in both.items():
        assert not (mask & ~m0[name]).any() and not (mask & ~m1[name]).any()
        assert int(mask.sum()) == pins["timing_intersection"][name]["keys"]
    for arm in ("lag0", "lag1"):
        assert pins["arms"][arm]["all_variant_intersection_equals_CD"] is True
    assert all(pins["rank_labels"][f"h{h}"]["evaluable_dates_below_min_names"] == 0 for h in f.HORIZONS)


# ── Stage-1 runner and pre-fit execution manifest (no model is fitted) ─────
import contextlib  # noqa: E402
import subprocess  # noqa: E402

import experiment_1f_gate_b as runner  # noqa: E402


@contextlib.contextmanager
def _patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextlib.contextmanager
def _xgboost_forbidden():
    """Any construction of an XGBoost estimator raises."""
    import xgboost

    class Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError("XGBRegressor constructed")

    with _patched(xgboost, "XGBRegressor", Forbidden):
        yield


def _git(root, *args):
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                         cwd=root, capture_output=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.decode().strip()


@contextlib.contextmanager
def _identity_repo():
    """A throwaway git repo holding every identity file (tiny stand-ins) plus an unrelated tracked file."""
    tmp = tempfile.mkdtemp()
    try:
        _git(tmp, "init", "-q")
        for name in runner.IDENTITY_FILES + ("README.md",):
            with open(os.path.join(tmp, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f"# {name}\n")
        _git(tmp, "add", "--", *runner.IDENTITY_FILES, "README.md")
        _git(tmp, "commit", "-q", "-m", "checkpoint")
        manifest = {"committed_head": runner.git_head(tmp), "requires_clean_tracked_tree": True,
                    "execution_code_git_content_sha256": {p: v["git_content_sha256"] for p, v in
                                                          runner.code_identity(runner.IDENTITY_FILES, tmp).items()}}
        yield tmp, manifest
    finally:
        shutil.rmtree(tmp, onexc=lambda fn, path, exc: (os.chmod(path, 0o700), fn(path)))


def test_manifest_git_identity_accepts_the_committed_checkpoint():
    with _identity_repo() as (tmp, manifest):
        assert runner.verify_git_identity(manifest, tmp) == []
        with open(os.path.join(tmp, "untracked_scratch.txt"), "w") as fh:
            fh.write("scratch")
        assert runner.verify_git_identity(manifest, tmp) == []          # untracked files do not dirty the tree


def test_manifest_rejects_wrong_head():
    with _identity_repo() as (tmp, manifest):
        with open(os.path.join(tmp, "README.md"), "a") as fh:
            fh.write("later\n")
        _git(tmp, "commit", "-q", "-am", "later commit")
        assert runner.verify_git_identity(manifest, tmp) == ["HEAD differs from the manifest's committed HEAD"]


def test_manifest_rejects_dirty_tracked_execution_code():
    with _identity_repo() as (tmp, manifest):
        with open(os.path.join(tmp, "experiment_1f_evaluation.py"), "a") as fh:
            fh.write("CASH_RETURN = 0.001\n")
        failures = runner.verify_git_identity(manifest, tmp)
        assert "tracked tree is not clean" in failures
        assert "execution code modified in worktree: experiment_1f_evaluation.py" in failures


def test_manifest_rejects_changed_committed_module_identity_and_incomplete_identity_set():
    with _identity_repo() as (tmp, manifest):
        with open(os.path.join(tmp, "price_audit.py"), "a") as fh:
            fh.write("TOL = 0.01\n")
        _git(tmp, "commit", "-q", "-am", "helper change")
        failures = runner.verify_git_identity(manifest, tmp)
        assert "execution code identity changed: price_audit.py" in failures
        assert "HEAD differs from the manifest's committed HEAD" in failures
    with _identity_repo() as (tmp, manifest):
        partial = copy.deepcopy(manifest)
        del partial["execution_code_git_content_sha256"]["signal_metrics.py"]
        assert "manifest code-identity file set differs from the runner's identity set" in \
            runner.verify_git_identity(partial, tmp)


def test_runner_loads_no_repo_local_module_outside_the_identity_set():
    assert runner.local_modules_outside_identity() == []
    assert runner.local_modules_outside_identity(identity=tuple(p for p in runner.IDENTITY_FILES
                                                                if p != "signal_metrics.py")) == ["signal_metrics.py"]


def _pending_as_listed(failures):
    ready = not contract.PENDING_DECISIONS
    expected = [] if ready else ["contract is not READY_FOR_FIRST_FIT or has pending decisions"]
    return failures == expected


def test_manifest_rejects_changed_contract_pins_and_export():
    manifest = runner._data_identity()
    assert _pending_as_listed(runner.verify_data_identity(manifest))
    with _patched(contract, "LIMITATIONS", contract.LIMITATIONS + ["an edit after the manifest"]):
        assert "contract_canonical_sha256 differs from the manifest" in runner.verify_data_identity(manifest)
    tmp = tempfile.mkdtemp()
    try:
        pins = contract.load_pins()
        pins["feature_ledger_digest"] = "0" * 64
        changed = os.path.join(tmp, "pins.json")
        with open(changed, "w", encoding="utf-8") as fh:
            json.dump(pins, fh)
        with _patched(contract, "PINS_FILE", changed):
            failures = runner.verify_data_identity(manifest)
            assert {"preparation_pins_sha256 differs from the manifest", "feature_ledger_digest differs from the manifest",
                    "contract_canonical_sha256 differs from the manifest"} <= set(failures)
    finally:
        shutil.rmtree(tmp)
    files = copy.deepcopy(contract.EXPORT["files"])
    files["gate_a_panel.parquet"]["sha256"] = "f" * 64
    with _patched(contract, "EXPORT", dict(contract.EXPORT, files=files)):
        assert "export_raw_sha256 differs from the manifest" in runner.verify_data_identity(manifest)
        if _real() is not None:
            assert "sha256 mismatch gate_a_panel.parquet" in contract.verify_inputs(recompute_logical=False)


def test_missing_manifest_and_unverified_tokens_refuse_every_fit():
    tmp = tempfile.mkdtemp()
    try:
        missing = os.path.join(tmp, "gate_b_execution_manifest_v1.json")
        assert runner.verify_execution_manifest(missing) == ["execution manifest does not exist"]
        before = dict(runner.FIT_COUNTER)
        with _xgboost_forbidden():
            for call in (lambda: runner.require_execution_manifest(missing),
                         lambda: runner.execute_stage1(missing),
                         lambda: runner.run_job(None, {}, None),
                         lambda: runner.run_job(None, {}, runner.ExecutionAuthorisation(missing, "0" * 64, "0" * 40))):
                try:
                    call()
                except runner.HardStop:
                    continue
                raise AssertionError("a fit path ran without a verified manifest")
            forged = runner.ExecutionAuthorisation(missing, "0" * 64, "0" * 40)
            runner._ISSUED.add(id(forged))            # even an issued token fails once HEAD/manifest differ
            try:
                runner.run_job(None, {}, forged)
            except runner.HardStop:
                pass
            else:
                raise AssertionError("a stale authorisation was accepted")
            finally:
                runner._ISSUED.discard(id(forged))
        assert runner.FIT_COUNTER == before
        assert not os.path.exists(os.path.join(tmp, "stage1"))
    finally:
        shutil.rmtree(tmp)


def test_establish_manifest_refuses_without_phrase_over_existing_or_on_dirty_tree():
    tmp = tempfile.mkdtemp()
    try:
        existing = os.path.join(tmp, "manifest.json")
        with open(existing, "w") as fh:
            fh.write("{}")
        for args, message in (((("wrong phrase",), {"path": os.path.join(tmp, "new.json")}), "confirmation"),
                              (((runner.CONFIRMATION_PHRASE,), {"path": existing}), "never overwritten")):
            try:
                runner.establish_execution_manifest(*args[0], **args[1])
            except runner.HardStop as exc:
                assert message in str(exc)
            else:
                raise AssertionError("manifest establishment did not refuse")
        with open(existing) as fh:
            assert fh.read() == "{}"
    finally:
        shutil.rmtree(tmp)
    with _identity_repo() as (repo, _):
        with open(os.path.join(repo, "experiment_1f_features.py"), "a") as fh:
            fh.write("# edit\n")
        target = os.path.join(repo, "manifest.json")
        try:
            runner.establish_execution_manifest(runner.CONFIRMATION_PHRASE, path=target, root=repo)
        except runner.HardStop as exc:
            assert "tracked tree is not clean" in str(exc)
        else:
            raise AssertionError("manifest established on a dirty tracked tree")
        assert not os.path.exists(target)


def test_fit_path_is_guarded_and_sets_every_registered_parameter():
    import inspect
    source = inspect.getsource(runner)
    body = inspect.getsource(runner.run_job)
    assert source.count(".fit(") == body.count(".fit(") == 1 and source.count(".predict(") == body.count(".predict(") == 1
    assert body.split("\n")[1].strip() == "_check_authorisation(authorisation)"
    assert body.index("_check_authorisation(authorisation)", body.index("for number")) < body.index(".fit(")
    params = runner.model_params(19)
    assert params["random_state"] == 19 and params["objective"] == "reg:squarederror" and params["eval_metric"] == "rmse"
    assert all(params[k] == v for k, v in contract.MODEL["fixed_params"].items())
    try:
        runner.model_params(42)
    except runner.HardStop:
        pass
    else:
        raise AssertionError("an unregistered seed was accepted")


def test_stage1_plan_is_pinned_and_complete():
    plan = runner.stage1_plan()
    assert plan["digest"] == contract.STAGE1_PLAN["digest"] == runner.stage1_plan()["digest"]
    assert (plan["total_models"], plan["total_fits"]) == (435, 11810) == tuple(contract.STAGE1_PLAN["totals"].values())
    assert list(runner.PREDICTION_COLUMNS) == contract.STAGE1_PLAN["prediction_ledger_columns"]
    jobs = plan["jobs"]
    assert len({j["job_id"] for j in jobs}) == len(jobs)
    placebo = [j for j in jobs if j["category"] == "placebo"]
    assert len(placebo) == 150 and {j["seed"] for j in placebo} == {17}
    assert {(j["placebo_family"], j["sample"], j["variant"]) for j in placebo} == {
        ("B_alignment", "AB", "B"), ("C_identity", "BC", "C"), ("D_state", "CD", "D")}
    assert all({j["placebo_draw"] for j in placebo if j["placebo_family"] == fam} == set(range(50))
               for fam in f.PLACEBO_STREAM)
    confirmatory = [j for j in jobs if j["category"] == "confirmatory"]
    assert len(confirmatory) == 35 and {j["arm"] for j in confirmatory} == {"lag1"} and {j["horizon"] for j in confirmatory} == {1}
    ids = {j["job_id"] for j in jobs}
    for category, arm, sample, small, large, horizon in runner.sensitivity_specs():
        for variant in [v for v in (small, large) if v]:
            assert all(runner._key(category, sample, variant, s, horizon=horizon, arm=arm) in ids for s in f.MODEL_SEEDS)


def _synthetic_results(rng):
    dates = [f"2026-{m:02d}-{d:02d}" for m in (1, 2, 3, 4) for d in range(1, 29)][:90]
    idx = pd.Index(dates)
    results = {}
    for job in runner.stage1_plan()["jobs"]:
        shift = {"A": 0.00, "B": 0.01, "C": 0.015, "D": 0.012}[job["variant"]]
        ic = pd.Series(rng.normal(0.02 + shift, 0.05, len(idx)), index=idx)
        top = {v: pd.Series(rng.normal(0.001, 0.01, len(idx)), index=idx) for v in ev.VIEWS} \
            if job["category"] != "placebo" and job["horizon"] == 1 else {}
        results[job["job_id"]] = {"daily_ic": ic, "top3": top, "execution": {}}
    return results


def test_graduation_and_sensitivity_assembly_on_synthetic_results_is_deterministic_and_exclusive():
    first = runner.graduation_report(_synthetic_results(np.random.default_rng(5)))
    second = runner.graduation_report(_synthetic_results(np.random.default_rng(5)))
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)
    assert set(first["increments"]) == {"B-A", "C-B", "D-C"}
    allowed = {"REJECT_FOR_NOW", "GRADUATE_TOWARD_SPECTRA", "SENSITIVITY_ONLY"}
    assert all(rec["category"] in allowed for rec in first["increments"].values()) and first["A"]["category"] in allowed
    sens = runner.sensitivity_report(_synthetic_results(np.random.default_rng(5)))
    assert len(sens) == len(runner.sensitivity_specs()) + 3
    assert all("category" not in rec for rec in sens.values())


def test_dry_run_resolves_the_full_plan_with_zero_fits_and_predictions():
    if _real() is None:
        return
    calls = []

    def verified(**kwargs):              # ledger recomputation is covered by the real-ledger test and the CLI dry run
        calls.append(kwargs)
        return []

    before = dict(runner.FIT_COUNTER)
    with _xgboost_forbidden(), _patched(contract, "verify_inputs", verified):
        report = runner.dry_run()
    assert calls == [{"recompute_logical": True, "recompute_ledgers": True}]
    assert report["plan_digest"] == contract.STAGE1_PLAN["digest"]
    assert (report["jobs_resolved"], report["total_models"], report["total_fits"]) == (435, 435, 11810)
    assert (report["models_constructed"], report["fits"], report["predictions"]) == (0, 0, 0)
    assert runner.FIT_COUNTER == before
    assert report["feature_columns_per_variant"] == contract.FAMILIES["counts"]
    assert {k: v["draws"] for k, v in report["placebo_schedule"].items()} == {"B_alignment": 50, "C_identity": 50,
                                                                            "D_state": 50}
    assert report["row_signatures_checked"] == len(report["row_counts"]) and report["row_signatures_checked"] > 0
    assert all(min(c["fit_rows"], c["eval_rows"], c["test_rows"]) > 0 for c in report["row_counts"].values())
    diag = report["execution_diagnostics"]
    assert diag["status"] == {k: contract.EXECUTABLE["counts_h1_evaluable"][k] for k in ev.STATUSES}
    assert report["prediction_ledger_schema"] == list(runner.PREDICTION_COLUMNS)


def main():
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
