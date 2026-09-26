"""Outcomes and metrics for the broker learning ledger (BROKER_LEARNING.md §4.5-4.6).

Turns rule rows (broker_rules.evaluate) and broker track-record events into
forward outcomes, then into what the dashboard and broker_learning.db hold:
per-rule stats with a bootstrap CI, a big-move rate and a status label, rule
weights, broker track-record scores, broker trading profitability, and the
retrospective alpha case library with its broker lift. All of it is
research-grade and describes OBSERVABLE BROKER INVENTORY, NOT BENEFICIAL
OWNERSHIP. Nothing here feeds daily_picks.py, the ARB veto or any ML
experiment, and no status is ever a promotion.

WHY EXCESS, AND WHY VOL-NEUTRAL
-------------------------------
The fetched year is one regime in which the universe drifted roughly +124%
(HANDOFF). A raw forward return mostly measures that drift, and under it every
bullish rule "works". Excess over the same date's cross-section removes the
drift. The cross-section is also split into rv20 quintiles, because the v1
rules select on price versus broker cost, which is partly a volatility filter:
a name 10% under its holders' cost is disproportionately a volatile name, and
volatile names had the wider (and in this regime, higher) forward returns.
Comparing each row only with rows of similar volatility on the same date keeps
a volatility tilt from reading as a broker effect. A date too thin to split
five ways (the watchlist, or an early date) falls back to one bucket.

WHY DATE-BALANCED, AND WHY A BLOCK BOOTSTRAP
--------------------------------------------
Events on one date share one market move (signal_metrics' module docstring
measures the overlap), so every mean here is taken per date first and only
then across dates. Consecutive dates are not independent either: an h-session
forward window shares h-1 sessions with the next date's window. A plain
bootstrap over dates would treat those as independent draws and give a CI
that is too narrow. Resampling contiguous blocks of h dates keeps the overlap
inside each block. The blocks are CIRCULAR (a block may wrap from the last
date to the first), so every date is equally likely to be drawn; a plain
moving-block scheme under-draws both ends of the series. The seed is fixed,
so the same inputs always give the same CI and a status cannot flip between
reruns by chance.

A bootstrap over fewer than three blocks has almost nothing to resample: with
~1.65 blocks of 60 dates, the pre-A2 integration run produced h = 60 CIs that
did not even contain their own mean. So a CI needs n_dates >= 3 * h, and
LOW_N is n_dates < max(30, 3 * h) to match (h = 60 needs 180 event dates).
With one rolling year that means every h = 60 verdict says "data belum
cukup" until history accumulates (§4.7). That is the honest answer.

WHY h = 60 IS A HOLDER'S RETURN (Amendment A2)
----------------------------------------------
price_audit's contiguity guard drops any window with a missing session. IDX
suspends stocks that run hard, so at h = 60 that guard removed 78% of the
>= +100% windows and 99% of the >= +300% ones: exactly the moves R6 is about.
hold_60 bridges a suspension instead, because a holder's position is simply
frozen through it. Everything else stays guarded: the entry must trade on the
very next calendar session, every OBSERVED close-to-close step must sit in
the ARA/ARB band (so a split still voids the window), and a limit-locked
entry is still unbuyable. susp_60 marks the windows that were bridged, and
the dashboard shows that share next to every h = 60 number: a suspension
freezes money, and the reopening can go either way. h in {5, 10, 20} keep the
guarded fwd_oo_h; every h = 60 statistic reads hold_60 (RET_COL).

WHY A BIG-MOVE RATE NEXT TO THE MEAN
------------------------------------
R6 encodes a thesis about rare multi-month moves (+50% in ~3 months), and a
mean excess can hide exactly that: a rule that flags a few real movers among
many flat names has an unremarkable mean. big_rate asks the owner's question
directly ("of what it flagged, how many took off?"), and big_base_rate asks
it of every eligible name on the same dates, because in a +124% year plenty of
names took off without any rule. Only the gap between the two means anything.

WHY THE ALPHA CASES ARE DEDUPED AND COMPARED
-------------------------------------------
A case is chosen AFTER the move, so the library is hindsight by construction.
A stock that rose for three months would otherwise contribute sixty
overlapping "cases" whose snapshots are nearly the same row, so only the first
T of an episode is kept. Even then a snapshot only tells you what the flyers
looked like, not whether that look predicts anything; broker_lift and the
rule scorecard's big_rate vs big_base_rate at h = 60 are the comparisons.

A case whose T has fewer than VISIBLE_MIN sessions before it in the data is
marked invisible: its accumulation phase happened before the rolling year
began, so its snapshot cannot show it. Such a case still counts (it did fly),
but broker_lift uses visible cases only, since an invisible case's "top
broker" describes the run-up, not the accumulation.

WHERE THE RULE LIST AND PRIMARY HORIZONS COME FROM
--------------------------------------------------
broker_rules.RULES is the one authoritative copy of the frozen v1 ids and
directions, and broker_rules.PRIMARY_H of each rule's primary horizon (10 for
R1-R5, 60 for R6). Both are imported lazily, never at module scope (neither is
broker_book), so this module imports and tests on its own; every function that
needs them also accepts `rules=` / `primary_h=` for exactly that reason.
"""

