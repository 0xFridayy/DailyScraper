"""Experiment #1F Gate-B feature, sample, split and placebo layer.

PREPARATION ONLY. This module builds feature matrices, analysis key sets, split
ledgers and placebo transformations from the immutable Gate-A export. It never
fits a model and never produces a prediction.

It deliberately does NOT inherit #1E's broker builder
(ml_v2_experiment_1.build_broker_identity_features): that builder recovers lots
from price, zero-fills invalid sessions, counts windows on each ticker's own
rows, resets inventory at holes and drops the aggregate block from C/D.

Semantics (pinned in experiment_1f_gate_b_contract):

  * Broker windows run on the GLOBAL market calendar. A window of w sessions
    ending at calendar session q exists only if the ticker has an axis row at
    every one of the w sessions and every one of them is a valid broker
    observation (broker rows present AND Gate-A broker_basis_valid).
  * Inside a valid session an absent broker code contributes 0 lots. An invalid
    or unobserved session makes every window containing it unavailable (NaN).
  * Price quarantine is a price-domain fact only. A broker-valid observation on
    a price-quarantined session is consumed by later windows; that session has
    no decision row because it has no price row.
  * Timing: for decision session T and lag L the latest broker session any
    B/C/D feature may use is calendar session T-L. The anchor session is
    resolved on the calendar first and every window is formed from
    observations at or before it, so no lag is ever applied by shifting
    ticker rows.
"""

import hashlib
import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(HERE, "backtest_out", "experiment_1f_candidate", "gate_b_inputs")

KEY = ["ticker", "date"]
FLOW_WINDOWS = (1, 3, 5, 10, 20)
ACCUM_WINDOW = 60
TIMING_ARMS = {"lag0": 0, "lag1": 1}
HORIZONS = (1, 2, 3, 4, 5)
CONFIRMATORY_HORIZON = 1
#: identical to walk_forward_backtest.RETAIL_BROKERS (asserted in tests)
RETAIL_BROKERS = frozenset({"XL", "XC", "YP", "PD"})
A_COLUMNS = ("lag_1", "lag_3", "lag_5", "lag_10", "lag_20", "volume_ratio", "log_adv20")
B_ADDED = ("broker_concentration", "n_brokers", "net_buy_ratio", "retail_presence_pct",
           "broker_correlation_1d")
FORBIDDEN_FEATURE_PREFIXES = ("fwd_", "gap_", "target", "label", "y_")
PAIRS = (("A", "B"), ("B", "C"), ("C", "D"))
SPLIT_PARAMS = {"train_min": 30, "test_window": 6, "eval_fraction": 0.20, "embargo": 0}
MIN_DAILY_IC_NAMES = 10
#: 17 (digest of record) and the next four primes. Chosen before any fit.
MODEL_SEEDS = (17, 19, 23, 29, 31)
PLACEBO_DRAWS = 50
PLACEBO_STREAM = {"B_alignment": 2, "C_identity": 3, "D_state": 4}
PLACEBO_SEED = 17   # placebo nulls are computed under model seed 17 only


def flow_column(code, window):
    return f"broker_{code}_flow_{window}d"


def accum_column(code):
    return f"broker_{code}_accum_flow_{ACCUM_WINDOW}d"


# ── inputs ─────────────────────────────────────────────────────────────────
def load_inputs(directory=EXPORT_DIR, verify=True):
    """Load the immutable export. `verify` re-hashes the pinned bytes first."""
    if verify:
        import experiment_1f_gate_b_contract as contract
        failures = contract.verify_file_pins(
            {name: spec["sha256"] for name, spec in contract.EXPORT["files"].items()}, directory)
        if failures:
            raise RuntimeError(f"Gate-B inputs do not match the contract: {failures}")
    with open(os.path.join(directory, "gate_a_calendar.json"), encoding="utf-8") as fh:
        calendar = tuple(json.load(fh)["calendar"])
    return {"panel": pd.read_parquet(os.path.join(directory, "gate_a_panel.parquet")),
            "broker": pd.read_parquet(os.path.join(directory, "gate_a_broker.parquet")),
            "axis": pd.read_parquet(os.path.join(directory, "gate_a_session_axis.parquet")),
            "calendar": calendar}


