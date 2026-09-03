"""
Regime-gated momentum entry: does filtering the known-decent XGBoost momentum
signal (>1%/>2% pred threshold, 1-day exit, no TP/SL) to only fire on
risk_on macro-regime dates improve on the un-gated baseline?

Reuses, does not reinvent:
  - walk_forward_backtest.build_panel / DB_PATH        (neobdm.db feature panel)
  - strategy_variants.get_walk_forward_predictions      (same expanding-window CV)
  - strategy_variants._index_price_history              (ticker->date index helper)
  - ara_arb_simulation.annotate_limits                  (ARA/ARB tagging)
  - pattern_backtest.build_regime                       (20d rolling foreign-flow regime)
  - transaction_cost_model (moderate/ideal/conservative presets)

Rigor bar applied:
  1. No lookahead: regime at date d uses only foreign flow <= d (rolling sum
     with min_periods=window); predictions at date d use only the expanding
     walk-forward CV's already-fitted model for that test cycle.
  2. Genuine 70/30 search/holdout split on the "eligible" date universe (dates
     where the model has a prediction AND the 20d regime rolling window is
     already filled) -- this SAME date split and SAME eligible universe is
     used for the gated and un-gated arms so the comparison is apples-to-apples.
     Because there's no parameter being fit here beyond picking which of the
     two fixed thresholds (>1%/>2%) to headline, "search" picks the threshold
     by date-balanced hit edge and "holdout" reports that pick's out-of-sample
     performance.
  3. IDX fill realism: annotate_limits() applied; at_ara entry days excluded;
     at_arb exit days rolled forward (capped at 20 days).
  4. Real transaction costs: moderate preset is headline; ideal/conservative
     reported for context.
  5. Signal accuracy, not a sizing model: threshold-trade hit rate is compared
     with all eligible model rows on the SAME dates. The headline hit edge is
     first computed within each date, then averaged with one vote per date so
     concurrent tickers do not masquerade as independent evidence.
  6. Daily cross-sectional IC is reported from every eligible prediction;
     per-trade ret_per_risk is unannualised context only and is not a Sharpe.
  7. Fewer than 30 holdout signal dates is flagged explicitly as low-N.
"""

import sqlite3
import numpy as np
import pandas as pd

from walk_forward_backtest import build_panel, DB_PATH
from strategy_variants import get_walk_forward_predictions, _index_price_history
from ara_arb_simulation import annotate_limits
from pattern_backtest import build_regime, DB as TX_DB
from price_audit import clean_panel
from signal_metrics import signal_stats
from transaction_cost_model import COST_PRESETS, apply_costs_to_returns, net_return_stats

THRESHOLDS = [0.010, 0.020]
MAX_ROLL = 20
SEARCH_FRAC = 0.70
MIN_SIGNAL_DAYS_FOR_SELECTION = 20


def load_neobdm():
    conn = sqlite3.connect(DB_PATH)
    panel = build_panel(conn)
    px = clean_panel(conn, horizons=(1,))
    conn.close()
    return panel, px


def load_regime():
    conn = sqlite3.connect(TX_DB)
    foreign = pd.read_sql("SELECT date, ticker, daily_flow FROM category_flow WHERE category='f'", conn)
    conn.close()
    regime, agg, roll = build_regime(foreign)
    return regime, agg, roll


def simulate_trade(px_by_ticker, date_idx_by_ticker, ticker, entry_date):
    """1-day fixed exit, no TP/SL, but with ARA entry-block + ARB roll-forward
    (same mechanic as ara_arb_simulation.simulate_trade_with_limits). Every
    transition must carry a gap-guarded fwd_1 from clean_panel()."""
    g = px_by_ticker.get(ticker)
    idx_map = date_idx_by_ticker.get(ticker)
    if g is None or entry_date not in idx_map:
        return None
    i0 = idx_map[entry_date]
    if g.loc[i0, "at_ara"]:
        return None  # entry blocked: can't reliably buy into a locked limit-up
    if i0 + 1 >= len(g):
        return None
    entry_price = g.loc[i0, "close"]

    j = i0 + 1
    while j < len(g) and g.loc[j, "at_arb"] and j < i0 + MAX_ROLL:
        j += 1
    if j >= len(g):
        j = len(g) - 1
    if j <= i0 or g.loc[i0:j - 1, "fwd_1"].isna().any():
        return None
    total_gross = (g.loc[j, "close"] - entry_price) / entry_price
    return dict(total_gross=total_gross, hold_days=j - i0,
                exit_delayed=(j - i0) > 1)


def simulate_predictions(preds_subset, px_by_ticker, date_idx_by_ticker):
    """Apply fill realism once; transaction costs are added during scoring."""
    trades = []
    n_entry_blocked = 0
    for _, row in preds_subset.iterrows():
        res = simulate_trade(
            px_by_ticker, date_idx_by_ticker, row["ticker"], row["date"]
        )
        if res is None:
            n_entry_blocked += 1
            continue
        trades.append(dict(
            ticker=row["ticker"], entry_date=row["date"], pred=row["pred"],
            total_gross=res["total_gross"], hold_days=res["hold_days"],
            exit_delayed=res["exit_delayed"],
        ))
    return pd.DataFrame(trades), n_entry_blocked