import numpy as np
import pandas as pd

import price_audit
import signal_metrics

HORIZONS = (5, 10, 20, 60)
HOLD_H = 60                    # the horizon measured as a holder's return (Amendment A2)
# The return column each horizon is judged on: guarded fwd_oo_h, but hold_60
# at h = 60 (module docstring). Every statistic reads its return through this.
RET_COL = {5: "fwd_oo_5", 10: "fwd_oo_10", 20: "fwd_oo_20", HOLD_H: f"hold_{HOLD_H}"}
SUSP_COL = f"susp_{HOLD_H}"
EXIT_COL = f"exit_{HOLD_H}"
BIG = {5: 0.15, 10: 0.20, 20: 0.30, 60: 0.50}   # a "big move" at each h (spec §4.5)
ALPHA_MIN = 0.50               # an alpha case: hold_60 >= +50% (spec §4.6, §10)
ALPHA_H = 60                   # its horizon, and the sessions skipped after a case
ALPHA_PRIOR = 20               # rules_prior20 looks back this many sessions
VISIBLE_MIN = 80               # a case is visible with >= 80 sessions before T (§10)
ROUND_TRIP_COST = 0.0073       # transaction_cost_model "moderate" round trip
BOOT_B = 1000
BOOT_SEED = 17
LOW_N_DATES = 30               # the floor; the bar is max(30, LOW_N_BLOCKS * h)
LOW_N_BLOCKS = 3               # a CI needs at least this many blocks of h dates
EXCESS_CAP = 0.15
MIN_QUINTILE_ROWS = 25

N_BUCKETS = 5                  # rv20 quintiles
SINGLE_BUCKET = 0              # rv20_q on a date that is not split by volatility
ENTRY_LOCK_SLACK = 0.01        # an open within 1pt of ARA, at the high, is locked
SHRINK_K = 20                  # n/(n+20), as in daily_picks.learn_weights
WEIGHT_GAIN = 0.5
WEIGHT_MIN, WEIGHT_MAX = 0.25, 2.0
SIDE_DIR = {"buy": +1, "sell": -1}

# Spec §4.5 status table. Research labels only; none of them means "proven".
STATUS_LABELS = {
    "LOW_N": "data belum cukup",
    "CONSISTENT": "konsisten (retro), belum terbukti",
    "CONTRARY": "berlawanan dengan dugaan",
    "DIRECTIONAL": "searah, belum robust",
    "NEUTRAL": "tidak ada edge terlihat",
}

FWD_COLS = [f"fwd_oo_{h}" for h in HORIZONS]
X_COLS = [f"x_{h}" for h in HORIZONS]


# ── Helpers ────────────────────────────────────────────────────────────────

def _rule_defs(rules=None):
    """[(rule_id, dir)] in ruleset order."""
    if rules is None:
        from broker_rules import RULES as rules  # lazy: see module docstring
    return [(r["id"], int(r["dir"])) for r in rules]


def _primary_h(primary_h=None):
    """{rule_id: primary horizon}, broker_rules.PRIMARY_H unless given."""
    if primary_h is None:
        from broker_rules import PRIMARY_H as primary_h  # lazy: see module docstring
    return {str(k): int(v) for k, v in primary_h.items()}


def _h_of(rule_id, primary_h):
    """A rule's primary horizon. A rule with none, or one outside HORIZONS, is
    a ruleset mismatch and fails loudly rather than defaulting to 10."""
    h = primary_h.get(rule_id)
    if h not in HORIZONS:
        raise ValueError(f"no usable primary horizon for rule {rule_id!r}: {h!r}")
    return h


def low_n_min(h):
    """Fewest event dates a statistic at horizon h needs: max(30, 3*h).
    Below it the circular block bootstrap has under three blocks to draw."""
    return max(LOW_N_DATES, LOW_N_BLOCKS * int(h))


def _flag(series):
    """Strict boolean view: only True counts; NaN/None/NA is False (fail closed)."""
    return series.eq(True).fillna(False).astype(bool)


def _num(value):
    """A plain float, NaN when missing (never 0)."""
    if value is None:
        return np.nan
    value = float(value)
    return value if np.isfinite(value) else np.nan


def _per_date_mean(values, dates):
    """Mean per date, ascending by date: the order the bootstrap blocks follow."""
    return values.groupby(np.asarray(dates, dtype=object)).mean().sort_index()


def _text(value):
    """A plain str, None when missing (a broker code that is NaN or empty)."""
    if value is None or (isinstance(value, float) and np.isnan(value)) or value is pd.NA:
        return None
    value = str(value)
    return value or None


def _hit(returns):
    return float((returns > 0).mean()) if len(returns) else np.nan


def _big(returns, h):
    """Share of returns at or above the big-move bar BIG[h]."""
    return float((returns >= BIG[h]).mean()) if len(returns) else np.nan


# ── Outcomes ───────────────────────────────────────────────────────────────

