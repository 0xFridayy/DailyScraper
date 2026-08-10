"""
Daily automated report (see ml-daily-report.yml): runs the XGBoost
walk-forward backtest
(walk_forward_backtest.py) and the DDQN entry/exit agent (ddqn_entry_exit.py)
against the full current neobdm.db, then reports the headline numbers two
ways - a short Telegram message (for checking on your phone) and a fuller
markdown table written to the GitHub Actions job summary (for checking from
a browser when you're at a PC). Read-only: never touches neobdm.db.

send_telegram() is duplicated here (not imported from neobdm_scraper.py) on
purpose - importing that module pulls in Playwright and requires NEOBDM
login credentials, neither of which this report needs.
"""

import os
import sqlite3

import requests

from walk_forward_backtest import build_panel, run_walk_forward, DB_PATH
from ddqn_entry_exit import (
    build_episode_frame, split_search_holdout, fit_normalizer,
    normalize_features, make_envs, train_ddqn, evaluate_policy,
    evaluate_policy_with_trade_log, FEATURES, STATE_EXTRA,
)

RECENT_TRADES_SHOWN = 15

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


def format_telegram_message(xgb, ddqn):
    return (
        f"📈 Daily ML report\n\n"
        f"XGBoost walk-forward ({xgb['n_dates']}d, {xgb['n_tickers']} tickers, "
        f"{xgb['date_min']} to {xgb['date_max']}):\n"
        f"  Sharpe {xgb['sharpe']:.2f} | hit_rate {xgb['hit_rate']:.1%} | n_trades {xgb['n_trades']}\n\n"
        f"DDQN entry/exit ({ddqn['n_dates']}d, {ddqn['n_tickers']} tickers):\n"
        f"  search  Sharpe {ddqn['search']['trades']['sharpe']:.2f} | "
        f"hit_rate {ddqn['search']['trades']['hit_rate']:.1%}\n"
        f"  holdout Sharpe {ddqn['holdout']['trades']['sharpe']:.2f} | "
        f"hit_rate {ddqn['holdout']['trades']['hit_rate']:.1%}\n\n"
        f"Neither has cleared the Sharpe > 1.5 validation bar yet. "
        f"Full run in GitHub Actions."
    )


def write_step_summary(xgb, ddqn):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    xt = ddqn["search"]["trades"]
    ht = ddqn["holdout"]["trades"]
    with open(path, "a") as f:
        f.write("# Daily ML Report\n\n")
        f.write(f"## XGBoost walk-forward\n\n")
        f.write(f"{xgb['n_dates']} dates ({xgb['date_min']} to {xgb['date_max']}), {xgb['n_tickers']} tickers\n\n")
        f.write("| Sharpe | Hit rate | n_trades |\n|---|---|---|\n")
        f.write(f"| {xgb['sharpe']:.2f} | {xgb['hit_rate']:.1%} | {xgb['n_trades']} |\n\n")
        f.write(f"## DDQN entry/exit\n\n")
        f.write(f"{ddqn['n_dates']} dates ({ddqn['date_min']} to {ddqn['date_max']}), {ddqn['n_tickers']} tickers\n\n")
        f.write("| Split | Trade Sharpe | Trade hit rate | n_trades |\n|---|---|---|---|\n")
        f.write(f"| Search (train) | {xt['sharpe']:.2f} | {xt['hit_rate']:.1%} | {xt['n_trades']} |\n")
        f.write(f"| Holdout | {ht['sharpe']:.2f} | {ht['hit_rate']:.1%} | {ht['n_trades']} |\n\n")
        f.write(
            "Neither model has cleared the Sharpe > 1.5 validation bar. See "
            "`walk_forward_backtest.py` / `ddqn_entry_exit.py` docstrings for full caveats "
            "(small sample, backfilled-vs-live data, DDQN overfitting) before reading "
            "either number as a real edge.\n\n"
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
    ddqn_result = run_ddqn_report(conn)
    conn.close()

    message = format_telegram_message(xgb_result, ddqn_result)
    print(message)
    try:
        send_telegram(message)
    except Exception as e:
        print(f"Telegram delivery failed (job summary below still ran): {e}")
    write_step_summary(xgb_result, ddqn_result)
