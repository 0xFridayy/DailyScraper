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
CAPTURE_SCOPES = ("TARGETED_SELECTOR_UNION", "EXPLICIT_FOLLOWUP")
NULL_REASONS = frozenset("""OBSERVED_ZERO OBSERVED_NONZERO UNOBSERVED INVALID QUARANTINED
    KNOWN_BASIS_CONFLICT SUSPENSION_UNCERTAIN BROKER_NOT_RETURNED REQUEST_SCOPE_MISMATCH
    REQUEST_SESSION_OUT_OF_RANGE CAPTURE_SCOPE_MISMATCH UNSUPPORTED_UNIT_CONTRACT UNSUPPORTED_SCOPE
    NULL_OR_MALFORMED_LOTS LOTS_OUT_OF_RANGE NULL_OR_NONFINITE_VALUE NEGATIVE_GROSS_FLOW
    LOT_CONSERVATION VALUE_CONSERVATION LOTS_WITHOUT_VALUE MISSING_BROKER_COVERAGE
    MISSING_EXPECTED_SESSION CONFLICTING_REVISIONS CALENDAR_UNSUPPORTED INSUFFICIENT_HISTORY
    INCOMPLETE_COVERAGE INCOMPATIBLE_SEGMENTS INCOMPLETE_VERIFIED_MARKET_DATA ZERO_ADV
    ZERO_DENOMINATOR VALUE_WITHOUT_REPORTED_LOTS NUMERIC_OVERFLOW INCOMPLETE_OBSERVED_SUBSET
    SESSION_NOT_FINAL_AT_CAPTURE""".split())


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
        if self.capture_scope not in CAPTURE_SCOPES:
            raise ValueError("unsupported capture scope")


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
        dates = dict(params)
        for field in ("start_date", "end_date"):
            if field in dates:
                market_date(dates[field])
        if dates.get("start_date", "") > dates.get("end_date", "9999-12-31"):
            raise ValueError("request start_date follows end_date")
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
        if self.null_reason is not None and self.null_reason not in NULL_REASONS:
            raise ValueError("null reason must be a controlled evidence reason")


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


def _request_issue(o):
    params = dict(o.capture.request_parameters)
    if not params.get("start_date", "0001-01-01") <= o.canonical_session_date <= params.get("end_date", "9999-12-31"):
        return "REQUEST_SESSION_OUT_OF_RANGE"
    # No authoritative exchange close-time contract exists here. Daily evidence
    # is final only when the response's Jakarta date is after the source session.
    local_date = timestamp(o.capture.response_at).astimezone(timezone(timedelta(hours=7))).date()
    if local_date <= market_date(o.canonical_session_date):
        return "SESSION_NOT_FINAL_AT_CAPTURE"
    return None


def _coverage(o):
    issue = _request_issue(o)
    if issue:
        return INVALID, issue
    if o.coverage not in OBSERVED:
        return o.coverage, o.null_reason or o.coverage
    raw = _raw(o)
    if o.broker_code not in o.capture.returned_brokers:
        return INVALID, "BROKER_NOT_RETURNED"
    params = dict(o.capture.request_parameters)
    if (params.get("symbol") != o.ticker or params.get("investor_type") != o.scope.investor_type
            or ("market" in params and params["market"] != o.scope.market_scope)):
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
    return {d: tuple(sorted(rows, key=lambda r: canonical_json(r.capture.document())))
            for d, rows in groups.items() if len({canonical_json({
        "close": r.close if _finite(r.close) else None,
        "volume": r.volume if _finite(r.volume) else None,
        "unit": r.volume_unit, "basis": r.basis_version, "market": r.market_scope,
        "contract": r.measurement_contract, "valid": r.valid,
        "request_issue": _market_request_issue(r)}) for r in rows}) == 1}


def _market_request_issue(row):
    params = dict(row.capture.request_parameters)
    if (params.get("symbol") != row.ticker
            or ("market" in params and params["market"] != row.market_scope)):
        return "REQUEST_SCOPE_MISMATCH"
    return _request_issue(row)