def outcomes(ohlc_all):
    """Executable forward returns for every (date, ticker) row of the fetch.

    Delegates to price_audit.add_forward_returns(open_anchored=True) rather
    than re-deriving anything: that function already carries the guards this
    repo learned the hard way (holes on the panel axis, splits inside the
    window, corrupt opens that hide behind ordinary closes). all_dates is the
    union across tickers, so a session one ticker is missing shows up as a
    hole on its axis and the window is dropped, not bridged.

    On top of that, an entry that could not have been bought is removed. If
    open(T+1) opened at its high and within ENTRY_LOCK_SLACK of the ARA bound,
    it was a limit-up lock: the queue at the open does not fill, so a return
    measured from that open was never available. price_audit still treats
    such an open as a valid anchor (it is a real print), which is why the mask
    lives here. Every horizon goes NaN, because they share the one entry.

    hold_60 / susp_60 / exit_60 come from holder_returns() (Amendment A2) and
    take the same entry-lock mask.

    Returns date, ticker, fwd_oo_5, fwd_oo_10, fwd_oo_20, fwd_oo_60,
    hold_60, susp_60, exit_60, entry_blocked.
    """
    need = ["date", "ticker", "open", "high", "low", "close"]
    missing = [c for c in need if c not in ohlc_all.columns]
    if missing:
        raise ValueError(f"outcomes() needs OHLC columns, missing {missing}")
    px = ohlc_all[need].copy()
    px["date"] = px["date"].astype(str)
    if px.duplicated(["date", "ticker"]).any():
        raise ValueError("duplicate (date, ticker) rows in the OHLC frame")
    all_dates = sorted(px["date"].unique())

    fr = price_audit.add_forward_returns(
        px, all_dates, horizons=HORIZONS, open_anchored=True
    )
    fr = fr.sort_values(["ticker", "date"]).reset_index(drop=True)
    hold = holder_returns(fr, all_dates)

    g = fr.groupby("ticker")
    entry_open = g["open"].shift(-1)
    entry_high = g["high"].shift(-1)
    bound = fr["close"].map(price_audit.ara_bound)
    gap = entry_open / fr["close"] - 1
    blocked = _flag((entry_open == entry_high) & (gap >= bound - ENTRY_LOCK_SLACK))

    out = fr[["date", "ticker"]].copy()
    for col in FWD_COLS:
        out[col] = fr[col].where(~blocked)
    hold_ret = hold[RET_COL[HOLD_H]].where(~blocked)
    out[RET_COL[HOLD_H]] = hold_ret
    out[SUSP_COL] = hold[SUSP_COL].where(hold_ret.notna())
    out[EXIT_COL] = hold[EXIT_COL].where(hold_ret.notna())
    out["entry_blocked"] = blocked
    return out


def holder_returns(px, all_dates, h=HOLD_H):
    """A holder's open(T+1) -> exit return over h calendar sessions, with
    suspensions bridged (Amendment A2; see the module docstring).

    px: OHLC rows (date, ticker, open, high, low, close), one per (date,
    ticker). all_dates: the calendar, i.e. every session of the fetch in
    order; a date a ticker is missing from is a missing session for it.

    For the row at calendar position p:
      - entry is open at p+1. If the ticker has no row at p+1 (it did not
        trade on the next calendar session), there is no entry: NaN.
      - exit is the open of the ticker's first row at a position >= p+1+h.
        None yet (end of data, delisted, still suspended): NaN.
      - every observed close-to-close step from close(T) to the close before
        the exit must sit in the ARA/ARB band (price_audit.ara_bound,
        ARB_BOUND, TOL), and both anchor opens must pass price_audit's open
        check against the close before them. A missing session is bridged,
        but a split or a reverse split still voids the window. Nothing after
        the exit open is read.
    When the window has no missing session this is exactly price_audit's
    guarded fwd_oo_h: same anchors, same steps, same open checks.

    Returns px's date and ticker, sorted by (ticker, date), plus hold_{h}
    (NaN where undefined), susp_{h} (1.0 when a session inside the window
    was missing, NaN where hold is NaN) and exit_{h} (the exit session).
    """
    pos = {d: i for i, d in enumerate(all_dates)}
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    at = px["date"].map(pos)
    if at.isna().any():
        raise ValueError("holder_returns(): a row's date is not on the calendar")
    at = at.to_numpy(dtype=np.int64)
    g = px.groupby("ticker")
    # Shared with add_forward_returns: an open is an anchor only if it is
    # positive, inside [low, high] and in the ARA/ARB band vs the previous close.
    open_ok = price_audit._open_anchor_valid(px, g).fillna(False).to_numpy(dtype=bool)
    prev_close = g["close"].shift(1)
    step = px["close"] / prev_close - 1
    upper = prev_close.map(price_audit.ara_bound) + price_audit.TOL
    # step_ok[k]: the observed step close(k-1) -> close(k), whatever the gap
    step_ok = step.between(price_audit.ARB_BOUND - price_audit.TOL, upper).to_numpy(dtype=bool)
    opens = px["open"].to_numpy(dtype=float)
    dates = px["date"].to_numpy(dtype=object)

    ret = np.full(len(px), np.nan)
    susp = np.full(len(px), np.nan)
    exit_date = np.full(len(px), None, dtype=object)
    for idx in g.indices.values():
        n = len(idx)
        if n < 3:
            continue
        p = at[idx]
        i = np.arange(n - 1)
        j = np.searchsorted(p, p[i] + 1 + h, side="left")
        ok = (p[i + 1] == p[i] + 1) & (j < n)
        i, j = i[ok], j[ok]
        # bad steps among k = i+1 .. j-1, i.e. close(T) .. close(exit - 1)
        bad = np.cumsum(~step_ok[idx])
        ok = ((bad[j - 1] - bad[i]) == 0) & open_ok[idx[i + 1]] & open_ok[idx[j]]
        i, j = i[ok], j[ok]
        rows = idx[i]
        ret[rows] = opens[idx[j]] / opens[idx[i + 1]] - 1
        susp[rows] = ((p[j] - p[i]) > (j - i)).astype(float)
        exit_date[rows] = dates[idx[j]]

    out = px[["date", "ticker"]].copy()
    out[f"hold_{h}"] = ret
    out[f"susp_{h}"] = susp
    out[f"exit_{h}"] = exit_date
    return out


