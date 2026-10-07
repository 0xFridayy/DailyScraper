"""
The DDQN episode frame: the torch-free data half of ddqn_entry_exit.py.

ddqn_entry_exit.py imports torch at module level and the ml-health CI does not
install torch, so the frame the DDQN environment steps through is built here,
where CI can run it. ddqn_entry_exit re-exports build_episode_frame; the
environment, network, training and evaluation stay there, unchanged.

BROKER FLOW INPUT (HANDOFF Lampiran W). The same contract as
walk_forward_backtest.build_panel() (Lampiran V), through the same code:
walk_forward_backtest.load_canonical_inputs() reads the canonical broker flow
under an EXPLICIT manifest and the clean prices from one verified SQLite
snapshot. Feature date = canonical_session_date, never acquisition_date; only
PROVEN rows (INFERRED_ONLY / MIXED excluded, quarantined sessions such as
2026-07-03 contribute nothing); identical acquisition copies are collapsed by
the reader, never summed; an absent broker stays absent and NULL netval stays
NaN. No manifest -> refused. There is no raw broker_flow fallback, and nothing
here ever generates or refreshes a manifest:

    python broker_flow_manifest_refresh.py --db neobdm.db --out <manifest>
    python ddqn_entry_exit.py --broker-flow-manifest <manifest>

Prices are exactly what they were: clean_panel(conn, horizons=(), lags=(1, 5)),
momentum_1d, volume_ratio, daily_return and annotate_limits()' at_ara / at_arb.

EPISODES. TickerEnv steps from one row of its episode to the next, so every
(ticker, episode_id) group must be consecutive sessions of the clean price
session axis (every date clean_panel returns for any ticker; weekends and
exchange holidays are not on it, so they are not holes). The episode id is
assigned on the FINAL frame, after the broker join: a row whose previous row
for that ticker is not the previous session starts a new episode. That one
rule splits at every hole there is - a price gap or corporate action (that
row has no daily_return and is dropped), a session the canonical reader
withheld for everyone, and a session one ticker has no canonical broker rows
for. Nothing is filled: a missing broker feature is a missing row.

broker_correlation_1d compares a ticker's broker vector with the ticker's
previous broker-flow session. It is kept only where that is the previous
price session; across any hole (withheld session, as in build_panel(), and
here also a ticker's own missing session) it is NaN rather than a correlation
with an older session that the environment would read as adjacent.

POINT-IN-TIME: NOT PROVEN. canonical_session_date is the session a row belongs
to, not when it was known. This frame is session-aligned; it is not thereby
leakage-free or a live-tradable EOD(T) state. Provenance (same contract as
build_panel's): panel.attrs["broker_flow"].
"""

import numpy as np
import pandas as pd

import walk_forward_backtest as wfb
from ara_arb_simulation import annotate_limits

CLI_DESCRIPTION = ("DDQN entry/exit agent (Roadmap #5) on the canonical broker-flow "
                   "episode frame.")


def session_episode_ids(panel, sessions):
    """Per-ticker episode counter (1, 2, ...) for `panel`, sorted by (ticker,
    date): a new episode starts wherever the ticker's previous row is not the
    immediately preceding entry of `sessions` (the sorted clean price session
    axis)."""
    from price_contract_frame import default_registry
    registry = default_registry()
    pos = panel["date"].map({d: i for i, d in enumerate(sessions)})
    cuts = pos.groupby(panel["ticker"]).diff().ne(1)
    previous = panel.groupby("ticker")["date"].shift(1)
    for i, (ticker, start, end) in enumerate(zip(panel.ticker, previous, panel.date)):
        if isinstance(start, str) and any(start < e.session <= end for e in registry.matching(ticker, "REGULAR")):
            cuts.iloc[i] = True
    return cuts.groupby(panel["ticker"]).cumsum()


def _previous_session_observed(bf, sessions):
    """(ticker, date) keys of `bf` whose previous broker-flow date for that
    ticker is the previous session of `sessions`: the only keys where
    _broker_correlation_1d compared adjacent sessions."""
    keys = bf[["ticker", "date"]].drop_duplicates().sort_values(["ticker", "date"])
    pos = keys["date"].map({d: i for i, d in enumerate(sessions)})
    return pd.MultiIndex.from_frame(keys[pos.groupby(keys["ticker"]).diff().eq(1)])


def build_episode_frame(conn, *, broker_flow_db_path, broker_flow_manifest_path):
    """Like walk_forward_backtest.build_panel(), but keeps daily_return and
    ARA/ARB flags per ticker/session instead of collapsing to an aggregated
    statistic - the DDQN environment needs to step through sessions in order.

    Both broker-flow arguments are required; see load_canonical_inputs() for
    the snapshot checks and what it raises. Provenance:
    panel.attrs["broker_flow"]."""
    from price_contract import refuse_unmigrated
    refuse_unmigrated("ddqn_episode_data.build_episode_frame")
    px, bf, canonical, image = wfb.load_canonical_inputs(
        conn, broker_flow_db_path=broker_flow_db_path,
        broker_flow_manifest_path=broker_flow_manifest_path,
        horizons=(), lags=(1, 5))
    sessions = sorted(px["date"].unique())

    agg = wfb._broker_day_aggregates(bf)
    corr = wfb._broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")

    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["momentum_1d"] = px["lag_1"]
    px["vol_ma5"] = px.groupby("ticker")["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    px["vol_ma5"] = px["vol_ma5"].where(px["lag_5"].notna())
    px["volume_ratio"] = px["volume"] / px["vol_ma5"]
    px["daily_return"] = px["momentum_1d"]  # today's close-over-close return, same quantity walk_forward_backtest calls "target" one day earlier
    px = annotate_limits(px)  # adds at_ara / at_arb, using the same tiered/flat bounds as ara_arb_simulation.py

    panel = agg.merge(
        px[["ticker", "date", "momentum_1d", "volume_ratio", "daily_return", "at_ara", "at_arb"]],
        on=["ticker", "date"], how="inner",
    )
    panel = panel.dropna(subset=["daily_return"]).sort_values(["ticker", "date"]).reset_index(drop=True)
    bridged = ~pd.MultiIndex.from_frame(panel[["ticker", "date"]]).isin(
        _previous_session_observed(bf, sessions))
    panel.loc[bridged, "broker_correlation_1d"] = np.nan
    panel["episode_id"] = session_episode_ids(panel, sessions)
    panel.attrs["broker_flow"] = wfb.broker_flow_provenance(canonical, image)
    return panel


def parse_cli(argv=None):
    """ddqn_entry_exit.py's CLI: --db and a REQUIRED --broker-flow-manifest
    (walk_forward_backtest.parse_cli); a missing manifest is a usage error."""
    return wfb.parse_cli(argv, description=CLI_DESCRIPTION)
