"""Experiment #1F Gate-B labels, execution policy and pre-registered statistics.

PREPARATION ONLY. Everything here is a pure function frozen before the first
Gate-B fit. Nothing reads a model; score columns are supplied later by the
Stage-1 runner.

TOP-3 ATTEMPTED-H1 EXECUTION (decision session T; long only; OHLC-observable rules)
  select at EOD(T) -> attempt entry at open(T+1) -> attempt exit at open(T+2)

The admitted data carries no point-in-time board or price-limit regime, so no
rule below claims a bar was legally limit-locked. Both blocking rules are
conservative OHLC assumptions.

  ENTRY  valid iff gap_1 is non-null (Gate-A: contiguous T+1 row, close step
         in band, open(T+1) usable). SINGLE_PRICE_UP_ENTRY: T+1 has
         open == high == low and open > close(T) -> unfilled, cash.
  EXIT   a ticker session s after the entry session is a usable exit iff
           (1) its open is usable under Gate-A price_audit._open_anchor_valid;
           (2) it is not SINGLE_PRICE_DOWN_EXIT: open == high == low and
               open < close of the ticker's previous session;
           (3) every close-to-close step between the entry session and s's
               previous session is inside the ARA/ARB band (a broken path,
               e.g. a corporate action, cannot be held through).
  HOLD_THROUGH searches the ticker's sessions forward in calendar order from
  T+2 for the first usable exit. A path break or running out of sessions ends
  the search as UNRESOLVED.

Per-name status (identical for selected names and for the benchmark):
  UNFILLED_NO_ENTRY          gap_1 null                                   cash 0
  UNFILLED_SINGLE_PRICE_UP   entry bar single-price above close(T)        cash 0
  EXIT_H1                    usable exit at calendar T+2  == fwd_oo_1 (asserted)
  EXIT_HOLD_THROUGH          first usable exit later than T+2; VARIABLE holding period
  UNRESOLVED                 entered, no usable exit                      cash 0 (counted)

Views:
  HOLD_THROUGH     PRIMARY EXECUTION PROXY (above)
  CASH             CONSERVATIVE SENSITIVITY: EXIT_HOLD_THROUGH and UNRESOLVED return 0
  EXCLUDED         DIAGNOSTIC ONLY: those slots dropped; never decides graduation
  OHLC_OPTIMISTIC  SENSITIVITY: single-price bars block neither entry nor exit
"""

from statistics import NormalDist

import numpy as np
import pandas as pd

from normalize_market_data import tick_size

KEY = ["ticker", "date"]
TOP_K = 3
CASH_RETURN = 0.0
TEN_PERCENT_LEVEL = 0.10   # descriptive price-level proximity only; not a board attribution
MIN_LABEL_NAMES = 10
MIN_IC_NAMES = 10
STATUSES = ("UNFILLED_NO_ENTRY", "UNFILLED_SINGLE_PRICE_UP", "EXIT_H1", "EXIT_HOLD_THROUGH", "UNRESOLVED")
VIEWS = {"HOLD_THROUGH": "return_hold_through", "CASH": "return_cash", "EXCLUDED": "return_excluded",
         "OHLC_OPTIMISTIC": "return_ohlc_optimistic"}
GRADUATION_VIEWS = ("HOLD_THROUGH", "CASH")
BOOTSTRAP = {"replicates": 10000, "block_length": 10, "sensitivity_block_lengths": (5, 20), "level": 0.95}
FAMILY_ALPHA = 0.05
PLACEBO_QUANTILE = 0.95


