"""
Transaction-cost model for IDX backtests.

Integrates with walk_forward_backtest.py and strategy_variants.py to answer
the question raised in v6/v8: "does the apparent edge survive real costs?"

IDX cost structure (as of 2026 — re-verify if regulations change):
  - Brokerage commission: typically 0.15%–0.25% per side (buy + sell),
    negotiable for active traders; 0.18% is a reasonable mid-range default.
  - IDX transaction levy + clearing fee: ~0.007% + 0.01% = ~0.017% per side
  - VAT (PPN 11%) on the brokerage commission portion only

Total round-trip cost (buy + sell) ≈ 2 × (commission + levy + VAT on commission)
  = 2 × (0.0018 + 0.00017 + 0.11×0.0018)
  = 2 × 0.002168
  ≈ 0.43% of traded notional

Spread / slippage:
  - Large-cap IDX (BBCA, BBRI, TLKM): tight spread, ~0.05%–0.15%
  - Mid/small-cap bandar plays (the CUAN/BRPT/TPIA universe): wider,
    especially on momentum days when the strategy fires. 0.20%–0.50%
    entry slippage is realistic for the >2% threshold variant, which
    specifically targets already-extreme-volume days.
  - No intraday data → we model slippage as a flat percentage off the
    assumed fill price (close). Conservative default: 0.25% per side
    (0.50% round-trip) for the mid/small-cap universe this strategy targets.

Combined realistic drag per round-trip trade: ~0.9%–1.0% (fees + spread/slippage).

Usage:
    from transaction_cost_model import estimate_net_return, COST_PRESETS
    net = estimate_net_return(gross_return=0.0249, preset="conservative")
    # or manually:
    net = estimate_net_return(gross_return=0.0249, commission_pct=0.0018,
                              levy_pct=0.00017, vat_rate=0.11,
                              slippage_pct_entry=0.0025, slippage_pct_exit=0.0025)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_metrics import trade_stats

# ── Defaults (IDX-specific) ───────────────────────────────────────

# Brokerage commission as a fraction of notional (per side)
DEFAULT_COMMISSION_PCT = 0.0018   # 0.18%

# IDX levy + clearing fee as a fraction of notional (per side)
DEFAULT_LEVY_PCT = 0.00017        # ~0.017%

# VAT rate applied to the brokerage-commission portion only
DEFAULT_VAT_RATE = 0.11           # 11% PPN

# Slippage: difference between assumed fill (close) and actual fill,
# expressed as a fraction of price. Higher on momentum days / smaller caps.
DEFAULT_SLIPPAGE_ENTRY_PCT = 0.0025   # 0.25%
DEFAULT_SLIPPAGE_EXIT_PCT = 0.0025    # 0.25%

# Presets keyed to liquidity / market-cap tier of the ticker universe
COST_PRESETS = {
    "ideal": {
        "commission_pct": 0.0015,
        "levy_pct": 0.00017,
        "vat_rate": 0.11,
        "slippage_pct_entry": 0.0005,
        "slippage_pct_exit": 0.0005,
        "note": "Best-case: low-commission broker + large-cap tight spread",
    },
    "moderate": {
        "commission_pct": 0.0018,
        "levy_pct": 0.00017,
        "vat_rate": 0.11,
        "slippage_pct_entry": 0.0015,
        "slippage_pct_exit": 0.0015,
        "note": "Mid-range: standard retail broker + average spread",
    },
    "conservative": {
        "commission_pct": 0.0025,
        "levy_pct": 0.00017,
        "vat_rate": 0.11,
        "slippage_pct_entry": 0.0030,
        "slippage_pct_exit": 0.0030,
        "note": "Conservative: higher-commission broker + wide spread on momo days",
    },
}


def total_cost_per_side(
    commission_pct: float = DEFAULT_COMMISSION_PCT,
    levy_pct: float = DEFAULT_LEVY_PCT,
    vat_rate: float = DEFAULT_VAT_RATE,
    slippage_pct: float = DEFAULT_SLIPPAGE_ENTRY_PCT,
) -> float:
    """Total friction for ONE side (buy OR sell) as a fraction of notional.

    Cost stack:
      commission + levy + VAT_on_commission + slippage
    """
    commission_with_vat = commission_pct * (1 + vat_rate)
    return commission_with_vat + levy_pct + slippage_pct


def estimate_net_return(
    gross_return: float,
    commission_pct: float = DEFAULT_COMMISSION_PCT,
    levy_pct: float = DEFAULT_LEVY_PCT,
    vat_rate: float = DEFAULT_VAT_RATE,
    slippage_pct_entry: float = DEFAULT_SLIPPAGE_ENTRY_PCT,
    slippage_pct_exit: float = DEFAULT_SLIPPAGE_EXIT_PCT,
    preset: str | None = None,
) -> float:
    """Convert a gross per-trade return into an estimated net return after
    IDX transaction costs.

    Args:
        gross_return: (exit_price - entry_price) / entry_price  (can be negative)
        preset: if given, overrides the individual cost parameters with a
                named preset from COST_PRESETS.

    Returns:
        Estimated net return after entry costs, exit costs, and slippage.
    """
    if preset is not None:
        if preset not in COST_PRESETS:
            raise ValueError(f"Unknown preset '{preset}'. Choose from {list(COST_PRESETS)}")
        p = COST_PRESETS[preset]
        commission_pct = p["commission_pct"]
        levy_pct = p["levy_pct"]
        vat_rate = p["vat_rate"]
        slippage_pct_entry = p["slippage_pct_entry"]
        slippage_pct_exit = p["slippage_pct_exit"]

    entry_cost = total_cost_per_side(commission_pct, levy_pct, vat_rate, slippage_pct_entry)
    exit_cost = total_cost_per_side(commission_pct, levy_pct, vat_rate, slippage_pct_exit)

    # A gross return of +2.49% becomes +2.49% - entry_cost - exit_cost in net terms.
    # Costs are subtracted linearly from the gross percentage return.
    return gross_return - entry_cost - exit_cost


def apply_costs_to_returns(
    returns: list[float] | np.ndarray | pd.Series,
    preset: str = "moderate",
    **overrides,
) -> np.ndarray:
    """Vectorized: apply estimate_net_return to a sequence of gross returns."""
    arr = np.asarray(returns, dtype=float)
    if preset not in COST_PRESETS:
        raise ValueError(f"Unknown preset '{preset}'")
    p = dict(COST_PRESETS[preset])
    p.pop("note", None)
    p.update(overrides)
    entry_cost = total_cost_per_side(p["commission_pct"], p["levy_pct"], p["vat_rate"], p["slippage_pct_entry"])
    exit_cost = total_cost_per_side(p["commission_pct"], p["levy_pct"], p["vat_rate"], p["slippage_pct_exit"])
    return arr - entry_cost - exit_cost


def net_return_stats(
    gross_returns: list[float] | np.ndarray | pd.Series,
    preset: str = "moderate",
    base_rate: float | None = None,
    **overrides,
) -> dict:
    """Cost-adjusted summary of a set of per-trade gross returns. No annualisation.

    Replaces sharpe_from_net_returns(), which computed
    ``net.mean() / net.std() * sqrt(252)`` on a PER-TRADE array. That is the
    defect HANDOFF.md TEMUAN 2 removed from this repo, in both of its parts:

      1. dimensional -- sqrt(252) converts a DAILY observation to annual. These
         observations are trades, not days. Rescaling to sqrt(252 / hold_days)
         does not repair this; it only hides it, because the second error is
         untouched and the guard stops seeing it.
      2. independence -- concurrent trades across tickers are moved by one
         index. signal_metrics.py measures mean pairwise daily correlation at
         +0.275 on this panel, so ~39 tickers carry ~3.4 independent series.
         Counting each trade as an independent draw deflates std and inflates
         any ratio built on it.

    TEMUAN 2's prescription is "ganti metrik, jangan tambal formula" -- replace
    the metric, do not patch the formula. So the ratio is gone rather than
    rescaled. Cost adjustment is unchanged: apply_costs_to_returns() does
    exactly what it did before.

    Returns signal_metrics.trade_stats() keys -- n_trades, mean_ret, median_ret,
    hit_rate, ret_per_risk, base_rate, hit_edge -- plus mean_gross and
    mean_cost_drag. `ret_per_risk` is mean/std, dimensionless and deliberately
    unscaled; it ranks variants against each other and is NOT a Sharpe ratio.
    Never render it under a column headed "Sharpe".

    Pass `base_rate` (the universe's positive-return rate over the same rows)
    when it is known; hit_rate carries little meaning without it.
    """
    gross = np.asarray(gross_returns, dtype=float)
    net = apply_costs_to_returns(gross, preset=preset, **overrides)
    stats = trade_stats(net, base_rate=base_rate)
    if len(net) == 0:
        stats.update(mean_gross=np.nan, mean_cost_drag=np.nan)
    else:
        stats.update(
            mean_gross=float(np.nanmean(gross)),
            mean_cost_drag=float(np.nanmean(gross) - np.nanmean(net)),
        )
    return stats

def cost_report() -> str:
    """Human-readable breakdown of per-trade costs by preset."""
    lines = ["IDX Transaction-Cost Model Breakdown", "=" * 50]
    for name, p in COST_PRESETS.items():
        entry = total_cost_per_side(
            p["commission_pct"], p["levy_pct"], p["vat_rate"], p["slippage_pct_entry"]
        )
        exit_ = total_cost_per_side(
            p["commission_pct"], p["levy_pct"], p["vat_rate"], p["slippage_pct_exit"]
        )
        round_trip = entry + exit_
        lines.append(f"\n{name.upper()} — {p['note']}")
        lines.append(f"  Entry cost : {entry*100:.3f}%")
        lines.append(f"  Exit cost  : {exit_*100:.3f}%")
        lines.append(f"  Round-trip : {round_trip*100:.3f}%")
    return "\n".join(lines)


# ── Backtest-integration helpers ──────────────────────────────────

def integrate_with_strategy_variants():
    """Example: how to wire this into strategy_variants.py without editing it.

    Add this at the bottom of strategy_variants.py (or import and call):

        from transaction_cost_model import net_return_stats, cost_report
        from signal_metrics import format_trade_stats
        print(cost_report())
        # Re-evaluate the winner with costs:
        winner_returns = [...]  # the list of gross returns for the winning variant
        for preset in ("moderate", "conservative"):
            print(format_trade_stats(net_return_stats(winner_returns, preset=preset),
                                     label=preset))
    """
    print(cost_report())


if __name__ == "__main__":
    print(cost_report())
    print()

    # Sanity-check against the v7 >2% threshold population:
    #   mean gross return = +2.49%, mean gross loss (losing trades) ≈ -2.x%
    # Conservative round-trip drag ≈ 1.07% → net mean ≈ +1.42%
    gross = 0.0249
    for preset in COST_PRESETS:
        net = estimate_net_return(gross, preset=preset)
        print(f"  Gross +{gross*100:.2f}%  ->  Net +{net*100:.2f}%  ({preset})")

    print()
    # What about the losing side? A -2.0% gross becomes -3.07% net after costs.
    gross_loss = -0.020
    for preset in COST_PRESETS:
        net = estimate_net_return(gross_loss, preset=preset)
        print(f"  Gross {gross_loss*100:.2f}%  ->  Net {net*100:.2f}%  ({preset})")
