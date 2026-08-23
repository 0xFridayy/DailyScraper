"""
Daily automated report (see ml-daily-report.yml): runs the XGBoost
walk-forward backtest (walk_forward_backtest.py), the strategy_variants.py
entry/exit grid search on top of that same signal, and the DDQN entry/exit
agent (ddqn_entry_exit.py) against the full current neobdm.db, then reports
the headline numbers two ways - a short Telegram message (for checking on
your phone) and a fuller markdown table written to the GitHub Actions job
summary (for checking from a browser when you're at a PC). Read-only:
never touches neobdm.db.

This report no longer computes a Sharpe. The one this repo used was wrong twice
over - per-trade returns annualised with the daily sqrt(252) factor, and 45
cross-sectionally correlated tickers counted as 45 independent draws - so both
the numbers and the SATISFACTION_SHARPE = 1.5 bar they were compared against are
VOID. The bar was the user's target for a CORRECT statistic, so choosing its
replacement is their call; this script reports what was measured (IC,
top-decile hit rate against the universe base rate, return edge) and declares no
winner. See signal_metrics.py. Only strategy_variants
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
from signal_metrics import trade_stats, format_trade_stats

RECENT_TRADES_SHOWN = 15

# SATISFACTION_SHARPE = 1.5 used to gate this report. It is gone, not moved:
# nothing here computes a Sharpe any more, because the one this repo computed was
# wrong twice over (see signal_metrics.py). The 1.5 bar was the USER's stated
# target, so replacing it with some other number is the user's call, not this
# script's. Until they set one, the report states what was measured and refuses
# to declare a winner.
KONGLO_TRACK_DAYS = 3  # per explicit instruction: track flagged konglo tickers 1-3 trading days

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
        pooled=pooled, recent_trades=recent,
    )


def run_strategy_variants_report(conn):
    panel = build_panel(conn)
    px = pd.read_sql("SELECT date, ticker, open, high, low, close FROM price_history", conn)
    result = run_strategy_search(panel, px)
    return dict(
        winner_label=result["winner_label"],
        search_mean=result["winner_search_mean"],
        holdout_mean=result["winner_holdout_mean"],
        holdout_n=result["winner_holdout_n"],
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


def run_konglo_watch_report(conn, max_days=KONGLO_TRACK_DAYS):
    """For every konglo ticker flagged by neobdm_scraper.record_konglo_signals()
    (recorded live, whenever it shows up in the daily radar - Top Akum
    Bandar / Dashboard / Broker Stalker), reports its performance since the
    flag day's close over the next up-to-`max_days` trading days:
    highest % reached (using intraday high, not just close) and max
    drawdown % (using intraday low) - exactly as requested, not a
    close-to-close approximation. A signal with days_elapsed < max_days is
    still "active"; once it reaches max_days it's "resolved" and its final
    (day-max_days close) return feeds a pooled Sharpe across all resolved
    signals - a single signal's own Sharpe isn't statistically meaningful
    (n=1), same caveat as every other Sharpe in this repo, so this only
    reports one once there's more than a handful of resolved signals."""
    try:
        watch = pd.read_sql("SELECT flag_date, ticker, sources FROM konglo_signal_watch", conn)
    except pd.errors.DatabaseError:
        watch = pd.DataFrame(columns=["flag_date", "ticker", "sources"])
    if watch.empty:
        return dict(signals=[], resolved=trade_stats([]))

    px = pd.read_sql("SELECT date, ticker, high, low, close FROM price_history", conn)
    px_by_ticker = {t: g.sort_values("date").reset_index(drop=True) for t, g in px.groupby("ticker")}

    signals = []
    resolved_returns = []
    for _, row in watch.iterrows():
        g = px_by_ticker.get(row["ticker"])
        if g is None:
            continue
        idx = g.index[g["date"] == row["flag_date"]]
        if len(idx) == 0:
            continue  # price_history hasn't caught up to the flag day yet
        i0 = idx[0]
        window = g.iloc[i0 + 1: i0 + 1 + max_days]
        if window.empty:
            continue
        entry_close = g.loc[i0, "close"]
        days_elapsed = len(window)
        resolved = days_elapsed >= max_days
        entry = dict(
            ticker=row["ticker"], flag_date=row["flag_date"], sources=row["sources"],
            days_elapsed=days_elapsed, resolved=resolved,
            current_pct=(window["close"].iloc[-1] - entry_close) / entry_close,
            highest_pct=(window["high"].max() - entry_close) / entry_close,
            drawdown_pct=(window["low"].min() - entry_close) / entry_close,
        )
        signals.append(entry)
        if resolved:
            resolved_returns.append(entry["current_pct"])

    signals.sort(key=lambda e: e["flag_date"], reverse=True)
    return dict(signals=signals, resolved=trade_stats(resolved_returns))


