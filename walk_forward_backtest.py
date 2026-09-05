"""
================================================================================
VOID — every Sharpe figure quoted below (0.94, 0.81, 5.36, 6.95, and the
"Sharpe > 1.5" bar) was produced by `mean/std * sqrt(252)` applied to per-trade,
cross-sectionally overlapping returns. Both halves of that are wrong and they
compound. The numbers are kept, not deleted, so nobody re-derives them and
believes them a second time. See signal_metrics.py for what replaced them.

Stage 3 (2026-08-23) first honest read, same 249-day panel:
  IC -0.025 | top-decile hit 43.5% vs base 42.3% (edge +1.2pp)
  threshold rule hit 42.4% (edge +0.1pp vs base)
i.e. no directional information, exactly as the base-rate finding predicted.
Return-based edges from this run are NOT usable yet: 82 contaminated rows drag
the panel mean from +0.32% to +14.38%, so mean-based figures stay meaningless
until build_panel() sources price_audit.clean_panel() (HANDOFF stage 1).
================================================================================

Roadmap #2 — XGBoost walk-forward backtest.

Data status (2026-07-07): neobdm_scraper.py started writing live broker_flow
rows (full bval/sval/bavg/savg) on 2026-07-05 (see save_daily_broker_flow).
2025-08-01 through 2026-07-04 was backfilled separately from the
/inventory/ page's per-broker cumulative Plotly chart. IMPORTANT: that
chart's default view only shows ~3 months, but it has a DateRangePicker
(Start Date / End Date fields) that goes back much further — 2025-08-01 is
the actual earliest available date (verified by walking the calendar back
month by month; July 2025 and earlier are greyed out "Not available"). The
first backfill pass only used the default ~3-month view and missed this;
don't repeat that mistake. The broker_stalker page itself still can't be
queried for an arbitrary past date (only rolling windows anchored to today),
but /inventory/ is not similarly limited once you drive the date picker.

RESULT, revised 2026-07-07 after extending the backfill from ~57 to ~220
days (read this instead of chasing the same experiments on more data — this
run already exists): pooled walk-forward Sharpe went from clearly negative
at 19-57 days to +0.94 at 218 days (31 cycles, train_min=30/test_window=6) —
the earlier negative results were mostly small-sample noise/overfitting, as
suspected at the time. But this is NOT a validated edge:
  - Still below SYSTEM.md's Sharpe > 1.5 bar, and hit_rate is 42.9% (under a
    coin flip) — whatever positive Sharpe exists comes from winning trades
    being larger than losing ones, not from being right more often.
  - Every feature-set variant's test MAE is still WORSE than a naive
    "predict zero" baseline (full/price-only/broker-only all ~0.044 vs
    naive ~0.042) — the model doesn't actually forecast magnitude well, even
    where the crude long/no-trade rule nets a positive Sharpe.
  - SHAP now puts broker_concentration at rank 7 of 8 (worse than the 5/8
    seen at 57 days), with momentum_1d dominating by ~3x over every other
    feature. price-only features alone score BETTER pooled Sharpe (0.76)
    than the full set (0.54) or broker-only (0.68) — see feature_ablation.py.
  - The one specific lead flagged at 57 days (CUAN's net_flow_total vs
    next-day return, r=-0.44) was explicitly marked unconfirmed pending more
    data. With 218 rows instead of 57 it collapsed to r=-0.06 — noise, as
    the caveat anticipated. Recorded here so it isn't re-investigated.
  Conclusion: this is a price-momentum-driven signal, not evidence of the
  broker-accumulation thesis SYSTEM.md is actually testing for. Do not
  proceed to Roadmap #5/#6 (live trading, DDQN) on this result — SYSTEM.md
  gates those on a validated edge, which this still isn't, and specifically
  not a broker-driven one. kelly_sizing.py (Roadmap #4) stays inert; a
  Sharpe of 0.94 built mostly on momentum isn't the edge Layer 1 exists to
  find.

UPDATE 2026-08-10: price_history had silently stopped updating on
2026-07-06 (a separate gap, now fixed - see price-history-topup.yml) while
broker_flow kept accumulating; this is the first re-run since that fix,
now on the full panel through 2026-08-06 (242 dates, 45 tickers, 24 more
days than the 218-day run above). Pooled Sharpe: 0.81 (down from 0.94),
hit_rate 42.8% (essentially unchanged). Within the noise this project has
already documented between runs (per-cycle Sharpe still swings from -8.7
to +12.0), not a meaningful move either direction - still below the 1.5
bar, still the same picture. Re-run again once meaningfully more live data
has accumulated rather than after every few days of drift.

Backfill caveats — read before trusting feature importances from this run:
  - Only `netval` is populated for backfilled rows; bval/sval/bavg/savg are
    NULL (the inventory chart only exposes net position, not the buy/sell
    split). `WHERE bval IS NULL` marks a backfilled (netval-only) row vs a
    live-scraped one. retail_presence_pct below is therefore a netval-share
    proxy, not a true volume-share — treat it as directional, not exact.
  - netval is derived as (cum_lot[d] - cum_lot[d-1]) * 100 * close[d], not
    read directly off the site — validated against the live-scraped
    2026-07-05/06 rows (within ~1-5%, from using close price as a stand-in
    for the true weighted avg trade price).
  - Broker coverage per ticker is whatever the site's own chart renders
    (~14-20 codes typically), not the full BROKER_FLOW_CODES list.
  - ALJI, BTEL, BUMI have no /inventory/ chart at all (no backfill possible).
  - `price_history` (date, ticker, open/high/low/close/volume; 2025-08-01
    onward, per-ticker start may be later if it listed/IPO'd after that) is
    the forward-return price source used for the target below.

Feature set actually implemented (subset of SYSTEM.md's named features —
order_size_uniformity and spread_bps need data this pipeline doesn't have:
individual order sizes and bid/ask quotes respectively):
  - broker_concentration: top-3 brokers' |netval| share of total |netval|
    for that ticker-day
  - net_flow_total: sum(netval) across brokers (raw net pressure)
  - n_brokers: how many brokers had flow that day (liquidity/coverage proxy)
  - net_buy_ratio: fraction of brokers net-buying vs net-selling
  - retail_presence_pct: retail codes' (XL/XC/YP/PD) |netval| share of total
    (proxy — see caveat above)
  - broker_correlation_1d: correlation of today's vs yesterday's per-broker
    netval vector (brokers common to both days) — persistence of the flow
    pattern
  - momentum_1d: today's realized return (close/prev_close - 1)
  - volume_ratio: today's volume / trailing 5-day avg volume (lagged, no
    leakage)
Target: forward_return = next trading day's (close - today's close) / today's close.

Design (expanding window, adapted to however many usable dates build_panel()
actually returns — was ~19 on the June8-Jul4-only backfill, ~57 with the
April backfill, now ~218 with the full Aug-2025 backfill):
  - Expanding window: train grows each cycle, test is a fixed slice right
    after. Defaults here (train_min=30, test_window=6) give 31 cycles on the
    full panel.
  - Sharpe comes from a tradeable rule, not raw prediction error:
        position = 1 if pred > 0.005 else 0
        strategy_return = position * actual_return
        sharpe = strategy_return.mean() / strategy_return.std() * sqrt(252)
    Also reports n_trades and hit_rate — with this little data, a Sharpe with
    n_trades=2 is noise, not a signal.
  - Treat any Sharpe > 1.5 here as "the code works," not "the edge is real" —
    SYSTEM.md's actual validation bar needs 60+ days of *live* (not
    reconstructed) NeoBDM history, sustained over 8+ weeks.
"""

