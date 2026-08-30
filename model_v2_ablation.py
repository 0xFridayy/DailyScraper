"""
ML V2, Experiment #1 — "does broker IDENTITY add anything?" (roadmap sections
30, 43).

THE QUESTION
------------
Everything this repo has concluded about broker flow was concluded from six
aggregate statistics, on a contaminated panel. Both halves of that have now
changed: HANDOFF stage 1 cleaned the panel, and broker_identity_features.py
keeps the broker dimension instead of collapsing it. So the question is open
again, and it is a comparison, not a measurement:

    A  price_only          momentum + volume, nothing else
    B  broker_aggregate    A + the six existing summary statistics
    C  broker_identity     A + per-broker trailing net flow
    D  identity+inventory  C + observable inventory concentration and velocity

If C and D do not beat A and B, the broker-intelligence thesis has no empirical
support yet and the roadmap's ownership graph, owner-broker affinity and
markup-probability layers should not be built on top of it (section 43 states
this gate explicitly). If they do, that is the first real evidence the thesis
has ever had.

WHAT MAKES THIS A FAIR TEST
---------------------------
Four things, each of which this repo has previously got wrong somewhere:

1. IDENTICAL ROWS. Every set is scored on the rows where ALL sets are complete.
   Inventory needs a 60-day window and drops the first fifth of each ticker's
   history; letting D run on a different, later, easier slice than A would make
   the comparison meaningless in the direction that flatters the new features.

2. PURGE/EMBARGO (section 29). A 10-day target on training day t reaches into
   t+10. Without an embargo the last 10 training days carry targets drawn from
   the test window, and a model can score well by having partly seen the
   answer. purged_walk_forward() drops the final `horizon` training dates of
   every cycle. On a 10d target that is not a technicality — it is most of a
   test window.

3. DAILY CROSS-SECTIONAL IC (section 28), not pooled. Pooled IC counts 45
   correlated names on one day as 45 draws. Ranking within a day removes the
   common market factor by construction.

4. AN EXCESS-RETURN READ (section 35). If a feature set's edge is really "it
   found the days the whole market went up", the edge disappears once each
   day's cross-sectional mean is subtracted. Computed on the same predictions
   rather than by refitting: the model's job is to RANK, and the excess edge
   asks whether its ranking survives the market being removed.

WHAT THIS IS NOT
----------------
Not a strategy, and no returns are compounded, sized or annualised. No
transaction costs. The output is evidence about a signal, and section 8's
gate applies before any of it becomes a trading decision: 45 names moving
together is far fewer independent observations than the row count suggests.
"""

import os
import sqlite3
import sys

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

from broker_identity_features import DB_PATH, build_identity_panel
from signal_metrics import (daily_ic, ic_summary, format_ic_summary,
                            signal_stats, evaluation_scorecard, TOP_QUANTILE)
from walk_forward_backtest import (XGB_PARAMS, _broker_day_aggregates,
                                   _broker_correlation_1d)

PRICE_FEATURES = ["momentum_1d", "momentum_5d", "volume_ratio"]
AGGREGATE_FEATURES = ["broker_concentration", "net_flow_total", "n_brokers",
                      "net_buy_ratio", "retail_presence_pct", "broker_correlation_1d"]

TRAIN_MIN = 60      # expanding window start, in dates
TEST_WINDOW = 10    # dates scored per cycle


def build_full_panel(conn, horizons=(1, 5, 10)):
    """Identity panel + the aggregate broker features set B needs."""
    panel = build_identity_panel(conn, horizons=horizons)
    bf = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    agg = _broker_day_aggregates(bf)
    corr = _broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")
    out = panel.merge(agg, on=["ticker", "date"], how="left")
    out.attrs.update(panel.attrs)
    return out


def feature_sets(panel, diagnose=False):
    """The four pre-registered sets, optionally plus narrower diagnostics.

    The four are fixed BEFORE looking at any result — that is what makes the
    comparison a test rather than a search.

    The diagnostic sets are different in kind and are labelled as such. They
    were added AFTER seeing C lose the top-decile metrics, to separate two very
    different explanations of that loss:

        (i)  broker identity carries no usable information, or
        (ii) 38 identity columns on ~4,500 rows is too wide, so the model's
             CONFIDENT predictions — the top decile — are the noisiest part of
             it, even while its average within-day ranking improves.

    Those imply opposite next steps, so the distinction is worth measuring. But
    a narrow set winning here is a HYPOTHESIS FOR A FRESH TEST, never a result:
    it was chosen after seeing the data, which is exactly the data-snooping the
    rest of this module is built to avoid. Roadmap section 22's Level 3
    (weighted aggregates over brokers instead of one column each) is the
    principled version of (ii) and is not implemented yet.
    """
    ident = panel.attrs["identity_features"]
    inv = panel.attrs["inventory_features"]
    sets = {
        "A price_only": PRICE_FEATURES,
        "B broker_aggregate": PRICE_FEATURES + AGGREGATE_FEATURES,
        "C broker_identity": PRICE_FEATURES + ident,
        "D identity+inventory": PRICE_FEATURES + ident + inv,
    }
    if diagnose:
        brokers = panel.attrs["identity_brokers"]
        sets["C1 identity 10d only (diag)"] = (
            PRICE_FEATURES + [f for f in ident if f.endswith("_net_10d")])
        top8 = brokers[:8]
        sets["C2 identity top-8 brokers (diag)"] = (
            PRICE_FEATURES + [f for f in ident if f.split("_net_")[0] in top8])
    return sets