def attach_excess(rows, outs, single_bucket=False):
    """Rule rows + outcomes + rv20 bucket + vol-neutral excess x_h.

    rows need date, ticker, eligible, rv20. Outcomes are left-merged, so a
    row with no outcome keeps NaN rather than disappearing.

    rv20_q is 1..5 (low to high volatility) on a date with at least
    MIN_QUINTILE_ROWS eligible rows that have rv20. On a thinner date, or
    everywhere when single_bucket=True, every eligible row sits in bucket
    SINGLE_BUCKET (0) and excess is against the equal-weight eligible mean.
    Ranks are averaged, so equal rv20 always lands in the same bucket whatever
    the ticker order. An eligible row without rv20 on a quintile date cannot
    be placed and gets no bucket.

    x_h = ret_h - mean(ret_h over eligible rows with the same date and bucket
    that have an outcome), where ret_h is RET_COL[h]: fwd_oo_h, but hold_60
    at h = 60. NaN where the outcome is NaN, the row is not eligible, or it
    has no bucket. Ineligible rows never enter any mean.
    """
    missing = {"date", "ticker", "eligible", "rv20"} - set(rows.columns)
    if missing:
        raise ValueError(f"attach_excess() rows missing {sorted(missing)}")
    clash = [c for c in outs.columns if c not in ("date", "ticker") and c in rows.columns]
    if clash:
        raise ValueError(f"rows already carry outcome columns {clash}")

    x = rows.merge(outs, on=["date", "ticker"], how="left", validate="one_to_one")
    date = x["date"]
    elig = _flag(x["eligible"])
    rv = pd.to_numeric(x["rv20"], errors="coerce")
    rv = rv.where(np.isfinite(rv))

    with_rv = elig & rv.notna()
    n_rv = with_rv.groupby(date).transform("sum")
    quint_date = (n_rv >= MIN_QUINTILE_ROWS) & (not single_bucket)
    in_quint = with_rv & quint_date
    in_single = elig & ~quint_date

    rv_q = rv.where(in_quint)
    rank = rv_q.groupby(date).rank(method="average")
    count = rv_q.groupby(date).transform("count")
    bucket = pd.Series(np.nan, index=x.index)
    bucket.loc[in_quint] = np.ceil(N_BUCKETS * rank[in_quint] / count[in_quint])
    bucket.loc[in_single] = SINGLE_BUCKET
    x["rv20_q"] = bucket

    for h in HORIZONS:
        fwd = x[RET_COL[h]].where(bucket.notna())
        x[f"x_{h}"] = fwd - fwd.groupby([date, bucket]).transform("mean")
    return x


# ── Statistics ─────────────────────────────────────────────────────────────

def block_bootstrap_ci(per_date, block, B=BOOT_B, seed=BOOT_SEED):
    """2.5/97.5 percentile CI of the mean by CIRCULAR moving-block bootstrap.

    Values are taken in index (date) order; blocks of `block` consecutive
    dates are drawn with replacement from all n starts, wrapping from the last
    date back to the first, and concatenated to length n. Wrapping gives every
    date the same chance of being drawn, where a non-circular scheme draws the
    first and last block-1 dates less often. A fresh default_rng(seed) per
    call keeps it deterministic.

    Fewer than LOW_N_BLOCKS (3) blocks' worth of values gives no CI at all.
    With one or two blocks a resample is little more than the series itself
    shuffled in halves, and the pre-A2 run showed what that gives at h = 60:
    CIs too narrow to contain their own mean. NaN makes status_of() say LOW_N,
    and the dashboard show "—", instead of a verdict the data cannot carry.

    Returns (lo, hi), or (nan, nan) with fewer than 2 finite values or fewer
    than 3 * block of them.
    """
    values = pd.Series(per_date).sort_index().to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    n = len(values)
    length = int(max(1, block))
    if n < 2 or n < LOW_N_BLOCKS * length:
        return (np.nan, np.nan)
    k = -(-n // length)                                   # blocks per resample
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(B, k))
    idx = ((starts[:, :, None] + np.arange(length)) % n).reshape(B, k * length)[:, :n]
    means = values[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(lo), float(hi))