# ── broker observations on the session axis ───────────────────────────────
def broker_daily(axis, broker, calendar, codes=None):
    """Dense per-session broker observations on the original session axis."""
    axis = axis.sort_values(KEY, kind="mergesort").reset_index(drop=True)
    if axis.duplicated(KEY).any() or broker.duplicated(["date", "ticker", "broker_code"]).any():
        raise ValueError("duplicate keys in axis or broker frame")
    calpos = {d: i for i, d in enumerate(calendar)}
    pos = axis["date"].map(calpos)
    if pos.isna().any():
        raise ValueError("axis date outside the calendar")
    pos = pos.to_numpy(np.int64)
    tick = axis["ticker"].to_numpy()
    n = len(axis)
    first = np.r_[True, tick[1:] != tick[:-1]]
    start = np.maximum.accumulate(np.where(first, np.arange(n), 0))
    tickers = tuple(pd.unique(tick))
    tid = pd.Series(np.arange(len(tickers)), index=tickers)[tick].to_numpy()
    lookup = np.full((len(tickers), len(calendar)), -1, dtype=np.int64)
    lookup[tid, pos] = np.arange(n)

    codes = tuple(codes) if codes is not None else tuple(sorted(broker["broker_code"].unique()))
    code_index = {c: i for i, c in enumerate(codes)}
    row_of = pd.Series(np.arange(n), index=pd.MultiIndex.from_arrays([axis["ticker"], axis["date"]]))
    rid = row_of.reindex(pd.MultiIndex.from_arrays([broker["ticker"], broker["date"]]))
    if rid.isna().any():
        raise ValueError("broker observation outside the session axis")
    rid = rid.to_numpy(np.int64)
    cid = broker["broker_code"].map(code_index)
    if cid.isna().any():
        raise ValueError("broker code outside the fixed vocabulary")
    cid = cid.to_numpy(np.int64)

    nlot = np.zeros((n, len(codes)), dtype=np.int64)
    nlot[rid, cid] = broker["nlot"].to_numpy(np.int64)
    netval = np.full((n, len(codes)), np.nan)
    netval[rid, cid] = broker["netval"].to_numpy(float)
    blot = np.bincount(rid, weights=broker["blot"].to_numpy(np.int64), minlength=n).astype(np.int64)
    rows = np.bincount(rid, minlength=n)
    observed = rows > 0
    valid = observed & axis["broker_basis_valid"].to_numpy(bool)
    return {"axis": axis, "calendar": tuple(calendar), "codes": codes, "pos": pos, "start": start,
            "tid": tid, "tickers": tickers, "lookup": lookup, "nlot": nlot, "netval": netval,
            "blot": blot, "rows": rows, "observed": observed, "valid": valid}


def _prefix(values):
    return np.concatenate([np.zeros((1,) + values.shape[1:], dtype=values.dtype), np.cumsum(values, axis=0)])


def window_state(daily, window, nlot=None):
    """Per axis row as window END: availability and exact lot sums over `window` sessions."""
    nlot = daily["nlot"] if nlot is None else nlot
    n = len(daily["pos"])
    idx = np.arange(n)
    lo = idx - (window - 1)
    lo_c = np.clip(lo, 0, None)
    ok = lo >= daily["start"]
    ok &= (daily["pos"] - daily["pos"][lo_c]) == (window - 1)
    invalid = _prefix((~daily["valid"]).astype(np.int64))
    ok &= (invalid[idx + 1] - invalid[lo_c]) == 0
    den = _prefix(daily["blot"])
    den = den[idx + 1] - den[lo_c]
    ok &= den > 0
    num = _prefix(nlot)
    num = num[idx + 1] - num[lo_c]
    return ok, num, den


def c_identity_placebo(daily, draw):
    """Permute broker-code identity inside every valid ticker-day, before any window.

    Unit: one valid (ticker, session). The 93-value nlot vector of that session
    is permuted with a permutation drawn independently per session. Preserved
    exactly: the session's multiset of per-code nlot values, its sum, sum_b blot,
    the validity mask, and every A and B input. Destroyed: which broker a column
    denotes, across tickers and across days.
    """
    rng = np.random.default_rng([PLACEBO_SEED, PLACEBO_STREAM["C_identity"], int(draw)])
    order = np.argsort(rng.random(daily["nlot"].shape), axis=1, kind="stable")
    permuted = np.take_along_axis(daily["nlot"], order, axis=1)
    return np.where(daily["valid"][:, None], permuted, daily["nlot"])


