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
from unicodedata import normalize
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

# Trust restart. A predecessor close is a limit reference only while it is an
# evidenced anchor: an admitted official corporate-action bar, a daily step
# admitted against an anchor, or the last session of a restart window. A
# restart window is RESTART_SESSIONS consecutive verified sessions whose
# observations are each admissible, whose daily full-bar steps each sit inside
# the exchange band, and whose series-break medians each have their complete
# backward context. Nothing positional (a frame's first row) is evidence.
TRUST_POLICY_VERSION = "evidence-restart.v1"
RESTART_SESSIONS = 10
SERIES_BREAK_WINDOW = 21
SERIES_CONTEXT_ROWS = SERIES_BREAK_WINDOW // 2
# A frame holding this many earlier rows of a session's ticker segment
# reproduces that session's adjudication exactly; fewer can only fail closed.
# The window's first step is judged against the session before it, whose own
# discontinuity status reads one session further back.
DEPENDENCY_ROWS = SERIES_CONTEXT_ROWS + RESTART_SESSIONS + 1


class PriceContractError(ValueError):
    """Invalid registry or missing supported consumer contract."""


class UnsupportedPriceContract(PriceContractError):
    """A source or consumer cannot certify this contract yet."""

    def __init__(self, message, *, consumer=None):
        super().__init__(message)
        self.consumer = consumer
        self.status = "UNSUPPORTED"
        self.contract_version = CONTRACT_VERSION

    def as_dict(self):
        return {"status": self.status, "consumer": self.consumer,
                "contract_version": self.contract_version, "reason": str(self)}


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


def quarantine_recoverable(reasons, reference, *, bar_admitted, independent_defect,
                           representation):
    """Derived recovery never changes raw quarantine or unrelated findings."""
    return (reasons == "limit_violation" and representation == RAW_ACTUAL
            and reference.kind == "OFFICIAL_CORPORATE_ACTION_REFERENCE"
            and reference.status == "RESOLVED" and bar_admitted
            and not independent_defect)


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
                "model_tick_version": MODEL_TICK_VERSION, "trust_policy_version": TRUST_POLICY_VERSION}

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


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


# Placeholder-shaped text is absence of evidence, never a provenance claim.
PLACEHOLDER_TOKENS = frozenset({"", "UNKNOWN", "UNVERIFIED", "NONE", "NULL", "N/A", "NA", "TBD", "TBA",
                                "TODO", "PLACEHOLDER", "-", "--", "?", "MISSING", "PENDING", "NAN", "XXX"})
_PLACEHOLDER_WORD = re.compile(r"(?<![A-Z0-9])(?:UNKNOWN|UNVERIFIED|TBD|TBA|TODO|PLACEHOLDER|N/A|NULL|NONE)(?![A-Z0-9])")
EMPTY_CONTENT_SHA256 = sha256(b"").hexdigest()


# Metadata labels name a claim; they cannot substantiate a placeholder identity.
_PROVENANCE_LABELS = frozenset({
    "AUTHOR", "SOURCE", "DOCUMENT", "DOC", "EVIDENCE", "ID", "IDENTITY", "NAME",
    "PROVENANCE", "REPORT", "SESSION", "REPRESENTATION", "REFERENCE", "REF",
    "STATUS", "METADATA", "LABEL", "FIELD", "OWNER", "BY", "TITLE", "ORIGIN",
    "URL", "HASH", "SHA256", "PUBLICATION", "DATE", "NUMBER", "PUBLISHER",
})