def format_telegram_message(xgb, strat, ddqn, konglo):
    # No pass/fail line any more. The old one compared a Sharpe that was wrong
    # twice over against a bar the user set for a correct one; keeping the shape
    # while changing the metric underneath would be worse than dropping it.
    # The number that answers "is the signal any good" is hit_edge: the
    # top-decile hit rate MINUS the universe base rate. Zero means no
    # directional information, whatever the raw hit rate looks like.
    p = xgb["pooled"]
    edge_line = (
        f"Signal edge: top-decile hit {p['top_hit']:.1%} vs base {p['base_rate']:.1%} "
        f"= {p['top_hit_edge']:+.1%}, IC {p['ic']:+.3f}"
    )
    bar_line = (
        edge_line + "\n"
        "No Sharpe target line: Sharpe is no longer computed (see signal_metrics.py). "
        "A replacement bar needs your call."
    )

    active = [s for s in konglo["signals"] if not s["resolved"]]
    konglo_lines = [f"\n🔭 Konglo radar watch ({KONGLO_TRACK_DAYS}d tracking, {len(active)} active):"]
    for s in active[:8]:
        konglo_lines.append(
            f"  {s['ticker']} d{s['days_elapsed']} ({s['flag_date']}): "
            f"now {s['current_pct']:+.1%} | high {s['highest_pct']:+.1%} | dd {s['drawdown_pct']:+.1%}"
        )
    if konglo["resolved"]["n_trades"] >= 5:
        konglo_lines.append(
            "  Resolved: " + format_trade_stats(konglo["resolved"])
        )
    elif konglo["resolved"]["n_trades"] > 0:
        konglo_lines.append(f"  {konglo['resolved']['n_trades']} resolved so far - too few to read.")
    konglo_section = "\n".join(konglo_lines) if active or konglo["resolved"]["n_trades"] else ""

    return (
        f"📈 Daily ML report\n\n"
        f"XGBoost walk-forward ({xgb['n_dates']}d, {xgb['n_tickers']} tickers, "
        f"{xgb['date_min']} to {xgb['date_max']}):\n"
        f"  IC {p['ic']:+.3f} | top-decile hit {p['top_hit']:.1%} vs base {p['base_rate']:.1%} "
        f"({p['top_hit_edge']:+.1%}) | return edge {p['edge']:+.2%}\n"
        f"  threshold rule: n={p['n_trades']} mean {p['trade_mean']:+.2%} "
        f"hit {p['trade_hit']:.1%} ({p['trade_hit_edge']:+.1%})\n\n"
        f"Strategy search ({len(strat['search_results'])} entry/exit variants), winner: {strat['winner_label']}\n"
        f"  search  mean/trade {strat['search_mean']:+.2%}\n"
        f"  holdout mean/trade {strat['holdout_mean']:+.2%} (n={strat['holdout_n']})\n\n"
        f"DDQN entry/exit ({ddqn['n_dates']}d, {ddqn['n_tickers']} tickers):\n"
        f"  search  {format_trade_stats(ddqn['search']['trades'])}\n"
        f"  holdout {format_trade_stats(ddqn['holdout']['trades'])}\n"
        f"{konglo_section}\n\n"
        f"{bar_line}\n"
        f"Full run in GitHub Actions."
    )


def _write_variant_table(f, df):
    f.write("| Rank | Strategy | Mean/trade | Median | Hit rate | vs base | ret/risk | n |\n"
            "|---|---|---|---|---|---|---|---|\n")
    for i, (_, row) in enumerate(df.iterrows(), 1):
        base = "n/a" if pd.isna(row.get("hit_edge")) else f"{row['hit_edge']:+.1%}"
        rpr = "n/a" if pd.isna(row.get("ret_per_risk")) else f"{row['ret_per_risk']:.2f}"
        f.write(f"| {i} | {row['label']} | {row['mean_ret']:+.2%} | {row['median_ret']:+.2%} | "
                f"{row['hit_rate']:.1%} | {base} | {rpr} | {row['n_trades']} |\n")