def evaluate(preds_subset, baseline_subset, px_by_ticker, date_idx_by_ticker,
             preset="moderate"):
    """Trade stats versus a same-date baseline, with one vote per date."""
    trades_df, n_entry_blocked = simulate_predictions(
        preds_subset, px_by_ticker, date_idx_by_ticker
    )

    if len(trades_df) == 0:
        return dict(n_trades=0, n_entry_blocked=n_entry_blocked, n_signal_days=0,
                    hit_rate=np.nan, base_rate=np.nan, hit_edge=np.nan,
                    daily_hit_edge=np.nan, positive_edge_days=np.nan,
                    ret_per_risk=np.nan, mean_net=np.nan, daily_ic=np.nan,
                    n_daily_ic=0, n_exit_delayed=0, reliability="LOW_N")

    baseline_df, _ = simulate_predictions(
        baseline_subset, px_by_ticker, date_idx_by_ticker
    )
    signal_dates = trades_df["entry_date"].unique()
    baseline_df = baseline_df[baseline_df["entry_date"].isin(signal_dates)].copy()

    trades_df["total_net"] = apply_costs_to_returns(
        trades_df["total_gross"].values, preset=preset
    )
    baseline_df["total_net"] = apply_costs_to_returns(
        baseline_df["total_gross"].values, preset=preset
    )
    base_rate = float((baseline_df["total_net"] > 0).mean())
    stats = net_return_stats(
        trades_df["total_gross"].values, preset=preset, base_rate=base_rate
    )

    signal_daily = trades_df.assign(
        hit=trades_df["total_net"] > 0
    ).groupby("entry_date")["hit"].mean()
    baseline_daily = baseline_df.assign(
        hit=baseline_df["total_net"] > 0
    ).groupby("entry_date")["hit"].mean()
    daily_edge = (signal_daily - baseline_daily).dropna()
    quality = signal_stats(
        baseline_df["pred"], baseline_df["total_net"],
        groups=baseline_df["entry_date"],
    )

    return dict(
        n_trades=int(stats["n_trades"]),
        n_entry_blocked=n_entry_blocked,
        n_exit_delayed=int(trades_df["exit_delayed"].sum()),
        n_signal_days=int(len(daily_edge)),
        hit_rate=float(stats["hit_rate"]),
        base_rate=base_rate,
        hit_edge=float(stats["hit_edge"]),
        daily_hit_edge=float(daily_edge.mean()),
        positive_edge_days=float((daily_edge > 0).mean()),
        ret_per_risk=float(stats["ret_per_risk"]),
        mean_net=float(stats["mean_ret"]),
        daily_ic=float(quality["daily_ic"]),
        n_daily_ic=int(quality["n_daily_ic"]),
        reliability="LOW_N" if len(daily_edge) < 30 else "OK",
    )


def select_threshold(search_arm, min_signal_days=MIN_SIGNAL_DAYS_FOR_SELECTION):
    """Pick a threshold using search-period date-balanced hit edge only.

    Variants below the minimum number of independent signal dates are excluded
    when at least one adequately supported variant exists. Pooled hit edge is a
    deterministic tie-breaker, not the headline selection metric.
    """
    usable = search_arm[search_arm["daily_hit_edge"].notna()].copy()
    if usable.empty:
        raise ValueError("cannot select a threshold without a finite daily hit edge")

    eligible = usable[usable["n_signal_days"] >= min_signal_days]
    reliable_selection = not eligible.empty
    if eligible.empty:
        eligible = usable

    winner = eligible.sort_values(
        ["daily_hit_edge", "hit_edge"],
        ascending=False,
        kind="stable",
    ).iloc[0]
    return winner, reliable_selection