def add_excess_targets(panel, horizons):
    """Each day's cross-sectional mean removed (section 35).

    The universe's own daily mean stands in for an index return. That is the
    right control here anyway: the claim under test is that a name outperforms
    THE OTHER NAMES IN THIS UNIVERSE, not that it beats the IHSG.
    """
    for h in horizons:
        col = f"ret_{h}"
        if col in panel:
            panel[f"exc_{h}"] = panel[col] - panel.groupby("date")[col].transform("mean")
    return panel


def purged_cycles(dates, horizon, train_min=TRAIN_MIN, test_window=TEST_WINDOW):
    """(train_dates, test_dates) pairs with a `horizon`-date embargo.

    Pure and DB-free so the embargo can be tested without fitting anything —
    an off-by-one here is invisible in the output and inflates every score.

    The embargo drops the LAST `horizon` training dates of each cycle. A target
    measured over `horizon` days from training day t is realised at t+horizon,
    so the final `horizon` training dates carry outcomes drawn from inside the
    test window. Keeping them lets the model score well partly by having seen
    the answer. On a 10-day target against a 10-date test window, that is one
    whole window's worth of overlap, not a rounding detail.
    """
    cycles = []
    train_end = train_min
    while train_end + test_window <= len(dates):
        test_dates = dates[train_end:train_end + test_window]
        train_dates = dates[:max(0, train_end - horizon)]
        train_end += test_window
        if len(train_dates) < train_min // 2:
            continue
        cycles.append((train_dates, test_dates))
    return cycles


def purged_walk_forward(panel, features, target, horizon,
                        train_min=TRAIN_MIN, test_window=TEST_WINDOW):
    """Expanding-window walk-forward with a `horizon`-date embargo.

    Returns the out-of-sample predictions as a frame, not a statistic — every
    metric in this module is computed from the same predictions, so they cannot
    disagree about which rows they scored.
    """
    p = panel.dropna(subset=list(features) + [target]).sort_values("date")
    dates = sorted(p["date"].unique())
    rows = []

    for train_dates, test_dates in purged_cycles(dates, horizon, train_min, test_window):
        train_df = p[p["date"].isin(train_dates)]
        test_df = p[p["date"].isin(test_dates)].copy()
        if train_df.empty or test_df.empty:
            continue

        split = int(len(train_df) * 0.8)
        fit_df, eval_df = train_df.iloc[:split], train_df.iloc[split:]
        if fit_df.empty or eval_df.empty:
            continue

        model = XGBRegressor(**XGB_PARAMS)
        model.fit(fit_df[features], fit_df[target],
                  eval_set=[(eval_df[features], eval_df[target])], verbose=False)
        test_df["pred"] = model.predict(test_df[features])
        rows.append(test_df)

    if not rows:
        return pd.DataFrame(columns=["ticker", "date", "pred", target])
    return pd.concat(rows, ignore_index=True)


def score(preds, target, excess_col=None):
    """The scorecard, the daily-IC summary, its stability, and the excess edge."""
    if preds.empty:
        return None
    sig = signal_stats(preds["pred"], preds[target])
    series = daily_ic(preds["date"], preds["pred"], preds[target])
    ics = ic_summary(series)

    # Roadmap section 38, gates 1 and 2: is the daily IC positive across most
    # walk-forward periods, or is one extreme stretch carrying the mean? Thirds
    # of the scored calendar, in order - crude, but it answers exactly that.
    thirds = []
    if len(series) >= 9:
        k = len(series) // 3
        for part in (series.iloc[:k], series.iloc[k:2 * k], series.iloc[2 * k:]):
            thirds.append(float(part.mean()))

    exc_edge = np.nan
    if excess_col and excess_col in preds:
        e = preds[[excess_col, "pred"]].dropna()
        if len(e):
            cut = e["pred"].quantile(TOP_QUANTILE)
            exc_edge = float(e.loc[e["pred"] >= cut, excess_col].mean()
                             - e[excess_col].mean())
    return dict(sig=sig, ic=ics, ic_thirds=thirds, excess_edge=exc_edge, n=len(preds))