# A finite vocabulary and inflection rule describe structure, not authenticity.
_PROVENANCE_FUNCTION_WORDS = frozenset({
    "AND", "OR", "YET", "BUT", "NOR", "SO", "FOR", "AS", "AT", "BY",
    "IN", "OF", "ON", "TO", "WITH", "THE", "A", "AN",
})
_PLACEHOLDER_NAMES = tuple(sorted({
    re.sub(r"[\W_]+", "", token) for token in PLACEHOLDER_TOKENS
    if re.sub(r"[\W_]+", "", token)
}, key=lambda token: (-len(token), token)))
_SEPARATED_PLACEHOLDER_WORD = re.compile(
    r"(?<![^\W_])(?:" + "|".join(r"[\W_]*".join(token) for token in _PLACEHOLDER_NAMES)
    + r")(?![^\W_])"
)
_KEY_VALUE_PLACEHOLDER = re.compile(
    r"[:=][\W_]*(?:(?:" + "|".join(_PLACEHOLDER_NAMES)
    + r")(?![^\W_])|[-?](?![^\W_])|$)"
)


def _provenance_label(part):
    """Recognize a generic label stem and its regular plural inflections."""
    return (part in _PROVENANCE_LABELS
            or (part.endswith("IES") and part[:-3] + "Y" in _PROVENANCE_LABELS)
            or (part.endswith("ES") and part[:-2] in _PROVENANCE_LABELS)
            or (part.endswith("S") and part[:-1] in _PROVENANCE_LABELS))



def _compound_placeholder_text(value):
    """A placeholder-bearing claim needs a substantive identity beyond labels.

    NFKC and Unicode separators canonicalize the finite placeholder vocabulary.
    A placeholder value after ':' or '=' refuses regardless of the key or suffix.
    Regular label plurals and finite function words cannot substantiate identity.
    Separated N-A may be part of a real name (N-A Securities Research); explicit
    N/A remains forbidden by meaningful_identity's existing notation rule.
    A substantive token has at least three characters including a letter, or
    at least four digits with distinct values. This does not authenticate a source.
    Single tokens retain the whole-field policy in placeholder_text below.
    """
    canonical = normalize("NFKC", value).upper()
    canonical = _SEPARATED_PLACEHOLDER_WORD.sub(lambda match: re.sub(r"[\W_]+", "", match[0]), canonical)
    if _KEY_VALUE_PLACEHOLDER.search(canonical):
        return True
    parts = re.findall(r"[^\W_]+", canonical)
    if len(parts) <= 1:
        return False
    has_placeholder = any(part in PLACEHOLDER_TOKENS for part in parts)
    substantive = any(
        part not in PLACEHOLDER_TOKENS and not _provenance_label(part)
        and part not in _PROVENANCE_FUNCTION_WORDS
        and ((len(part) >= 3 and any(char.isalpha() for char in part))
             or (part.isdigit() and len(part) >= 4 and len(set(part)) > 1))
        for part in parts
    )
    return has_placeholder and not substantive


def placeholder_text(value):
    """True for anything that is not meaningful single-line evidence text."""
    # Whole-field absence and compound claims share one provenance policy.
    return (not _text(value) or value.upper() in PLACEHOLDER_TOKENS
            or re.sub(r"[\W_]+", "", normalize("NFKC", value)).upper() in PLACEHOLDER_TOKENS
            or _compound_placeholder_text(value)
            or re.fullmatch(r"[\W_0]*", value) is not None)


def meaningful_identity(value):
    """Identity text (authors, document ids, evidence names): no placeholder words."""
    return not placeholder_text(value) and _PLACEHOLDER_WORD.search(value.upper()) is None


