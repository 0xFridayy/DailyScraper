"""
Roadmap #2 — XGBoost walk-forward backtest.

STUB: not runnable yet. neobdm_scraper.py only started writing broker_flow
rows to neobdm.db on 2026-07-05 (see save_daily_broker_flow). Check backfill
progress via the "Backfill progress" log lines the nightly job prints, or
query directly:

    SELECT ticker, COUNT(DISTINCT date) FROM broker_flow GROUP BY ticker;

Design agreed so far (do not lose these when filling this in):
  - Expanding window, not fixed-size: train grows each cycle
    (e.g. days 0:18, 0:22, 0:26), test is a fixed 4-day slice right after.
    Fixed 18/6 train/test windows don't fit in 30 days for more than 2 cycles.
  - Sharpe must come from a tradeable rule, not raw prediction error:
        position = 1 if pred > 0.005 else 0
        strategy_return = position * actual_return
        sharpe = strategy_return.mean() / strategy_return.std() * sqrt(252)
    Also report n_trades and hit_rate — with this little data, a Sharpe with
    n_trades=2 is noise, not a signal.
  - This first run (30 days, ~3 cycles) is pipeline validation only. Treat
    any Sharpe > 1.5 here as "the code works," not "the edge is real" —
    SYSTEM.md's actual validation bar needs 60+ days of NeoBDM history.

Also unresolved, needed before this can run at all:
  - Feature engineering from raw broker_flow rows (date, ticker, broker_code,
    bval, sval, netval, bavg, savg) to the features SYSTEM.md names
    (broker_concentration, retail_presence_pct, broker_correlation_1d,
    order_size_uniformity, spread_bps, volume_ratio) doesn't exist yet.
  - A target/label column (forward return per ticker per day) — not part of
    broker_flow, needs its own price data source.
"""

raise NotImplementedError(
    "Blocked on neobdm.db backfill + feature engineering — see module docstring."
)
