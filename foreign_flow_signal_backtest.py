"""
Per-ticker foreign-flow accumulation signal (rule-based, no ML).

Signal: for each ticker independently, take txchart_history.db's
category_flow WHERE category='f' (foreign net flow — verified one-sided,
legit per-ticker series, NOT the market-wide aggregate regime filter tested
elsewhere). Build a rolling N-day trailing sum of that ticker's own
daily_flow (N in {3,5,10}). Entry candidates:
  - "cross": rolling sum flips from <=0 to >0 (fresh accumulation starting)
  - "level": rolling sum is simply >0 (in-accumulation, re-fires every day
    the condition holds — more turnover, tests the instructions' alternate
    phrasing)
Exit: fixed hold of H in {1,3,5} trading days, subject to ARA/ARB realism.

Full rigor bar applied:
  1. No lookahead: rolling sum at date d uses only daily_flow with date<=d;
     entry executes at date d's own close (consistent with this project's
     established convention in ara_arb_simulation.py / strategy_variants.py
     of trading at the same close the signal is computed from — the feature
     itself never uses future information).
  2. Genuine date-based search/holdout split: cutoff at the 70th percentile
     of the trading calendar spanned by category_flow (2025-10-06..2026-07-06,
     176 dates). N, H, and signal-type are chosen ONLY on search-period
     entries (entry_date <= cutoff); holdout entries (entry_date > cutoff)
     never influence that choice.
  3. IDX fill realism via ara_arb_simulation.annotate_limits(): at_ara entry
     days excluded, at_arb exit days rolled forward (capped +20 days).
  4. Real transaction costs via transaction_cost_model (ideal/moderate/
     conservative presets); moderate is the headline.
  5. Signal accuracy, not a sizing model: every net-of-cost trade hit rate is
     compared with the always-long universe over the SAME entry dates and
     holding period. hit_edge = signal hit rate - universe base rate.
  6. No cross-sectional pseudo-replication in selection: hit edge is computed
     within each entry date, then averaged with one vote per date. The search
     period's mean daily hit edge selects the frozen variant. Per-trade
     ret_per_risk is unannualised context only and is not a Sharpe.
  7. Fewer than 30 holdout signal dates is flagged explicitly as low-N.
"""

import os
import sqlite3
import numpy as np
import pandas as pd

from ara_arb_simulation import annotate_limits
from transaction_cost_model import apply_costs_to_returns, net_return_stats

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "txchart_history.db")

N_GRID = [3, 5, 10]
H_GRID = [1, 3, 5]
TYPE_GRID = ["cross", "level"]
MIN_SIGNAL_DAYS_FOR_SELECTION = 20
SEARCH_FRACTION = 0.70


def load_data():
    conn = sqlite3.connect(DB)
    flow = pd.read_sql(
        "SELECT date, ticker, daily_flow FROM category_flow WHERE category='f' AND ticker != 'COMPOSITE'",
        conn,
    )
    px = pd.read_sql(
        "SELECT date, ticker, open, high, low, close, volume FROM ohlcv WHERE ticker != 'COMPOSITE'",
        conn,
    )
    conn.close()
    flow = flow.sort_values(["ticker", "date"]).reset_index(drop=True)
    px = annotate_limits(px)
    return flow, px


def build_signals(flow):
    """Returns dict[(N,type)] -> DataFrame[ticker,date]."""
    out = {}
    for N in N_GRID:
        rows_cross, rows_level = [], []
        for ticker, g in flow.groupby("ticker", sort=False):
            g = g.sort_values("date").reset_index(drop=True)
            roll = g["daily_flow"].rolling(N, min_periods=N).sum()
            prev_roll = roll.shift(1)
            is_cross = (roll > 0) & (prev_roll <= 0)  # NaN prev_roll -> False, no lookahead
            is_level = roll > 0
            for d in g.loc[is_cross, "date"]:
                rows_cross.append((ticker, d))
            for d in g.loc[is_level, "date"]:
                rows_level.append((ticker, d))
        out[(N, "cross")] = pd.DataFrame(rows_cross, columns=["ticker", "date"])
        out[(N, "level")] = pd.DataFrame(rows_level, columns=["ticker", "date"])
    return out


def index_price(px):
    px_by_ticker = {t: g.reset_index(drop=True) for t, g in px.groupby("ticker")}
    date_idx_by_ticker = {t: {d: i for i, d in enumerate(g["date"])} for t, g in px_by_ticker.items()}
    return px_by_ticker, date_idx_by_ticker


