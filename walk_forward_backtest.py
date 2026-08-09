"""
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
from xgboost import XGBRegressor

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neobdm.db")
RETAIL_BROKERS = {"XL", "XC", "YP", "PD"}

FEATURES = [
    "broker_concentration", "net_flow_total", "n_brokers", "net_buy_ratio",
    "retail_presence_pct", "broker_correlation_1d", "momentum_1d", "volume_ratio",
]

XGB_PARAMS = dict(
    max_depth=4, learning_rate=0.05, n_estimators=100,
    subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
    early_stopping_rounds=10, eval_metric="mae",
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
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["prev_close"] = px.groupby("ticker")["close"].shift(1)
    px["momentum_1d"] = (px["close"] - px["prev_close"]) / px["prev_close"]
    px["vol_ma5"] = px.groupby("ticker")["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    px["volume_ratio"] = px["volume"] / px["vol_ma5"]
    px["next_close"] = px.groupby("ticker")["close"].shift(-1)
    px["target"] = (px["next_close"] - px["close"]) / px["close"]
    return px[["ticker", "date", "momentum_1d", "volume_ratio", "target"]]


def build_panel(conn):
    bf = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    px = pd.read_sql("SELECT date, ticker, close, volume FROM price_history", conn)

    agg = _broker_day_aggregates(bf)
    corr = _broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")
    pxf = _price_features_and_target(px)

    panel = agg.merge(pxf, on=["ticker", "date"], how="inner")
    panel = panel.dropna(subset=["target"]).sort_values("date").reset_index(drop=True)
    return panel


def sharpe_stats(pred, actual):
    position = (pred > 0.005).astype(int)
    strategy_return = position * actual
    n_trades = int(position.sum())
    if n_trades == 0:
        return dict(sharpe=np.nan, n_trades=0, hit_rate=np.nan)
    traded = strategy_return[position == 1]
    std = traded.std()
    sharpe = (traded.mean() / std * np.sqrt(252)) if std > 0 else np.nan
    hit_rate = (actual[position == 1] > 0).mean()
    return dict(sharpe=sharpe, n_trades=n_trades, hit_rate=hit_rate)


def run_walk_forward(panel, train_min=30, test_window=6):
    dates = sorted(panel["date"].unique())
    n = len(dates)
    cycles = []
    train_end = train_min
    while train_end + test_window <= n:
        cycles.append((dates[:train_end], dates[train_end:train_end + test_window]))
        train_end += test_window

    results = []
    all_preds, all_actuals = [], []
    for i, (train_dates, test_dates) in enumerate(cycles, 1):
        train_df = panel[panel["date"].isin(train_dates)]
        test_df = panel[panel["date"].isin(test_dates)]

        split = int(len(train_df) * 0.8)
        fit_df, eval_df = train_df.iloc[:split], train_df.iloc[split:]

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

        stats = sharpe_stats(pd.Series(test_pred, index=test_df.index), test_df["target"])
        results.append(dict(
            cycle=i, train_days=len(train_dates), test_days=len(test_dates),
            train_mae=train_mae, eval_mae=eval_mae, test_mae=test_mae, **stats,
        ))
        all_preds.append(test_pred)
        all_actuals.append(test_df["target"].values)

    pooled_stats = sharpe_stats(pd.Series(np.concatenate(all_preds)), pd.Series(np.concatenate(all_actuals)))
    return pd.DataFrame(results), pooled_stats


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build_panel(conn)
    conn.close()

    print(f"Panel: {len(panel)} ticker-day rows, {panel['date'].nunique()} dates, "
          f"{panel['ticker'].nunique()} tickers")

    cycle_results, pooled = run_walk_forward(panel)
    print("\nPer-cycle results:")
    print(cycle_results.to_string(index=False))

    print(f"\nPooled across all test cycles: sharpe={pooled['sharpe']:.2f} "
          f"n_trades={pooled['n_trades']} hit_rate={pooled['hit_rate']:.2%}"
          if pooled["n_trades"] else "\nPooled: no trades triggered across any test cycle.")

    print(
        f"\nNOTE: this is pipeline validation on {panel['date'].nunique()} days of "
        "mostly-reconstructed (netval-only) data. A Sharpe > 1.5 here means the code "
        "runs end-to-end, not that the edge is real - see SYSTEM.md (needs 60+ days "
        "of live NeoBDM history, sustained, to actually validate). The 2026-07-07 run "
        "on the full 218-day backfill found a positive but sub-threshold Sharpe that "
        "SHAP/ablation trace to price momentum, not broker flow - see this module's "
        "docstring before treating this number as progress toward the actual thesis."
    )
