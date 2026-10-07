"""Offline IDX price references and Option A return comparability.

No source acquisition, database access, adjustment factors or economic rights
accounting lives here. A reference decision never certifies a return.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
import re
from zoneinfo import ZoneInfo

from idx_calendar import (CALENDAR_VERSION, is_idx_session, latest_idx_session_before,
                          IdxCalendarUnavailable)

RULES_VERSION = "idx-admission-existing.v1"
RETURN_POLICY_VERSION = "option-a-phase-spans.v1"
CONTRACT_VERSION = "actual-price-reference.v1"
ARB_BOUND = -0.15
TOL = 0.005
NEAR_LIMIT_TOLERANCE = 0.01
MODEL_TICK_VERSION = "legacy-two-pass-tick-model.v1"
RAW_ACTUAL = "RAW_ACTUAL_IDR_PER_SHARE"


class PriceContractError(ValueError):
    """Invalid registry or missing supported consumer contract."""


class UnsupportedPriceContract(PriceContractError):
    """A source or consumer cannot certify this contract yet."""


class ExplicitStatusResult:
    def __bool__(self):
        raise TypeError("Use the explicit status; unresolved results are not Boolean approvals")


def ara_bound(reference):
    if reference is None or not math.isfinite(float(reference)):
        return float("nan")
    return 0.35 if reference < 200 else 0.25 if reference <= 5000 else 0.20


def model_tick(price):
    """Existing analytical convention, not certification of exchange rounding."""
    return 1 if price < 200 else 2 if price < 500 else 5 if price < 2000 else 10 if price < 5000 else 25


def model_snap(raw, down):
    tick = model_tick(raw)
    rounding = math.floor if down else math.ceil
    value = rounding(raw / tick) * tick
    second_tick = model_tick(value)
    return value if second_tick == tick else rounding(raw / second_tick) * second_tick


def model_limit_price(reference, upper):
    return model_snap(reference * (1 + (ara_bound(reference) if upper else ARB_BOUND)), upper)


def canonical_session(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise PriceContractError("NONCANONICAL_SESSION")
    return date.fromisoformat(value)


def positive_real(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise PriceContractError("INVALID_PRICE")
    try:
        real = float(value)
    except (OverflowError, ValueError) as exc:
        raise PriceContractError("INVALID_PRICE") from exc
    if not math.isfinite(real) or real <= 0:
        raise PriceContractError("INVALID_PRICE")
    return real


def actual_bar_reason(bar):
    """Independent complete-OHLCV domain guard, without price adjustment."""
    try:
        opened, high, low, close = (positive_real(bar.get(k)) for k in ("open", "high", "low", "close"))
        if not low <= opened <= high or not low <= close <= high:
            return "INVALID_OHLC_SHAPE"
        volume = bar.get("volume")
        if (isinstance(volume, bool) or not isinstance(volume, (int, float))
                or not math.isfinite(float(volume)) or volume < 0):
            return "INVALID_VOLUME"
    except (PriceContractError, OverflowError, ValueError, TypeError):
        return "INVALID_OHLC_DOMAIN"
    return None


@dataclass(frozen=True)
class Anchor:
    session: str
    phase: str
    verified: bool = True

    def key(self):
        canonical_session(self.session)
        if not self.verified or self.phase not in ("OPEN", "CLOSE"):
            raise PriceContractError("UNVERIFIED_ANCHOR")
        return self.session, 0 if self.phase == "OPEN" else 1


@dataclass(frozen=True)
class PreviousActual:
    session: str
    price: float
    source: str
    trusted: bool = True
    representation: str = RAW_ACTUAL


@dataclass(frozen=True)
class Event:
    event_id: str
    revision: int
    venue: str
    markets: tuple
    ticker: str
    session: str
    status: str
    reference: float | None
    source: str
    available_at: datetime | None


@dataclass(frozen=True)
class Coverage:
    venue: str
    markets: tuple
    tickers: tuple
    start: str
    end: str


@dataclass(frozen=True)
class Registry:
    version: str
    content_sha256: str
    events: tuple
    coverage: tuple

    @property
    def identity(self):
        return {"contract_version": CONTRACT_VERSION, "registry_version": self.version,
                "registry_sha256": self.content_sha256, "rules_version": RULES_VERSION,
                "return_policy_version": RETURN_POLICY_VERSION, "calendar_version": CALENDAR_VERSION,
                "model_tick_version": MODEL_TICK_VERSION}

    def matching(self, ticker, market, session=None, venue="IDX"):
        return tuple(e for e in self.events if e.ticker == ticker and e.venue == venue
                     and market in e.markets and (session is None or e.session == session))

    def covers(self, ticker, market, start, end, venue="IDX"):
        return any(c.venue == venue and ticker in c.tickers and market in c.markets
                   and c.start <= start <= end <= c.end for c in self.coverage)


def _availability(value):
    if not isinstance(value, dict):
        raise PriceContractError("AVAILABILITY_PRECISION_REQUIRED")
    if value.get("precision") == "UNKNOWN":
        return None
    zone = ZoneInfo(value["timezone"])
    if value.get("precision") == "DAY":
        # The entire local day must have elapsed before this knowledge is usable.
        day = canonical_session(value["date"]) + timedelta(days=1)
        return datetime.combine(day, time(), zone).astimezone(timezone.utc)
    if value.get("precision") == "INSTANT":
        stamp = datetime.fromisoformat(value["timestamp"])
        if stamp.tzinfo is None:
            raise PriceContractError("TIMEZONE_REQUIRED")
        return stamp.astimezone(timezone.utc)
    raise PriceContractError("AVAILABILITY_PRECISION_REQUIRED")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PriceContractError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def parse_registry(document):
    """Validate the whole revision atomically. Conflicts never partially load."""
    try:
        doc = json.loads(document, object_pairs_hook=_object) if isinstance(document, (str, bytes)) else document
        if type(doc["schema_version"]) is not int or doc["schema_version"] != 1 or not isinstance(doc["registry_version"], str) or not doc["registry_version"]:
            raise PriceContractError("UNSUPPORTED_REGISTRY")
        events, keys, ids = [], set(), set()
        for r in doc["events"]:
            session = canonical_session(r["effective_session"])
            if not is_idx_session(session):
                raise PriceContractError("EVENT_NOT_SESSION")
            markets = tuple(r["market_scope"])
            if (r["venue"] != "IDX" or not markets or len(set(markets)) != len(markets)
                    or not set(markets) <= {"REGULAR", "NEGOTIATED", "CASH"}
                    or not re.fullmatch(r"[A-Z]{4}", r["ticker"])
                    or r["event_type"] not in {"RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT"}
                    or r["reference_kind"] != "OFFICIAL_JATS_EX_RIGHTS_REFERENCE"
                    or r["currency_unit"] != "IDR_PER_SHARE"
                    or r["status"] not in {"CONFIRMED_REFERENCE", "PENDING_REFERENCE", "REVOKED"}
                    or type(r["revision"]) is not int or r["revision"] < 1
                    or not r["event_id"] or r["event_id"] in ids
                    or not r["source_document_id"] or not r["source"]["author"]
                    or not r["evidence_refs"]):
                raise PriceContractError("INVALID_EVENT")
            ids.add(r["event_id"])
            ref = r["reference_price"]
            if r["status"] == "CONFIRMED_REFERENCE":
                if not isinstance(ref, str):
                    raise PriceContractError("DECIMAL_REFERENCE_REQUIRED")
                ref = positive_real(Decimal(ref))
            elif ref is not None:
                raise PriceContractError("UNCONFIRMED_REFERENCE")
            for market in markets:
                key = r["venue"], market, r["ticker"], r["effective_session"]
                if key in keys:
                    raise PriceContractError("CONFLICTING_EVENTS")
                keys.add(key)
            observed, verified = _availability(r["observed_at"]), _availability(r["verified_at"])
            available = max(observed, verified) if observed is not None and verified is not None else None
            if available is not None and "enrolled_at" in r:
                enrolled = _availability(r["enrolled_at"])
                available = max(available, enrolled) if enrolled is not None else None
            events.append(Event(r["event_id"], r["revision"], r["venue"], markets, r["ticker"],
                                r["effective_session"], r["status"], ref, r["source_document_id"], available))
        coverage = []
        for c in doc["reviewed_coverage"]:
            canonical_session(c["from"])
            canonical_session(c["through"])
            if (c["venue"] != "IDX" or c["from"] > c["through"] or not c["tickers"]
                    or not all(re.fullmatch(r"[A-Z]{4}", t) for t in c["tickers"])
                    or not c["market_scope"] or not set(c["market_scope"]) <= {"REGULAR", "NEGOTIATED", "CASH"}
                    or not c["evidence_refs"]):
                raise PriceContractError("INVALID_COVERAGE")
            coverage.append(Coverage(c["venue"], tuple(c["market_scope"]), tuple(c["tickers"]), c["from"], c["through"]))
        digest = sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        return Registry(doc["registry_version"], digest, tuple(events), tuple(coverage))
    except PriceContractError:
        raise
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError) as exc:
        raise PriceContractError("INVALID_REGISTRY") from exc


@dataclass(frozen=True)
class ReferenceResult(ExplicitStatusResult):
    status: str
    reason: str
    price: float | None = None
    kind: str = "UNRESOLVED_REFERENCE"
    source: str | None = None
    previous_actual: PreviousActual | None = None
    event_id: str | None = None
    registry_sha256: str | None = None


def _known(event, as_of):
    if as_of is None:
        return True
    if as_of.tzinfo is None:
        raise PriceContractError("AS_OF_TIMEZONE_REQUIRED")
    return event.available_at is not None and event.available_at <= as_of


def resolve_limit_reference(ticker, session, market, previous_actual, registry,
                            session_axis=None, input_representation="UNKNOWN",
                            venue="IDX", as_of=None):
    """Exact event reference or immediately preceding trusted actual close."""
    def unresolved(reason, event=None):
        return ReferenceResult("UNRESOLVED", reason, previous_actual=previous_actual,
                               event_id=event.event_id if event else None,
                               registry_sha256=registry.content_sha256)
    try:
        if venue != "IDX" or market not in {"REGULAR", "NEGOTIATED", "CASH"} or not re.fullmatch(r"[A-Z]{4}", ticker):
            return unresolved("UNSUPPORTED_IDENTITY_SCOPE")
        day = canonical_session(session)
        if not is_idx_session(day) or session_axis is not None and session not in session_axis:
            return unresolved("UNVERIFIED_SESSION")
        matches = registry.matching(ticker, market, session, venue)
        if len(matches) > 1:
            return unresolved("CONFLICTING_EVENTS")
        event = matches[0] if matches else None
        if event:
            if not _known(event, as_of):
                return unresolved("EVENT_NOT_KNOWN_AS_OF", event)
            if event.status != "CONFIRMED_REFERENCE":
                return unresolved(event.status, event)
        if input_representation != RAW_ACTUAL:
            return unresolved("UNKNOWN_REPRESENTATION", event)
        if event:
            return ReferenceResult("RESOLVED", "", event.reference, "OFFICIAL_CORPORATE_ACTION_REFERENCE",
                                   event.source, previous_actual, event.event_id, registry.content_sha256)
        if previous_actual is None:
            return unresolved("MISSING_PREDECESSOR")
        if not previous_actual.trusted or previous_actual.representation != RAW_ACTUAL or not previous_actual.source:
            return unresolved("UNTRUSTED_PREDECESSOR")
        if canonical_session(previous_actual.session) != latest_idx_session_before(day):
            return unresolved("MISSING_IMMEDIATE_PREDECESSOR")
        return ReferenceResult("RESOLVED", "", positive_real(previous_actual.price),
                               "PREVIOUS_TRUSTED_ACTUAL_CLOSE", previous_actual.source,
                               previous_actual, registry_sha256=registry.content_sha256)
    except (ValueError, TypeError, OverflowError) as exc:
        return unresolved(str(exc))
    except IdxCalendarUnavailable:
        return unresolved("UNSUPPORTED_CALENDAR")


@dataclass(frozen=True)
class AdmissionResult(ExplicitStatusResult):
    status: str
    reason: str
    limit_change: float | None = None


def validate_actual_price(actual_price, reference, rules_version=RULES_VERSION):
    if rules_version != RULES_VERSION:
        raise PriceContractError("UNSUPPORTED_RULES")
    try:
        actual = positive_real(actual_price)
    except PriceContractError:
        return AdmissionResult("UNRESOLVED", "INVALID_PRICE")
    if reference.status != "RESOLVED":
        return AdmissionResult("UNRESOLVED", reference.reason)
    change = actual / reference.price - 1
    in_band = ARB_BOUND - TOL <= change <= ara_bound(reference.price) + TOL
    return AdmissionResult("IN_BAND" if in_band else "OUT_OF_BAND", "" if in_band else "LIMIT_VIOLATION", change)


@dataclass(frozen=True)
class SpanResult(ExplicitStatusResult):
    status: str
    reason: str
    event_ids: tuple = ()


def return_span_status(ticker, start_anchor, end_anchor, market, registry,
                       session_axis=None, input_representation="UNKNOWN", venue="IDX", as_of=None):
    try:
        start, end = start_anchor.key(), end_anchor.key()
        if start > end or venue != "IDX" or market not in {"REGULAR", "NEGOTIATED", "CASH"} or not re.fullmatch(r"[A-Z]{4}", ticker):
            return SpanResult("WITHHELD", "INVALID_SPAN_SCOPE")
        events = registry.matching(ticker, market, venue=venue)
        # At OPEN(E) the reset has already happened. An earlier start includes E.
        crossed = tuple(e for e in events if start < (e.session, 0) <= end)
        if crossed:
            reason = "CORPORATE_ACTION_BOUNDARY" if all(e.status != "REVOKED" and _known(e, as_of) for e in crossed) else "UNRESOLVED_EVENT"
            return SpanResult("WITHHELD", reason, tuple(e.event_id for e in crossed))
        if input_representation != RAW_ACTUAL:
            return SpanResult("WITHHELD", "UNKNOWN_REPRESENTATION")
        if not registry.covers(ticker, market, start[0], end[0], venue):
            return SpanResult("WITHHELD", "UNKNOWN_ACTION_COVERAGE")
        day, last = canonical_session(start[0]), canonical_session(end[0])
        while day <= last:
            if is_idx_session(day):
                if session_axis is None or day.isoformat() not in session_axis:
                    return SpanResult("WITHHELD", "MISSING_VERIFIED_SESSION")
            elif day in {canonical_session(start[0]), last}:
                return SpanResult("WITHHELD", "UNVERIFIED_ANCHOR")
            day += timedelta(days=1)
        return SpanResult("COMPARABLE", "")
    except (ValueError, TypeError, OverflowError):
        return SpanResult("WITHHELD", "INVALID_ANCHOR")
    except IdxCalendarUnavailable:
        return SpanResult("WITHHELD", "UNSUPPORTED_CALENDAR")


def refuse_unmigrated(consumer):
    raise UnsupportedPriceContract(
        f"{consumer}: corporate-action contract {CONTRACT_VERSION} unsupported; "
        "source session/representation, complete holding windows and versioned "
        "output identity must be migrated before this route can run. "
        "Frozen artifacts retain their original contract.")