def status_of(direction, mean, lo, hi, n_dates, h):
    """Spec §4.5 status for one (rule, h), with the §10 LOW_N bar.

    LOW_N when n_dates < max(30, 3*h) (low_n_min), and also whenever the CI
    is missing: a statistic with no CI has no verdict, so it can never read
    as NEUTRAL or CONSISTENT.

    The CI is read in the rule's own direction: [dir*lo, dir*hi], re-sorted.
    That is what "dir*ci_lo > 0" in the spec table means for a bearish rule.
    Read literally for dir = -1, it would be -lo > 0, i.e. just "lo < 0",
    which almost every CI satisfies. So CONSISTENT is "the whole signed CI is
    above zero" and CONTRARY is "the whole signed CI is below zero", for
    either direction. A CI that touches zero crosses it.
    """
    if n_dates < low_n_min(h) or not (np.isfinite(lo) and np.isfinite(hi)):
        return "LOW_N"
    signed_lo, signed_hi = sorted((direction * lo, direction * hi))
    if signed_lo > 0:
        return "CONSISTENT"
    if signed_hi < 0:
        return "CONTRARY"
    if direction * mean > 0 and signed_lo <= 0 <= signed_hi:
        return "DIRECTIONAL"
    return "NEUTRAL"


def _event_stats(x, fwd, dates, events, universe, h, susp=None):
    """Shared per-(set, h) statistics for rule rows.

    events and universe are boolean masks that already require a non-NaN
    x_h, so every event is also a universe row and both sides of each hit
    edge are measured on rows that could be scored.
    """
    per_date = _per_date_mean(x[events], dates[events])
    n_dates = len(per_date)
    ev_dates = set(per_date.index)
    uni = universe & dates.isin(ev_dates)
    lo, hi = block_bootstrap_ci(per_date, block=h)
    hit_rate = _hit(fwd[events])
    base_rate = _hit(fwd[uni])
    daily = signal_metrics.date_balanced_hit_edge(
        fwd[events], dates[events], fwd[uni], dates[uni]
    )
    return {
        "n_events": int(events.sum()),
        "n_dates": int(n_dates),
        "mean_excess": _num(per_date.mean()) if n_dates else np.nan,
        "ci_lo": _num(lo),
        "ci_hi": _num(hi),
        "hit_rate": hit_rate,
        "base_rate": base_rate,
        "hit_edge": hit_rate - base_rate,
        "daily_hit_edge": _num(daily["daily_hit_edge"]),
        "big_rate": _big(fwd[events], h),
        "big_base_rate": _big(fwd[uni], h),
        "susp_rate": _num(susp[events].mean()) if susp is not None and events.any() else np.nan,
    }


def rule_stats(rows_x, as_of, window, ruleset="v1", rules=None):
    """One dict per (rule, h) with exactly the rule_stats table columns.

    rows_x is attach_excess() output plus the R1..R6 flags. An event is an
    eligible row where the rule fired and x_h is known; n_events counts those
    scored events and n_dates the distinct dates among them. mean_excess is
    date-balanced and RAW: a bearish rule is not sign-flipped here, dir is
    applied only by status_of. base_rate and big_base_rate are over the
    eligible, scorable rows on the rule's own event dates, so hit_edge and
    big_rate vs big_base_rate compare like with like. At h = 60 the return
    is hold_60 (RET_COL) and susp_rate is the share of the events whose
    window bridged a suspension; it is NULL at the other horizons, which
    never bridge one.

    Every row carries the status its own h's CI gives, so the scorecard can
    show all four horizons uniformly. The rule's headline status is the row
    at its primary horizon (primary_status()); the other rows are context and
    part of the multiple-testing caveat, not a second chance to look good.
    """
    window_start, window_end = window
    dates = rows_x["date"].astype(str)
    elig = _flag(rows_x["eligible"])
    out = []
    for rule_id, direction in _rule_defs(rules):
        fired = elig & _flag(rows_x[rule_id])
        for h in HORIZONS:
            x = rows_x[f"x_{h}"]
            fwd = rows_x[RET_COL[h]]
            susp = rows_x[SUSP_COL] if h == HOLD_H else None
            scorable = elig & x.notna()
            s = _event_stats(x, fwd, dates, fired & scorable, scorable, h, susp)
            out.append({
                "as_of": as_of, "ruleset": ruleset, "rule_id": rule_id, "h": int(h),
                **s,
                "low_n": int(s["n_dates"] < low_n_min(h)),
                "status": status_of(direction, s["mean_excess"], s["ci_lo"],
                                    s["ci_hi"], s["n_dates"], h),
                "window_start": window_start, "window_end": window_end,
            })
    return out


def primary_status(stats, primary_h=None):
    """{rule_id: status} read at each rule's primary horizon (spec §4.5).

    stats is rule_stats() output (list of dicts) or load_rule_stats() frame.
    This is the headline a rule is judged by: R1-R5 at h = 10, R6 at h = 60.
    A rule with no row at its primary h is left out rather than guessed.
    """
    ph = _primary_h(primary_h)
    rows = stats.to_dict("records") if isinstance(stats, pd.DataFrame) else list(stats)
    out = {}
    for r in rows:
        rule_id = str(r["rule_id"])
        if rule_id in ph and int(r["h"]) == ph[rule_id]:
            out[rule_id] = r["status"]
    return out