# ── label ──────────────────────────────────────────────────────────────────
def rank_label(panel, horizon=1, min_names=MIN_LABEL_NAMES):
    """Deterministic within-date percentile rank of fwd_oo_h.

    Reference set: every Gate-A panel row with a valid fwd_oo_h on that date
    (independent of any analysis sample, arm or variant). Ties: average rank.
    Scaling: (average_rank - 0.5) / n, in (0, 1), date mean exactly 0.5.
    Dates with fewer than `min_names` valid labels get no label. Missing or
    censored labels stay missing; they are never imputed.
    """
    col = f"fwd_oo_{horizon}"
    out = pd.Series(np.nan, index=panel.index, name=f"rank_label_h{horizon}")
    valid = panel[col].notna()
    grouped = panel.loc[valid].groupby("date")[col]
    n = grouped.transform("size")
    pct = (grouped.rank(method="average") - 0.5) / n
    out.loc[valid] = pct.where(n >= min_names)
    return out


def daily_spearman_ic(frame, score_column, return_column, min_names=MIN_IC_NAMES):
    """Per date: Spearman correlation of score with the RAW return over rows where
    both are present. Undefined below `min_names`. A date with constant scores has
    no ranking information and scores IC 0.0. Equivalent to using the rank label,
    which is a tie-preserving monotone transform of the raw return."""
    ok = frame[score_column].notna() & frame[return_column].notna()
    out = {}
    for date, day in frame.loc[ok].groupby("date", sort=True):
        if len(day) < min_names:
            continue
        s = day[score_column].rank(method="average").to_numpy()
        r = day[return_column].rank(method="average").to_numpy()
        if np.all(s == s[0]):
            out[date] = 0.0
            continue
        sd, rd = s - s.mean(), r - r.mean()
        den = np.sqrt((sd ** 2).sum() * (rd ** 2).sum())
        out[date] = float((sd * rd).sum() / den) if den > 0 else np.nan
    return pd.Series(out, dtype=float)


def paired_delta_series(ic_large_by_seed, ic_small_by_seed):
    """Per seed: daily IC delta; PRIMARY series = mean across seeds per date.
    Returns (primary_series, per_seed_delta_series). Date sets must match."""
    seeds = sorted(ic_large_by_seed)
    if seeds != sorted(ic_small_by_seed):
        raise ValueError("seed sets differ between variants")
    deltas = {}
    reference = None
    for s in seeds:
        large, small = ic_large_by_seed[s].dropna(), ic_small_by_seed[s].dropna()
        if not large.index.equals(small.index):
            raise AssertionError(f"seed {s}: daily IC dates differ between the paired variants")
        if reference is None:
            reference = large.index
        elif not large.index.equals(reference):
            raise AssertionError(f"seed {s}: daily IC dates differ across seeds")
        deltas[s] = large - small
    primary = pd.concat(deltas, axis=1).mean(axis=1)
    return primary, deltas


# ── execution policy ───────────────────────────────────────────────────────
def _level(close, band):
    """Tick-rounded price level close*(1+band), tick taken from the level's own band."""
    raw = close * (1.0 + band)
    tick = tick_size(raw)
    return np.floor(raw / tick + 1e-9) * tick, tick


def open_usable(px):
    """Gate-A open usability: price_audit._open_anchor_valid on the Gate-A panel (reused, not redefined)."""
    from price_audit import _open_anchor_valid
    return _open_anchor_valid(px, px.groupby("ticker", sort=False)).fillna(False).to_numpy(bool)


def close_step_in_band(px):
    """Row k: close(prev ticker row) -> close(k) inside the ARA/ARB band used by Gate A's
    _step_valid, WITHOUT its calendar-contiguity requirement (a hold may span a suspension)."""
    from price_audit import ARB_BOUND, TOL, ara_bound
    prev = px.groupby("ticker", sort=False)["close"].shift(1)
    change = px["close"] / prev - 1
    upper = prev.apply(ara_bound) + TOL
    return change.between(ARB_BOUND - TOL, upper).fillna(False).to_numpy(bool)


