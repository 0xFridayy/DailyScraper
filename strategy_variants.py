"""
Strategy (not feature) variation: same trained model/entry-signal as
walk_forward_backtest.py, but tests different EXIT mechanics (entry
threshold, holding period, take-profit/stop-loss using actual daily
high/low) to see which combination gives the best Sharpe. Everything up to
this point only varied features while keeping the crude "1-day fixed exit,
0.5% threshold, no TP/SL" strategy constant - this tests the strategy
itself.

Includes a search/holdout split (search_dates = first 70% of predicted
dates, holdout = last 30%, never touched during the grid search) specifically
to guard against picking a variant that just curve-fit the historical
window - trying 11 variants and reporting only the best without an
out-of-sample check would be classic data-snooping bias.

2026-07-07 result: "Tighter entry >1.0%, 3d hold, TP=5%/SL=3%" won the search
period (Sharpe 5.36, n=1317) and held up on the untouched holdout (Sharpe
6.95, n=168, hit_rate 58.9%) - it did NOT collapse out-of-sample the way an
overfit result typically would. See run_strategy_search() for the full
ranked results in both periods.

IMPORTANT CAVEATS before reading too much into this:
  - NO transaction costs, spread, or slippage modeled. Sharpe 5-7 is far
    outside what's normal for a real strategy (2-3 is excellent for a
    professional fund) - at this trade frequency (hundreds of trades),
    IDX brokerage fees + spread could materially erode or eliminate the
    edge. Model this before taking it seriously.
  - IDX has daily price limit rules (ARA/ARB) not simulated here - a stock
    can gap past a TP/SL level or get stuck limit-up/limit-down, which
    would change these fills in practice.
  - The underlying entry signal is still the same model established
    elsewhere (walk_forward_backtest.py, feature_ablation.py) to be
    momentum/volume-driven, not broker-flow-driven. This result is about
    managing exits on THAT signal better (cutting losses early via SL,
    locking gains via TP) - it is not evidence the broker-accumulation
    thesis works.
  - One single 70/30 split, not multiple splits - directionally reassuring,
    not definitive. A more rigorous check would repeat this with several
    different split points.
"""

import sqlite3
import numpy as np
import pandas as pd
from xgboost import XGBRegressor
from walk_forward_backtest import build_panel, FEATURES, XGB_PARAMS, DB_PATH

VARIANTS = [
    ("Baseline: >0.5%, 1d hold, no TP/SL", 0.005, 1, None, None),
    ("Looser entry >0.3%, 1d hold, no TP/SL", 0.003, 1, None, None),
    ("Tighter entry >1.0%, 1d hold, no TP/SL", 0.010, 1, None, None),
    ("Tighter entry >2.0%, 1d hold, no TP/SL", 0.020, 1, None, None),
    ("Same entry, 3d hold, no TP/SL", 0.005, 3, None, None),
    ("Same entry, 5d hold, no TP/SL", 0.005, 5, None, None),
    ("Same entry, 3d hold, TP=3%/SL=3%", 0.005, 3, 0.03, 0.03),
    ("Same entry, 5d hold, TP=5%/SL=3%", 0.005, 5, 0.05, 0.03),
    ("Same entry, 5d hold, TP=3%/SL=5%", 0.005, 5, 0.03, 0.05),
    ("Same entry, 3d hold, TP=2%/SL=2% (tight)", 0.005, 3, 0.02, 0.02),
    ("Tighter entry >1.0%, 3d hold, TP=5%/SL=3%", 0.010, 3, 0.05, 0.03),
]


def get_walk_forward_predictions(panel):
    """Same expanding-window CV as walk_forward_backtest.py, but keeps
    ticker/date on every test-set prediction instead of just aggregating
    Sharpe, so different exit mechanics can be simulated on the same
    entry signal."""
    dates = sorted(panel["date"].unique())
    train_min, test_window = 30, 6
    cycles, train_end = [], train_min
    while train_end + test_window <= len(dates):
        cycles.append((dates[:train_end], dates[train_end:train_end + test_window]))
        train_end += test_window

    pred_rows = []
    for train_dates, test_dates in cycles:
        train_df = panel[panel["date"].isin(train_dates)]
        test_df = panel[panel["date"].isin(test_dates)].copy()
        split = int(len(train_df) * 0.8)
        fit_df, eval_df = train_df.iloc[:split], train_df.iloc[split:]
        model = XGBRegressor(**XGB_PARAMS)
        model.fit(fit_df[FEATURES], fit_df["target"],
                  eval_set=[(eval_df[FEATURES], eval_df["target"])], verbose=False)
        test_df["pred"] = model.predict(test_df[FEATURES])
        pred_rows.append(test_df[["ticker", "date", "pred"]])
    return pd.concat(pred_rows, ignore_index=True)