def rule_weights(rows_x, as_of, ruleset="v1", primary_h=None, rules=None):
    """Per-rule weight, mirroring daily_picks.learn_weights at the rule's
    primary horizon H (10 for R1-R5, 60 for R6).

    Each event's x_H is capped at +/-EXCESS_CAP BEFORE the per-date mean, so
    one ARA run cannot carry a date, and the date means are then averaged:

        avg_pct = 100 * dir * mean over dates of mean(capped x_H)
        n       = n_dates / H           (overlapping H-session windows)
        weight  = clip(1 + 0.5 * n/(n+20) * avg_pct, 0.25, 2.0)

    n = n_dates / H is why R6 moves slowly: sixty-session windows overlap so
    heavily that a year of dates is only a handful of independent looks.

    avg_excess_pct is stored dir-signed (positive = the rule's preregistered
    direction held), so (n_dates, avg_excess_pct) reproduce the weight. With
    no scored event the weight is the neutral 1.0 and avg_excess_pct is NaN,
    not 0: nothing was measured.
    """
    ph = _primary_h(primary_h)
    dates = rows_x["date"].astype(str)
    elig = _flag(rows_x["eligible"])
    out = []
    for rule_id, direction in _rule_defs(rules):
        h = _h_of(rule_id, ph)
        x = rows_x[f"x_{h}"]
        events = elig & _flag(rows_x[rule_id]) & x.notna()
        per_date = _per_date_mean(x[events].clip(-EXCESS_CAP, EXCESS_CAP), dates[events])
        s = len(per_date)
        avg_pct = 100.0 * direction * float(per_date.mean()) if s else np.nan
        n = s / h
        if s:
            weight = 1 + WEIGHT_GAIN * (n / (n + SHRINK_K)) * avg_pct
            weight = float(min(WEIGHT_MAX, max(WEIGHT_MIN, weight)))
        else:
            weight = 1.0
        out.append({"as_of": as_of, "ruleset": ruleset, "rule_id": rule_id,
                    "weight": weight, "n_dates": int(s), "avg_excess_pct": avg_pct})
    return out


def net_trade_stats(rows_x, primary_h=None, rules=None):
    """Informational net-of-cost trade stats for the dir = +1 rules.

    {rule_id: signal_metrics.trade_stats(ret_H - ROUND_TRIP_COST)} over the
    scored events at the rule's primary horizon H. The base rate passed in is
    the universe's net hit rate on the same event dates, so the hit rate is
    never shown without it. This is a per-trade summary with no sizing and no
    annualisation; it is not part of rule_stats, whose columns are fixed by
    the schema.
    """
    ph = _primary_h(primary_h)
    dates = rows_x["date"].astype(str)
    elig = _flag(rows_x["eligible"])
    out = {}
    for rule_id, direction in _rule_defs(rules):
        if direction != +1:
            continue
        h = _h_of(rule_id, ph)
        net = rows_x[RET_COL[h]] - ROUND_TRIP_COST
        scorable = elig & rows_x[f"x_{h}"].notna()
        events = scorable & _flag(rows_x[rule_id])
        uni = scorable & dates.isin(set(dates[events]))
        base = _hit(net[uni])
        out[rule_id] = signal_metrics.trade_stats(
            net[events], base_rate=None if np.isnan(base) else base
        )
    return out


def broker_scores(events_x, as_of):
    """Track-record score per (broker, side, h), same machinery as the rules.

    events_x is broker_rules.track_record_events() merged with the x_h of each
    event's (date, ticker) row. Only scored events (x_h known) count; a
    (broker, side, h) with none is omitted rather than written as zeros.
    mean_excess is RAW: a good seller shows NEGATIVE excess (buy is +1, sell
    is -1, and the dashboard applies that). shrunk = n/(n+20) * mean_excess
    with n = n_dates / h keeps its raw sign.

    A broker code aggregates many unrelated clients; this scores the code's
    observable flow, not any entity behind it.
    """
    need = {"date", "ticker", "broker", "side", *X_COLS}
    missing = need - set(events_x.columns)
    if missing:
        raise ValueError(f"broker_scores() events missing {sorted(missing)}")
    unknown = set(events_x["side"].dropna().unique()) - set(SIDE_DIR)
    if unknown:
        raise ValueError(f"unknown event side(s) {sorted(unknown)}")

    out = []
    for h in HORIZONS:
        col = f"x_{h}"
        scored = events_x[events_x[col].notna()]
        for (broker, side), grp in scored.groupby(["broker", "side"], sort=True):
            per_date = _per_date_mean(grp[col], grp["date"].astype(str))
            n_dates = len(per_date)
            mean = _num(per_date.mean())
            lo, hi = block_bootstrap_ci(per_date, block=h)
            n = n_dates / h
            out.append({
                "as_of": as_of, "broker": broker, "side": side, "h": int(h),
                "n_events": int(len(grp)), "n_dates": int(n_dates),
                "n_tickers": int(grp["ticker"].nunique()),
                "mean_excess": mean, "ci_lo": _num(lo), "ci_hi": _num(hi),
                "shrunk": n / (n + SHRINK_K) * mean,
                "low_n": int(n_dates < low_n_min(h)),
            })
    out.sort(key=lambda r: (r["broker"], r["side"], r["h"]))
    return out