def _search_exit(e, ticker, open_ok, step_ok, down_blocked):
    """First usable exit row after entry row `e` within the same ticker.

    Returns (exit_row or None, unresolved_reason or None, blocked_down_attempts, unusable_open_attempts).
    The ticker's rows are in calendar order, so row e+1 is the first attempt (T+2 when contiguous)."""
    n = len(ticker)
    k = e + 1
    blocked = unusable = 0
    while k < n and ticker[k] == ticker[e]:
        if k >= e + 2 and not step_ok[k - 1]:
            return None, "path_break", blocked, unusable
        if not open_ok[k]:
            unusable += 1
        elif down_blocked[k]:
            blocked += 1
        else:
            return k, None, blocked, unusable
        k += 1
    return None, "no_later_usable_open", blocked, unusable


def slot_outcomes(panel, calendar):
    """Per decision row: attempted-H1 execution status, holding period and returns per view."""
    px = panel.sort_values(KEY, kind="mergesort").reset_index(drop=True)
    calpos = {d: i for i, d in enumerate(calendar)}
    pos = px["date"].map(calpos).to_numpy(np.int64)
    ticker = px["ticker"].to_numpy()
    dates = px["date"].to_numpy()
    n = len(px)
    open_, high, low, close = (px[c].to_numpy(float) for c in ("open", "high", "low", "close"))
    prev_close = px.groupby("ticker", sort=False)["close"].shift(1).to_numpy(float)
    single = (open_ == high) & (high == low)
    sp_up_row = single & (open_ > prev_close)
    sp_down_row = single & (open_ < prev_close)
    open_ok = open_usable(px)
    step_ok = close_step_in_band(px)
    h1 = px["fwd_oo_1"].to_numpy(float)

    same_next = np.r_[ticker[1:] == ticker[:-1], False]
    entry_valid = px["gap_1"].notna().to_numpy()
    e_row = np.arange(n) + 1
    contiguous_entry = same_next & (pos[np.clip(e_row, 0, n - 1)] == pos + 1)
    if (entry_valid & ~contiguous_entry).any():
        raise AssertionError("gap_1 valid without a contiguous T+1 panel session")
    t2_row = np.arange(n) + 2
    t2_exists = entry_valid & np.r_[same_next[1:], False, False][:n] & (pos[np.clip(t2_row, 0, n - 1)] == pos + 2)
    t2_open_ok = t2_exists & open_ok[np.clip(t2_row, 0, n - 1)]
    if not np.array_equal(t2_open_ok, ~np.isnan(h1) & entry_valid) or not np.array_equal(~np.isnan(h1), ~np.isnan(h1) & entry_valid):
        raise AssertionError("usable open at T+2 does not reproduce Gate-A fwd_oo_1 validity")

    entry_sp_up = entry_valid & sp_up_row[np.clip(e_row, 0, n - 1)]
    no_block = np.zeros(n, dtype=bool)

    def resolve(filled, down_blocked):
        status = np.full(n, "UNFILLED_NO_ENTRY", dtype=object)
        ret = np.full(n, CASH_RETURN)
        hold = np.full(n, np.nan)
        delay = np.full(n, None, dtype=object)
        unresolved = np.full(n, None, dtype=object)
        exit_date = np.full(n, None, dtype=object)
        attempts = np.zeros((n, 2), dtype=np.int64)
        for i in np.flatnonzero(filled):
            e = i + 1
            k, reason, blocked, unusable = _search_exit(e, ticker, open_ok, step_ok, down_blocked)
            attempts[i] = blocked, unusable
            if k is None:
                status[i], unresolved[i] = "UNRESOLVED", reason
                continue
            ret[i] = open_[k] / open_[e] - 1.0
            hold[i] = pos[k] - pos[e]
            exit_date[i] = dates[k]
            if pos[k] == pos[i] + 2:
                status[i] = "EXIT_H1"
                if ret[i] != h1[i]:
                    raise AssertionError(f"EXIT_H1 return differs from fwd_oo_1 at row {i}")
            else:
                status[i] = "EXIT_HOLD_THROUGH"
                delay[i] = ("t2_missing" if not t2_exists[i] else
                            "t2_open_unusable" if not open_ok[i + 2] else "t2_single_price_down")
        return status, ret, hold, delay, unresolved, exit_date, attempts

    unfilled_sp = entry_valid & entry_sp_up
    status, ret, hold, delay, unresolved, exit_date, attempts = resolve(entry_valid & ~entry_sp_up, sp_down_row)
    status[unfilled_sp] = "UNFILLED_SINGLE_PRICE_UP"
    opt_status, opt_ret, opt_hold, _, _, _, _ = resolve(entry_valid, no_block)

    later = (status == "EXIT_HOLD_THROUGH") | (status == "UNRESOLVED")
    out = px[KEY].copy()
    out["status"] = status
    out["exit_delay_reason"] = delay
    out["unresolved_reason"] = unresolved
    out["holding_sessions"] = hold
    out["exit_date"] = exit_date
    out["blocked_down_exit_attempts"] = attempts[:, 0]
    out["unusable_open_exit_attempts"] = attempts[:, 1]
    out["return_hold_through"] = ret
    out["return_cash"] = np.where(later, CASH_RETURN, ret)
    out["return_excluded"] = np.where(later, np.nan, ret)
    out["status_ohlc_optimistic"] = opt_status
    out["return_ohlc_optimistic"] = opt_ret
    out["holding_sessions_ohlc_optimistic"] = opt_hold

    from price_audit import ara_bound
    level10, tick10 = _level(close, TEN_PERCENT_LEVEL)
    level_ara, tick_ara = _level(close, np.array([ara_bound(c) for c in close]))
    entry_open = open_[np.clip(e_row, 0, n - 1)]
    out["diag_entry_sp_up_near_10pct_level"] = entry_sp_up & (np.abs(entry_open - level10) <= tick10 + 1e-9)
    out["diag_entry_sp_up_near_ara_level"] = entry_sp_up & (np.abs(entry_open - level_ara) <= tick_ara + 1e-9)
    out["entry_single_price_up"] = entry_sp_up
    return out