def _market_measurement(rows, axis, n, market, kind, offset=0):
    doc = _measurement(rows, axis, n, offset=offset)
    dates = doc["window"]["session_dates"]
    if kind == "adv20":
        end = max(0, len(axis.sessions) - offset)
        dates = list(axis.sessions[max(0, end - 20):end])
    else:
        start = len(axis.sessions) - offset - n
        dates = ([axis.sessions[start - 1]] if start > 0 else []) + dates
    selected = [market[d][0] if d in market else None for d in dates]
    scope = next((r["scope"] for r in reversed(rows) if r["canonical_session_date"] in
                  doc["window"]["session_dates"] and r["scope"]), None)
    needed = 20 if kind == "adv20" else n + 1
    valid = [r for r in selected if r is not None and r.valid and not _market_request_issue(r) and scope is not None and
             r.basis_version == scope["basis_version"] and r.market_scope == scope["market_scope"] and
             r.measurement_contract == MARKET_CONTRACT and
             (_finite(r.volume) and r.volume >= 0 and r.volume_unit in ("shares", "lots")
              if kind == "adv20" else _finite(r.close) and r.close > 0)]
    refs = [{"source_id": r.capture.source_id, "capture_id": r.capture.capture_id, "content_sha256": r.capture.content_sha256,
             "source_market_session": r.canonical_session_date, "measurement_contract": r.measurement_contract}
            for representative in valid for r in market[representative.canonical_session_date]]
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
               input_refs=sorted({canonical_json(ref): ref for ref in doc["input_refs"] + refs}.values(), key=canonical_json))
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