def broker_profitability(books, as_of):
    """Per-broker trading P/L summed over the anchored books (spec §3, §4.5).

    books: {ticker: broker_book.average_cost_book frame}; the caller passes
    only tickers eligible at the last session with basis_ok, and a None or
    empty book is skipped. A row whose total_rp or turnover_rp is not finite
    is left out of every aggregate rather than summed as zero.

    This is mark-to-market trading P/L of a broker CODE over the rolling
    window. It is not a statement about who owns what.
    """
    frames = []
    for ticker, book in books.items():
        if book is None or len(book) == 0:
            continue
        f = book[["broker", "total_rp", "turnover_rp"]].copy()
        f["ticker"] = ticker
        frames.append(f)
    if not frames:
        return []
    allb = pd.concat(frames, ignore_index=True)
    total = pd.to_numeric(allb["total_rp"], errors="coerce")
    turn = pd.to_numeric(allb["turnover_rp"], errors="coerce")
    allb = allb[np.isfinite(total) & np.isfinite(turn)]

    out = []
    for broker, grp in allb.groupby("broker", sort=True):
        total_pnl = float(grp["total_rp"].sum())
        turnover = float(grp["turnover_rp"].sum())
        out.append({
            "as_of": as_of, "broker": broker,
            "n_tickers": int(grp["ticker"].nunique()),
            "total_pnl_rp": total_pnl, "turnover_rp": turnover,
            "pnl_per_turnover": total_pnl / turnover if turnover > 0 else np.nan,
            "share_profitable": float((grp["total_rp"] > 0).mean()),
        })
    return out


def live_outcome_rows(pending, outs, recorded_utc):
    """live_outcomes rows for pending live signals whose outcome is now known.

    pending: session_date, ticker, h (broker_learning_db.pending_live).
    outs: outcomes() of the current fetch. fwd_oo is the return RET_COL[h]
    (hold_60 at h = 60, as in every retrospective h = 60 statistic, so R6's
    live and retro numbers measure the same thing). exit_date is the session
    of the exit open: T+1+h on the ticker's own axis, or at h = 60 the
    holder's exit (exit_60). susp is susp_60 at h = 60, NULL otherwise.

    A row whose outcome is still unknown, or was masked (entry blocked, split
    in the window), is not returned and stays pending: the ledger is insert-
    only, so nothing is written until there is a real number to write.

    The excess over the watchlist mean is deliberately NOT stored. It depends
    on the rest of the session's cross-section, and at h = 60 a ticker whose
    exit a suspension delays resolves weeks after the others (a ticker
    dropped from the watchlist never does). A stored excess would freeze a
    benchmark that leaves such a ticker out, while the ticker's own later
    excess would use one that includes everybody, and a sign can flip between
    the two. broker_learning_db.live_summary computes it at read time from the
    recorded fwd_oo instead, over every outcome recorded for that session,
    exactly as attach_excess does retrospectively.
    """
    if pending is None or len(pending) == 0:
        return []
    o = outs.sort_values(["ticker", "date"]).reset_index(drop=True)
    want_h = pd.to_numeric(pending["h"]).astype(int)

    rows = []
    for h in sorted(set(want_h)):
        if h not in HORIZONS:
            raise ValueError(f"unknown horizon {h}")
        view = pd.DataFrame({
            "session_date": o["date"].astype(str),
            "ticker": o["ticker"],
            "fwd_oo": o[RET_COL[h]],
            "susp": o[SUSP_COL] if h == HOLD_H else np.nan,
            "exit_date": (o[EXIT_COL] if h == HOLD_H
                          else o.groupby("ticker")["date"].shift(-(1 + h))),
        })
        known = view[view["fwd_oo"].notna()]
        want = pending.loc[want_h == h, ["session_date", "ticker"]].drop_duplicates()
        got = want.merge(known, on=["session_date", "ticker"], how="inner")
        for r in got.itertuples(index=False):
            rows.append({
                "session_date": r.session_date, "ticker": r.ticker, "h": int(h),
                "fwd_oo": float(r.fwd_oo),
                "susp": None if pd.isna(r.susp) else int(r.susp),
                "exit_date": str(r.exit_date), "recorded_utc": recorded_utc,
            })
    rows.sort(key=lambda r: (r["session_date"], r["ticker"], r["h"]))
    return rows


# ── Alpha case library (spec §4.6) ─────────────────────────────────────────

ALPHA_RET = RET_COL[ALPHA_H]      # hold_60 (Amendment A2)
ALPHA_NEED = ("date", "ticker", "eligible", ALPHA_RET, SUSP_COL, "a_broker", "a_nl60_adv",
              "a_gap", "range60", "val20")


