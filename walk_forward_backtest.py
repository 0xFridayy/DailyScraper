"""
Roadmap #2 — XGBoost walk-forward backtest.

STUB: not runnable yet. neobdm_scraper.py only started writing live broker_flow
rows (full bval/sval/bavg/savg) to neobdm.db on 2026-07-05 (see
save_daily_broker_flow). 2026-06-08 through 2026-07-04 was backfilled
separately (2026-07-07) from the /inventory/ page's per-broker cumulative
Plotly chart, which has genuine daily history back to 2026-04-06 — the
broker_stalker page itself can't be queried for an arbitrary past date, only
rolling windows anchored to today (Today/2d/3d/.../60d).

Backfill caveats — read before building features on this data:
  - Only `netval` is populated for backfilled rows; bval/sval/bavg/savg are
    NULL (the inventory chart only exposes net position, not the buy/sell
    split). `WHERE bval IS NULL` marks a backfilled (netval-only) row vs a
    live-scraped one.
  - netval is derived as (cum_lot[d] - cum_lot[d-1]) * 100 * close[d], not
    read directly off the site — validated against the live-scraped
    2026-07-05/06 rows (within ~1-5%, from using close price as a stand-in
    for the true weighted avg trade price). Do NOT rederive it from the
    site's cumulative Rp string instead — that string truncates to 2 decimals
    at its display tier (e.g. "Rp -0.14Trl"), which silently rounds whole
    single-day flows of several billion Rupiah down to zero for brokers with
    large cumulative positions.
  - Broker coverage per ticker is whatever the site's own chart renders
    (~11-21 codes typically), not the full BROKER_FLOW_CODES list — a broker
    absent for a given (date, ticker) may mean "no flow" or "not in the
    chart's top set," those are indistinguishable here.
  - ALJI, BTEL, BUMI have no /inventory/ chart at all (no backfill possible);
    BUMI still has its 2 live-scraped days from July 5/6.
  - A `price_history` table now exists (date, ticker, open/high/low/close/
    volume; 2026-04-06 onward, from the same chart) — this is the forward-
    return price source called out as missing below.

Check backfill progress via the "Backfill progress" log lines the nightly
job prints, or query directly:

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
    order_size_uniformity, spread_bps, volume_ratio) doesn't exist yet. Note
    retail_presence_pct and anything needing the bval/sval split can't be
    computed on backfilled rows (NULL there) — only on the live-scraped days.
  - A target/label column (forward return per ticker per day) — `price_history`
    (see above) now has the daily close needed to compute this, but the join
    + return calc itself still needs to be written.
"""

raise NotImplementedError(
    "Blocked on neobdm.db backfill + feature engineering — see module docstring."
)