def meaningful_sha256(value):
    """A lowercase SHA-256 that is not empty content or a degenerate pattern."""
    return (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
            and value != EMPTY_CONTENT_SHA256 and len(set(value)) >= 8
            and not any(value == value[:size] * (64 // size) for size in (1, 2, 4, 8, 16, 32)))


def meaningful_url(value):
    from urllib.parse import urlsplit
    if not _text(value) or any(c.isspace() for c in value):
        return False
    parts = urlsplit(value)
    return (parts.scheme in {"http", "https"} and "." in (parts.hostname or "")
            and _PLACEHOLDER_WORD.search(value.upper()) is None)


def _interval(value, default_zone="Asia/Jakarta"):
    """(earliest, latest) UTC instants a recorded clock can denote, or None.

    A date-only value is its whole local day; nothing finer is invented.
    """
    if isinstance(value, str):
        day = canonical_session(value)
        zone = ZoneInfo(default_zone)
    elif isinstance(value, dict) and value.get("precision") == "DAY":
        day, zone = canonical_session(value["date"]), ZoneInfo(value["timezone"])
    elif isinstance(value, dict) and value.get("precision") == "INSTANT":
        stamp = _availability(value)
        return stamp, stamp
    else:
        return None
    start = datetime.combine(day, time(), zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(timezone.utc)
    return start, end - timedelta(microseconds=1)


def _coherent_chronology(record):
    """publication <= observation <= verification <= enrolment (knowledge cutoff).

    A pair contradicts only when even the latest instant the later clock can
    denote precedes the earliest instant of the earlier one: certainly first.
    A confirmed reference must state its publication, observation and
    verification clocks; a pending one is checked wherever it states them.
    """
    clocks = [("published", record["source"].get("published_on")), ("observed", record.get("observed_at")),
              ("verified", record.get("verified_at")), ("enrolled", record.get("enrolled_at"))]
    known = [(name, _interval(value)) for name, value in clocks if value is not None]
    known = [(name, span) for name, span in known if span is not None]
    if record["status"] == "CONFIRMED_REFERENCE" and [n for n, _ in known][:3] != ["published", "observed", "verified"]:
        raise PriceContractError("CONFIRMED_REFERENCE_CLOCKS_REQUIRED")
    for i, (_, earlier) in enumerate(known):
        for _, later in known[i + 1:]:
            if later[1] < earlier[0]:
                raise PriceContractError("CONTRADICTORY_EVIDENCE_CHRONOLOGY")


def _string_list(value, supported=None):
    return (isinstance(value, list) and bool(value)
            and all(_text(v) for v in value) and len(set(value)) == len(value)
            and (supported is None or set(value) <= supported))


def _event_evidence(record):
    """Meaningful, internally coherent provenance; placeholders never qualify."""
    source, evidence = record["source"], record["evidence_refs"]
    if (not isinstance(source, dict) or not isinstance(evidence, dict)
            or not all(meaningful_identity(source.get(k)) for k in ("author", "retrieval_medium"))
            or not meaningful_url(source.get("url"))
            or not meaningful_identity(record["source_document_id"])
            or placeholder_text(record.get("notes"))
            or "direct_exchange_original_sha256" not in evidence):
        raise PriceContractError("INVALID_EVENT_PROVENANCE")
    hashes, identities = [], []
    for key, value in evidence.items():
        if not meaningful_identity(key):
            raise PriceContractError("INVALID_EVIDENCE")
        if key.endswith("sha256"):
            if value is None and key == "direct_exchange_original_sha256":
                continue
            if not meaningful_sha256(value):
                raise PriceContractError("INVALID_EVIDENCE_HASH")
            hashes.append(value)
        elif key.endswith("url"):
            if not meaningful_url(value):
                raise PriceContractError("INVALID_EVIDENCE_URL")
        elif not meaningful_identity(value) or value.startswith(("https://", "http://")):
            raise PriceContractError("INVALID_EVIDENCE_IDENTITY")
        else:
            identities.append(value)
    if not hashes or not identities:
        raise PriceContractError("OFFLINE_EVIDENCE_REQUIRED")
    if record["status"] == "CONFIRMED_REFERENCE":
        canonical_session(source["published_on"])
    _coherent_chronology(record)


def parse_registry(document):
    """Validate the whole revision atomically. Conflicts never partially load."""
    try:
        doc = json.loads(document, object_pairs_hook=_object) if isinstance(document, (str, bytes)) else document
        if (not isinstance(doc, dict) or type(doc.get("schema_version")) is not int
                or doc["schema_version"] != 1 or not meaningful_identity(doc.get("registry_version"))
                or not isinstance(doc.get("events"), list)
                or not isinstance(doc.get("reviewed_coverage"), list)):
            raise PriceContractError("UNSUPPORTED_REGISTRY")
        events, keys, ids = [], set(), set()
        for r in doc["events"]:
            if not isinstance(r, dict):
                raise PriceContractError("INVALID_EVENT")
            session = canonical_session(r["effective_session"])
            if not is_idx_session(session):
                raise PriceContractError("EVENT_NOT_SESSION")
            if not _string_list(r["market_scope"], {"REGULAR", "NEGOTIATED", "CASH"}):
                raise PriceContractError("INVALID_MARKET_SCOPE")
            markets = tuple(r["market_scope"])
            if (r["venue"] != "IDX" or not markets or len(set(markets)) != len(markets)
                    or not set(markets) <= {"REGULAR", "NEGOTIATED", "CASH"}
                    or not re.fullmatch(r"[A-Z]{4}", r["ticker"])
                    or r["event_type"] not in {"RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT"}
                    or r["reference_kind"] != "OFFICIAL_JATS_EX_RIGHTS_REFERENCE"
                    or r["currency_unit"] != "IDR_PER_SHARE"
                    or r["status"] not in {"CONFIRMED_REFERENCE", "PENDING_REFERENCE", "REVOKED"}
                    or type(r["revision"]) is not int or r["revision"] < 1
                    or not meaningful_identity(r["event_id"]) or r["event_id"] in ids):
                raise PriceContractError("INVALID_EVENT")
            _event_evidence(r)
            ids.add(r["event_id"])
            ref = r["reference_price"]
            if r["status"] == "CONFIRMED_REFERENCE":
                if not isinstance(ref, str) or not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", ref):
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
            if not isinstance(c, dict):
                raise PriceContractError("INVALID_COVERAGE")
            canonical_session(c["from"])
            canonical_session(c["through"])
            if (c["venue"] != "IDX" or c["from"] > c["through"] or not _string_list(c["tickers"])
                    or not all(re.fullmatch(r"[A-Z]{4}", t) for t in c["tickers"])
                    or not _string_list(c["market_scope"], {"REGULAR", "NEGOTIATED", "CASH"})
                    or not _string_list(c["evidence_refs"])
                    or not all(meaningful_identity(v) for v in c["evidence_refs"])):
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


TRADING_FIELDS = ("open", "high", "low", "close")


def full_bar_band_status(bar, reference_price):
    """Open, high, low and close against one reference: IN_BAND only if all are."""
    reference = ReferenceResult("RESOLVED", "", positive_real(reference_price))
    statuses = [validate_actual_price(bar.get(k), reference).status for k in TRADING_FIELDS]
    if "OUT_OF_BAND" in statuses:
        return "OUT_OF_BAND"
    return "IN_BAND" if all(s == "IN_BAND" for s in statuses) else "UNRESOLVED"


@dataclass(frozen=True)
class _SessionAnchor:
    local_ok: bool        # the observation itself is usable evidence
    discontinuous: bool   # out of band against its own usable predecessor
    admissible: bool
    trusted: bool
    has_event: bool
    streak: int
    close: object
    representation: str


def _verified_session(session):
    try:
        day = canonical_session(session)
    except (PriceContractError, ValueError, TypeError):
        return None, "NONCANONICAL_SESSION"
    try:
        return day, None if is_idx_session(day) else "NOT_A_SESSION"
    except IdxCalendarUnavailable:
        return day, "UNSUPPORTED_CALENDAR"


def adjudicate_series(ticker, rows, registry, *, market="REGULAR", representation="UNKNOWN",
                      as_of=None, source="input-price-snapshot"):
    """Admission and anchor trust for one ticker, shared by writer, audit and monitor.

    Each row carries its OHLCV, ``date`` and the adapter's evidence:
    ``external_reason`` (None, or why the observation itself is unusable: a
    duplicate, cross-ticker duplicate, series break, quarantine or contradicted
    source context), ``source_known`` and ``context_complete`` (its series-break
    median has SERIES_CONTEXT_ROWS earlier rows of its segment).

    Predecessors come from the verified calendar, never from row positions. A
    session whose immediate predecessor session has no admissible observation is
    compared with nothing; it can only start a restart window. Trust therefore
    depends on a bounded calendar window, and a missing or truncated window only
    withholds trust. Order of ``rows`` is irrelevant; records align with it.
    """
    order = sorted(range(len(rows)), key=lambda i: (str(rows[i].get("date")), i))
    occurrences = {}
    for i in order:
        occurrences.setdefault(rows[i].get("date"), []).append(i)
    in_scope = (market in {"REGULAR", "NEGOTIATED", "CASH"} and isinstance(ticker, str)
                and re.fullmatch(r"[A-Z]{4}", ticker) is not None)
    state, records, last = {}, [None] * len(rows), None
    for i in order:
        row, session = rows[i], rows[i].get("date")
        day, reason = _verified_session(session)
        if reason is None and not in_scope:
            reason = "UNSUPPORTED_IDENTITY_SCOPE"
        events = registry.matching(ticker, market, session) if day is not None else ()
        event = events[0] if events else None
        external = row.get("external_reason")
        if len(occurrences[session]) > 1:
            external = external or "DUPLICATE_IDENTITY"
        source_known = bool(row.get("source_known", True))
        if not source_known:
            external = external or "UNSUPPORTED_SOURCE_CONTEXT"
        # Unknown legacy basis still permits narrower ordinary-band diagnostics.
        # They never certify representation, admit an event or produce returns.
        diagnostic = RAW_ACTUAL if representation == "UNKNOWN" and not events else representation
        input_representation = diagnostic if source_known else "UNKNOWN"
        predecessor, boundary = None, False
        if reason is None:
            try:
                predecessor = latest_idx_session_before(day).isoformat()
            except IdxCalendarUnavailable:
                boundary = True
        prior = state.get(predecessor) if predecessor is not None else None
        if boundary and not events:
            # No verified predecessor exists, whatever older rows were loaded.
            ref = ReferenceResult("UNRESOLVED", "UNSUPPORTED_CALENDAR",
                                  registry_sha256=registry.content_sha256)
        else:
            previous = None if prior is None else PreviousActual(
                predecessor, prior.close, source, prior.trusted, prior.representation)
            ref = resolve_limit_reference(ticker, session, market, previous, registry,
                                          input_representation=input_representation, as_of=as_of)
            if ref.status == "UNRESOLVED" and ref.reason == "MISSING_PREDECESSOR" and predecessor:
                # The calendar names the predecessor session; whether older
                # rows happen to be loaded cannot change the finding.
                ref = ReferenceResult("UNRESOLVED", "MISSING_IMMEDIATE_PREDECESSOR",
                                      registry_sha256=registry.content_sha256)
        admissions = {k: validate_actual_price(row.get(k), ref) for k in TRADING_FIELDS}
        complete_in_band = all(a.status == "IN_BAND" for a in admissions.values())
        bar_status = ("OUT_OF_BAND" if any(a.status == "OUT_OF_BAND" for a in admissions.values())
                      else admissions["close"].status)
        domain = actual_bar_reason(row) is None
        volume = row.get("volume")
        traded = (not isinstance(volume, bool) and isinstance(volume, (int, float))
                  and math.isfinite(float(volume)) and volume > 0)
        try:
            low, opened, high = (positive_real(row.get(k)) for k in ("low", "open", "high"))
            open_domain = low <= opened <= high
        except PriceContractError:
            open_domain = False
        if reason is None and not domain:
            reason = "INVALID_DOMAIN"
        if reason is None and not traded:
            reason = "ZERO_VOLUME"
        if reason is None and external:
            reason = external
        if reason is None and events:
            # An event bar is admissible only through its own official reference.
            if ref.status != "RESOLVED":
                reason = "UNRESOLVED_EVENT_REFERENCE"
            elif not complete_in_band:
                reason = "LIMIT_VIOLATION"
        if reason is None and not events and input_representation != RAW_ACTUAL:
            reason = "UNSUPPORTED_REPRESENTATION"
        local_ok = reason is None
        # Consistency with a usable but not yet trusted predecessor detects a
        # discontinuity; it never certifies the step. A step out of a bar that
        # was itself a discontinuity (a spike's return) is not flagged again.
        # Each decision reads at most two sessions back, never a whole chain.
        consistency_price, consistency = None, "UNAVAILABLE"
        if not events and prior is not None and prior.local_ok and domain:
            consistency_price, consistency = prior.close, full_bar_band_status(row, prior.close)
        discontinuity = consistency == "OUT_OF_BAND" and not prior.discontinuous
        if local_ok and (bar_status == "OUT_OF_BAND" or discontinuity):
            reason = "LIMIT_VIOLATION"
        admissible = reason is None
        event_admitted = admissible and bool(events)
        linked = (admissible and not events and prior is not None and prior.admissible
                  and consistency == "IN_BAND")
        has_event = event_admitted or (linked and prior.has_event)
        if not admissible or not row.get("context_complete"):
            streak = 0
        elif linked and prior.streak >= 1:
            streak = min(prior.streak + 1, RESTART_SESSIONS)
        else:
            streak = 1
        # Trust continues only through admitted daily steps from an anchor.
        trusted = admissible and (has_event or streak >= RESTART_SESSIONS or (linked and prior.trusted))
        if not admissible:
            status = "INADMISSIBLE"
        elif event_admitted:
            status = "TRUSTED_EVENT_ANCHOR"
        elif trusted and linked and prior.trusted:
            status = "TRUSTED_CHAIN"
        elif trusted:
            status = "TRUSTED_RESTART_ANCHOR"
        else:
            status = "RESTART_PENDING"
            reason = "SERIES_CONTEXT_INCOMPLETE" if not row.get("context_complete") else "RESTART_WINDOW_INCOMPLETE"
        source_ok = external is None
        records[i] = {
            "previous_actual_close": last[1] if last else None,
            "previous_actual_session": last[0] if last else None,
            "limit_reference_price": ref.price,
            "limit_reference_kind": ref.kind,
            "limit_reference_source": ref.source,
            "limit_reference_status": ref.status,
            "limit_unresolved_reason": ref.reason,
            "limit_change": admissions["close"].limit_change,
            "price_admissibility_status": ((bar_status if representation == RAW_ACTUAL else "UNRESOLVED_REPRESENTATION")
                                           if traded else "UNVERIFIED_TRADING_SESSION") if domain else "INVALID_DOMAIN",
            "limit_admission_status": bar_status,
            "consistency_reference_price": consistency_price,
            "consistency_status": consistency,
            "consistency_violation": discontinuity,
            "price_step_admissible": complete_in_band and domain and traded and source_ok,
            "close_anchor_admissible": trusted,
            "entry_open_admissible": (admissions["open"].status == "IN_BAND" and traded and open_domain
                                      and admissions["high"].status == "IN_BAND"
                                      and admissions["low"].status == "IN_BAND" and source_ok),
            "corporate_action_boundary": bool(event),
            "corporate_action_event_id": event.event_id if event else None,
            "corporate_action_status": event.status if event else None,
            "domain_violation": not domain,
            "input_price_trusted": source_ok,
            "anchor_trust_status": status,
            "anchor_trust_reason": reason or "",
            "restart_window_sessions": streak,
        }
        state[session] = _SessionAnchor(local_ok, consistency == "OUT_OF_BAND", admissible, trusted,
                                        has_event, streak, row.get("close"), diagnostic)
        last = (session, row.get("close"))
    return records


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
        "Frozen artifacts retain their original contract.", consumer=consumer)
