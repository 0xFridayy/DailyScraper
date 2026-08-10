"""
Daily automated report (see ml-daily-report.yml): runs the XGBoost
walk-forward backtest (walk_forward_backtest.py), the strategy_variants.py
entry/exit grid search on top of that same signal, and the DDQN entry/exit
agent (ddqn_entry_exit.py) against the full current neobdm.db, then reports
the headline numbers two ways - a short Telegram message (for checking on
your phone) and a fuller markdown table written to the GitHub Actions job
summary (for checking from a browser when you're at a PC). Read-only:
never touches neobdm.db.

SATISFACTION_SHARPE is the holdout bar the user set as "satisfied" (grow a
small portfolio, safest + highest risk-adjusted return, RR = Sharpe 1.5+) -
matches the Sharpe > 1.5 validation bar already documented throughout this
project, not a new number invented for this report. Only strategy_variants
.py's EXISTING 11 variants are searched here (per explicit instruction:
wire in the existing grid first, compare against a bigger one later) - do
not silently expand VARIANTS without that being a deliberate, separate
step. Searching more variants against the same holdout without limit is
the exact data-snooping trap strategy_variants.py's own docstring already
warns about; growing that search space is a decision to make deliberately,
not something to do quietly inside a report script.

send_telegram() is duplicated here (not imported from neobdm_scraper.py) on
purpose - importing that module pulls in Playwright and requires NEOBDM
login credentials, neither of which this report needs.
"""

import os
import sqlite3

import pandas as pd
import requests

from walk_forward_backtest import build_panel, run_walk_forward, DB_PATH
from strategy_variants import run_strategy_search
from ddqn_entry_exit import (
    build_episode_frame, split_search_holdout, fit_normalizer,
    normalize_features, make_envs, train_ddqn, evaluate_policy,
    evaluate_policy_with_trade_log, FEATURES, STATE_EXTRA,
)

RECENT_TRADES_SHOWN = 15
SATISFACTION_SHARPE = 1.5

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    if not resp.ok:
        print(f"Telegram error {resp.status_code}: {resp.text}")


def _format_features(pairs):
    if not pairs:
        return ""
    return ", ".join(f"{name}({val:+.4f})" for name, val in pairs)


def run_xgboost_report(conn):
    panel = build_panel(conn)
    _, pooled, trade_log = run_walk_forward(panel)
    recent = trade_log.tail(RECENT_TRADES_SHOWN).to_dict("records")
    return dict(
        n_dates=panel["date"].nunique(), n_tickers=panel["ticker"].nunique(),
        date_min=panel["date"].min(), date_max=panel["date"].max(),
        sharpe=pooled["sharpe"], hit_rate=pooled["hit_rate"], n_trades=pooled["n_trades"],
        recent_trades=recent,
    )


def run_strategy_variants_report(conn):
    panel = build_panel(conn)
    px = pd.read_sql("SELECT date, ticker, open, high, low, close FROM price_history", conn)
    result = run_strategy_search(panel, px)
    return dict(
        winner_label=result["winner_label"],
        search_sharpe=result["winner_search_sharpe"],
        holdout_sharpe=result["winner_holdout_sharpe"],
        holdout_n=result["winner_holdout_n"],
        satisfied=bool(result["winner_holdout_sharpe"] >= SATISFACTION_SHARPE),
        search_results=result["search_results"],
        holdout_results=result["holdout_results"],
    )


def run_ddqn_report(conn):
    panel = build_episode_frame(conn)
    search_df, holdout_df = split_search_holdout(panel)
    mean, std = fit_normalizer(search_df)
    search_df, holdout_df = search_df.copy(), holdout_df.copy()
    search_df[FEATURES] = normalize_features(search_df, mean, std)
    holdout_df[FEATURES] = normalize_features(holdout_df, mean, std)

    train_envs = make_envs(search_df)
    holdout_envs = make_envs(holdout_df)
    net = train_ddqn(train_envs, state_dim=len(FEATURES) + len(STATE_EXTRA), n_epochs=40)

    # evaluate_policy() gives the aggregate stats (unchanged, cheap inference);
    # evaluate_policy_with_trade_log() reruns the same greedy holdout rollout
    # to also capture per-trade Q-value margins - same trades, same numbers,
    # just also recorded for the "why" detail.
    holdout_trade_log = evaluate_policy_with_trade_log(net, holdout_envs)
    recent = holdout_trade_log.tail(RECENT_TRADES_SHOWN).to_dict("records")

    return dict(
        n_dates=panel["date"].nunique(), n_tickers=panel["ticker"].nunique(),
        date_min=panel["date"].min(), date_max=panel["date"].max(),
        search=evaluate_policy(net, train_envs), holdout=evaluate_policy(net, holdout_envs),
        recent_holdout_trades=recent,
    )


