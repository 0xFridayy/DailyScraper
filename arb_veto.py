"""Weekly ARB veto list: names the limit-down model flags as most likely to
hit ARB inside the next five sessions, written where daily_picks.py can read
them with the standard library alone.

Why a veto and not a position. The ARB ranking is the strongest signal this
repo has found -- on the walk-forward, top-1% hits 39.00% against a 3.569%
base rate (lift 10.9x, AUC 0.788), and the top-0.1% Wilson interval starts at
49%, nowhere near the base rate. Trading it directly would mean shorting,
which IDX retail access does not reliably allow. Refusing to buy costs
nothing and needs no borrow.

Why price features and not price+inv. Around the depth this writes, top-1% on
the walk-forward, price-only measured better: 39.00% against 35.38%. price+inv
only wins
further out at top-0.1%, on 36 rows. The simpler model also keeps this script
independent of how well the inventory harvest went.

Why weekly. The label is "an ARB close anywhere in T+1..T+5", so a list is
meaningful for about a trading week -- its staleness is the horizon it was
trained on, not an arbitrary cache. One harvest a week also keeps well clear
of the abnormal-usage throttle neobdm_scraper.py documents.

No walk-forward here. That machinery exists to *evaluate* the ranking, and
ara_multiday.py does it. This script predicts forward, so it trains on all
labelled history and scores the latest session; there is no future to leak.

Pipeline (the first three are the same ones scan_ara_arb.py drives):

    harvest_inventory.py     /api/inventory -> inventory_raw/*.json.gz
    build_inventory_db.py    cache -> ohlc.parquet + broker_daily.parquet
    inventory_features.py    parquet -> panel.parquet
    arb_veto.py              panel -> arb_veto rows in daily_picks.db

Usage:
    python arb_veto.py                 # score and write
    python arb_veto.py --dry-run       # print the list, write nothing
    python arb_veto.py --top-n 8
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ara_multiday import add_horizon_labels  # noqa: E402
from test_inventory_signal import PX_FEATS  # noqa: E402

PANEL = os.path.join(HERE, "panel.parquet")
PICKS_DB = os.path.join(HERE, "daily_picks.db")

# Rp bn 20d turnover. This tracks daily_picks.MIN_VALUE_BN, not the Rp0.5bn
# scan_ara_arb uses: a veto only earns its keep over names that could be
# picked. The first real run floored at 0.5 and its five names -- one of them
# priced at Rp11 -- were all far too thin to clear the picks filter, so the
# list could never have bitten anything.
MIN_TURN = 2.0
HORIZON = 5         # an ARB close anywhere in T+1..T+5
# A count, not a share. The universe this scores swings with the liquidity
# floor (524 names at 0.5, far fewer at 2.0), and a percentage silently turns
# into a different number of vetoes when it moves. What matters is how many
# names get blocked, so say that.
TOP_N = 5
VALID_DAYS = 7      # calendar days covering those five sessions

SCHEMA = """CREATE TABLE IF NOT EXISTS arb_veto (
        as_of TEXT NOT NULL, ticker TEXT NOT NULL, p REAL NOT NULL,
        rank INTEGER NOT NULL, valid_until TEXT NOT NULL,
        recorded_utc TEXT NOT NULL, PRIMARY KEY (as_of, ticker))"""


def model():
    return HistGradientBoostingClassifier(
        max_depth=3, max_iter=250, learning_rate=0.06, min_samples_leaf=60,
        l2_regularization=1.0, class_weight="balanced", random_state=0)


def score(panel_path=PANEL, top_n=TOP_N, min_turn=MIN_TURN, horizon=HORIZON):
    """(as_of date, DataFrame of the veto list) from the latest panel session."""
    d = pd.read_parquet(panel_path)
    d["date"] = pd.to_datetime(d["date"])
    d = d[(d.turn20 >= min_turn) & (d.volume > 0) & d.close.notna()]
    d = d.sort_values(["ticker", "date"]).reset_index(drop=True)
    d = add_horizon_labels(d, horizon)

    feats = [f for f in PX_FEATS if f in d.columns]
    train = d[d.window_ok & d.y_arb_h.notna()]
    if len(train) < 500 or train.y_arb_h.sum() < 25:
        raise SystemExit(f"not enough labelled history to train: {len(train)} rows, "
                         f"{int(train.y_arb_h.sum()) if len(train) else 0} events")

    as_of = d.date.max()
    latest = d[d.date == as_of].copy()
    clf = model()
    clf.fit(train[feats], train.y_arb_h.astype(int))
    latest["p"] = clf.predict_proba(latest[feats])[:, 1]

    n = min(top_n, len(latest))
    top = latest.nlargest(n, "p")[["ticker", "p", "close", "rv20"]].reset_index(drop=True)
    top["rank"] = top.index + 1
    print(f"trained on {len(train):,} rows ({int(train.y_arb_h.sum())} ARB windows), "
          f"scored {len(latest)} names as of {as_of.date()}, vetoing top {n}")
    return as_of, top


def write(as_of, top, picks_db=PICKS_DB, valid_days=VALID_DAYS):
    valid_until = (as_of + timedelta(days=valid_days)).date().isoformat()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(picks_db)
    try:
        conn.execute(SCHEMA)
        conn.executemany(
            "INSERT OR REPLACE INTO arb_veto "
            "(as_of, ticker, p, rank, valid_until, recorded_utc) VALUES (?,?,?,?,?,?)",
            [(as_of.date().isoformat(), r.ticker, float(r.p), int(r.rank),
              valid_until, now) for r in top.itertuples()])
        conn.commit()
    finally:
        conn.close()
    return valid_until


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--picks-db", default=PICKS_DB)
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.panel):
        sys.exit(f"{a.panel} not found -- run harvest_inventory.py -> "
                 f"build_inventory_db.py -> inventory_features.py first")

    as_of, top = score(a.panel, a.top_n)
    for r in top.itertuples():
        print(f"  {r.rank:2d}. {r.ticker:<6s} p={r.p:.3f}  close={r.close:,.0f}  "
              f"rv20={r.rv20:.3f}" if pd.notna(r.rv20) else
              f"  {r.rank:2d}. {r.ticker:<6s} p={r.p:.3f}")
    if a.dry_run:
        print("\n--dry-run: nothing written")
        return
    valid_until = write(as_of, top, a.picks_db)
    print(f"\nwrote {len(top)} rows to {os.path.basename(a.picks_db)}, "
          f"valid through {valid_until}")


if __name__ == "__main__":
    main()