def main():
    panel, px = load_neobdm()
    px = annotate_limits(px)
    px_by_ticker, date_idx_by_ticker = _index_price_history(px)

    preds = get_walk_forward_predictions(panel)
    regime, agg, roll = load_regime()
    print(f"Regime series: {regime.notna().sum()} defined dates "
          f"({(regime == 'risk_on').sum()} risk_on, {(regime == 'risk_off').sum()} risk_off), "
          f"first defined {roll.dropna().index[0]}, last {roll.index[-1]}")

    preds["regime"] = preds["date"].map(regime)
    preds_eligible = preds.dropna(subset=["regime"]).copy()
    print(f"Predictions: {len(preds)} total rows across {preds['date'].nunique()} dates; "
          f"{len(preds_eligible)} rows across {preds_eligible['date'].nunique()} dates have a "
          f"defined regime label (this is the common date-universe used below).")

    elig_dates = sorted(preds_eligible["date"].unique())
    split_idx = int(len(elig_dates) * SEARCH_FRAC)
    search_dates = set(elig_dates[:split_idx])
    holdout_dates = set(elig_dates[split_idx:])
    print(f"Eligible-date split: search={len(search_dates)} dates "
          f"({elig_dates[0]}..{elig_dates[split_idx-1]}), "
          f"holdout={len(holdout_dates)} dates ({elig_dates[split_idx]}..{elig_dates[-1]})")

    search_df = preds_eligible[preds_eligible["date"].isin(search_dates)]
    holdout_df = preds_eligible[preds_eligible["date"].isin(holdout_dates)]

    results = []
    for thresh in THRESHOLDS:
        for gate_label, gate_fn in [
            ("ungated (eligible window, no regime filter)", lambda d: d),
            ("regime-gated (risk_on only)", lambda d: d[d["regime"] == "risk_on"]),
        ]:
            for period_label, period_df in [("search", search_df), ("holdout", holdout_df)]:
                sub = gate_fn(period_df)
                sub = sub[sub["pred"] > thresh]
                for preset in COST_PRESETS:
                    stats = evaluate(
                        sub, period_df, px_by_ticker, date_idx_by_ticker,
                        preset=preset,
                    )
                    results.append(dict(
                        thresh=thresh, gate=gate_label, period=period_label, preset=preset, **stats
                    ))

    res_df = pd.DataFrame(results)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)

    print("\n=== FULL RESULTS (moderate preset shown; ideal/conservative also computed) ===")
    mod = res_df[res_df["preset"] == "moderate"].drop(columns=["preset"])
    print(mod.to_string(index=False))

    # Winner selection uses one vote per signal date. The holdout remains sealed
    # until after each gate arm's threshold has been frozen on the search period.
    print("\n=== WINNER SELECTION (by search-period moderate-cost daily hit edge) "
          "& HOLDOUT CONFIRMATION ===")
    summary_rows = []
    for gate_label in mod["gate"].unique():
        garm = res_df[res_df["gate"] == gate_label]
        search_arm = garm[(garm["period"] == "search") & (garm["preset"] == "moderate")]
        winner, reliable_selection = select_threshold(search_arm)
        winner_thresh = winner["thresh"]
        if not reliable_selection:
            print(f"\nCAVEAT: no {gate_label} threshold reaches "
                  f"{MIN_SIGNAL_DAYS_FOR_SELECTION} search signal dates; "
                  "falling back to the best low-N estimate.")
        print(f"\n-- {gate_label} -- winner thresh = {winner_thresh:.1%} "
              f"(search daily hit edge={winner['daily_hit_edge']:+.2%}, "
              f"pooled edge={winner['hit_edge']:+.2%}, "
              f"n_days={int(winner['n_signal_days'])})")
        for preset in COST_PRESETS:
            srow = res_df[(res_df["gate"] == gate_label) & (res_df["thresh"] == winner_thresh) &
                          (res_df["period"] == "search") & (res_df["preset"] == preset)].iloc[0]
            hrow = res_df[(res_df["gate"] == gate_label) & (res_df["thresh"] == winner_thresh) &
                          (res_df["period"] == "holdout") & (res_df["preset"] == preset)].iloc[0]
            print(f"  [{preset:12s}] search: daily_edge={srow['daily_hit_edge']:+.2%} "
                  f"pooled_edge={srow['hit_edge']:+.2%} n={srow['n_trades']:>4} "
                  f"signal_days={srow['n_signal_days']:>3} ret/risk={srow['ret_per_risk']:+.3f}  |  "
                  f"holdout: daily_edge={hrow['daily_hit_edge']:+.2%} "
                  f"pooled_edge={hrow['hit_edge']:+.2%} n={hrow['n_trades']:>4} "
                  f"signal_days={hrow['n_signal_days']:>3} ret/risk={hrow['ret_per_risk']:+.3f} "
                  f"[{hrow['reliability']}]")
            summary_rows.append(dict(gate=gate_label, thresh=winner_thresh, preset=preset,
                                      selection_reliable=reliable_selection,
                                      search=srow.to_dict(), holdout=hrow.to_dict()))

        moderate_holdout = res_df[
            (res_df["gate"] == gate_label)
            & (res_df["thresh"] == winner_thresh)
            & (res_df["period"] == "holdout")
            & (res_df["preset"] == "moderate")
        ].iloc[0]
        if moderate_holdout["n_signal_days"] == 0:
            print("  CAVEAT: this arm has no holdout signal dates, so there is "
                  "no out-of-sample evidence for or against it.")
        elif moderate_holdout["n_signal_days"] < 30:
            print(f"  CAVEAT: only {int(moderate_holdout['n_signal_days'])} holdout "
                  "signal dates; treat the point estimate as noisy, not confirmed.")

    res_df.to_csv("regime_gated_momentum_results.csv", index=False)
    print("\nSaved full grid to regime_gated_momentum_results.csv")
    return res_df, summary_rows


if __name__ == "__main__":
    main()