import os
import sqlite3
import numpy as np
import pandas as pd
import shap
from xgboost import XGBRegressor

from price_audit import clean_panel
from signal_metrics import signal_stats, trade_stats

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neobdm.db")
RETAIL_BROKERS = {"XL", "XC", "YP", "PD"}
RANDOM_SEED = 17

FEATURES = [
    "broker_concentration", "net_flow_total", "n_brokers", "net_buy_ratio",
    "retail_presence_pct", "broker_correlation_1d", "momentum_1d", "volume_ratio",
]

XGB_PARAMS = dict(
    max_depth=4, learning_rate=0.05, n_estimators=100,
    subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
    early_stopping_rounds=10, eval_metric="mae",
    random_state=RANDOM_SEED, n_jobs=1,
)


def _broker_day_aggregates(bf):
    def agg_group(g):
        absnet = g["netval"].abs()
        total_abs = absnet.sum()
        top3 = absnet.sort_values(ascending=False).head(3).sum()
        retail_abs = absnet[g["broker_code"].isin(RETAIL_BROKERS)].sum()
        return pd.Series({
            "broker_concentration": (top3 / total_abs) if total_abs > 0 else np.nan,
            "net_flow_total": g["netval"].sum(),
            "n_brokers": len(g),
            "net_buy_ratio": (g["netval"] > 0).mean(),
            "retail_presence_pct": (retail_abs / total_abs) if total_abs > 0 else np.nan,
        })
    return bf.groupby(["ticker", "date"]).apply(agg_group, include_groups=False).reset_index()


