"""
ML V2, layer 1 — broker IDENTITY features (roadmap sections 7, 9, 22).

WHAT QUESTION THIS CHANGES
--------------------------
Every broker feature this repo has used so far collapses the broker dimension
before the model ever sees it:

    broker_concentration, net_flow_total, n_brokers, net_buy_ratio,
    retail_presence_pct, broker_correlation_1d

Two tickers can carry an identical net_flow_total and an identical
broker_concentration with completely different buyers behind them. The roadmap's
central claim is that on IDX *who* is buying can matter more than how much is
bought, and none of the features above can express that claim, let alone test
it. So the model has never been asked the question, and "broker flow shows no
edge" was never actually a finding about broker flow — it was a finding about
six summary statistics.

This module keeps the broker dimension: per-broker trailing net flow, and the
observable inventory that flow accumulates into.

WHAT THIS MODULE IS NOT
-----------------------
It carries NO entity, ownership or affinity layer. Roadmap sections 5, 6 and 16
describe those, and section 43 gates them explicitly: build owner-broker
affinity only if per-broker identity first beats the price-only and
aggregate-broker baselines. model_v2_ablation.py runs that test. Until it
passes, an entity graph would be an elaborate structure resting on nothing.

Nor does it carry historical broker alpha (section 8). That one needs
strictly-expanding out-of-sample estimation to avoid exactly the leakage the
roadmap warns about — full-history broker performance pasted back onto history
— and it is P2, after this question is answered.

FEATURE COUNT IS A DESIGN CONSTRAINT, NOT AN AFTERTHOUGHT
---------------------------------------------------------
Roadmap section 22 warns against one-hot explosion, and this panel is small:
~10,000 ticker-days over 45 names that move together (mean pairwise daily
correlation +0.275, so roughly 3.4 independent series). Twenty-nine brokers
times five horizons times several statistics would be several hundred features
fitted to a few thousand effectively-independent observations, and it would
"work" — on the training set.

So the identity layer is deliberately narrow, exactly what section 43's
Experiment #1 specifies and no more:

  - Level 2 (dedicated per-broker features): trailing 5d and 10d net flow for
    brokers meeting MIN_COVERAGE, normalised by the same window's turnover.
  - Level 1/3 (aggregates over all brokers): inventory concentration and
    velocity — a handful of columns, not one per broker.

Widening this is a deliberate, separate decision, the same way strategy
variants are not silently added to the search.

NORMALISATION
-------------
Raw netval is in billions of Rupiah and is not comparable across a Rp 300
small-cap and a Rp 30tn name. Every identity feature is divided by the SAME
window's traded value, so it reads as "this broker's net flow was X% of what
traded" — dimensionless, comparable across tickers and across time.

LEAKAGE POSTURE
---------------
Every feature uses data up to and including day t's close; targets look forward
from day t's close. This matches the existing convention (momentum_1d is today's
realised return, which is known at today's close).

The one judgement call worth naming: the broker universe is selected on
full-history COVERAGE — how often a broker appears at all — which is computed
over dates later than some of the rows it is applied to. That is not the leakage
roadmap section 8 forbids, because coverage uses no return information
whatsoever; it answers "is this broker in the feed", not "is this broker good".
Selecting on broker PERFORMANCE that way would be leakage, and is why historical
broker alpha is not in this module.

GAP HANDLING
------------
Prices come from price_audit.clean_panel(), so quarantined rows and the windows
spanning them are already gone (HANDOFF stage 1). Trailing FLOW sums over a
surviving-row window can still span a hole — five available days covering six
calendar days. That is deliberate and is the same call made for volume_ratio: a
summed flow over a slightly wider window is a mild distortion of a level, not a
fabricated observation. Returns are the thing that must never bridge a hole,
and they do not.
"""

import os
import sqlite3

import numpy as np
import pandas as pd

from price_audit import clean_panel

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neobdm.db")