def write_step_summary(xgb, strat, ddqn, konglo):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    xt = ddqn["search"]["trades"]
    ht = ddqn["holdout"]["trades"]
    with open(path, "a") as f:
        f.write("# Daily ML Report\n\n")
        f.write(f"## XGBoost walk-forward (default strategy: >0.5% threshold, 1d hold, no TP/SL)\n\n")
        f.write(f"{xgb['n_dates']} dates ({xgb['date_min']} to {xgb['date_max']}), {xgb['n_tickers']} tickers\n\n")
        p = xgb["pooled"]
        f.write("| IC | Top-decile hit | Base rate | Hit edge | Return edge | n |\n"
                "|---|---|---|---|---|---|\n")
        f.write(f"| {p['ic']:+.3f} | {p['top_hit']:.1%} | {p['base_rate']:.1%} | "
                f"{p['top_hit_edge']:+.1%} | {p['edge']:+.2%} | {p['n']} |\n\n")
        f.write("**Hit edge is the number that matters.** A top-decile hit rate only means "
                "something next to the base rate of the same rows; this repo reported 42.8% "
                "for months while the base rate was also 42.8%, i.e. no directional "
                "information at all. See `signal_metrics.py`.\n\n")
        f.write(f"Threshold rule (>{0.5}% pred): n={p['n_trades']}, mean {p['trade_mean']:+.2%}, "
                f"hit {p['trade_hit']:.1%} ({p['trade_hit_edge']:+.1%} vs base)\n\n")

        f.write(f"## Strategy search ({len(strat['search_results'])} entry/exit variants on the same XGBoost signal)\n\n")
        f.write(
            f"Winner (by search-period mean return per trade): **{strat['winner_label']}** - "
            f"search {strat['search_mean']:+.2%}/trade, holdout {strat['holdout_mean']:+.2%}/trade "
            f"(n={strat['holdout_n']})\n\n"
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
        f.write("| Split | Mean/trade | Median | Hit rate | ret/risk | n |\n|---|---|---|---|---|---|\n")
        for name, d in (("Search (train)", xt), ("Holdout", ht)):
            rpr = "n/a" if pd.isna(d["ret_per_risk"]) else f"{d['ret_per_risk']:.2f}"
            f.write(f"| {name} | {d['mean_ret']:+.2%} | {d['median_ret']:+.2%} | "
                    f"{d['hit_rate']:.1%} | {rpr} | {d['n_trades']} |\n")
        f.write("\n")
        f.write(
            "No Sharpe and no target line: the Sharpe this repo used was wrong twice over "
            "(per-trade returns annualised as if daily, and cross-sectionally overlapping "
            "observations counted as independent), and the 1.5 bar was set for a correct one. "
            "A replacement bar is the user's call. See `signal_metrics.py`, and "
            "`walk_forward_backtest.py` / `strategy_variants.py` / `ddqn_entry_exit.py` docstrings "
            "for full caveats (small sample, backfilled-vs-live data, DDQN overfitting) before "
            "reading any of these numbers as a real edge.\n\n"
        )

        f.write(f"## Konglo radar watch ({KONGLO_TRACK_DAYS}-day tracking from signal-day close)\n\n")
        f.write(
            "Every time a tracked konglo ticker shows up in a daily radar section (Top Akum Bandar, "
            "Dashboard presets, or Broker Stalker), it gets tracked here for the next "
            f"{KONGLO_TRACK_DAYS} trading days. Highest % and drawdown % use intraday high/low "
            "(not just close), same as strategy_variants.py's TP/SL checks. A signal only feeds "
            f"the pooled Sharpe once it resolves ({KONGLO_TRACK_DAYS} days elapsed) - a single "
            "signal's own Sharpe isn't statistically meaningful.\n\n"
        )
        if not konglo["signals"]:
            f.write("No konglo tickers have appeared in the daily radar yet.\n\n")
        else:
            f.write("| Flag date | Ticker | Source(s) | Days elapsed | Status | Return so far | Highest % | Drawdown % |\n"
                    "|---|---|---|---|---|---|---|---|\n")
            for s in konglo["signals"]:
                status = "resolved" if s["resolved"] else "active"
                f.write(f"| {s['flag_date']} | {s['ticker']} | {s['sources']} | {s['days_elapsed']}/{KONGLO_TRACK_DAYS} | "
                        f"{status} | {s['current_pct']:+.2%} | {s['highest_pct']:+.2%} | {s['drawdown_pct']:+.2%} |\n")
            f.write("\n")
            if konglo["resolved"]["n_trades"] > 0:
                note = "" if konglo["resolved"]["n_trades"] >= 5 else " (too few resolved signals for this to mean much yet)"
                f.write(f"Across {konglo['resolved']['n_trades']} resolved signals: "
                        + format_trade_stats(konglo["resolved"]) + f"{note}.\n\n")

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
    konglo_result = run_konglo_watch_report(conn)
    conn.close()

    message = format_telegram_message(xgb_result, strat_result, ddqn_result, konglo_result)
    print(message)
    try:
        send_telegram(message)
    except Exception as e:
        print(f"Telegram delivery failed (job summary below still ran): {e}")
    write_step_summary(xgb_result, strat_result, ddqn_result, konglo_result)
