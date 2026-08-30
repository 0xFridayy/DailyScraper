"""
Roadmap #4 — Kelly criterion position sizing.

VOID — every historical Sharpe and the "Sharpe > 1.5" gate quoted below used
the invalid per-trade/cross-sectional formula documented in signal_metrics.py.
They remain only so the old experiment is not rediscovered. No replacement
threshold has been chosen, and this module remains inert.

DORMANT: not wired into anything yet. Feed kelly_fraction a negative or zero
edge (equal-ish avg_win/avg_loss with hit_rate <= 50%) and it correctly
returns 0 — but a positive number out of the formula is NOT the same thing
as "Layer 1 found the broker-flow edge SYSTEM.md is looking for." As of
2026-07-07, on the full ~218-day backfill (2025-08-01 to 2026-07-04, 45
tickers), pooled walk-forward Sharpe is +0.94 (full feature set) / +0.76
(price-only) / +0.68 (broker-only) across 31 cycles — better than the
clearly-negative Sharpe seen on the earlier 19/57 day backfills (mostly
small-sample noise, in hindsight), and plugging its actual win_rate/avg_win/
avg_loss into kelly_fraction below does output a small positive fraction.
But that Sharpe improvement traces to price momentum (momentum_1d dominates
SHAP importance by ~3x), not broker flow: price-only features alone score a
BETTER pooled Sharpe than the full set, and broker_concentration ranks 7th
of 8 by SHAP. hit_rate is 42.9%, under a coin flip (the positive Sharpe comes
entirely from winners being bigger than losers), every variant's test MAE is
still worse than a naive "predict zero" baseline, and it's well below
SYSTEM.md's Sharpe > 1.5 validation bar. See walk_forward_backtest.py's
docstring and SYSTEM.md's Backtest Results Log for the full diagnostic —
this module is still inert; see the __main__ block below for why a positive
number out of the formula doesn't change that.

This module exists so the formula is ready and tested if a real edge is later
validated under criteria chosen by the user, not because it is safe to use now.
"""

def kelly_fraction(win_rate, avg_win, avg_loss, fraction_cap=0.5):
    """Full Kelly fraction of capital to risk per trade.

    win_rate: probability of a winning trade (0-1).
    avg_win: average return on winning trades (positive number).
    avg_loss: average loss on losing trades (positive number, i.e. magnitude).
    fraction_cap: multiply the raw Kelly output by this (e.g. 0.5 = "half
        Kelly") since full Kelly is famously overconfident under estimation
        error in win_rate/avg_win/avg_loss — exactly the situation with a
        few dozen live-scraped trades.

    Returns 0 if the edge is non-positive (negative or zero expectancy) —
    Kelly betting on a losing edge is nonsensical, not "bet negative."
    """
    if avg_loss <= 0 or avg_win <= 0 or not (0 <= win_rate <= 1):
        raise ValueError("avg_win/avg_loss must be positive; win_rate in [0,1]")

    payoff_ratio = avg_win / avg_loss
    raw_kelly = win_rate - (1 - win_rate) / payoff_ratio
    if raw_kelly <= 0:
        return 0.0
    return min(raw_kelly * fraction_cap, 1.0)


def kelly_from_trades(returns, fraction_cap=0.5):
    """Convenience wrapper: derive win_rate/avg_win/avg_loss straight from a
    sequence of realized per-trade returns (e.g. the strategy_return series
    from sharpe_stats in walk_forward_backtest.py) and apply kelly_fraction."""
    wins = [r for r in returns if r > 0]
    losses = [-r for r in returns if r < 0]
    if not returns:
        return 0.0
    win_rate = len(wins) / len(returns)
    if not wins or not losses:
        return 0.0
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)
    return kelly_fraction(win_rate, avg_win, avg_loss, fraction_cap=fraction_cap)


if __name__ == "__main__":
    # Sanity check against the textbook example: 60% win rate, 1:1 payoff -> 20% Kelly.
    f = kelly_fraction(win_rate=0.6, avg_win=1.0, avg_loss=1.0, fraction_cap=1.0)
    assert abs(f - 0.2) < 1e-9, f
    print("kelly_fraction sanity check passed (60%/1:1 -> 0.20 Kelly).")

    # This project's ACTUAL current numbers (pooled, full feature set, 218-day
    # backfill, 2026-07-07): win_rate=0.4288, avg_win=0.0525, avg_loss=0.0330 —
    # wins are bigger than losses even though hit_rate is under 50%, so the raw
    # math is NOT zero here (unlike the negative-Sharpe runs on less data).
    f = kelly_fraction(win_rate=0.4288, avg_win=0.0525, avg_loss=0.0330, fraction_cap=0.5)
    print(f"Current Layer 1 numbers -> Kelly fraction = {f:.3f} (half-Kelly)")
    print(
        "DO NOT read this as 'ready to size trades.' A positive Kelly output "
        "just means the formula is doing its job on whatever numbers you feed "
        "it - it has no way to know this Sharpe (+0.94) is unvalidated: mostly "
        "momentum-driven rather than the broker-flow edge SYSTEM.md is testing "
        "for (see walk_forward_backtest.py's docstring), built on 31 walk-"
        "forward cycles with overlapping/autocorrelated market history (not 31 "
        "independent trials), and still below the Sharpe > 1.5 validation bar. "
        "Garbage-in-garbage-out applies to Kelly like anything else."
    )
