"""SIG1 admission contract, independent of observed database membership.

The full versioned registry defines collection membership independently of
observed database rows. Registry membership is not a listing-date certification. No missing bar is waived by default.
Exceptions require dated, reviewed evidence; unknown evidence remains required.
This does not change selection, weights or historical prices.
"""

from dataclasses import dataclass
from datetime import date
import re
from urllib.parse import urlsplit


from ticker_registry import load_registry

_REGISTRY = load_registry()
UNIVERSE_VERSION = _REGISTRY.version
UNIVERSE_SOURCE = _REGISTRY.source
EXPECTED_TICKERS = _REGISTRY.tickers


@dataclass(frozen=True)
class CoverageException:
    ticker: str
    kind: str  # pre_listing, suspension, or exclusion
    start: date
    end: date
    evidence_uri: str
    evidence_sha256: str
    reviewed_by: str
    verified: bool = False
    listing_date: date | None = None

    def applies(self, ticker, session):
        if self.verified is not True or not self.reviewed_by.strip() or not self.evidence_uri:
            return False
        uri = urlsplit(self.evidence_uri)
        if uri.scheme not in {"https", "git", "fixture"} or not (uri.netloc or uri.path):
            return False
        if not re.fullmatch(r"[0-9a-f]{64}", self.evidence_sha256):
            return False
        if self.kind not in {"pre_listing", "suspension", "exclusion"}:
            return False
        if self.kind == "pre_listing" and (
                self.listing_date is None or self.end >= self.listing_date):
            return False
        return self.ticker == ticker and self.start <= session <= self.end


@dataclass(frozen=True)
class UniverseContract:
    version: str
    source: str
    tickers: tuple[str, ...]
    exceptions: tuple[CoverageException, ...] = ()

    def __post_init__(self):
        if not self.version or not self.source or not self.tickers:
            raise ValueError("Expected universe requires a version, source and members")
        if len(set(self.tickers)) != len(self.tickers) or any(
                not re.fullmatch(r"[A-Z][A-Z0-9]{3}", t) for t in self.tickers):
            raise ValueError("Expected universe has duplicate or invalid ticker identities")
        for item in self.exceptions:
            if item.ticker not in self.tickers or item.start > item.end:
                raise ValueError("Exception outside expected universe or invalid interval")

    def required(self, session):
        return {t for t in self.tickers if not any(
            e.applies(t, session) for e in self.exceptions)}


EXPECTED_UNIVERSE = UniverseContract(UNIVERSE_VERSION, UNIVERSE_SOURCE, EXPECTED_TICKERS)
# Latest completed session plus its immediately preceding official session.
# This is a current-report admission contract, not a certification of all
# historical per-ticker availability. Historical dates are validated separately.
REQUIRED_SESSION_COUNT = 2

# Numeric meanings are defined by the request contract, never learned from a
# storage-class baseline. Identity and top_5_buyer are deliberately excluded.
NUMERIC_SOURCE_FIELDS = frozenset({
    "close", "high", "low", "m_dn_0", "nr_dn_0", "f_dn_0", "m_cn_5", "m_dn_3",
    "clean_score", "tval", "market_cap_t", "pct_5", "is_unusual_volume",
})