def generate_trades(signals_df, H, px_by_ticker, date_idx_by_ticker):
    """For each (ticker,date) signal, simulate a fixed-H-day hold with
    ARA-entry-exclusion and ARB-exit-roll-forward. Returns list of trade dicts."""
    trades = []
    n_signals = 0
    n_entry_blocked = 0
    n_no_forward_data = 0
    for ticker, date in zip(signals_df["ticker"], signals_df["date"]):
        n_signals += 1
        g = px_by_ticker.get(ticker)
        idx_map = date_idx_by_ticker.get(ticker)
        if g is None or date not in idx_map:
            n_no_forward_data += 1
            continue
        i0 = idx_map[date]
        if bool(g.loc[i0, "at_ara"]):
            n_entry_blocked += 1
            continue  # can't reliably buy into a locked limit-up
        j_nominal = i0 + H
        if j_nominal >= len(g):
            n_no_forward_data += 1
            continue
        j = j_nominal
        while j < len(g) and bool(g.loc[j, "at_arb"]) and j < i0 + 20:
            j += 1
        if j >= len(g):
            j = len(g) - 1
        if j <= i0:
            n_no_forward_data += 1
            continue

        closes = g.loc[i0:j, "close"].values.astype(float)
        gross_return = closes[-1] / closes[0] - 1.0

        trades.append(dict(
            ticker=ticker, entry_date=date, exit_date=g.loc[j, "date"],
            i0=i0, j=j, hold_days_actual=j - i0,
            gross_return=gross_return,
        ))
    meta = dict(n_signals=n_signals, n_entry_blocked=n_entry_blocked, n_no_forward_data=n_no_forward_data)
    return trades, meta


def trade_stats(trades, universe_trades, preset="moderate"):
    """Cost-adjusted signal stats against the same-date always-long universe."""
    if not trades:
        return dict(n_trades=0, hit_rate=np.nan, mean_net_return=np.nan,
                    median_net_return=np.nan, ret_per_risk=np.nan,
                    base_rate=np.nan, hit_edge=np.nan)

    entry_dates = {t["entry_date"] for t in trades}
    comparison = [t for t in universe_trades if t["entry_date"] in entry_dates]
    if comparison:
        base_net = apply_costs_to_returns(
            [t["gross_return"] for t in comparison], preset=preset
        )
        base_rate = float((base_net > 0).mean())
    else:
        base_rate = np.nan

    stats = net_return_stats(
        [t["gross_return"] for t in trades], preset=preset,
        base_rate=None if np.isnan(base_rate) else base_rate,
    )
    return dict(
        n_trades=int(stats["n_trades"]),
        hit_rate=float(stats["hit_rate"]),
        mean_net_return=float(stats["mean_ret"]),
        median_net_return=float(stats["median_ret"]),
        ret_per_risk=float(stats["ret_per_risk"]),
        base_rate=base_rate,
        hit_edge=float(stats["hit_edge"]),
    )


def date_balanced_hit_edge(trades, universe_trades, preset="moderate"):
    """Mean within-entry-date hit edge, so each calendar date votes once."""
    if not trades:
        return dict(n_signal_days=0, daily_hit_edge=np.nan,
                    daily_hit_edge_median=np.nan, positive_edge_days=np.nan)

    signal = pd.DataFrame(
        [(t["entry_date"], t["gross_return"]) for t in trades],
        columns=["entry_date", "gross_return"],
    )
    signal["net_return"] = apply_costs_to_returns(
        signal["gross_return"].values, preset=preset
    )
    signal_daily = signal.assign(hit=signal["net_return"] > 0).groupby("entry_date")["hit"].mean()

    comparison = pd.DataFrame(
        [(t["entry_date"], t["gross_return"]) for t in universe_trades
         if t["entry_date"] in signal_daily.index],
        columns=["entry_date", "gross_return"],
    )
    if comparison.empty:
        return dict(n_signal_days=0, daily_hit_edge=np.nan,
                    daily_hit_edge_median=np.nan, positive_edge_days=np.nan)
    comparison["net_return"] = apply_costs_to_returns(
        comparison["gross_return"].values, preset=preset
    )
    universe_daily = comparison.assign(hit=comparison["net_return"] > 0).groupby("entry_date")["hit"].mean()
    edge = (signal_daily - universe_daily).dropna()
    return dict(
        n_signal_days=int(len(edge)),
        daily_hit_edge=float(edge.mean()),
        daily_hit_edge_median=float(edge.median()),
        positive_edge_days=float((edge > 0).mean()),
    )


def evaluate_combo(signals_df, H, px_by_ticker, date_idx_by_ticker, cutoff_date, preset="moderate"):
    trades, meta = generate_trades(signals_df, H, px_by_ticker, date_idx_by_ticker)
    search_trades = [t for t in trades if t["entry_date"] <= cutoff_date]
    holdout_trades = [t for t in trades if t["entry_date"] > cutoff_date]
    return search_trades, holdout_trades, meta