def execution_diagnostics(outcomes, calendar):
    """Model-free execution diagnostics over H1-evaluable decision rows (calendar T+2 exists)."""
    calpos = {d: i for i, d in enumerate(calendar)}
    rows = outcomes.loc[outcomes["date"].map(calpos).to_numpy() <= len(calendar) - 3]
    status = rows["status"].value_counts()
    held = rows.loc[rows["status"] == "EXIT_HOLD_THROUGH", "holding_sessions"]
    opt = rows["status_ohlc_optimistic"].value_counts()
    sp_up = rows["entry_single_price_up"]
    delayed = rows["exit_delay_reason"].dropna().value_counts()
    unresolved = rows["unresolved_reason"].dropna().value_counts()
    return {
        "rows": int(len(rows)),
        "status": {s: int(status.get(s, 0)) for s in STATUSES},
        "hold_through_delay_reason": {r: int(delayed.get(r, 0))
                                      for r in ("t2_missing", "t2_open_unusable", "t2_single_price_down")},
        "unresolved_reason": {r: int(unresolved.get(r, 0)) for r in ("path_break", "no_later_usable_open")},
        "blocked_single_price_down_exit_attempts": int(rows["blocked_down_exit_attempts"].sum()),
        "positions_with_a_blocked_exit_attempt": int((rows["blocked_down_exit_attempts"] > 0).sum()),
        "unusable_open_exit_attempts": int(rows["unusable_open_exit_attempts"].sum()),
        "holding_sessions_hold_through": {
            "n": int(len(held)), "p50": float(held.quantile(0.5)), "p90": float(held.quantile(0.9)),
            "p99": float(held.quantile(0.99)), "max": float(held.max()),
            "distribution": {str(int(k)): int(v) for k, v in held.value_counts().sort_index().items()}},
        "ohlc_optimistic_status": {s: int(opt.get(s, 0)) for s in STATUSES},
        "diagnostics_price_level_proximity_only": {
            "entry_single_price_up": int(sp_up.sum()),
            "entry_single_price_up_near_10pct_level": int(rows["diag_entry_sp_up_near_10pct_level"].sum()),
            "entry_single_price_up_near_ara_level": int(rows["diag_entry_sp_up_near_ara_level"].sum())},
    }