def permute_vectors_within_date(frame, columns, mask, stream, draw):
    """Permute the row-VECTOR of `columns` jointly across tickers within each date,
    among `mask` rows (deterministic: rows visited by date then ticker)."""
    rng = np.random.default_rng([PLACEBO_SEED, int(stream), int(draw)])
    out = frame.copy()
    values = frame[columns].to_numpy()
    new = values.copy()
    rows = np.flatnonzero(np.asarray(mask))
    dates = frame["date"].to_numpy()[rows]
    order = np.lexsort((frame["ticker"].to_numpy()[rows], dates))
    rows, dates = rows[order], dates[order]
    boundaries = np.flatnonzero(np.r_[True, dates[1:] != dates[:-1], True])
    for lo, hi in zip(boundaries[:-1], boundaries[1:]):
        group = rows[lo:hi]
        new[group] = values[group[rng.permutation(len(group))]]
    out[columns] = new
    return out


def b_alignment_placebo(frame, mask, draw):
    """B-only aggregate vector permuted jointly across tickers within each date of the
    A-vs-B sample. Preserved: A columns, keys, dates, labels, each date's multiset of
    B vectors (joint covariance of B). Destroyed: ticker <-> aggregate-broker linkage."""
    return permute_vectors_within_date(frame, list(B_ADDED), mask, PLACEBO_STREAM["B_alignment"], draw)


def d_state_placebo(frame, mask, draw):
    """D block permuted jointly across tickers within each date of the C-vs-D sample.
    Preserved: every non-D column, keys, each date's multiset of D vectors.
    Destroyed: the ticker a D vector belongs to."""
    d_cols = [c for c in frame.columns if c.endswith(f"_accum_flow_{ACCUM_WINDOW}d")]
    return permute_vectors_within_date(frame, d_cols, mask, PLACEBO_STREAM["D_state"], draw)


# ── features ───────────────────────────────────────────────────────────────
def price_features(panel):
    """Variant A on the Gate-A panel; no future row enters any column."""
    px = panel.sort_values(KEY, kind="mergesort").reset_index(drop=True)
    g = px.groupby("ticker", sort=False)["volume"]
    vol_ma5 = g.transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    adv20 = g.transform(lambda s: s.rolling(20, min_periods=20).mean())
    with np.errstate(divide="ignore", invalid="ignore"):
        volume_ratio = px["volume"] / vol_ma5
        log_adv20 = np.log(adv20)
    out = px[KEY].copy()
    for lag in ("lag_1", "lag_3", "lag_5", "lag_10", "lag_20"):
        out[lag] = px[lag]
    out["volume_ratio"] = volume_ratio.where(px["lag_5"].notna())
    out["log_adv20"] = log_adv20.where(px["lag_20"].notna())
    return out.replace([np.inf, -np.inf], np.nan)


def _day_aggregates(daily):
    netval = daily["netval"]
    absval = np.nan_to_num(np.abs(netval), nan=0.0)
    total = absval.sum(axis=1)
    k = min(3, absval.shape[1])
    top3 = -np.partition(-absval, k - 1, axis=1)[:, :k].sum(axis=1)
    retail = np.array([c in RETAIL_BROKERS for c in daily["codes"]])
    retail_abs = absval[:, retail].sum(axis=1)
    rows = daily["rows"].astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        agg = {
            "broker_concentration": np.where(total > 0, top3 / total, np.nan),
            "n_brokers": np.where(rows > 0, rows, np.nan),
            "net_buy_ratio": np.where(rows > 0, (np.nan_to_num(netval, nan=0.0) > 0).sum(axis=1) / rows, np.nan),
            "retail_presence_pct": np.where(total > 0, retail_abs / total, np.nan),
        }
    valid = daily["valid"]
    return {name: np.where(valid, values, np.nan) for name, values in agg.items()}