def _broker_correlation_1d(bf):
    rows = []
    for ticker, g in bf.groupby("ticker"):
        dates = sorted(g["date"].unique())
        piv = g.pivot_table(index="date", columns="broker_code", values="netval")
        for i in range(1, len(dates)):
            d, dprev = dates[i], dates[i - 1]
            common = piv.loc[d].dropna().index.intersection(piv.loc[dprev].dropna().index)
            corr = np.corrcoef(piv.loc[d, common], piv.loc[dprev, common])[0, 1] if len(common) >= 3 else np.nan
            rows.append({"ticker": ticker, "date": d, "broker_correlation_1d": corr})
    return pd.DataFrame(rows)


def _price_features_and_target(px):
    """Features as of close(T); target under the EXECUTABLE contract.

    The features themselves are unchanged. `momentum_1d`/`volume_ratio` are
    pure price features and are genuinely known by EOD(T) by construction —
    they are functions of that session's own OHLCV, computed here. The
    broader "decision at EOD(T)" premise this module's docstrings invoke
    elsewhere, when broker-flow-derived features are joined in later
    (`build_panel`, `ml_v2_experiment_1.py`), carries an availability
    requirement that is NOT independently verified: per HANDOFF.md, ~95% of
    `broker_flow` rows are backfilled from `netval` only, and the historical
    CAPTURE TIME of those rows relative to EOD(T) is unverified — only the
    ~5% of rows carrying live bval/sval/bavg/savg (starting ~2026-07-05) have
    confirmed same-session provenance. This caveat is independent of, and
    predates, the target-contract fix below.

    The TARGET is not what it used to be: it is now open(T+1) -> open(T+2), because a
    decision taken at EOD(T) cannot transact at close(T) — that same close and
    the post-session broker summary are both inputs to the decision. The old
    close(T) -> close(T+1) value rides along as `target_cc` for diagnosis only.

    Measured on this panel, the pre-entry close(T)->open(T+1) gap carries
    +0.5463%/day while the reachable intraday window is -0.1945%/day, so the
    old target was crediting a window the signal could never have traded.
    """
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["prev_close"] = px.groupby("ticker")["close"].shift(1)
    px["momentum_1d"] = px["lag_1"] if "lag_1" in px else (
        (px["close"] - px["prev_close"]) / px["prev_close"]
    )
    px["vol_ma5"] = px.groupby("ticker")["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    if "lag_5" in px:
        px["vol_ma5"] = px["vol_ma5"].where(px["lag_5"].notna())
    px["volume_ratio"] = px["volume"] / px["vol_ma5"]

    if "fwd_oo_1" in px:
        px["target"] = px["fwd_oo_1"]
    elif "open" in px:
        # Unguarded fallback for fixtures that carry OHLC but were not built
        # through clean_panel(open_anchored=True). Never falls back to the
        # close-anchored value: silently reverting the contract is exactly the
        # failure this change exists to remove.
        nxt_open = px.groupby("ticker")["open"].shift(-1)
        px["target"] = px.groupby("ticker")["open"].shift(-2) / nxt_open - 1
    else:
        raise ValueError(
            "executable target needs fwd_oo_1 (clean_panel(open_anchored=True)) "
            "or an `open` column; refusing to silently fall back to the "
            "unexecutable close(T)->close(T+1) target"
        )

    if "fwd_1" in px:
        px["target_cc"] = px["fwd_1"]
    else:
        px["target_cc"] = px.groupby("ticker")["close"].shift(-1) / px["close"] - 1

    return px[["ticker", "date", "momentum_1d", "volume_ratio", "target", "target_cc"]]


def build_panel(conn):
    px = clean_panel(conn, horizons=(1,), lags=(1, 5), open_anchored=True)
    bf = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    # Backfilled netval was derived from the same close that was contaminated.
    # Dropping bad price keys from y but retaining their broker rows in X would
    # only move the defect from the target into the features.
    bf = bf.merge(px[["date", "ticker"]], on=["date", "ticker"], how="inner")

    agg = _broker_day_aggregates(bf)
    corr = _broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")
    pxf = _price_features_and_target(px)

    panel = agg.merge(pxf, on=["ticker", "date"], how="inner")
    # Correlation is a one-day feature too. If the price lag says a quarantine
    # or suspension made the day non-contiguous, do not compare broker vectors
    # across that same hole.
    panel.loc[panel["momentum_1d"].isna(), "broker_correlation_1d"] = np.nan
    panel = panel.dropna(subset=["target"]).sort_values("date").reset_index(drop=True)
    return panel


TRADE_THRESHOLD = 0.005

#: Minimum FIT block, measured AFTER both realization purges. 24 is not a
#: round number picked for comfort: the pre-contract protocol's fit block was
#: 0.8 * train_min = 0.8 * 30 = 24 dates at h=1 with no purges, so requiring 24
#: preserves exactly the training depth every published Experiment #1 result
#: was actually computed at, instead of quietly training on thinner and thinner
#: data as the horizon grows.
MIN_FIT_DAYS = 24
#: n_eval can otherwise collapse to 1-2 dates, which makes XGBoost's early
#: stopping a coin flip rather than a criterion.
MIN_EVAL_DAYS = 3


def make_walk_forward_splits(dates, horizon=1, train_min=30, test_window=6,
                             eval_fraction=0.20, embargo=0,
                             min_fit_days=MIN_FIT_DAYS,
                             min_eval_days=MIN_EVAL_DAYS):
    """Date-level expanding-window folds with TWO mandatory realization purges.

        [ FIT ] - purge h - [ EVAL ] - purge h (+embargo) - [ TEST ]

    Under the executable contract a label decided at EOD(T) enters at OPEN(T+1)
    and realizes at OPEN(T+1+h). Both purges are `horizon` and both are
    mandatory for correctness, not stylistic:

      - OUTER (train -> test): without it the last training rows' labels
        realize inside the test window.
      - INTERNAL (fit -> eval): the one a date-level 80/20 cut alone misses.
        Labels from the tail of FIT realize inside EVAL, so the fitted model
        has already seen outcomes belonging to its own early-stopping
        validation period.

    Why `purge = h` and not `h + 1`: with p = h the last training label
    realizes at OPEN(train_end), while the first test decision is EOD(train_end).
    OPEN precedes EOD within the same session, so that label was already public
    before the decision. The invariant is a TIMESTAMP ordering
    (max realized_at < min decision_at), not a date comparison — a date-only
    `<` would demand h+1 and discard a whole session for no informational
    reason.

    EMBARGO is a different thing and is deliberately not conflated with purge.
    Serial correlation (+0.275 mean pairwise ticker correlation on this panel)
    is dependence/inference optimism, not leakage, so embargo defaults to 0 and
    is applied at the outer boundary only — the test block is the measurement
    of record. embargo=5 is a robustness variant, never the headline.

    Returns (splits, report). Folds whose FIT block is empty (arithmetically
    impossible at long horizons on a short panel) or thinner than the minimums
    are SKIPPED and counted in `report` — never silently scored.
    """
    dates = tuple(dates)
    splits, n_infeasible, n_thin = [], 0, 0
    train_end = train_min
    while train_end + test_window <= len(dates):
        train_block = dates[: train_end - horizon - embargo]
        n_eval = max(1, int(np.ceil(len(train_block) * eval_fraction))) if train_block else 0
        n_fit = len(train_block) - n_eval - horizon

        if n_fit <= 0 or n_eval == 0:
            n_infeasible += 1
        elif n_fit < min_fit_days or n_eval < min_eval_days:
            n_thin += 1
        else:
            splits.append({
                "fit": train_block[:n_fit],
                "eval": train_block[len(train_block) - n_eval:],
                "test": dates[train_end:train_end + test_window],
            })
        train_end += test_window

    report = {
        "n_folds_nominal": len(splits) + n_infeasible + n_thin,
        "n_folds_scored": len(splits),
        "n_infeasible": n_infeasible,
        "n_too_thin": n_thin,
        "horizon": horizon,
        "embargo": embargo,
        "min_fit_days": min_fit_days,
        "min_eval_days": min_eval_days,
    }
    return splits, report


def signal_quality(pred, actual, dates=None):
    """Replaces sharpe_stats(). See signal_metrics.py for why Sharpe is gone.

    Two questions, kept separate because they are separate:

      1. Is the SIGNAL informative?  IC over every row, plus the top-decile hit
         rate and mean return against the base rate and mean of the whole
         universe. No threshold, no sizing, no annualisation.
      2. What did the TRADE_THRESHOLD rule actually capture?  The same rows the
         old function scored, summarised without pretending to annualise.

    Note what the old version did wrong beyond sqrt(252): it computed its
    statistic over TRIGGERED rows only, so every day the rule sat out vanished
    from the denominator. The signal half below always scores every row.
    """
    p = pd.Series(np.asarray(pred, dtype=float)).reset_index(drop=True)
    a = pd.Series(np.asarray(actual, dtype=float)).reset_index(drop=True)

    sig = signal_stats(p, a, groups=dates)
    traded = a[p > TRADE_THRESHOLD]
    tr = trade_stats(traded, base_rate=sig["base_rate"])

    return dict(
        n=sig["n"], ic=sig["ic"], daily_ic=sig["daily_ic"],
        daily_ic_median=sig["daily_ic_median"],
        positive_ic_days=sig["positive_ic_days"], n_daily_ic=sig["n_daily_ic"],
        base_rate=sig["base_rate"],
        top_n=sig["n_top"], top_hit=sig["hit_rate"], top_hit_edge=sig["hit_edge"],
        top_mean=sig["top_mean"], all_mean=sig["all_mean"], edge=sig["edge"],
        n_trades=tr["n_trades"], trade_mean=tr["mean_ret"],
        trade_hit=tr["hit_rate"], trade_hit_edge=tr["hit_edge"],
        trade_ret_per_risk=tr["ret_per_risk"],
    )


def run_walk_forward(panel, train_min=30, test_window=6, top_k_features=3,
                     horizon=1, embargo=0, min_fit_days=MIN_FIT_DAYS,
                     min_eval_days=MIN_EVAL_DAYS):
    """Returns (cycle_results, pooled_stats, trade_log). trade_log is built
    from the SAME model fit that produced each cycle's out-of-sample
    predictions (not a separate fit) - one row per triggered trade
    (pred > TRADE_THRESHOLD), with the top_k_features SHAP-driving features
    for that specific prediction, so you can see WHY that trade fired.
    shap.TreeExplainer is fast for XGBoost, so this adds negligible cost.

    `horizon` must match the target's horizon: it sets both realization purges
    (see make_walk_forward_splits). pooled_stats carries a `split_report` with
    the skipped/infeasible fold counts — at h=20 on a ~258-date panel some
    folds are arithmetically impossible, and that is reported, not hidden."""
    splits, split_report = make_walk_forward_splits(
        sorted(panel["date"].unique()), horizon=horizon, train_min=train_min,
        test_window=test_window, embargo=embargo,
        min_fit_days=min_fit_days, min_eval_days=min_eval_days,
    )

    results = []
    all_preds, all_actuals, all_test_dates = [], [], []
    trade_rows = []
    for i, sp in enumerate(splits, 1):
        train_dates, test_dates = sp["fit"] + sp["eval"], sp["test"]
        # Cut on DATES, never .iloc: a positional 80/20 split lands mid-date and
        # puts the same trading day on both sides of the early-stopping
        # boundary, contaminating the stopping criterion cross-sectionally.
        fit_df = panel[panel["date"].isin(sp["fit"])]
        eval_df = panel[panel["date"].isin(sp["eval"])]
        test_df = panel[panel["date"].isin(test_dates)].copy()

        model = XGBRegressor(**XGB_PARAMS)
        model.fit(
            fit_df[FEATURES], fit_df["target"],
            eval_set=[(eval_df[FEATURES], eval_df["target"])],
            verbose=False,
        )

        train_mae = np.abs(model.predict(fit_df[FEATURES]) - fit_df["target"]).mean()
        eval_mae = np.abs(model.predict(eval_df[FEATURES]) - eval_df["target"]).mean()
        test_pred = model.predict(test_df[FEATURES])
        test_mae = np.abs(test_pred - test_df["target"]).mean()
        test_df["pred"] = test_pred

        stats = signal_quality(test_pred, test_df["target"], test_df["date"])
        results.append(dict(
            cycle=i, train_days=len(train_dates), test_days=len(test_dates),
            train_mae=train_mae, eval_mae=eval_mae, test_mae=test_mae, **stats,
        ))
        all_preds.append(test_pred)
        all_actuals.append(test_df["target"].values)
        all_test_dates.append(test_df["date"].values)

        triggered = test_df[test_df["pred"] > TRADE_THRESHOLD]
        if len(triggered):
            explainer = shap.TreeExplainer(model)
            shap_vals = explainer.shap_values(triggered[FEATURES])
            for row_i, (_, row) in enumerate(triggered.iterrows()):
                top_idx = np.argsort(-np.abs(shap_vals[row_i]))[:top_k_features]
                top_features = [(FEATURES[j], float(shap_vals[row_i][j])) for j in top_idx]
                trade_rows.append(dict(
                    cycle=i, ticker=row["ticker"], date=row["date"],
                    pred=float(row["pred"]), actual=float(row["target"]),
                    top_features=top_features,
                ))

    pooled_stats = signal_quality(
        np.concatenate(all_preds), np.concatenate(all_actuals), np.concatenate(all_test_dates)
    )
    pooled_stats["split_report"] = split_report
    trade_log = pd.DataFrame(trade_rows).sort_values("date").reset_index(drop=True) if trade_rows else pd.DataFrame(
        columns=["cycle", "ticker", "date", "pred", "actual", "top_features"]
    )
    return pd.DataFrame(results), pooled_stats, trade_log


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build_panel(conn)
    conn.close()

    print(f"Panel: {len(panel)} ticker-day rows, {panel['date'].nunique()} dates, "
          f"{panel['ticker'].nunique()} tickers")

    cycle_results, pooled, _trade_log = run_walk_forward(panel)
    print("\nPer-cycle results:")
    print(cycle_results.to_string(index=False))

    print("\nPooled across all test cycles:")
    print(f"  n={pooled['n']} IC {pooled['ic']:+.3f} | daily IC mean "
          f"{pooled['daily_ic']:+.3f}, median {pooled['daily_ic_median']:+.3f} "
          f"positive {pooled['positive_ic_days']:.1%} "
          f"({pooled['n_daily_ic']}d) | top-decile n={pooled['top_n']} "
          f"hit {pooled['top_hit']:.1%} vs base {pooled['base_rate']:.1%} "
          f"(edge {pooled['top_hit_edge']:+.1%}) | return {pooled['top_mean']:+.2%} vs "
          f"{pooled['all_mean']:+.2%} (edge {pooled['edge']:+.2%})")
    if pooled["n_trades"]:
        print(f"  threshold rule (>{TRADE_THRESHOLD:.1%}): n={pooled['n_trades']} "
              f"mean {pooled['trade_mean']:+.2%} hit {pooled['trade_hit']:.1%} "
              f"({pooled['trade_hit_edge']:+.1%} vs base)")
    else:
        print("  threshold rule: no trades triggered across any test cycle.")

    print(
        f"\nNOTE: pipeline validation on {panel['date'].nunique()} days of "
        "mostly-reconstructed (netval-only) data. Read hit_edge, not hit rate: a "
        "top-decile hit rate only means something next to the base rate of the same "
        "rows, and this repo reported 42.8% for months while the base rate was also "
        "42.8%. No Sharpe appears here any more - the one this module used was wrong "
        "twice over, see signal_metrics.py. SHAP/ablation trace whatever signal exists "
        "to price momentum rather than broker flow; see this module's docstring before "
        "treating any of it as progress toward the actual thesis."
    )