def select_top_k(day, score_column, k=TOP_K):
    """Top-k by score (desc), ticker ascending on ties. Uses only the EOD(T) score;
    no post-decision status removes or replaces a pick."""
    return day.sort_values([score_column, "ticker"], ascending=[False, True], kind="mergesort").head(k)


def portfolio_return(selected, view, k=TOP_K):
    """Equal ex-ante weight 1/k per slot. Unfilled and missing slots stay in cash.
    EXCLUDED (diagnostic) averages resolved slots only."""
    values = selected[VIEWS[view]].to_numpy(float)
    if view == "EXCLUDED":
        resolved = values[~np.isnan(values)]
        return float(resolved.mean()) if len(resolved) else np.nan
    return float((np.nan_to_num(values, nan=CASH_RETURN).sum() + CASH_RETURN * (k - len(values))) / k)


def benchmark_return(day, view):
    """Membership: every eligible key on date T (fixed at EOD(T)). Equal ex-ante
    weight; unfilled entries keep their weight in cash; censoring mirrors the
    portfolio. No renormalisation except in the EXCLUDED diagnostic."""
    values = day[VIEWS[view]].to_numpy(float)
    if view == "EXCLUDED":
        values = values[~np.isnan(values)]
    return float(np.nan_to_num(values, nan=CASH_RETURN).mean()) if len(values) else np.nan


def daily_top3_excess(day, score_column, view="HOLD_THROUGH", k=TOP_K):
    return portfolio_return(select_top_k(day, score_column, k), view, k) - benchmark_return(day, view)


def pairwise_top3_delta(excess_large_by_seed, excess_small_by_seed):
    """Mean over the pair's executable test dates of the 5-seed mean of
    [Top-3 excess(larger variant) - Top-3 excess(smaller variant)]. The benchmark
    is identical for both variants of a pair, so it cancels."""
    seeds = sorted(excess_large_by_seed)
    per_seed = {}
    for s in seeds:
        large, small = excess_large_by_seed[s], excess_small_by_seed[s]
        if not large.index.equals(small.index):
            raise AssertionError(f"seed {s}: Top-3 dates differ between the paired variants")
        per_seed[s] = large - small
    daily = pd.concat(per_seed, axis=1).mean(axis=1)
    return float(daily.mean()), daily


# ── statistics ─────────────────────────────────────────────────────────────
def bootstrap_rng(pair_index, block_length):
    return np.random.default_rng([17, 7, int(pair_index), int(block_length)])


def circular_block_bootstrap_means(values, block_length, replicates, rng):
    x = np.asarray(values, dtype=float)
    n = len(x)
    blocks = int(np.ceil(n / block_length))
    starts = rng.integers(0, n, size=(replicates, blocks))
    idx = ((starts[:, :, None] + np.arange(block_length)) % n).reshape(replicates, -1)[:, :n]
    return x[idx].mean(axis=1)


def basic_bootstrap_ci(theta_hat, boot, level=BOOTSTRAP["level"]):
    """Basic (reverse-percentile) interval, the inversion consistent with the p-value below."""
    tail = (1.0 - level) / 2.0
    lo_q, hi_q = np.quantile(boot, [tail, 1.0 - tail])
    return float(2 * theta_hat - hi_q), float(2 * theta_hat - lo_q)


def basic_bootstrap_pvalue(theta_hat, boot):
    """Equal-tailed two-sided p for H0: theta = 0 from the centred bootstrap:
    p = min(1, 2 * min(1 + #{theta* - theta_hat >= |theta_hat|},
                       1 + #{theta* - theta_hat <= -|theta_hat|}) / (1 + B)).
    The basic 95% interval excludes 0 exactly when this p is <= 0.05, up to the
    +1 finite-sample correction.

    The thresholds use |theta_hat|. Signed thresholds (as before PR #41 review)
    are correct only for theta_hat > 0: for a negative estimate both counts span
    the centre of the distribution and p collapses to 1 even when the interval
    excludes 0 -- the Stage-1 record's p = 1.0 for the negative C-B and D-C
    increments came from that."""
    centred = np.asarray(boot) - theta_hat
    magnitude = abs(theta_hat)
    upper = 1 + int((centred >= magnitude).sum())
    lower = 1 + int((centred <= -magnitude).sum())
    return float(min(1.0, 2.0 * min(upper, lower) / (1 + len(centred))))