def _correlation_1d(daily, anchor):
    """Pearson correlation of per-code netval between the anchor session and the
    immediately preceding MARKET session, over codes present on both; >= 3 codes."""
    out = np.full(len(anchor), np.nan)
    ok = anchor >= 0
    a = anchor[ok]
    prev_pos = daily["pos"][a] - 1
    b = np.where(prev_pos >= 0, daily["lookup"][daily["tid"][a], np.clip(prev_pos, 0, None)], -1)
    usable = (b >= 0) & daily["valid"][a]
    usable &= np.where(b >= 0, daily["valid"][np.clip(b, 0, None)], False)
    x = daily["netval"][a]
    y = daily["netval"][np.clip(b, 0, None)]
    common = ~np.isnan(x) & ~np.isnan(y) & usable[:, None]
    cnt = common.sum(axis=1)
    xs = np.where(common, x, 0.0)
    ys = np.where(common, y, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        mx = xs.sum(axis=1) / cnt
        my = ys.sum(axis=1) / cnt
        dx = np.where(common, x - mx[:, None], 0.0)
        dy = np.where(common, y - my[:, None], 0.0)
        den = np.sqrt((dx ** 2).sum(axis=1) * (dy ** 2).sum(axis=1))
        corr = np.where((cnt >= 3) & (den > 0), (dx * dy).sum(axis=1) / den, np.nan)
    out[np.flatnonzero(ok)] = corr
    return out


def anchors(daily, decision_keys, lag):
    """Axis row of calendar session T-lag for each decision key (-1 if absent)."""
    row_of = pd.Series(np.arange(len(daily["axis"])),
                       index=pd.MultiIndex.from_arrays([daily["axis"]["ticker"], daily["axis"]["date"]]))
    di = row_of.reindex(pd.MultiIndex.from_arrays([decision_keys["ticker"], decision_keys["date"]]))
    if di.isna().any():
        raise ValueError("decision key outside the session axis")
    di = di.to_numpy(np.int64)
    q = daily["pos"][di] - lag
    return np.where(q >= 0, daily["lookup"][daily["tid"][di], np.clip(q, 0, None)], -1)


def feature_ledger(codes):
    """Ordered column ledger A, B, C, D with family and definition identifiers."""
    ledger = [{"column": c, "family": "A", "kind": "price"} for c in A_COLUMNS]
    ledger += [{"column": c, "family": "B", "kind": "broker_aggregate"} for c in B_ADDED]
    ledger += [{"column": flow_column(code, w), "family": "C", "kind": "exact_nlot_flow_share",
                "window": w, "code": code} for w in FLOW_WINDOWS for code in codes]
    ledger += [{"column": accum_column(code), "family": "D", "kind": "exact_nlot_accum_adv_units",
                "window": ACCUM_WINDOW, "code": code} for code in codes]
    return ledger


def family_columns(ledger):
    fam = {f: [row["column"] for row in ledger if row["family"] == f] for f in "ABCD"}
    return {"A": fam["A"], "B": fam["A"] + fam["B"], "C": fam["A"] + fam["B"] + fam["C"],
            "D": fam["A"] + fam["B"] + fam["C"] + fam["D"]}


def assert_no_label_features(columns):
    bad = [c for c in columns if c.startswith(FORBIDDEN_FEATURE_PREFIXES)]
    if bad:
        raise ValueError(f"label/future columns in a feature matrix: {bad[:5]}")


def build_features(inputs, arm, c_placebo_draw=None, daily=None):
    """Decision-row feature matrix for one timing arm. Returns (frame, provenance)."""
    lag = TIMING_ARMS[arm]
    daily = daily or broker_daily(inputs["axis"], inputs["broker"], inputs["calendar"])
    ledger = feature_ledger(daily["codes"])
    price = price_features(inputs["panel"])
    anchor = anchors(daily, price, lag)
    has = anchor >= 0
    a_safe = np.clip(anchor, 0, None)
    nlot = daily["nlot"] if c_placebo_draw is None else c_identity_placebo(daily, c_placebo_draw)

    blocks = {}
    agg = _day_aggregates(daily)
    for name in B_ADDED[:-1]:
        blocks[name] = np.where(has, agg[name][a_safe], np.nan)
    blocks["broker_correlation_1d"] = _correlation_1d(daily, anchor)

    c_arrays = []
    for w in FLOW_WINDOWS:
        ok, num, den = window_state(daily, w, nlot)
        use = has & ok[a_safe]
        with np.errstate(divide="ignore", invalid="ignore"):
            share = num[a_safe] / den[a_safe][:, None]
        c_arrays.append(np.where(use[:, None], share, np.nan))
    ok, num, den = window_state(daily, ACCUM_WINDOW, nlot)
    use = has & ok[a_safe]
    with np.errstate(divide="ignore", invalid="ignore"):
        adv_units = num[a_safe] / (den[a_safe] / ACCUM_WINDOW)[:, None]
    d_array = np.where(use[:, None], adv_units, np.nan)

    columns = family_columns(ledger)["D"]
    values = np.column_stack([price[list(A_COLUMNS)].to_numpy(float)]
                             + [blocks[name][:, None] for name in B_ADDED] + c_arrays + [d_array])
    frame = pd.concat([price[KEY].reset_index(drop=True),
                       pd.DataFrame(values, columns=columns)], axis=1)
    assert_no_label_features(columns)

    cal = np.array(daily["calendar"], dtype=object)
    anchor_pos = np.where(has, daily["pos"][a_safe], -1)
    provenance = pd.DataFrame({
        "ticker": frame["ticker"], "date": frame["date"],
        "broker_anchor_date": np.where(has, cal[np.clip(anchor_pos, 0, None)], None),
        "broker_earliest_date_accum": np.where(use, cal[np.clip(anchor_pos - (ACCUM_WINDOW - 1), 0, None)], None),
    })
    return frame, {"ledger": ledger, "provenance": provenance, "daily": daily, "anchor": anchor}


# ── samples, splits, ledgers ───────────────────────────────────────────────
def sample_masks(frame, ledger):
    """Complete-case masks for A and each pair; the common intersection == D-complete."""
    fam = family_columns(ledger)
    complete = {f: frame[cols].notna().all(axis=1).to_numpy() for f, cols in fam.items()}
    masks = {"A_full": complete["A"]}
    for small, large in PAIRS:
        masks[f"{small}{large}"] = complete[large]
    masks["common_ABCD"] = complete["A"] & complete["B"] & complete["C"] & complete["D"]
    for inner, outer in (("AB", "A_full"), ("BC", "AB"), ("CD", "BC")):
        if (masks[inner] & ~masks[outer]).any():
            raise AssertionError(f"sample nesting violated: {inner} not within {outer}")
    if not np.array_equal(masks["common_ABCD"], masks["CD"]):
        raise AssertionError("all-variant intersection no longer equals the C-vs-D key set")
    return masks


TIMING_INTERSECTION_SAMPLES = ("A_full", "AB", "BC", "CD")


def timing_intersection_masks(masks_lag0, masks_lag1, frame_lag0, frame_lag1):
    """Keys eligible under BOTH timing arms, per sample (same decision-row order)."""
    if not frame_lag0[KEY].equals(frame_lag1[KEY]):
        raise AssertionError("timing arms do not share the decision-row order")
    return {name: masks_lag0[name] & masks_lag1[name] for name in TIMING_INTERSECTION_SAMPLES}


def split_dates(sample_dates, calendar, horizon):
    """Contiguous calendar slice spanning the sample, keeping sessions whose
    exit open(T+1+h) still lies inside the calendar (purges count market sessions)."""
    calpos = {d: i for i, d in enumerate(calendar)}
    if not len(sample_dates):
        return ()
    lo = min(calpos[d] for d in sample_dates)
    hi = min(max(calpos[d] for d in sample_dates), len(calendar) - 1 - (1 + horizon))
    return tuple(calendar[lo:hi + 1]) if hi >= lo else ()


def assert_horizon_binding(label_column, split_horizon, horizon):
    if label_column != f"fwd_oo_{horizon}" or split_horizon != horizon:
        raise AssertionError(f"horizon binding violated: label={label_column} split_h={split_horizon} h={horizon}")


def split_ledger(sample_dates, calendar, horizon):
    from walk_forward_backtest import make_walk_forward_splits  # frozen split arithmetic, no fitting
    dates = split_dates(sample_dates, calendar, horizon)
    assert_horizon_binding(f"fwd_oo_{horizon}", horizon, horizon)
    splits, report = make_walk_forward_splits(dates, horizon=horizon, **SPLIT_PARAMS)
    payload = {"horizon": horizon, "params": SPLIT_PARAMS, "dates": list(dates),
               "splits": [{k: list(v) for k, v in s.items()} for s in splits]}
    test_dates = sorted({d for s in splits for d in s["test"]})
    return {"digest": sha256_json(payload), "folds": report["n_folds_scored"],
            "folds_skipped": report["n_folds_nominal"] - report["n_folds_scored"],
            "test_dates": len(test_dates), "first_test": test_dates[0] if test_dates else None,
            "last_test": test_dates[-1] if test_dates else None}, splits


def sha256_json(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


def frame_value_digest(frame, columns):
    """Order-sensitive digest: key strings + float64 little-endian bytes, NaN canonical."""
    h = hashlib.sha256()
    h.update("\n".join(frame["ticker"] + " " + frame["date"]).encode("utf-8"))
    h.update("|".join(columns).encode("utf-8"))
    values = np.ascontiguousarray(frame[columns].to_numpy(np.float64))
    values = np.where(np.isnan(values), np.float64("nan"), values).astype("<f8")
    h.update(values.tobytes())
    return h.hexdigest()


def daily_ic_capacity(frame, mask, panel, horizon, test_dates):
    """Dates on which a daily IC is defined for a sample (>= MIN_DAILY_IC_NAMES label-valid keys)."""
    label = panel.set_index(KEY)[f"fwd_oo_{horizon}"]
    keys = frame.loc[mask, KEY]
    valid = label.reindex(pd.MultiIndex.from_frame(keys)).notna().to_numpy()
    counts = keys.loc[valid, "date"].value_counts()
    test = set(test_dates)
    return int(sum(1 for d, c in counts.items() if d in test and c >= MIN_DAILY_IC_NAMES))


def history_eligible(frame, daily, min_sessions=20):
    """PIT history rule: >= min_sessions market sessions since the ticker's first axis session."""
    first_pos = pd.Series(daily["pos"][daily["start"]], index=daily["axis"]["ticker"]).groupby(level=0).min()
    calpos = {d: i for i, d in enumerate(daily["calendar"])}
    t_pos = frame["date"].map(calpos).to_numpy()
    return (t_pos - first_pos.reindex(frame["ticker"]).to_numpy()) >= min_sessions


def preparation_ledgers(inputs):
    """Everything the contract pins about features, samples and splits. No fitting."""
    daily = broker_daily(inputs["axis"], inputs["broker"], inputs["calendar"])
    ledger = feature_ledger(daily["codes"])
    fam = family_columns(ledger)
    result = {"feature_ledger_digest": sha256_json(ledger),
              "family_counts": {f: len(cols) for f, cols in fam.items()},
              "broker_codes": len(daily["codes"]),
              "broker_codes_sha256": hashlib.sha256("\n".join(daily["codes"]).encode()).hexdigest(),
              "arms": {}, "timing_intersection": {}, "rank_labels": {}}

    def sample_record(frame, mask):
        dates = sorted(set(frame.loc[mask, "date"]))
        record = {"keys": int(mask.sum()), "dates": len(dates),
                  "keys_sha256": hashlib.sha256("\n".join(frame.loc[mask, "ticker"] + " " + frame.loc[mask, "date"])
                                                .encode()).hexdigest(),
                  "splits": {}}
        for h in HORIZONS:
            rec, splits = split_ledger(dates, inputs["calendar"], h)
            if h == CONFIRMATORY_HORIZON:
                rec["daily_ic_dates_h1"] = daily_ic_capacity(frame, mask, inputs["panel"], h,
                                                             [d for s in splits for d in s["test"]])
            record["splits"][f"h{h}"] = rec
        return record

    frames, masks = {}, {}
    for arm in TIMING_ARMS:
        frames[arm], _ = build_features(inputs, arm, daily=daily)
        masks[arm] = sample_masks(frames[arm], ledger)
        result["arms"][arm] = {"feature_value_digest": frame_value_digest(frames[arm], fam["D"]),
                               "decision_rows": int(len(frames[arm])),
                               "all_variant_intersection_equals_CD": bool(np.array_equal(masks[arm]["common_ABCD"],
                                                                                        masks[arm]["CD"])),
                               "samples": {name: sample_record(frames[arm], m) for name, m in masks[arm].items()}}
    both = timing_intersection_masks(masks["lag0"], masks["lag1"], frames["lag0"], frames["lag1"])
    result["timing_intersection"] = {name: sample_record(frames["lag1"], m) for name, m in both.items()}

    from experiment_1f_evaluation import MIN_LABEL_NAMES, rank_label
    panel = inputs["panel"].sort_values(KEY, kind="mergesort").reset_index(drop=True)
    calpos = {d: i for i, d in enumerate(inputs["calendar"])}
    for h in HORIZONS:
        label = rank_label(panel, h)
        evaluable = panel["date"].map(calpos) <= len(inputs["calendar"]) - 2 - h
        counts = panel.loc[panel[f"fwd_oo_{h}"].notna()].groupby("date").size()
        short = [d for d in sorted(set(panel.loc[evaluable, "date"])) if counts.get(d, 0) < MIN_LABEL_NAMES]
        frame = panel[KEY].assign(rank_label=label)
        result["rank_labels"][f"h{h}"] = {"digest": frame_value_digest(frame, ["rank_label"]),
                                          "labelled_rows": int(label.notna().sum()),
                                          "evaluable_dates_below_min_names": len(short)}
    return result


if __name__ == "__main__":
    print(json.dumps(preparation_ledgers(load_inputs()), indent=1, sort_keys=True))
