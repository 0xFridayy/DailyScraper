"""Inventory evidence v1: pure calculations over explicit, available observations.

Inventory is cumulative broker net flow since a finite observable anchor. A
negative curve is net selling since that anchor. Opening position is unknown.
This module performs no I/O. See HANDOFF.md for the public contract and examples.
"""

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from decimal import (Decimal, Context, localcontext, ROUND_HALF_EVEN,
                     InvalidOperation, DivisionByZero, Overflow)
import hashlib
import json
import math
import re

import idx_calendar

SCHEMA = "inventory_evidence"
VERSION = "1"
OBSERVED_ZERO = "OBSERVED_ZERO"
OBSERVED_NONZERO = "OBSERVED_NONZERO"
UNOBSERVED = "UNOBSERVED"
INVALID = "INVALID"
QUARANTINED = "QUARANTINED"
COVERAGE_STATES = (OBSERVED_ZERO, OBSERVED_NONZERO, UNOBSERVED, INVALID, QUARANTINED)
OBSERVED = (OBSERVED_ZERO, OBSERVED_NONZERO)
FLOW_FIELDS = ("buy_lots", "sell_lots", "net_lots", "buy_value_rp", "sell_value_rp", "net_value_rp")
REQUEST_FIELDS = ("symbol", "start_date", "end_date", "investor_type", "market")
MEASUREMENT_CONTRACT = "NEOBDM_INVENTORY_REPORTED_LOTS_RUPIAH_V1"
MARKET_CONTRACT = "OHLC_MARKET_VOLUME_V1"
SHARES_PER_LOT = 100