def holm(pvalues, alpha=FAMILY_ALPHA):
    """Holm step-down. Returns {name: (adjusted_p, rejected)}."""
    items = sorted(pvalues.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(items)
    out, running, still = {}, 0.0, True
    for i, (name, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        still = still and p <= alpha / (m - i)
        out[name] = (running, still)
    return out


def minimum_detectable_effect(standard_error, alpha, power=0.80):
    z = NormalDist()
    return float((z.inv_cdf(1 - alpha / 2) + z.inv_cdf(power)) * standard_error)


def placebo_statistics(observed_seed17, null_seed17):
    null = np.asarray(null_seed17, dtype=float)
    q95 = float(np.quantile(null, PLACEBO_QUANTILE, method="linear"))
    return {"q95": q95, "median": float(np.median(null)),
            "p_empirical": float((1 + int((null >= observed_seed17).sum())) / (1 + len(null))),
            "exceeds_q95": bool(observed_seed17 > q95)}


def period_halves(test_dates):
    dates = sorted(test_dates)
    cut = int(np.ceil(len(dates) / 2))
    return dates[:cut], dates[cut:]


def classify_increment(primary_mean, holm_rejected, observed_seed17, placebo, seed_means,
                       half_means, top3_delta_hold_through, top3_delta_cash):
    """Mutually exclusive outcome for B, C or D (all inputs from the LAG-1 arm, H1).

    REJECT_FOR_NOW   primary_mean <= 0 OR observed_seed17 <= placebo median
    GRADUATE         every condition G1..G5 holds
    SENSITIVITY_ONLY otherwise
    """
    conditions = {
        "G1_primary_positive_and_holm_rejected": bool(primary_mean > 0 and holm_rejected),
        "G2_seed17_exceeds_placebo_q95": bool(observed_seed17 > placebo["q95"]),
        "G3_all_five_seeds_positive": bool(len(seed_means) == 5 and all(m > 0 for m in seed_means)),
        "G4_both_halves_positive": bool(len(half_means) == 2 and all(m > 0 for m in half_means)),
        "G5_top3_not_reduced_hold_through_and_cash": bool(top3_delta_hold_through >= 0 and top3_delta_cash >= 0),
    }
    if primary_mean <= 0 or observed_seed17 <= placebo["median"]:
        category = "REJECT_FOR_NOW"
    elif all(conditions.values()):
        category = "GRADUATE_TOWARD_SPECTRA"
    else:
        category = "SENSITIVITY_ONLY"
    return category, conditions


def classify_price_core(ic_mean, ic_ci, top3_mean, top3_ci, ic_seed_means, ic_half_means,
                        top3_seed_means, top3_half_means):
    """Family A: GRADUATE iff both basic 95% intervals are above 0 and the IC and
    Top-3 point estimates are positive in all 5 seeds and both halves;
    REJECT_FOR_NOW iff ic_mean <= 0; SENSITIVITY_ONLY otherwise."""
    stable = (all(m > 0 for m in ic_seed_means) and all(m > 0 for m in ic_half_means)
              and all(m > 0 for m in top3_seed_means) and all(m > 0 for m in top3_half_means))
    if ic_mean <= 0:
        return "REJECT_FOR_NOW"
    if ic_ci[0] > 0 and top3_ci[0] > 0 and top3_mean > 0 and stable:
        return "GRADUATE_TOWARD_SPECTRA"
    return "SENSITIVITY_ONLY"
