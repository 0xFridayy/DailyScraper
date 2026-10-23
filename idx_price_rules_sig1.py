"""SIG1 diagnostic evidence only. Does not change financial labels or OHLCV.

The legacy price_audit detector remains visible. A percent move alone cannot
establish corruption: reference prices, boards and effective dates matter.
This module is deliberately separate from HX-2 and from stock selection.
"""

from datetime import date
import math

IDX_RULE_SOURCE = "https://www.idx.id/en/products-services/trading-hours-and-mechanism/"
RULE_EFFECTIVE_FROM = date(2026, 9, 28)
# Do not extrapolate a verified snapshot into rules that have not been reviewed.
RULE_VERIFIED_THROUGH = date(2026, 10, 15)
ENRG_PROSPECTUS = (
    "https://indopremier.com/xdir/product/f/aireport_2026_09_28/ENRG/"
    "aireport_20260928_enrg_keterbukaan_informasi_terkait_aksi_korporasi_32161313_lamp1.pdf"
)
ENRG_KSEI = "https://web.ksei.co.id/services/registered-securities/shares/lc/ENRG?setLocale=id-ID"
ENRG_RIGHT = "https://web.ksei.co.id/services/registered-securities/rights/lc/ENRG-R?setLocale=id-ID"


def price_band(reference, session, board):
    """Verified percent/absolute bands, or None when regime/board is unknown."""
    if not RULE_EFFECTIVE_FROM <= session <= RULE_VERIFIED_THROUGH:
        return None
    if board not in {"main", "development", "new_economy", "acceleration", "watchlist"}:
        return None
    if not math.isfinite(reference) or reference < 1:
        return None
    if reference <= 10:
        return max(1, reference - 1), reference + 1
    if board in {"acceleration", "watchlist"}:
        return reference * .90, reference * 1.10
    upper = .35 if reference <= 200 else .25 if reference <= 5000 else .20
    return reference * .85, reference * (1 + upper)


def assess_limit(row):
    """Explain a detector finding; keep unknown bars unresolved, never exempt a ticker."""
    result = {"classification": "UNKNOWN", "bar_verified": False}
    if row.ticker != "ENRG" or row.date != "2026-10-05":
        return result
    # The issuer's 25 September prospectus pp. 1-2 confirms 2:1 rights at
    # Rp310, cum 2 Oct, ex 5 Oct. KSEI independently confirms ratio and dates.
    result.update(classification="CORPORATE_ACTION_BOUNDARY",
                  sources=[ENRG_PROSPECTUS, ENRG_KSEI, ENRG_RIGHT, IDX_RULE_SOURCE],
                  detail="ENRG rights issue, ex regular market 2026-10-05; 2 old shares : 1 new at Rp310")
    if not math.isfinite(row.prev_close) or row.prev_close <= 0:
        return result
    theoretical = (2 * row.prev_close + 310) / 3
    result.update(theoretical_unrounded=theoretical, reference_verified=False,
                  adjusted_move=row.close / theoretical - 1)
    # This is an inference, not an independently verified exchange reference
    # or bar. No rounding rule or dated board membership is assumed.
    result["detail"] += (f"; inferred unrounded reference {theoretical:.6g}, "
                         f"close move {result['adjusted_move']:+.2%}; official reference and OHLCV still unverified")
    return result