def format_telegram_message(xgb, strat, ddqn):
    xgb_satisfied = xgb["sharpe"] >= SATISFACTION_SHARPE
    hits = []
    if xgb_satisfied:
        hits.append("XGBoost default strategy")
    if strat["satisfied"]:
        hits.append(f"strategy search winner ({strat['winner_label']})")
    if ddqn["holdout"]["trades"]["sharpe"] >= SATISFACTION_SHARPE:
        hits.append("DDQN holdout")
    bar_line = (
        f"🎯 TARGET HIT (holdout Sharpe ≥ {SATISFACTION_SHARPE}): " + ", ".join(hits) + "!"
        if hits else
        f"Nothing has cleared the Sharpe ≥ {SATISFACTION_SHARPE} target yet."
    )

    return (
        f"📈 Daily ML report\n\n"
        f"XGBoost walk-forward ({xgb['n_dates']}d, {xgb['n_tickers']} tickers, "
        f"{xgb['date_min']} to {xgb['date_max']}):\n"
        f"  Sharpe {xgb['sharpe']:.2f} | hit_rate {xgb['hit_rate']:.1%} | n_trades {xgb['n_trades']}\n\n"
        f"Strategy search ({len(strat['search_results'])} entry/exit variants), winner: {strat['winner_label']}\n"
        f"  search  Sharpe {strat['search_sharpe']:.2f}\n"
        f"  holdout Sharpe {strat['holdout_sharpe']:.2f} (n={strat['holdout_n']})\n\n"
        f"DDQN entry/exit ({ddqn['n_dates']}d, {ddqn['n_tickers']} tickers):\n"
        f"  search  Sharpe {ddqn['search']['trades']['sharpe']:.2f} | "
        f"hit_rate {ddqn['search']['trades']['hit_rate']:.1%}\n"
        f"  holdout Sharpe {ddqn['holdout']['trades']['sharpe']:.2f} | "
        f"hit_rate {ddqn['holdout']['trades']['hit_rate']:.1%}\n\n"
        f"{bar_line}\n"
        f"Full run in GitHub Actions."
    )


def _write_variant_table(f, df):
    f.write("| Rank | Strategy | Sharpe | Hit rate | n_trades |\n|---|---|---|---|---|\n")
    for i, (_, row) in enumerate(df.iterrows(), 1):
        f.write(f"| {i} | {row['label']} | {row['sharpe']:.2f} | {row['hit_rate']:.1%} | {row['n_trades']} |\n")