def _index_price_history(px):
    px_by_ticker = {t: g.sort_values("date").reset_index(drop=True) for t, g in px.groupby("ticker")}
    date_idx_by_ticker = {t: {d: i for i, d in enumerate(g["date"])} for t, g in px_by_ticker.items()}
    return px_by_ticker, date_idx_by_ticker


def simulate_trade(px_by_ticker, date_idx_by_ticker, ticker, entry_date, hold_days, tp_pct, sl_pct):
    """Enter at entry_date's close. Each subsequent day, check that day's
    high/low for a TP/SL hit BEFORE checking the timed exit - a trade that
    hits both TP and SL on the same day is treated as SL (conservative)."""
    g = px_by_ticker.get(ticker)
    idx_map = date_idx_by_ticker.get(ticker)
    if g is None or entry_date not in idx_map:
        return None
    i0 = idx_map[entry_date]
    if i0 + 1 >= len(g):
        return None
    entry_price = g.loc[i0, "close"]

    for k in range(1, hold_days + 1):
        if i0 + k >= len(g):
            break
        day = g.loc[i0 + k]
        if sl_pct is not None and day["low"] <= entry_price * (1 - sl_pct):
            return -sl_pct
        if tp_pct is not None and day["high"] >= entry_price * (1 + tp_pct):
            return tp_pct
        if k == hold_days or i0 + k == len(g) - 1:
            return (day["close"] - entry_price) / entry_price
    return None


def sharpe_from_returns(returns):
    returns = np.array(returns)
    n = len(returns)
    if n == 0:
        return dict(sharpe=np.nan, n_trades=0, hit_rate=np.nan)
    std = returns.std()
    sharpe = (returns.mean() / std * np.sqrt(252)) if std > 0 else np.nan
    return dict(sharpe=sharpe, n_trades=n, hit_rate=(returns > 0).mean())


def eval_variant(preds_subset, px_by_ticker, date_idx_by_ticker, thresh, hold_days, tp, sl):
    triggered = preds_subset[preds_subset["pred"] > thresh]
    trade_returns = [
        r for r in (
            simulate_trade(px_by_ticker, date_idx_by_ticker, row["ticker"], row["date"], hold_days, tp, sl)
            for _, row in triggered.iterrows()
        ) if r is not None
    ]
    return sharpe_from_returns(trade_returns)


def run_strategy_search(panel, px, search_frac=0.7):
    preds = get_walk_forward_predictions(panel)
    px_by_ticker, date_idx_by_ticker = _index_price_history(px)

    pred_dates = sorted(preds["date"].unique())
    split_idx = int(len(pred_dates) * search_frac)
    search_dates = set(pred_dates[:split_idx])
    holdout_dates = set(pred_dates[split_idx:])

    search_preds = preds[preds["date"].isin(search_dates)]
    holdout_preds = preds[preds["date"].isin(holdout_dates)]

    def score_all(preds_subset):
        rows = []
        for label, thresh, hold_days, tp, sl in VARIANTS:
            stats = eval_variant(preds_subset, px_by_ticker, date_idx_by_ticker, thresh, hold_days, tp, sl)
            rows.append(dict(label=label, thresh=thresh, hold_days=hold_days, tp=tp, sl=sl, **stats))
        return pd.DataFrame(rows).sort_values("sharpe", ascending=False)

    search_df = score_all(search_preds)
    holdout_df = score_all(holdout_preds)
    winner_label = search_df.iloc[0]["label"]
    winner_holdout = holdout_df[holdout_df["label"] == winner_label].iloc[0]

    return dict(
        search_dates=(min(search_dates), max(search_dates), len(search_dates)),
        holdout_dates=(min(holdout_dates), max(holdout_dates), len(holdout_dates)),
        search_results=search_df,
        holdout_results=holdout_df,
        winner_label=winner_label,
        winner_search_sharpe=search_df.iloc[0]["sharpe"],
        winner_holdout_sharpe=winner_holdout["sharpe"],
        winner_holdout_n=winner_holdout["n_trades"],
    )


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build_panel(conn)
    px = pd.read_sql("SELECT date, ticker, open, high, low, close FROM price_history", conn)
    conn.close()

    result = run_strategy_search(panel, px)
    print(f"Search period: {result['search_dates'][2]} dates "
          f"({result['search_dates'][0]} to {result['search_dates'][1]})")
    print(f"Holdout period: {result['holdout_dates'][2]} dates "
          f"({result['holdout_dates'][0]} to {result['holdout_dates'][1]})\n")

    print("=== SEARCH PERIOD ===")
    print(result["search_results"].to_string(index=False))
    print(f"\nWinner: {result['winner_label']} (search sharpe={result['winner_search_sharpe']:.2f})")

    print("\n=== HOLDOUT PERIOD (all variants shown for context; only the winner matters) ===")
    print(result["holdout_results"].to_string(index=False))
    print(f"\nWinner's holdout performance: sharpe={result['winner_holdout_sharpe']:.2f} "
          f"(n={result['winner_holdout_n']})")
