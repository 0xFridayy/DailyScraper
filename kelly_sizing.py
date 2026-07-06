"""
Roadmap #4 — Kelly criterion position sizing.

DORMANT: not wired into anything yet. Kelly sizing only makes sense once
Layer 1 (walk_forward_backtest.py) shows a positive-expectancy edge — feed it
a negative or zero edge and it correctly tells you to bet nothing (or the
math goes negative, which just means "don't take this trade"). As of
2026-07-07 it hasn't: pooled walk-forward Sharpe was -3.91 (full feature
set), -3.35 (price-only), -5.99 (broker-only) across 4 cycles / 57 days /
45 tickers, and every variant tested was beaten by simply predicting zero
return. See walk_forward_backtest.py's docstring and SYSTEM.md's Backtest
Results Log for the full diagnostic (SHAP importance, horizon sweep,
turnover-normalized features — none of it found a usable signal yet).

This module exists so the formula is ready and tested for whenever a real
edge shows up (Layer 1 Sharpe > 1.5 sustained, per SYSTEM.md), not because
it's safe to use today.
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

    # This project's actual current numbers (pooled, full feature set) -> should be 0,
    # since hit_rate < 50% and Sharpe is negative (no edge to size).
    f = kelly_fraction(win_rate=0.3553, avg_win=0.045, avg_loss=0.045, fraction_cap=0.5)
    print(f"Current Layer 1 numbers -> Kelly fraction = {f:.3f} (expect 0.0: no edge yet)")