def write_step_summary(xgb, strat, ddqn):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    xt = ddqn["search"]["trades"]
    ht = ddqn["holdout"]["trades"]
    with open(path, "a") as f:
        f.write("# Daily ML Report\n\n")
        f.write(f"## XGBoost walk-forward (default strategy: >0.5% threshold, 1d hold, no TP/SL)\n\n")
        f.write(f"{xgb['n_dates']} dates ({xgb['date_min']} to {xgb['date_max']}), {xgb['n_tickers']} tickers\n\n")
        f.write("| Sharpe | Hit rate | n_trades |\n|---|---|---|\n")
        f.write(f"| {xgb['sharpe']:.2f} | {xgb['hit_rate']:.1%} | {xgb['n_trades']} |\n\n")

        f.write(f"## Strategy search ({len(strat['search_results'])} entry/exit variants on the same XGBoost signal)\n\n")
        f.write(
            f"Winner (by search-period Sharpe): **{strat['winner_label']}** - "
            f"search Sharpe {strat['search_sharpe']:.2f}, holdout Sharpe {strat['holdout_sharpe']:.2f} "
            f"(n={strat['holdout_n']}){' - **hit the Sharpe >= ' + str(SATISFACTION_SHARPE) + ' target**' if strat['satisfied'] else ''}\n\n"
        )
        f.write("### Search period (all variants, ranked)\n\n")
        _write_variant_table(f, strat["search_results"])
        f.write("\n### Holdout period (same ranking positions, never touched during search)\n\n")
        _write_variant_table(f, strat["holdout_results"])
        f.write(
            "\nOnly the winner's HOLDOUT number matters for judging real performance - the other "
            "10 holdout rows are shown for context, not to be cherry-picked after the fact "
            "(that would be the same data-snooping bias the search/holdout split exists to prevent). "
            "See strategy_variants.py's docstring for the caveats on this approach (no transaction "
            "costs/slippage, IDX ARA/ARB limits not simulated here - see ara_arb_simulation.py for that).\n\n"
        )

        f.write(f"## DDQN entry/exit\n\n")
        f.write(f"{ddqn['n_dates']} dates ({ddqn['date_min']} to {ddqn['date_max']}), {ddqn['n_tickers']} tickers\n\n")
        f.write("| Split | Trade Sharpe | Trade hit rate | n_trades |\n|---|---|---|---|\n")
        f.write(f"| Search (train) | {xt['sharpe']:.2f} | {xt['hit_rate']:.1%} | {xt['n_trades']} |\n")
        f.write(f"| Holdout | {ht['sharpe']:.2f} | {ht['hit_rate']:.1%} | {ht['n_trades']} |\n\n")
        f.write(
            f"Target: holdout Sharpe >= {SATISFACTION_SHARPE}. See "
            "`walk_forward_backtest.py` / `strategy_variants.py` / `ddqn_entry_exit.py` docstrings "
            "for full caveats (small sample, backfilled-vs-live data, DDQN overfitting) before "
            "reading any of these numbers as a real edge.\n\n"
        )

        f.write(f"## XGBoost: {len(xgb['recent_trades'])} most recent trades (of {xgb['n_trades']} total)\n\n")
        f.write("Top 3 features by SHAP contribution behind each prediction - not a raw feature "
                "value, but how much that feature pushed the prediction up (+) or down (-).\n\n")
        f.write("| Date | Ticker | Pred | Actual | Top SHAP features |\n|---|---|---|---|---|\n")
        for t in xgb["recent_trades"]:
            f.write(f"| {t['date']} | {t['ticker']} | {t['pred']:+.4f} | {t['actual']:+.4f} | "
                    f"{_format_features(t['top_features'])} |\n")
        f.write("\n")

        f.write(f"## DDQN holdout: {len(ddqn['recent_holdout_trades'])} most recent trades "
                f"(of {ht['n_trades']} total)\n\n")
        f.write("Q-margin = Q(long) - Q(flat) at the decision point - how strongly the agent "
                "preferred that action, not a feature attribution. Notable features are the "
                "top 3 by |z-score| at that point (\"what stood out\"), not a causal explanation "
                "the way SHAP is for XGBoost.\n\n")
        f.write("| Entry date | Exit date | Ticker | Return | Entry Q-margin | Entry notable features | Exit Q-margin |\n"
                "|---|---|---|---|---|---|---|\n")
        for t in ddqn["recent_holdout_trades"]:
            exit_date = t["exit_date"] if not t.get("still_open") else f"{t['exit_date']} (still open)"
            exit_margin = f"{t['exit_margin']:+.4f}" if t.get("exit_margin") is not None else "n/a"
            f.write(f"| {t['entry_date']} | {exit_date} | {t['ticker']} | {t['ret']:+.2%} | "
                    f"{t['entry_margin']:+.4f} | {_format_features(t['entry_notable'])} | {exit_margin} |\n")
        f.write("\n")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    xgb_result = run_xgboost_report(conn)
    strat_result = run_strategy_variants_report(conn)
    ddqn_result = run_ddqn_report(conn)
    conn.close()

    message = format_telegram_message(xgb_result, strat_result, ddqn_result)
    print(message)
    try:
        send_telegram(message)
    except Exception as e:
        print(f"Telegram delivery failed (job summary below still ran): {e}")
    write_step_summary(xgb_result, strat_result, ddqn_result)