def main():
    flow, px = load_data()
    dates_all = sorted(flow["date"].unique())
    cutoff_idx = int(len(dates_all) * SEARCH_FRACTION)
    cutoff_date = dates_all[cutoff_idx]
    print(f"Calendar: {dates_all[0]}..{dates_all[-1]} ({len(dates_all)} dates)")
    print(f"Search/holdout cutoff date: {cutoff_date} "
          f"(search={cutoff_idx} dates, holdout={len(dates_all)-cutoff_idx} dates)\n")

    px_by_ticker, date_idx_by_ticker = index_price(px)
    signal_sets = build_signals(flow)
    universe_signals = px[["ticker", "date"]]
    universe_by_hold = {
        H: generate_trades(universe_signals, H, px_by_ticker, date_idx_by_ticker)[0]
        for H in H_GRID
    }

    print("=" * 100)
    print("SEARCH-PERIOD GRID (moderate-cost daily hit edge, entry_date <= cutoff only)")
    print("=" * 100)
    grid_rows = []
    for (N, sig_type), sig_df in signal_sets.items():
        for H in H_GRID:
            search_trades, holdout_trades, meta = evaluate_combo(
                sig_df, H, px_by_ticker, date_idx_by_ticker, cutoff_date)
            universe = universe_by_hold[H]
            ts = trade_stats(search_trades, universe, "moderate")
            ds = date_balanced_hit_edge(search_trades, universe, "moderate")
            grid_rows.append(dict(
                N=N, type=sig_type, H=H, n_trades=ts["n_trades"],
                n_signal_days=ds["n_signal_days"], daily_hit_edge=ds["daily_hit_edge"],
                hit_rate=ts["hit_rate"], base_rate=ts["base_rate"],
                hit_edge=ts["hit_edge"], ret_per_risk=ts["ret_per_risk"],
                mean_net_return=ts["mean_net_return"],
            ))
    grid = pd.DataFrame(grid_rows).sort_values(
        ["daily_hit_edge", "hit_edge"], ascending=False
    )
    pd.set_option("display.width", 160)
    print(grid.to_string(index=False))

    eligible = grid[grid["n_signal_days"] >= MIN_SIGNAL_DAYS_FOR_SELECTION]
    if eligible.empty:
        print(f"\nNo combo reaches n_signal_days >= {MIN_SIGNAL_DAYS_FOR_SELECTION} in search period; "
              "falling back to best of all combos (flag as unreliable).")
        eligible = grid
    best = eligible.iloc[0]
    N_star, type_star, H_star = int(best["N"]), best["type"], int(best["H"])
    print(f"\nSelected on SEARCH period only: N={N_star}, type={type_star}, H={H_star} "
          f"(daily hit edge={best['daily_hit_edge']:+.2%}, "
          f"pooled hit edge={best['hit_edge']:+.2%}, "
          f"n_days={int(best['n_signal_days'])})")

    # ---- Apply the chosen, frozen combo to holdout ----
    sig_df = signal_sets[(N_star, type_star)]
    search_trades, holdout_trades, meta = evaluate_combo(
        sig_df, H_star, px_by_ticker, date_idx_by_ticker, cutoff_date)
    universe = universe_by_hold[H_star]

    print("\n" + "=" * 100)
    print(f"FROZEN COMBO N={N_star} type={type_star} H={H_star} — signal funnel")
    print("=" * 100)
    print(meta)

    print("\n--- SEARCH period (in-sample selection; for reference only) ---")
    for preset in ["ideal", "moderate", "conservative"]:
        ts = trade_stats(search_trades, universe, preset)
        ds = date_balanced_hit_edge(search_trades, universe, preset)
        print(f"  [{preset:12s}] daily_hit_edge={ds['daily_hit_edge']:+.2%} "
              f"n_signal_days={ds['n_signal_days']} n_trades={ts['n_trades']} "
              f"hit={ts['hit_rate']:.2%} base={ts['base_rate']:.2%} "
              f"edge={ts['hit_edge']:+.2%} mean_net={ts['mean_net_return']:+.4f} "
              f"ret/risk={ts['ret_per_risk']:+.3f}")

    print("\n--- HOLDOUT period (entry_date > cutoff; frozen params, never touched during selection) ---")
    holdout_results = {}
    for preset in ["ideal", "moderate", "conservative"]:
        ts = trade_stats(holdout_trades, universe, preset)
        ds = date_balanced_hit_edge(holdout_trades, universe, preset)
        holdout_results[preset] = dict(**ds, **ts)
        print(f"  [{preset:12s}] daily_hit_edge={ds['daily_hit_edge']:+.2%} "
              f"n_signal_days={ds['n_signal_days']} n_trades={ts['n_trades']} "
              f"hit={ts['hit_rate']:.2%} base={ts['base_rate']:.2%} "
              f"edge={ts['hit_edge']:+.2%} mean_net={ts['mean_net_return']:+.4f} "
              f"ret/risk={ts['ret_per_risk']:+.3f}")

    if holdout_results["moderate"]["n_signal_days"] < 30:
        print(f"\nCAVEAT: holdout n_signal_days="
              f"{holdout_results['moderate']['n_signal_days']} is < 30 -> treat "
              "the holdout point estimate as unreliable / noisy, not a confirmed edge.")

    # Also show entry-date-level trade log for transparency
    print(f"\nTotal distinct signal-tickers in holdout window: "
          f"{len(set(t['ticker'] for t in holdout_trades))}")
    print(f"Search trades: {len(search_trades)}, Holdout trades: {len(holdout_trades)}")

    return dict(
        N_star=N_star, type_star=type_star, H_star=H_star, cutoff_date=cutoff_date,
        search_trades=search_trades, holdout_trades=holdout_trades,
        holdout_results=holdout_results, grid=grid,
    )


if __name__ == "__main__":
    main()