def canonical_json(value):
    """Stable JSON, with nonfinite numbers refused, never serialized as NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def evidence_contract():
    return {"inventory_means": "CUMULATIVE_BROKER_NET_FLOW_FROM_FINITE_OBSERVATION_ANCHOR",
            "negative_curve_means": "NET_SELLING_SINCE_OBSERVATION_ANCHOR",
            "full_universe": False, "unobserved_is_zero": False,
            "lot_unit": "lots", "value_unit": "rupiah", "shares_per_lot": SHARES_PER_LOT}


def market_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("market session must be YYYY-MM-DD")
    return date.fromisoformat(value)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("availability timestamp must be timezone-aware ISO text")
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("availability timestamp must include a timezone")
    return d.astimezone(timezone.utc)


def utc_text(value):
    return timestamp(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _positive_int(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class Scope:
    investor_type: str
    market_scope: str
    capture_scope: str
    source_measurement_contract: str
    basis_version: str

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in asdict(self).values()):
            raise ValueError("every compatibility scope field must be declared")


@dataclass(frozen=True)
class Capture:
    source_id: str
    capture_id: str
    response_at: str
    durable_accepted_at: str
    content_sha256: str
    request_parameters: tuple = ()
    requested_brokers: tuple = ()
    requested_selectors: tuple = ()
    returned_brokers: tuple = ()
    query_sha256: str = None
    content_hash_kind: str = "RESPONSE_BYTES_SHA256"

    def __post_init__(self):
        if not self.source_id or not self.capture_id:
            raise ValueError("capture requires source and capture IDs")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise ValueError("capture requires a SHA256 content hash")
        if self.query_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.query_sha256):
            raise ValueError("query digest must be SHA256")
        if self.content_hash_kind not in ("RESPONSE_BYTES_SHA256", "RESPONSE_TEXT_UTF8_SHA256", "CANONICAL_JSON_SHA256"):
            raise ValueError("content hash must declare its byte/text/canonical meaning")
        if timestamp(self.durable_accepted_at) < timestamp(self.response_at):
            raise ValueError("durable acceptance cannot precede the response")
        # Copy to immutable, canonical tuples; never carry credentials or arbitrary keys.
        params = tuple(sorted(tuple(p) for p in self.request_parameters))
        if any(len(p) != 2 or p[0] not in REQUEST_FIELDS or not isinstance(p[1], str)
               for p in params) or len({p[0] for p in params}) != len(params):
            raise ValueError("request parameters must be unique allowlisted text pairs")
        object.__setattr__(self, "request_parameters", params)
        for field in ("requested_brokers", "returned_brokers", "requested_selectors"):
            values = getattr(self, field)
            if any(not isinstance(v, str) for v in values):
                raise ValueError("broker/selectors must be text")
            if field != "requested_selectors" and any(not re.fullmatch(r"[A-Z]{2}", v) for v in values):
                raise ValueError("broker codes must be two uppercase letters")
            if field == "requested_selectors" and any(not re.fullmatch(r"TOP_[A-Z0-9_]+", v) for v in values):
                raise ValueError("selector must be an explicit vendor selector token")
            object.__setattr__(self, field, tuple(sorted(set(values))) if field == "returned_brokers" else tuple(values))

    @property
    def known_at(self):
        return utc_text(self.durable_accepted_at)

    def document(self):
        doc = asdict(self)
        doc.update(response_at=utc_text(self.response_at),
                   durable_accepted_at=self.known_at, known_at=self.known_at)
        doc["request_parameters"] = dict(self.request_parameters)
        return doc


@dataclass(frozen=True)
class Observation:
    ticker: str
    broker_code: str
    canonical_session_date: str
    observation_revision: int
    scope: Scope
    capture: Capture
    buy_lots: object = None
    sell_lots: object = None
    net_lots: object = None
    buy_value_rp: object = None
    sell_value_rp: object = None
    net_value_rp: object = None
    coverage: str = OBSERVED_NONZERO
    null_reason: str = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z]{4}", self.ticker) or not re.fullmatch(r"[A-Z]{2}", self.broker_code):
            raise ValueError("invalid ticker or broker code")
        market_date(self.canonical_session_date)
        _positive_int(self.observation_revision, "observation_revision")
        if not isinstance(self.scope, Scope) or not isinstance(self.capture, Capture):
            raise ValueError("typed scope and capture are required")
        if self.coverage not in COVERAGE_STATES:
            raise ValueError("unknown coverage state")


@dataclass(frozen=True)
class MarketObservation:
    ticker: str
    canonical_session_date: str
    capture: Capture
    close: object
    volume: object
    volume_unit: str
    basis_version: str
    market_scope: str
    measurement_contract: str = MARKET_CONTRACT
    valid: bool = True

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z]{4}", self.ticker):
            raise ValueError("invalid market ticker")
        market_date(self.canonical_session_date)
        if not isinstance(self.capture, Capture) or type(self.valid) is not bool:
            raise ValueError("market evidence needs typed capture and explicit validity")


@dataclass(frozen=True)
class SessionAxis:
    start: str
    cutoff: str
    sessions: tuple
    status: str
    calendar_version: str

    def __post_init__(self):
        if market_date(self.start) > market_date(self.cutoff):
            raise ValueError("anchor must not follow cutoff")
        object.__setattr__(self, "sessions", tuple(self.sessions))
        if self.status not in ("VERIFIED", "UNSUPPORTED"):
            raise ValueError("unknown calendar status")
        if tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("session axis must be sorted and unique")
        for d in self.sessions:
            market_date(d)
            if not self.start <= d <= self.cutoff:
                raise ValueError("session outside declared axis")
        if self.status == "VERIFIED":
            expected = _idx_dates(self.start, self.cutoff)
            if self.calendar_version != idx_calendar.CALENDAR_VERSION or self.sessions != expected:
                raise ValueError("verified axis must contain every IDX session")


def _idx_dates(start, cutoff):
    d, end, sessions = market_date(start), market_date(cutoff), []
    while d <= end:
        if idx_calendar.is_idx_session(d):
            sessions.append(d.isoformat())
        d += timedelta(days=1)
    return tuple(sessions)


def idx_session_axis(start, cutoff):
    """Verified IDX axis or unsupported, never extrapolated weekday sessions."""
    try:
        sessions = _idx_dates(start, cutoff)
        return SessionAxis(start, cutoff, sessions, "VERIFIED", idx_calendar.CALENDAR_VERSION)
    except idx_calendar.IdxCalendarUnavailable:
        return SessionAxis(start, cutoff, (), "UNSUPPORTED", idx_calendar.CALENDAR_VERSION)


def _finite(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _numeric(value):
    """Convert calculated Decimal only; raw vendor values retain their types."""
    if value == value.to_integral_value():
        return int(value)
    result = float(value)
    return result if math.isfinite(result) else None


def _sum(rows, field):
    return sum((Decimal(str(r["raw"][field])) for r in rows), Decimal(0))


def _raw(o):
    return {field: getattr(o, field) for field in FLOW_FIELDS}


def _coverage(o):
    if o.coverage not in OBSERVED:
        return o.coverage, o.null_reason or o.coverage
    raw = _raw(o)
    if o.broker_code not in o.capture.returned_brokers:
        return INVALID, "BROKER_NOT_RETURNED"
    params = dict(o.capture.request_parameters)
    if params.get("symbol") != o.ticker or params.get("investor_type") != o.scope.investor_type:
        return INVALID, "REQUEST_SCOPE_MISMATCH"
    if o.scope.capture_scope == "TARGETED_SELECTOR_UNION" and (
            not o.capture.requested_selectors or o.capture.requested_brokers):
        return INVALID, "CAPTURE_SCOPE_MISMATCH"
    if o.scope.capture_scope == "EXPLICIT_FOLLOWUP" and (
            o.capture.requested_selectors or o.broker_code not in o.capture.requested_brokers):
        return INVALID, "CAPTURE_SCOPE_MISMATCH"
    if o.scope.source_measurement_contract != MEASUREMENT_CONTRACT:
        return INVALID, "UNSUPPORTED_UNIT_CONTRACT"
    if any(v.upper() in ("UNKNOWN", "UNSPECIFIED", "UNSUPPORTED") for v in asdict(o.scope).values()):
        return INVALID, "UNSUPPORTED_SCOPE"
    if any(type(raw[f]) is not int for f in FLOW_FIELDS[:3]):
        return INVALID, "NULL_OR_MALFORMED_LOTS"
    if any(abs(raw[f]) > 2**63 - 1 for f in FLOW_FIELDS[:3]):
        return INVALID, "LOTS_OUT_OF_RANGE"
    if any(not _finite(raw[f]) for f in FLOW_FIELDS[3:]):
        return INVALID, "NULL_OR_NONFINITE_VALUE"
    if any(raw[f] < 0 for f in ("buy_lots", "sell_lots", "buy_value_rp", "sell_value_rp")):
        return INVALID, "NEGATIVE_GROSS_FLOW"
    if raw["net_lots"] != raw["buy_lots"] - raw["sell_lots"]:
        return INVALID, "LOT_CONSERVATION"
    if abs(Decimal(str(raw["net_value_rp"])) - (Decimal(str(raw["buy_value_rp"])) -
                                                Decimal(str(raw["sell_value_rp"])))) > Decimal("0.5"):
        return INVALID, "VALUE_CONSERVATION"
    if any(raw[lots] > 0 and raw[value] == 0 for lots, value in
           (("buy_lots", "buy_value_rp"), ("sell_lots", "sell_value_rp"))):
        return INVALID, "LOTS_WITHOUT_VALUE"
    # Activity is determined from all six fields, never from net lots alone.
    return (OBSERVED_ZERO if all(v == 0 for v in raw.values()) else OBSERVED_NONZERO), None


def _ref(o):
    return {"source_id": o.capture.source_id, "capture_id": o.capture.capture_id, "content_sha256": o.capture.content_sha256,
            "source_market_session": o.canonical_session_date,
            "observation_revision": o.observation_revision}


def _refs(rows):
    return sorted({canonical_json(ref): ref for r in rows for ref in r["input_refs"]}.values(),
                  key=canonical_json)


def _series(broker, observations, axis, revision):
    groups = {}
    for o in observations:
        if o.broker_code == broker:
            groups.setdefault(o.canonical_session_date, []).append(o)
    dates = axis.sessions if axis.status == "VERIFIED" else tuple(sorted(groups))
    session_presence = {o.canonical_session_date for o in observations}
    rows, current_scope, segment, start = [], None, None, None
    local_lots = local_value = Decimal(0)
    anchor_lots = anchor_value = Decimal(0)
    intact = axis.status == "VERIFIED"
    pending = "OBSERVATION_ANCHOR" if intact else "CALENDAR_UNSUPPORTED"
    for d in dates:
        candidates = groups.get(d, [])
        refs, raw, scope, row_revision = [], None, None, None
        state, reason = UNOBSERVED, ("MISSING_BROKER_COVERAGE" if d in session_presence
                                     else "MISSING_EXPECTED_SESSION")
        if candidates:
            row_revision = max(o.observation_revision for o in candidates)
            candidates = [o for o in candidates if o.observation_revision == row_revision]
            signatures = {canonical_json({"raw": {k: v if _finite(v) else None for k, v in _raw(o).items()},
                                           "scope": asdict(o.scope), "state": _coverage(o)})
                          for o in candidates}
            refs = sorted({canonical_json(_ref(o)): _ref(o) for o in candidates}.values(), key=canonical_json)
            if len(signatures) != 1:
                state, reason = QUARANTINED, "CONFLICTING_REVISIONS"
            else:
                o = sorted(candidates, key=lambda o: canonical_json(_ref(o)))[0]
                state, reason = _coverage(o)
                raw = {k: v if _finite(v) else None for k, v in _raw(o).items()}
                if state == UNOBSERVED:
                    raw = None
                scope = asdict(o.scope)
        broken = state not in OBSERVED
        boundary = None
        if not broken and current_scope is not None and scope != current_scope:
            changed = [k for k in scope if scope[k] != current_scope[k]]
            boundary = "INCOMPATIBLE_" + "_AND_".join(k.upper() for k in sorted(changed))
        if axis.status != "VERIFIED":
            boundary = "CALENDAR_UNSUPPORTED"
        if broken or boundary:
            intact = False
            segment = None
            pending = boundary or reason
        if not broken:
            if segment is None:
                start = d
                segment = content_hash({"broker_code": broker, "ticker": o.ticker,
                                        "segment_start": start, "scope": scope})[:24]
                local_lots = local_value = Decimal(0)
                boundary = pending
                pending = None
            local_lots += Decimal(raw["net_lots"])
            local_value += Decimal(str(raw["net_value_rp"]))
            if intact:
                anchor_lots += Decimal(raw["net_lots"])
                anchor_value += Decimal(str(raw["net_value_rp"]))
            current_scope = scope
        else:
            current_scope = None
        rows.append({"canonical_session_date": d, "broker_code": broker,
                     "observation_revision": row_revision, "evidence_revision": revision,
                     "coverage": state, "null_reason": reason, "raw": raw, "scope": scope,
                     "input_refs": refs, "segment_id": None if broken else segment,
                     "segment_start": None if broken else start,
                     "opening_position_lots": None, "left_censored": True,
                     "break_reason": reason if broken else boundary,
                     "continuity_status": "CONTINUOUS_FROM_ANCHOR" if intact else
                         ("UNKNOWN_CALENDAR" if axis.status != "VERIFIED" else "ANCHOR_BROKEN"),
                     "cumulative_observable_lots": _numeric(anchor_lots) if intact else None,
                     "cumulative_observable_value": _numeric(anchor_value) if intact else None,
                     "segment_cumulative_net_lots": None if broken else _numeric(local_lots),
                     "segment_cumulative_net_value": None if broken else _numeric(local_value)})
    return rows


def _measurement(rows, axis, n, value=None, reason=None, offset=0):
    end = max(0, len(axis.sessions) - offset)
    dates = axis.sessions[max(0, end - n):end]
    selected = [r for r in rows if r["canonical_session_date"] in dates]
    observed = sum(r["coverage"] in OBSERVED for r in selected)
    if axis.status != "VERIFIED":
        reason = "CALENDAR_UNSUPPORTED"
    elif len(dates) != n:
        reason = "INSUFFICIENT_HISTORY"
    elif observed != n:
        reason = "INCOMPLETE_COVERAGE"
    elif len({r["segment_id"] for r in selected}) != 1:
        reason = "INCOMPATIBLE_SEGMENTS"
    return {"value": None if reason else value,
            "status": "WITHHELD" if reason else "CALCULATED", "null_reason": reason,
            "window": {"sessions": n, "offset_sessions": offset,
                       "start": dates[0] if dates else None, "end": dates[-1] if dates else axis.cutoff,
                       "session_dates": list(dates)},
            "expected_sessions": n, "observed_sessions": observed, "input_refs": _refs(selected)}


def _market_by_date(market):
    groups = {}
    for row in market:
        groups.setdefault(row.canonical_session_date, []).append(row)
    # Conflicting market captures are withheld, never averaged or arbitrarily selected.
    return {d: rows[0] for d, rows in groups.items() if len({canonical_json({
        "close": r.close if _finite(r.close) else None,
        "volume": r.volume if _finite(r.volume) else None,
        "unit": r.volume_unit, "basis": r.basis_version, "market": r.market_scope,
        "contract": r.measurement_contract, "valid": r.valid}) for r in rows}) == 1}


def _market_measurement(rows, axis, n, market, kind, offset=0):
    doc = _measurement(rows, axis, n, offset=offset)
    dates = doc["window"]["session_dates"]
    if kind == "adv20":
        end = max(0, len(axis.sessions) - offset)
        dates = list(axis.sessions[max(0, end - 20):end])
    else:
        start = len(axis.sessions) - offset - n
        dates = ([axis.sessions[start - 1]] if start > 0 else []) + dates
    selected = [market.get(d) for d in dates]
    scope = next((r["scope"] for r in reversed(rows) if r["canonical_session_date"] in
                  doc["window"]["session_dates"] and r["scope"]), None)
    needed = 20 if kind == "adv20" else n + 1
    valid = [r for r in selected if r is not None and r.valid and scope is not None and
             r.basis_version == scope["basis_version"] and r.market_scope == scope["market_scope"] and
             r.measurement_contract == MARKET_CONTRACT and
             (_finite(r.volume) and r.volume >= 0 and r.volume_unit in ("shares", "lots")
              if kind == "adv20" else _finite(r.close) and r.close > 0)]
    refs = [{"source_id": r.capture.source_id, "capture_id": r.capture.capture_id, "content_sha256": r.capture.content_sha256,
             "source_market_session": r.canonical_session_date, "measurement_contract": r.measurement_contract}
            for r in valid]
    reason = doc["null_reason"]
    if len(valid) != needed:
        reason = reason or "INCOMPLETE_VERIFIED_MARKET_DATA"
    result = None
    if not reason:
        if kind == "adv20":
            result = sum((Decimal(str(r.volume)) / (100 if r.volume_unit == "shares" else 1)
                          for r in valid), Decimal(0)) / 20
            if result == 0:
                reason = "ZERO_ADV"
        else:
            result = Decimal(str(valid[-1].close)) / Decimal(str(valid[0].close)) - 1
    doc.update(value=None if reason else _numeric(result), status="WITHHELD" if reason else "CALCULATED",
               null_reason=reason, unit="lots/session" if kind == "adv20" else "fraction",
               market_window={"session_dates": dates, "expected_sessions": needed,
                                                  "observed_sessions": len(valid)},
               input_refs=sorted(doc["input_refs"] + refs, key=canonical_json))
    if kind == "return":
        doc["market_window"].update(
            baseline_close_rp_per_share=valid[0].close if not reason else None,
            end_close_rp_per_share=valid[-1].close if not reason else None)
    return doc


def _metrics(rows, axis, n, market, offset=0):
    template = _measurement(rows, axis, n, offset=offset)
    dates = template["window"]["session_dates"]
    chosen = [r for r in rows if r["canonical_session_date"] in dates]
    complete = template["status"] == "CALCULATED"

    def measure(value=None, reason=None, **details):
        if isinstance(value, Decimal):
            value = _numeric(value)
            if value is None:
                reason = reason or "NUMERIC_OVERFLOW"
        return dict(_measurement(rows, axis, n, value, reason, offset), **details)

    net = _sum(chosen, "net_lots") if complete else Decimal(0)
    absolute = sum((abs(Decimal(r["raw"]["net_lots"])) for r in chosen), Decimal(0)) if complete else Decimal(0)
    numerator = sum(r["raw"]["net_lots"] > 0 for r in chosen) if complete else None
    directions = [1 if r["raw"]["net_lots"] > 0 else -1 for r in chosen
                  if r["raw"]["net_lots"] != 0] if complete else []
    result = {
        "net_flow": measure(net, unit="lots"),
        "net_flow_slope": measure(net / n, unit="lots/session"),
        "persistence": measure(Decimal(numerator) / n if complete else None,
                               numerator=numerator, denominator=n, unit="fraction"),
        "reversal": measure(sum(a != b for a, b in zip(directions, directions[1:])),
                            zero_handling="SKIP_ZEROS_WITHIN_COMPLETE_WINDOW", unit="count"),
        "direction_efficiency": measure(abs(net) / absolute if absolute else None,
                                        None if absolute else "ZERO_DENOMINATOR", unit="fraction"),
    }
    for name, field in (("gross_buy_lots", "buy_lots"), ("gross_sell_lots", "sell_lots")):
        result[name] = measure(_sum(chosen, field) if complete else None, unit="lots")
    result["two_sided_broker_activity_lots"] = measure(
        _sum(chosen, "buy_lots") + _sum(chosen, "sell_lots") if complete else None, unit="lots")
    for side in ("buy", "sell"):
        lots = _sum(chosen, f"{side}_lots") if complete else Decimal(0)
        unpriced = any(r["raw"][f"{side}_lots"] == 0 and r["raw"][f"{side}_value_rp"] != 0
                       for r in chosen) if complete else False
        reason = "VALUE_WITHOUT_REPORTED_LOTS" if unpriced else (None if lots else "ZERO_DENOMINATOR")
        result[f"implied_{side}_price"] = measure(
            _sum(chosen, f"{side}_value_rp") / (SHARES_PER_LOT * lots) if lots else None,
            reason, unit="rupiah/share", shares_per_lot=SHARES_PER_LOT)
    adv = _market_measurement(rows, axis, n, market, "adv20", offset)
    result["flow_vs_adv"] = measure(net / Decimal(str(adv["value"])) if adv["status"] == "CALCULATED" else None,
                                    adv["null_reason"], unit="ADV20 multiples", adv20=adv)
    result["flow_vs_adv"]["input_refs"] = adv["input_refs"]
    price = _market_measurement(rows, axis, n, market, "return", offset)
    facts = {"price_return": price, "net_flow": result["net_flow"],
             "net_flow_slope": result["net_flow_slope"], "coverage": template["status"]}
    result["price_flow_divergence"] = measure(
        {"price_return": price["value"], "net_flow_lots": result["net_flow"]["value"],
         "net_flow_slope_lots_per_session": result["net_flow_slope"]["value"]},
        price["null_reason"], facts=facts, unit="underlying_facts")
    result["price_flow_divergence"]["input_refs"] = price["input_refs"]
    return result


def _build_inventory_evidence(observations, *, ticker, broker_codes, axis, availability_cutoff,
                              observation_revision=1, parent_revision=None, windows=(5, 20),
                              acceleration_half_window=5, market_observations=(), reference_captures=()):
    """Build one as-of revision. Missing rows become unknown coverage, never flows.

    Inputs with revisions above observation_revision or acceptance after the
    availability cutoff cannot contribute. Equally revised conflicting flows
    are quarantined. A later revision can replace them in a new document only.
    Price return is close(end)/close(session before window)-1. ADV20 needs all
    twenty actual OHLC market volumes, compatible with the reported lot basis.
    """
    if not re.fullmatch(r"[A-Z]{4}", ticker) or not isinstance(axis, SessionAxis):
        raise ValueError("typed session axis and valid ticker required")
    _positive_int(observation_revision, "observation_revision")
    if parent_revision is not None:
        _positive_int(parent_revision, "parent_revision")
        if parent_revision >= observation_revision:
            raise ValueError("parent must precede revision")
    _positive_int(acceleration_half_window, "acceleration_half_window")
    windows = tuple(sorted(set(windows)))
    if not windows:
        raise ValueError("at least one window required")
    for n in windows:
        _positive_int(n, "window")
    codes = sorted(set(broker_codes))
    if not codes or any(not isinstance(b, str) or not re.fullmatch(r"[A-Z]{2}", b) for b in codes):
        raise ValueError("explicit evidence broker codes required")
    cutoff = timestamp(availability_cutoff)
    observations = tuple(observations)
    market_observations = tuple(market_observations)
    if any(not isinstance(o, Observation) or o.ticker != ticker for o in observations):
        raise ValueError("observations must be typed rows of this ticker")
    if any(not isinstance(o, MarketObservation) or o.ticker != ticker for o in market_observations):
        raise ValueError("market observations must be typed rows of this ticker")
    visible = [o for o in observations if axis.start <= o.canonical_session_date <= axis.cutoff
               and o.observation_revision <= observation_revision and timestamp(o.capture.known_at) <= cutoff]
    market = [o for o in market_observations if o.canonical_session_date <= axis.cutoff and
              timestamp(o.capture.known_at) <= cutoff]
    if axis.status == "VERIFIED" and any(o.canonical_session_date not in axis.sessions for o in visible):
        raise ValueError("observation reports a verified non-session")
    captures = {}
    for o in visible + market:
        if timestamp(o.capture.response_at).astimezone(timezone(timedelta(hours=7))).date() < market_date(o.canonical_session_date):
            raise ValueError("capture response precedes its source market session")
        doc = o.capture.document()
        key = (o.capture.source_id, o.capture.capture_id)
        if key in captures and captures[key] != doc:
            raise ValueError("capture ID has conflicting provenance")
        captures[key] = doc
    for capture in reference_captures:
        if not isinstance(capture, Capture) or timestamp(capture.known_at) > cutoff:
            raise ValueError("reference capture is not available at cutoff")
        key = (capture.source_id, capture.capture_id)
        doc = capture.document()
        if key in captures and captures[key] != doc:
            raise ValueError("reference capture ID has conflicting provenance")
        captures[key] = doc
    market_by_date = _market_by_date(sorted(market, key=lambda o: (o.canonical_session_date, o.capture.capture_id)))
    brokers, rotation = {}, {}
    for b in codes:
        rows = _series(b, visible, axis, observation_revision)
        measurements = {str(n): _metrics(rows, axis, n, market_by_date) for n in windows}
        h = acceleration_half_window
        acceleration = _measurement(rows, axis, 2 * h)
        if acceleration["status"] == "CALCULATED":
            chosen = [r for r in rows if r["canonical_session_date"] in acceleration["window"]["session_dates"]]
            acceleration["value"] = _numeric((_sum(chosen[h:], "net_lots") - _sum(chosen[:h], "net_lots")) / h)
        acceleration.update(unit="lots/session", recent_sessions=h, preceding_sessions=h)
        brokers[b] = {"series": rows, "measurements": measurements, "acceleration": acceleration}
        recent = measurements[str(windows[0])]
        prior = _metrics(rows, axis, windows[0], market_by_date, offset=windows[0])
        activity = next((r["canonical_session_date"] for r in rows if r["coverage"] == OBSERVED_NONZERO), None)
        rotation[b] = {"prior_window": prior, "recent_window": recent,
                       "first_observed_activity": activity, "left_censored": True}
    positive = [b for b in codes if rotation[b]["recent_window"]["net_flow"]["status"] == "CALCULATED"
                and rotation[b]["recent_window"]["net_flow"]["value"] > 0]
    count_inputs = [rotation[b]["recent_window"]["net_flow"] for b in codes]
    count = dict(count_inputs[0])
    count.update(value=len(positive), unit="broker-code count", observed_sessions=min(
        m["observed_sessions"] for m in count_inputs), input_refs=sorted({canonical_json(ref): ref
        for m in count_inputs for ref in m["input_refs"]}.values(), key=canonical_json))
    if any(m["status"] != "CALCULATED" for m in count_inputs):
        count.update(value=None, status="WITHHELD", null_reason="INCOMPLETE_OBSERVED_SUBSET")
    doc = {"schema": SCHEMA, "schema_version": VERSION, "ticker": ticker,
           "coverage_scope": "OBSERVED_BROKER_SUBSET",
           "observation_revision": observation_revision, "parent_revision": parent_revision,
           "availability_cutoff": utc_text(availability_cutoff),
           "known_at": max((c["known_at"] for c in captures.values()), default=None),
           "contract": evidence_contract(),
           "axis": asdict(axis), "declared_windows": list(windows),
           "provenance": sorted(captures.values(), key=lambda c: (c["source_id"], c["capture_id"])),
           "requested_evidence_brokers": codes,
           "observed_brokers": sorted({o.broker_code for o in visible if _coverage(o)[0] in OBSERVED}),
           "brokers": brokers,
           "rotation_evidence": {"context": {"ticker": ticker, "cutoff": axis.cutoff,
                                  "observation_revision": observation_revision,
                                  "coverage_scope": "OBSERVED_BROKER_SUBSET", "declared_windows": list(windows)},
                                 "broker_windows": rotation, "concurrent_positive_brokers": positive,
                                 "observed_subset_positive_broker_count": count,
                                 "concurrent_buying_means": "ABSORPTION_PROXY_ONLY_NO_COUNTERPARTY_LINK"}}
    # Round-trip detaches mutable input values and gives callers only JSON types.
    return json.loads(canonical_json(doc))


def build_inventory_evidence(observations, **parameters):
    """Public deterministic entry point; independent of the caller's Decimal context."""
    with localcontext(Context(prec=1000, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                              capitals=1, clamp=0, flags=[],
                              traps=[InvalidOperation, DivisionByZero, Overflow])):
        return _build_inventory_evidence(observations, **parameters)