def alpha_cases(rows_x, as_of, rules=None):
    """The retrospective "stocks that flew" library: one dict per case, with
    exactly the alpha_cases table columns.

    A case is an eligible (ticker, T) with hold_60 >= ALPHA_MIN: the holder's
    return, so a run the exchange interrupted with a suspension still counts
    (susp_60 says which). Only the earliest T of an episode is kept: after a
    case at T, that ticker's next ALPHA_H sessions are skipped, counted on the
    ticker's own row order. So rows_x must hold every session of the ticker
    (broker_rules.evaluate output after attach_excess), not only eligible
    rows, or "60 sessions" would span more than 60 sessions. The same row
    order gives sessions_before, the ticker's sessions before T in the data,
    and visible = sessions_before >= VISIBLE_MIN (module docstring).

    Every recorded value is as of T, the snapshot a trader could have seen:
    rules_at_t are the rules that fired at T, rules_prior20 those that fired
    on any of the ALPHA_PRIOR sessions before T (T itself excluded, it is in
    rules_at_t; together they cover [T-20, T]). Both are comma strings in
    ruleset order, "" when nothing fired, which is a fact rather than a
    missing value. top_* is broker_rules' "top broker by NL60" explain set.

    The case is chosen by its FUTURE return, so this is hindsight by
    construction; it describes, and never feeds a weight or a status.
    """
    ids = [rule_id for rule_id, _ in _rule_defs(rules)]
    missing = [c for c in (*ALPHA_NEED, *ids) if c not in rows_x.columns]
    if missing:
        raise ValueError(f"alpha_cases() rows missing {missing}")
    x = rows_x.assign(date=rows_x["date"].astype(str))
    if x.duplicated(["date", "ticker"]).any():
        raise ValueError("duplicate (date, ticker) rows")
    x = x.sort_values(["ticker", "date"]).reset_index(drop=True)

    elig = _flag(x["eligible"])
    fired = np.column_stack([(elig & _flag(x[r])).to_numpy() for r in ids]) if ids \
        else np.zeros((len(x), 0), dtype=bool)
    ret = pd.to_numeric(x[ALPHA_RET], errors="coerce")
    susp = pd.to_numeric(x[SUSP_COL], errors="coerce")
    is_case = (elig & (ret >= ALPHA_MIN)).to_numpy()   # NaN >= bar is False

    def names(mask):
        return ",".join(r for r, on in zip(ids, mask) if on)

    out = []
    for ticker, idx in x.groupby("ticker", sort=True).indices.items():
        next_ok = 0
        for p in np.flatnonzero(is_case[idx]):
            if p < next_ok:
                continue
            next_ok = p + ALPHA_H + 1
            i = idx[p]
            prior = fired[idx[max(0, p - ALPHA_PRIOR):p]].any(axis=0)
            s = _num(susp.iat[i])
            out.append({
                "as_of": as_of, "ticker": ticker, "session_date": x.at[i, "date"],
                ALPHA_RET: float(ret.iat[i]),
                SUSP_COL: None if np.isnan(s) else int(s),
                "sessions_before": int(p),
                "visible": int(p >= VISIBLE_MIN),
                "rules_at_t": names(fired[i]), "rules_prior20": names(prior),
                "top_broker": _text(x.at[i, "a_broker"]),
                "top_nl60_adv": _num(x.at[i, "a_nl60_adv"]),
                "top_cost_gap": _num(x.at[i, "a_gap"]),
                "range60": _num(x.at[i, "range60"]),
                "val20": _num(x.at[i, "val20"]),
            })
    return out


def _visible(case):
    """Only a case recorded as visible (1/True) counts; missing is not."""
    v = case.get("visible")
    return v is not None and not (isinstance(v, float) and np.isnan(v)) and int(v) == 1


def broker_lift(rows_x, cases, as_of):
    """How much more often a broker code is a VISIBLE case's top broker than
    it is the top broker on any row: one dict per broker (broker_lift table
    columns).

    Only visible cases count (alpha_cases, module docstring): an invisible
    case's accumulation happened before the data, so its top broker at T is
    whoever ran it up, not whoever collected it. The base is every eligible
    row with a known hold_60 (the rows a case could have come from):
        case_share = n_cases_top / n_visible_cases
        row_share  = n_rows_top / n_rows
        lift       = case_share / row_share
    A row or case without a top broker counts in its denominator but gives no
    broker row. A broker that tops many rows and no case gets lift 0, which is
    information; with no visible case or no base row there is nothing to
    divide and the result is empty rather than a table of NaN.

    Descriptive only. A broker code is a securities firm with many clients,
    not a person, and a lift from a handful of cases is noise.
    """
    cases = cases.to_dict("records") if isinstance(cases, pd.DataFrame) else list(cases)
    cases = [c for c in cases if _visible(c)]
    elig = _flag(rows_x["eligible"])
    ret = pd.to_numeric(rows_x[ALPHA_RET], errors="coerce")
    base = rows_x.loc[elig & ret.notna(), "a_broker"].map(_text)
    n_rows, n_cases = len(base), len(cases)
    if n_rows == 0 or n_cases == 0:
        return []
    rows_top = base.dropna().value_counts()
    cases_top = pd.Series([_text(c.get("top_broker")) for c in cases],
                          dtype=object).dropna().value_counts()

    out = []
    for broker in sorted(set(rows_top.index) | set(cases_top.index)):
        n_c, n_r = int(cases_top.get(broker, 0)), int(rows_top.get(broker, 0))
        case_share, row_share = n_c / n_cases, n_r / n_rows
        out.append({
            "as_of": as_of, "broker": broker, "n_cases_top": n_c, "n_rows_top": n_r,
            "case_share": case_share, "row_share": row_share,
            "lift": case_share / row_share if n_r else np.nan,
        })
    return out
