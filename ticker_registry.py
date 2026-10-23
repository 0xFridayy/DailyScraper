"""Versioned collection universe independent of observed source responses.

Membership is the repository registry, not a certification of listing lifecycle
or availability at a particular provider. Sources must report those exceptions.
"""
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class TickerRegistry:
    tickers: tuple[str, ...]
    version: str
    source: str
    sha256: str
    source_file_sha256: str = ""

    def __post_init__(self):
        if not isinstance(self.tickers, tuple) or not self.tickers or len(set(self.tickers)) != len(self.tickers):
            raise ValueError("Ticker registry must have unique nonempty membership")
        if any(not isinstance(t, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{3}", t)
               for t in self.tickers):
            raise ValueError("Ticker registry contains invalid identities")
        if not self.version or not self.source or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("Ticker registry requires version, source and SHA-256")

    def required_for(self, session):
        if isinstance(session, str):
            parsed = date.fromisoformat(session)
            if parsed.isoformat() != session:
                raise ValueError("Noncanonical registry session")
        elif not isinstance(session, date):
            raise ValueError("Expected an exchange session date")
        # Never waive members because the prior response omitted them.
        return frozenset(self.tickers)

    def metadata(self):
        return dict(version=self.version, source=self.source, sha256=self.sha256,
                    source_file_sha256=self.source_file_sha256,
                    count=len(self.tickers), listing_lifecycle_certification="UNVERIFIED")


def registry_from_tickers(tickers, source="explicit-test-or-source-contract"):
    members = tuple(tickers)
    if any(not isinstance(t, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{3}", t) for t in members):
        raise ValueError("Ticker registry contains invalid identities")
    canonical = json.dumps(sorted(members), separators=(",", ":")).encode("utf-8")
    sha = hashlib.sha256(canonical).hexdigest()
    return TickerRegistry(members, "idx-ticker-registry.v1:" + sha[:16], source, sha)


def load_registry(path=None):
    source = Path(path) if path is not None else Path(__file__).with_name("all_tickers.json")
    raw = source.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("Ticker registry must be a JSON list")
    registry = registry_from_tickers(data, source=source.name)
    return TickerRegistry(registry.tickers, registry.version, registry.source,
                          registry.sha256, hashlib.sha256(raw).hexdigest())