def validate_document(doc):
    """Refuse changed semantics or extra fields at the persistence boundary.

    The storage API accepts only this version's evidence vocabulary. Dynamic
    map keys are broker codes and integer window sizes, never actor labels.
    Arithmetic is produced by build_inventory_evidence, not recalculated here.
    """
    top = {"schema", "schema_version", "ticker", "coverage_scope", "observation_revision", "parent_revision",
           "availability_cutoff", "known_at", "contract", "axis", "declared_windows", "provenance",
           "requested_evidence_brokers", "observed_brokers", "brokers", "rotation_evidence"}
    if set(doc) != top or doc["schema"] != SCHEMA or doc["schema_version"] != VERSION:
        raise ValueError("not an inventory evidence v1 document")
    if doc["contract"] != evidence_contract() or doc["coverage_scope"] != "OBSERVED_BROKER_SUBSET":
        raise ValueError("inventory evidence v1 is partial coverage with unknown omissions")
    _positive_int(doc["observation_revision"], "observation_revision")
    utc_text(doc["availability_cutoff"])
    if doc["known_at"] is not None and timestamp(doc["known_at"]) > timestamp(doc["availability_cutoff"]):
        raise ValueError("known_at exceeds the availability cutoff")
    SessionAxis(**doc["axis"])
    if not re.fullmatch(r"[A-Z]{4}", doc["ticker"]):
        raise ValueError("invalid evidence ticker")
    if set(doc["brokers"]) != set(doc["requested_evidence_brokers"]):
        raise ValueError("broker map differs from declared evidence brokers")
    metrics = set("net_flow net_flow_slope persistence reversal direction_efficiency gross_buy_lots gross_sell_lots two_sided_broker_activity_lots implied_buy_price implied_sell_price flow_vs_adv price_flow_divergence".split())
    row_fields = set("canonical_session_date broker_code observation_revision evidence_revision coverage null_reason raw scope input_refs segment_id segment_start opening_position_lots left_censored break_reason continuity_status cumulative_observable_lots cumulative_observable_value segment_cumulative_net_lots segment_cumulative_net_value".split())

    def exact(value, fields):
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("evidence object fields differ from the v1 contract")

    def metric_map(value):
        exact(value, metrics)

    for broker, value in doc["brokers"].items():
        if not re.fullmatch(r"[A-Z]{2}", broker):
            raise ValueError("invalid evidence broker")
        exact(value, {"series", "measurements", "acceleration"})
        exact(value["measurements"], {str(n) for n in doc["declared_windows"]})
        for measurements in value["measurements"].values():
            metric_map(measurements)
        for row in value["series"]:
            exact(row, row_fields)
            if row["broker_code"] != broker or row["evidence_revision"] != doc["observation_revision"]:
                raise ValueError("row grain differs from document")
            if row["raw"] is not None:
                exact(row["raw"], set(FLOW_FIELDS))
            if row["scope"] is not None:
                Scope(**row["scope"])
    rotation = doc["rotation_evidence"]
    exact(rotation, {"context", "broker_windows", "concurrent_positive_brokers",
                     "observed_subset_positive_broker_count", "concurrent_buying_means"})
    exact(rotation["broker_windows"], set(doc["brokers"]))
    for value in rotation["broker_windows"].values():
        exact(value, {"prior_window", "recent_window", "first_observed_activity", "left_censored"})
        metric_map(value["prior_window"])
        metric_map(value["recent_window"])
    allowed = top | set(FLOW_FIELDS) | set(REQUEST_FIELDS) | set(Scope.__dataclass_fields__) | set(Capture.__dataclass_fields__)
    allowed |= set("inventory_means negative_curve_means full_universe unobserved_is_zero lot_unit value_unit shares_per_lot start cutoff sessions status calendar_version response_at durable_accepted_at known_at series measurements acceleration canonical_session_date broker_code evidence_revision coverage null_reason raw scope input_refs segment_id segment_start opening_position_lots left_censored break_reason continuity_status cumulative_observable_lots cumulative_observable_value segment_cumulative_net_lots segment_cumulative_net_value source_market_session net_flow net_flow_slope persistence reversal direction_efficiency gross_buy_lots gross_sell_lots two_sided_broker_activity_lots implied_buy_price implied_sell_price flow_vs_adv price_flow_divergence value window expected_sessions observed_sessions offset_sessions end session_dates unit numerator denominator zero_handling adv20 market_window measurement_contract facts price_return net_flow_lots net_flow_slope_lots_per_session recent_sessions preceding_sessions context coverage_scope broker_windows prior_window recent_window first_observed_activity concurrent_positive_brokers observed_subset_positive_broker_count concurrent_buying_means baseline_close_rp_per_share end_close_rp_per_share".split())

    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key not in allowed and not re.fullmatch(r"[A-Z]{2}|[1-9][0-9]*", key):
                    raise ValueError(f"field outside evidence v1 vocabulary: {key}")
                if key == "opening_position_lots" and child is not None:
                    raise ValueError("opening position must remain unknown")
                check(child)
        elif isinstance(value, (tuple, list)):
            for child in value:
                check(child)
    check(doc)
    canonical_json(doc)