# A broker needs to appear on this share of ticker-days to earn dedicated
# columns. At 0.70 the feed resolves 19 of 29 brokers; the tail below it is
# present on a third of days or fewer, where a "trailing 10d net flow" is mostly
# describing whether the broker showed up at all.
MIN_COVERAGE = 0.70

# Trailing windows for the identity layer. Exactly the pair roadmap section 43
# names for Experiment #1 — resist adding more before the experiment answers.
FLOW_WINDOWS = (5, 10)

# Observable-inventory anchor (section 9.1). A rolling window, not "since the
# start of data": the section lists several anchors and a rolling one is the
# only one that needs no structural-low or corporate-action detection to be
# well defined. It costs the first INVENTORY_WINDOW rows of every ticker.
INVENTORY_WINDOW = 60
INVENTORY_DELTAS = (3, 5, 10)

LOT_SIZE = 100  # IDX round lot, in shares


# ── broker universe ───────────────────────────

def broker_coverage(bf, n_ticker_days):
    """Share of ticker-days each broker appears on, most-covered first."""
    return (bf.groupby("broker_code").size() / n_ticker_days).sort_values(ascending=False)


def identity_brokers(bf, n_ticker_days, min_coverage=MIN_COVERAGE):
    cov = broker_coverage(bf, n_ticker_days)
    return sorted(cov[cov >= min_coverage].index.tolist())


# ── panel assembly ────────────────────────────

def _wide(bf, values, index):
    """(ticker, date) x broker matrix, aligned to `index`.

    A broker missing from a ticker-day is a real zero: the feed lists every
    broker with flow that day, so absence means no net flow, not unknown flow.
    Filling with NaN instead would make every trailing sum NaN the moment one
    broker sat out one day.
    """
    w = bf.pivot_table(index=["ticker", "date"], columns="broker_code",
                       values=values, aggfunc="sum")
    return w.reindex(index).fillna(0.0)


def _trailing_sum(w, window):
    """Trailing sum per ticker, requiring a full window (no partial ramp-up)."""
    out = w.groupby(level="ticker", group_keys=False).apply(
        lambda g: g.rolling(window, min_periods=window).sum())
    return out.reindex(w.index)