def run_experiment(panel, horizons=(1, 5, 10), targets=None, diagnose=False):
    targets = targets or [("ret_10", 10, "exc_10"), ("max_10", 10, None)]
    sets = feature_sets(panel, diagnose=diagnose)

    # Same rows for every set, every target - see WHAT MAKES THIS A FAIR TEST.
    needed = sorted({f for cols in sets.values() for f in cols}
                    | {t for t, _, _ in targets})
    common = panel.dropna(subset=needed).copy()

    results = []
    for target, horizon, excess_col in targets:
        for name, features in sets.items():
            preds = purged_walk_forward(common, features, target, horizon)
            s = score(preds, target, excess_col)
            if s is None:
                continue
            results.append(dict(target=target, feature_set=name,
                                n_features=len(features), **s))
    return common, results


def print_results(common, results):
    print(f"Scored rows: {len(common)} ({common['date'].nunique()} dates, "
          f"{common['ticker'].nunique()} tickers) — identical across every set\n")
    for target in dict.fromkeys(r["target"] for r in results):
        print(f"=== target {target} ===")
        for r in (x for x in results if x["target"] == target):
            s, ic = r["sig"], r["ic"]
            print(f"\n{r['feature_set']}  ({r['n_features']} features, n={r['n']})")
            print("  " + evaluation_scorecard(s["ic"], s["base_rate"], s["hit_rate"],
                                              s["top_mean"], s["all_mean"]))
            print("  " + format_ic_summary(ic))
            if r["ic_thirds"]:
                print("  daily IC by period (early/mid/late): "
                      + " / ".join(f"{v:+.3f}" for v in r["ic_thirds"]))
            if not np.isnan(r["excess_edge"]):
                print(f"  excess-return edge (market removed): {r['excess_edge']:+.2%}")
        if s["base_rate"] > 0.85:
            print(f"\n  NOTE: base rate {s['base_rate']:.1%} — for a max_h target the "
                  f"window's high is almost always above the entry close, so hit rate\n"
                  f"  carries almost no information here. Read daily IC and return edge.")
        print()

    print("=== verdict ===")
    print("Roadmap section 43's gate: identity (C/D) must beat BOTH baselines (A/B).\n"
          "Checked on the three metrics that survive this panel's cross-correlation.\n")
    for target in dict.fromkeys(r["target"] for r in results):
        rs = {x["feature_set"]: x for x in results if x["target"] == target}
        base_sets = [k for k in rs if k.startswith(("A ", "B "))]
        ident_sets = [k for k in rs if k.startswith(("C ", "D "))]

        def _best(keys, get):
            vals = [(k, get(rs[k])) for k in keys]
            vals = [(k, v) for k, v in vals if v is not None and not np.isnan(v)]
            return max(vals, key=lambda kv: kv[1]) if vals else (None, np.nan)

        metrics = [
            ("daily IC", lambda r: r["ic"]["mean_ic"]),
            ("hit edge", lambda r: r["sig"]["hit_edge"]),
            ("return edge", lambda r: r["sig"]["edge"]),
        ]
        print(f"{target}:")
        passes = 0
        for label, get in metrics:
            bk, bv = _best(base_sets, get)
            ik, iv = _best(ident_sets, get)
            won = not np.isnan(iv) and not np.isnan(bv) and iv > bv
            passes += won
            fmt = "{:+.3f}" if label == "daily IC" else "{:+.2%}"
            print(f"  {label:12s} baseline {bk} {fmt.format(bv)}  vs  identity "
                  f"{ik} {fmt.format(iv)}   -> {'identity' if won else 'baseline'}")
        print(f"  => identity wins {passes} of {len(metrics)}\n")

    print("A split verdict is NOT a pass. Section 43 says: if C/D do not beat A/B,\n"
          "do not start the owner-affinity layer. Read %-positive-days and the\n"
          "period thirds above before reading any mean — one run is not a result.")


if __name__ == "__main__":
    horizons = (1, 5, 10)
    diagnose = "--diagnose" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    panel = add_excess_targets(build_full_panel(conn, horizons=horizons), horizons)
    conn.close()

    common, results = run_experiment(panel, horizons=horizons, diagnose=diagnose)
    print_results(common, results)
    if diagnose:
        print("\nSets marked (diag) were added after seeing the result above. They "
              "diagnose WHY, and\ncannot themselves pass the gate — a set chosen "
              "with the answer visible has to be\nre-tested on data it has not seen.")
    sys.stdout.flush()