def _build_inventory_evidence(observations, *, ticker, broker_codes, axis, availability_cutoff, compatibility_scopes,
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
        if parent_revision + 1 != observation_revision:
            raise ValueError("child revision must equal parent + 1")
    elif observation_revision != 1:
        raise ValueError("a request chain must start at revision 1")
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
    declared_scopes = tuple(compatibility_scopes)
    if not declared_scopes or any(not isinstance(s, Scope) for s in declared_scopes):
        raise ValueError("nonempty typed compatibility scopes are required")
    if any(s.source_measurement_contract != MEASUREMENT_CONTRACT or
           any(v.upper() in ("UNKNOWN", "UNSPECIFIED", "UNSUPPORTED") for v in asdict(s).values())
           for s in declared_scopes):
        raise ValueError("unsupported declared compatibility scope")
    scopes = sorted({canonical_json(asdict(s)): asdict(s) for s in declared_scopes}.values(), key=canonical_json)
    visible = [o for o in observations if axis.start <= o.canonical_session_date <= axis.cutoff
               and o.observation_revision <= observation_revision and timestamp(o.capture.known_at) <= cutoff]
    if any(asdict(o.scope) not in scopes for o in visible if o.broker_code in codes):
        raise ValueError("row scope outside declared compatibility contract")
    identity = {"evidence_schema_version": VERSION, "ticker": ticker, "anchor": axis.start,
                "cutoff": axis.cutoff, "requested_evidence_brokers": codes,
                "declared_windows": list(windows), "acceleration_half_window": acceleration_half_window,
                "compatibility_scopes": scopes, "session_axis": asdict(axis),
                "market_measurement_contract": MARKET_CONTRACT}
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
    market_by_date = _market_by_date(market)
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
    positive_measurement = dict(count, value=positive if count["status"] == "CALCULATED" else None,
                                unit="broker-code list")
    doc = {"schema": SCHEMA, "schema_version": VERSION, "ticker": ticker,
           "coverage_scope": "OBSERVED_BROKER_SUBSET",
           "observation_revision": observation_revision, "parent_revision": parent_revision,
           "availability_cutoff": utc_text(availability_cutoff),
           "max_input_known_at": max((c["known_at"] for c in captures.values()), default=None),
           "request_identity": identity, "request_contract_sha256": content_hash(identity),
           "contract": evidence_contract(),
           "axis": asdict(axis), "declared_windows": list(windows),
           "provenance": sorted(captures.values(), key=lambda c: (c["source_id"], c["capture_id"])),
           "requested_evidence_brokers": codes,
           "observed_brokers": [b for b in codes if any(r["coverage"] in OBSERVED for r in brokers[b]["series"])],
           "brokers": brokers,
           "rotation_evidence": {"context": {"ticker": ticker, "cutoff": axis.cutoff,
                                  "observation_revision": observation_revision,
                                  "coverage_scope": "OBSERVED_BROKER_SUBSET", "declared_windows": list(windows)},
                                 "broker_windows": rotation, "concurrent_positive_brokers": positive_measurement,
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
    """Validate the v1 vocabulary and semantic constants before persistence.

    This boundary checks the declared evidence contract, availability, and
    envelope semantics. It does not recalculate flow or market measurements.
    """
    def exact(value, fields):
        if not isinstance(value, dict) or set(value) != set(fields):
            raise ValueError("evidence object fields differ from the v1 contract")

    def codes(value):
        if (not isinstance(value, list) or any(not isinstance(b, str) or
                not re.fullmatch(r"[A-Z]{2}", b) for b in value) or value != sorted(set(value))):
            raise ValueError("evidence broker codes must be sorted and unique")

    def number(value, nullable=False):
        if not (nullable and value is None) and not _finite(value):
            raise ValueError("evidence numeric value must be finite")

    def reason(value, nullable=True, boundary=False):
        if nullable and value is None:
            return
        if isinstance(value, str) and (value in NULL_REASONS or (boundary and value == "OBSERVATION_ANCHOR")):
            return
        if boundary and isinstance(value, str) and value.startswith("INCOMPATIBLE_"):
            fields = value[len("INCOMPATIBLE_"):].split("_AND_")
            allowed = {name.upper() for name in Scope.__dataclass_fields__}
            if fields and fields == sorted(set(fields)) and set(fields) <= allowed:
                return
        raise ValueError("null or break reason is outside the controlled v1 vocabulary")

    top = {"schema", "schema_version", "ticker", "coverage_scope", "observation_revision", "parent_revision",
           "availability_cutoff", "max_input_known_at", "request_identity", "request_contract_sha256",
           "contract", "axis", "declared_windows", "provenance", "requested_evidence_brokers",
           "observed_brokers", "brokers", "rotation_evidence"}
    exact(doc, top)
    if doc["schema"] != SCHEMA or doc["schema_version"] != VERSION:
        raise ValueError("not an inventory evidence v1 document")
    if canonical_json(doc["contract"]) != canonical_json(evidence_contract()) or doc["coverage_scope"] != "OBSERVED_BROKER_SUBSET":
        raise ValueError("inventory evidence v1 is partial coverage with unknown omissions")
    _positive_int(doc["observation_revision"], "observation_revision")
    if doc["parent_revision"] is not None:
        _positive_int(doc["parent_revision"], "parent_revision")
        if doc["parent_revision"] + 1 != doc["observation_revision"]:
            raise ValueError("child revision must equal parent + 1")
    elif doc["observation_revision"] != 1:
        raise ValueError("a request chain must start at revision 1")
    cutoff = timestamp(doc["availability_cutoff"])
    if doc["availability_cutoff"] != utc_text(doc["availability_cutoff"]):
        raise ValueError("availability cutoff must be canonical UTC text")
    exact(doc["axis"], SessionAxis.__dataclass_fields__)
    axis = SessionAxis(**doc["axis"])
    if not isinstance(doc["ticker"], str) or not re.fullmatch(r"[A-Z]{4}", doc["ticker"]):
        raise ValueError("invalid evidence ticker")
    windows = doc["declared_windows"]
    if not isinstance(windows, list) or not windows:
        raise ValueError("declared evidence windows are required")
    for n in windows:
        _positive_int(n, "window")
    if windows != sorted(set(windows)):
        raise ValueError("declared windows must be sorted and unique")
    codes(doc["requested_evidence_brokers"])
    if not doc["requested_evidence_brokers"]:
        raise ValueError("explicit evidence broker codes required")
    exact(doc["brokers"], doc["requested_evidence_brokers"])
    codes(doc["observed_brokers"])
    identity = doc["request_identity"]
    exact(identity, {"evidence_schema_version", "ticker", "anchor", "cutoff", "requested_evidence_brokers",
                     "declared_windows", "acceleration_half_window", "compatibility_scopes", "session_axis",
                     "market_measurement_contract"})
    _positive_int(identity["acceleration_half_window"], "acceleration_half_window")
    scopes = identity["compatibility_scopes"]
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("compatibility scopes must be a nonempty declared list")
    for scope in scopes:
        exact(scope, Scope.__dataclass_fields__)
        Scope(**scope)
        if (scope["source_measurement_contract"] != MEASUREMENT_CONTRACT or
                any(v.upper() in ("UNKNOWN", "UNSPECIFIED", "UNSUPPORTED") for v in scope.values())):
            raise ValueError("unsupported declared compatibility scope")
    if scopes != sorted({canonical_json(s): s for s in scopes}.values(), key=canonical_json):
        raise ValueError("compatibility scopes must be sorted and unique")
    expected_identity = {"evidence_schema_version": VERSION, "ticker": doc["ticker"], "anchor": axis.start,
                         "cutoff": axis.cutoff, "requested_evidence_brokers": doc["requested_evidence_brokers"],
                         "declared_windows": windows, "acceleration_half_window": identity["acceleration_half_window"],
                         "compatibility_scopes": scopes, "session_axis": doc["axis"],
                         "market_measurement_contract": MARKET_CONTRACT}
    if canonical_json(identity) != canonical_json(expected_identity) or doc["request_contract_sha256"] != content_hash(identity):
        raise ValueError("request identity differs from the evidence contract")

    captures = {}
    capture_objects = {}
    if not isinstance(doc["provenance"], list):
        raise ValueError("capture provenance must be a list")
    for capture in doc["provenance"]:
        exact(capture, set(Capture.__dataclass_fields__) | {"known_at"})
        if any(not isinstance(capture[k], str) for k in ("source_id", "capture_id")):
            raise ValueError("capture identifiers must be text")
        if not isinstance(capture["request_parameters"], dict):
            raise ValueError("capture request parameters must be an object")
        fields = {k: v for k, v in capture.items() if k != "known_at"}
        fields["request_parameters"] = tuple(fields["request_parameters"].items())
        typed_capture = Capture(**fields)
        rebuilt = typed_capture.document()
        if canonical_json(rebuilt) != canonical_json(capture) or timestamp(capture["known_at"]) > cutoff:
            raise ValueError("capture provenance is not canonical or available at cutoff")
        key = (capture["source_id"], capture["capture_id"])
        if key in captures:
            raise ValueError("capture provenance identifiers must be unique")
        captures[key] = capture
        capture_objects[key] = typed_capture
    if doc["provenance"] != sorted(captures.values(), key=lambda c: (c["source_id"], c["capture_id"])):
        raise ValueError("capture provenance must have stable ordering")
    if doc["max_input_known_at"] != max((c["known_at"] for c in captures.values()), default=None):
        raise ValueError("max_input_known_at must describe the actual input provenance")

    def refs(value, broker_only=False):
        if not isinstance(value, list):
            raise ValueError("input references must be a list")
        if value != sorted({canonical_json(r): r for r in value}.values(), key=canonical_json):
            raise ValueError("input references must be sorted and unique")
        for ref in value:
            base = {"source_id", "capture_id", "content_sha256", "source_market_session"}
            if not isinstance(ref, dict):
                raise ValueError("input reference must be an object")
            if "observation_revision" in ref:
                exact(ref, base | {"observation_revision"})
                _positive_int(ref["observation_revision"], "observation_revision")
                if ref["observation_revision"] > doc["observation_revision"]:
                    raise ValueError("input revision exceeds the evidence revision")
            else:
                exact(ref, base | {"measurement_contract"})
                if broker_only or ref["measurement_contract"] != MARKET_CONTRACT:
                    raise ValueError("input reference measurement contract differs from v1")
            market_date(ref["source_market_session"])
            capture = captures.get((ref["source_id"], ref["capture_id"]))
            if capture is None or capture["content_sha256"] != ref["content_sha256"]:
                raise ValueError("input reference lacks matching capture provenance")
            if "measurement_contract" in ref:
                params = capture["request_parameters"]
                source_market = MarketObservation(doc["ticker"], ref["source_market_session"],
                    capture_objects[(ref["source_id"], ref["capture_id"])], None, None, "shares", "",
                    params.get("market", ""))
                if (_market_request_issue(source_market) or ("market" in params and
                        params["market"] not in {s["market_scope"] for s in scopes})):
                    raise ValueError("market reference violates source request/session contract")

    base_fields = {"value", "status", "null_reason", "window", "expected_sessions", "observed_sessions", "input_refs", "unit"}

    def envelope(value, n, offset, unit, extras=()):
        exact(value, base_fields | set(extras))
        if value["status"] not in ("CALCULATED", "WITHHELD") or value["unit"] != unit:
            raise ValueError("measurement status or unit differs from the v1 contract")
        reason(value["null_reason"], nullable=value["status"] == "CALCULATED")
        if ((value["status"] == "WITHHELD" and value["value"] is not None) or
                (value["status"] == "CALCULATED" and (value["value"] is None or value["null_reason"] is not None))):
            raise ValueError("measurement value does not match its availability status")
        exact(value["window"], {"sessions", "offset_sessions", "start", "end", "session_dates"})
        end = max(0, len(axis.sessions) - offset)
        dates = list(axis.sessions[max(0, end - n):end])
        expected_window = {"sessions": n, "offset_sessions": offset,
                           "start": dates[0] if dates else None, "end": dates[-1] if dates else axis.cutoff,
                           "session_dates": dates}
        if (canonical_json(value["window"]) != canonical_json(expected_window) or
                type(value["expected_sessions"]) is not int or value["expected_sessions"] != n):
            raise ValueError("measurement window differs from the declared session axis")
        observed = value["observed_sessions"]
        if type(observed) is not int or not 0 <= observed <= len(dates):
            raise ValueError("invalid measurement observed-session count")
        if value["status"] == "CALCULATED" and (axis.status != "VERIFIED" or len(dates) != n or observed != n):
            raise ValueError("calculated measurements require a complete verified window")
        refs(value["input_refs"])

    def market_envelope(value, n, offset, kind):
        envelope(value, n, offset, "lots/session" if kind == "adv20" else "fraction", {"market_window"})
        fields = {"session_dates", "expected_sessions", "observed_sessions"}
        if kind == "return":
            fields |= {"baseline_close_rp_per_share", "end_close_rp_per_share"}
        exact(value["market_window"], fields)
        market_window = value["market_window"]
        end = max(0, len(axis.sessions) - offset)
        dates = list(axis.sessions[max(0, end - 20):end]) if kind == "adv20" else list(value["window"]["session_dates"])
        start = len(axis.sessions) - offset - n
        if kind == "return" and start > 0:
            dates.insert(0, axis.sessions[start - 1])
        needed = 20 if kind == "adv20" else n + 1
        if (market_window["session_dates"] != dates or type(market_window["expected_sessions"]) is not int or
                market_window["expected_sessions"] != needed or
                type(market_window["observed_sessions"]) is not int or
                not 0 <= market_window["observed_sessions"] <= len(dates)):
            raise ValueError("market window differs from verified market-session requirements")
        if value["status"] == "CALCULATED" and market_window["observed_sessions"] != needed:
            raise ValueError("calculated market measurement requires all market inputs")
        number(value["value"], nullable=True)
        if kind == "return":
            for key in ("baseline_close_rp_per_share", "end_close_rp_per_share"):
                number(market_window[key], nullable=value["status"] == "WITHHELD")
                if value["status"] == "WITHHELD" and market_window[key] is not None:
                    raise ValueError("withheld return must not expose calculated endpoint prices")

    metric_units = {"net_flow": "lots", "net_flow_slope": "lots/session", "persistence": "fraction",
                    "reversal": "count", "direction_efficiency": "fraction", "gross_buy_lots": "lots",
                    "gross_sell_lots": "lots", "two_sided_broker_activity_lots": "lots",
                    "implied_buy_price": "rupiah/share", "implied_sell_price": "rupiah/share",
                    "flow_vs_adv": "ADV20 multiples", "price_flow_divergence": "underlying_facts"}

    def metric_map(value, n, offset=0):
        exact(value, metric_units)
        for name, unit in metric_units.items():
            extras = ({"numerator", "denominator"} if name == "persistence" else
                      {"zero_handling"} if name == "reversal" else
                      {"shares_per_lot"} if name.startswith("implied_") else
                      {"adv20"} if name == "flow_vs_adv" else
                      {"facts"} if name == "price_flow_divergence" else set())
            measurement = value[name]
            envelope(measurement, n, offset, unit, extras)
            if name != "price_flow_divergence":
                number(measurement["value"], nullable=True)
            if name == "persistence":
                if measurement["denominator"] != n or type(measurement["denominator"]) is not int:
                    raise ValueError("persistence denominator must equal the declared window")
                numerator = measurement["numerator"]
                if not (measurement["status"] == "WITHHELD" and numerator is None) and (
                        type(numerator) is not int or not 0 <= numerator <= n):
                    raise ValueError("invalid persistence numerator")
            elif name == "reversal" and measurement["zero_handling"] != "SKIP_ZEROS_WITHIN_COMPLETE_WINDOW":
                raise ValueError("reversal zero handling differs from the v1 contract")
            elif name.startswith("implied_") and (type(measurement["shares_per_lot"]) is not int or
                                                   measurement["shares_per_lot"] != SHARES_PER_LOT):
                raise ValueError("implied-price lot/share contract differs from v1")
            elif name == "flow_vs_adv":
                market_envelope(measurement["adv20"], n, offset, "adv20")
            elif name == "price_flow_divergence":
                facts = measurement["facts"]
                exact(facts, {"price_return", "net_flow", "net_flow_slope", "coverage"})
                market_envelope(facts["price_return"], n, offset, "return")
                if (facts["net_flow"] != value["net_flow"] or facts["net_flow_slope"] != value["net_flow_slope"] or
                        facts["coverage"] != value["net_flow"]["status"]):
                    raise ValueError("price/flow facts differ from their declared measurements")
                if measurement["value"] is not None:
                    exact(measurement["value"], {"price_return", "net_flow_lots", "net_flow_slope_lots_per_session"})
                    for fact in measurement["value"].values():
                        number(fact)

    row_fields = set("canonical_session_date broker_code observation_revision evidence_revision coverage null_reason raw scope input_refs segment_id segment_start opening_position_lots left_censored break_reason continuity_status cumulative_observable_lots cumulative_observable_value segment_cumulative_net_lots segment_cumulative_net_value".split())
    observed_brokers = []
    for broker, value in doc["brokers"].items():
        exact(value, {"series", "measurements", "acceleration"})
        if not isinstance(value["series"], list):
            raise ValueError("broker series must be a list")
        dates = []
        for row in value["series"]:
            exact(row, row_fields)
            d = row["canonical_session_date"]
            market_date(d)
            dates.append(d)
            if (not axis.start <= d <= axis.cutoff or row["broker_code"] != broker or
                    type(row["evidence_revision"]) is not int or row["evidence_revision"] != doc["observation_revision"]):
                raise ValueError("row grain differs from the evidence document")
            if row["observation_revision"] is not None:
                _positive_int(row["observation_revision"], "observation_revision")
                if row["observation_revision"] > doc["observation_revision"]:
                    raise ValueError("row revision exceeds evidence revision")
            if row["coverage"] not in COVERAGE_STATES:
                raise ValueError("unknown row coverage state")
            reason(row["null_reason"], nullable=row["coverage"] in OBSERVED)
            reason(row["break_reason"], boundary=True)
            if row["opening_position_lots"] is not None or row["left_censored"] is not True:
                raise ValueError("opening position is unknown and every observed segment is left censored")
            if row["continuity_status"] not in ("CONTINUOUS_FROM_ANCHOR", "ANCHOR_BROKEN", "UNKNOWN_CALENDAR"):
                raise ValueError("unknown continuity status")
            if (row["continuity_status"] == "UNKNOWN_CALENDAR") != (axis.status == "UNSUPPORTED"):
                raise ValueError("continuity status differs from the declared calendar support")
            if row["raw"] is not None:
                exact(row["raw"], FLOW_FIELDS)
                for raw_value in row["raw"].values():
                    number(raw_value, nullable=True)
            if row["scope"] is not None:
                exact(row["scope"], Scope.__dataclass_fields__)
                Scope(**row["scope"])
                if row["scope"] not in scopes:
                    raise ValueError("row scope is outside the request compatibility contract")
            refs(row["input_refs"], broker_only=True)
            if row["coverage"] in OBSERVED:
                if row["null_reason"] is not None or row["raw"] is None or row["scope"] is None:
                    raise ValueError("observed rows require valid flow and scope evidence")
                for raw_value in row["raw"].values():
                    number(raw_value)
                if not row["input_refs"] or row["observation_revision"] is None:
                    raise ValueError("observed rows require source observation references")
                for ref in row["input_refs"]:
                    if ref["source_market_session"] != d or ref["observation_revision"] != row["observation_revision"]:
                        raise ValueError("observed row reference differs from its session/revision")
                    source_row = Observation(doc["ticker"], broker, d, row["observation_revision"],
                                             Scope(**row["scope"]),
                                             capture_objects[(ref["source_id"], ref["capture_id"])],
                                             **row["raw"], coverage=row["coverage"])
                    with localcontext(Context(prec=1000, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                                              capitals=1, clamp=0, flags=[],
                                              traps=[InvalidOperation, DivisionByZero, Overflow])):
                        state, issue = _coverage(source_row)
                    if state != row["coverage"] or issue is not None:
                        raise ValueError("observed row violates source request/session/flow contract")
                if (row["coverage"] == OBSERVED_ZERO) != all(v == 0 for v in row["raw"].values()):
                    raise ValueError("explicit-zero coverage must match all raw flow fields")
                if not isinstance(row["segment_id"], str) or not re.fullmatch(r"[0-9a-f]{24}", row["segment_id"]):
                    raise ValueError("observed row must identify its segment")
                market_date(row["segment_start"])
                if not axis.start <= row["segment_start"] <= d:
                    raise ValueError("segment start must precede its observed row")
                for field in ("segment_cumulative_net_lots", "segment_cumulative_net_value"):
                    number(row[field])
            else:
                if any(row[k] is not None for k in ("segment_id", "segment_start", "segment_cumulative_net_lots", "segment_cumulative_net_value")):
                    raise ValueError("unobserved or invalid rows cannot extend a segment")
                if row["coverage"] == UNOBSERVED and row["raw"] is not None:
                    raise ValueError("unobserved rows cannot contain synthesized raw flow")
            for field in ("cumulative_observable_lots", "cumulative_observable_value"):
                number(row[field], nullable=row["continuity_status"] != "CONTINUOUS_FROM_ANCHOR")
                if row["continuity_status"] != "CONTINUOUS_FROM_ANCHOR" and row[field] is not None:
                    raise ValueError("broken anchor continuity must withhold cumulative flow")
            if row["continuity_status"] == "CONTINUOUS_FROM_ANCHOR" and row["coverage"] not in OBSERVED:
                raise ValueError("continuous anchor requires observed coverage")
        if dates != sorted(set(dates)) or (axis.status == "VERIFIED" and dates != list(axis.sessions)):
            raise ValueError("broker series differs from the declared session axis")
        if any(row["coverage"] in OBSERVED for row in value["series"]):
            observed_brokers.append(broker)
        exact(value["measurements"], {str(n) for n in windows})
        for n in windows:
            metric_map(value["measurements"][str(n)], n)
        h = identity["acceleration_half_window"]
        acceleration = value["acceleration"]
        envelope(acceleration, 2 * h, 0, "lots/session", {"recent_sessions", "preceding_sessions"})
        if any(type(acceleration[k]) is not int or acceleration[k] != h
               for k in ("recent_sessions", "preceding_sessions")):
            raise ValueError("acceleration periods differ from the request contract")
        number(acceleration["value"], nullable=True)
    if doc["observed_brokers"] != sorted(observed_brokers):
        raise ValueError("observed brokers must derive from emitted valid requested series")

    rotation = doc["rotation_evidence"]
    exact(rotation, {"context", "broker_windows", "concurrent_positive_brokers",
                     "observed_subset_positive_broker_count", "concurrent_buying_means"})
    expected_context = {"ticker": doc["ticker"], "cutoff": axis.cutoff,
                        "observation_revision": doc["observation_revision"],
                        "coverage_scope": "OBSERVED_BROKER_SUBSET", "declared_windows": windows}
    if canonical_json(rotation["context"]) != canonical_json(expected_context):
        raise ValueError("rotation context differs from the bounded evidence contract")
    if rotation["concurrent_buying_means"] != "ABSORPTION_PROXY_ONLY_NO_COUNTERPARTY_LINK":
        raise ValueError("concurrent buying cannot assert a confirmed counterparty transfer")
    exact(rotation["broker_windows"], doc["requested_evidence_brokers"])
    n = windows[0]
    for broker, value in rotation["broker_windows"].items():
        exact(value, {"prior_window", "recent_window", "first_observed_activity", "left_censored"})
        if value["left_censored"] is not True:
            raise ValueError("rotation activity history is left censored")
        metric_map(value["prior_window"], n, offset=n)
        if value["recent_window"] != doc["brokers"][broker]["measurements"][str(n)]:
            raise ValueError("rotation recent window differs from declared broker measurements")
        first = next((r["canonical_session_date"] for r in doc["brokers"][broker]["series"]
                      if r["coverage"] == OBSERVED_NONZERO), None)
        if value["first_observed_activity"] != first:
            raise ValueError("first activity must derive from emitted observed nonzero rows")
    count = rotation["observed_subset_positive_broker_count"]
    positive = rotation["concurrent_positive_brokers"]
    envelope(count, n, 0, "broker-code count")
    envelope(positive, n, 0, "broker-code list")
    if {k: v for k, v in count.items() if k not in ("value", "unit")} != {
            k: v for k, v in positive.items() if k not in ("value", "unit")}:
        raise ValueError("concurrent broker list and count must share availability semantics")
    inputs = [doc["brokers"][b]["measurements"][str(n)]["net_flow"] for b in doc["requested_evidence_brokers"]]
    if any(m["status"] != "CALCULATED" for m in inputs):
        if count["status"] != "WITHHELD" or count["null_reason"] != "INCOMPLETE_OBSERVED_SUBSET":
            raise ValueError("incomplete broker subsets must withhold concurrent buying evidence")
    else:
        expected_positive = [b for b, m in zip(doc["requested_evidence_brokers"], inputs) if m["value"] > 0]
        if (count["status"] != "CALCULATED" or positive["value"] != expected_positive or
                type(count["value"]) is not int or count["value"] != len(expected_positive)):
            raise ValueError("concurrent buying evidence differs from the observed subset")
    canonical_json(doc)