def build_identity_panel(conn, horizons=(1, 5, 10), min_coverage=MIN_COVERAGE,
                         inventory_window=INVENTORY_WINDOW):
    """Clean prices + targets + per-broker identity and inventory features.

    Returns one row per surviving ticker-day, carrying:
      ret_{h}, max_{h}, mdd_{h}     targets, gap-guarded by clean_panel()
      momentum_1d, volume_ratio     the price-only baseline (set A)
      {BROKER}_net_{w}d             set C, normalised by the window's turnover
      inv_*                         set D's aggregates
    """
    px = clean_panel(conn, horizons=horizons, lags=(1, 5), extremes=True)
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)

    px["momentum_1d"] = px["lag_1"]
    px["momentum_5d"] = px["lag_5"]
    px["vol_ma5"] = px.groupby("ticker")["volume"].transform(
        lambda s: s.shift(1).rolling(5).mean())
    px["volume_ratio"] = px["volume"] / px["vol_ma5"]
    px["turnover"] = px["close"] * px["volume"]          # Rupiah traded that day
    px["lots"] = px["volume"] / LOT_SIZE

    for h in horizons:
        px[f"ret_{h}"] = px[f"fwd_{h}"]

    bf = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    # Inner-join semantics: broker rows on a quarantined price day are dropped
    # with it. netval is close-derived (netval = nlot * 100 * close / 1e9), so a
    # contaminated close makes a proportionally wrong netval - those rows would
    # be feeding the identity layer exactly the numbers stage 1 removed.
    keys = set(zip(px["ticker"], px["date"]))
    bf = bf[[(t, d) in keys for t, d in zip(bf["ticker"], bf["date"])]]

    index = pd.MultiIndex.from_arrays([px["ticker"], px["date"]], names=["ticker", "date"])
    brokers = identity_brokers(bf, len(px), min_coverage=min_coverage)

    netval = _wide(bf, "netval", index)                   # billions of Rupiah
    turnover = pd.Series(px["turnover"].to_numpy(), index=index)
    lots = pd.Series(px["lots"].to_numpy(), index=index)

    feats = {}

    # ── Level 2: per-broker trailing net flow, as a share of traded value ──
    for w in FLOW_WINDOWS:
        flow_w = _trailing_sum(netval[brokers], w) * 1e9
        turn_w = _trailing_sum(turnover.to_frame("t"), w)["t"]
        for b in brokers:
            feats[f"{b}_net_{w}d"] = (flow_w[b] / turn_w).to_numpy()

    # ── observable inventory (section 9) ──
    # netval = nlot * LOT_SIZE * close / 1e9, so the lot count is recoverable
    # exactly. Lots, not Rupiah: "how much stock moved" is the quantity that
    # stays comparable while the price is moving, which is the whole point of
    # measuring accumulation.
    close = pd.Series(px["close"].to_numpy(), index=index)
    net_lot = netval.div(close, axis=0) * 1e9 / LOT_SIZE
    inv = _trailing_sum(net_lot, inventory_window)         # observable net inventory
    lots_w = _trailing_sum(lots.to_frame("l"), inventory_window)["l"]

    pos = inv.clip(lower=0)                                # accumulators only
    pos_total = pos.sum(axis=1)
    share = pos.div(pos_total.replace(0, np.nan), axis=0)

    ranked = np.sort(share.to_numpy(), axis=1)[:, ::-1]    # descending per row
    feats["inv_top1_share"] = ranked[:, 0]
    feats["inv_top3_share"] = np.nansum(ranked[:, :3], axis=1)
    feats["inv_hhi"] = (share ** 2).sum(axis=1).to_numpy()
    feats["inv_pct_lots"] = (pos_total / lots_w).to_numpy()

    # Velocity (section 9.3): is the inventory still being built, or unwound?
    # Differenced within ticker, so a ticker's first rows are NaN rather than
    # differenced against another ticker's last rows.
    pos_by_ticker = pos_total.groupby(level="ticker")
    for d in INVENTORY_DELTAS:
        feats[f"inv_chg_{d}d"] = (
            (pos_total - pos_by_ticker.shift(d)) / lots_w).to_numpy()

    out = pd.concat([px.reset_index(drop=True),
                     pd.DataFrame(feats).reset_index(drop=True)], axis=1)
    out.attrs["identity_brokers"] = brokers
    out.attrs["identity_features"] = [f"{b}_net_{w}d" for w in FLOW_WINDOWS for b in brokers]
    out.attrs["inventory_features"] = (
        ["inv_top1_share", "inv_top3_share", "inv_hhi", "inv_pct_lots"]
        + [f"inv_chg_{d}d" for d in INVENTORY_DELTAS])
    return out


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build_identity_panel(conn)
    bf = pd.read_sql("SELECT ticker, broker_code FROM broker_flow", conn)
    conn.close()

    ident = panel.attrs["identity_features"]
    invf = panel.attrs["inventory_features"]
    print(f"panel: {len(panel)} rows, {panel['date'].nunique()} dates, "
          f"{panel['ticker'].nunique()} tickers")
    print(f"identity brokers ({len(panel.attrs['identity_brokers'])}): "
          f"{', '.join(panel.attrs['identity_brokers'])}")
    print(f"{len(ident)} identity features + {len(invf)} inventory features\n")

    cov = broker_coverage(bf, len(panel))
    print("broker coverage (share of ticker-days):")
    print(cov.round(3).to_string())

    print("\nnon-NaN share per feature family:")
    for name, cols in (("price", ["momentum_1d", "volume_ratio"]),
                       ("identity", ident), ("inventory", invf)):
        frac = panel[cols].notna().all(axis=1).mean()
        print(f"  {name:10s} {frac:.1%} of rows complete")
