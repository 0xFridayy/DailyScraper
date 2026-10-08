"""Dataframe/registry-file adapter; pure financial rules stay in price_contract."""

from pathlib import Path
from hashlib import sha256
import math
import json
import re
from datetime import datetime

import pandas as pd
import numpy as np

from price_contract import (Anchor, PreviousActual, RAW_ACTUAL, parse_registry,
                            resolve_limit_reference, return_span_status,
                            validate_actual_price, UnsupportedPriceContract, actual_bar_reason,
                            positive_real, PriceContractError, actual_predecessor_trusted)

SOURCE_COLUMNS = ("ticker", "date", "open", "high", "low", "close", "volume")
CERTIFICATE_COLUMN = "price_contract_row_sha256"
SOURCE_IDENTITY_COLUMNS = ("source", "source_identity", "source_document_id", "source_session",
                           "source_session_status", "representation", "input_representation")
ADMISSION_COLUMNS = (
    "previous_actual_close", "previous_actual_session", "limit_reference_price", "limit_reference_kind",
    "limit_reference_source", "limit_reference_status", "limit_unresolved_reason", "limit_change",
    "price_admissibility_status", "limit_admission_status", "price_step_admissible",
    "close_anchor_admissible", "entry_open_admissible", "corporate_action_boundary",
    "corporate_action_event_id", "corporate_action_status", "domain_violation", "price_segment_id",
    "input_price_trusted",
)


def label_column(column):
    return re.fullmatch(r"(?:fwd(?:_oo|_oc)?|lag|max|mdd|gap)_\d+(?:_reason)?", column) is not None


def _scalar(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) or hasattr(value, "item"):
        value = value.item() if hasattr(value, "item") else value
        if isinstance(value, bool):
            return value
        try:
            number = float(value)
        except OverflowError:
            return "NUMBER:" + str(value)
        return number if math.isfinite(number) else "NUMBER:" + str(number)
    return str(value)


def _row_certificates(px, identity):
    columns = identity["certificate_columns"]
    context = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return [sha256((context + json.dumps([_scalar(v) for v in values],
                    separators=(",", ":"), allow_nan=False)).encode()).hexdigest()
            for values in px[columns].itertuples(index=False, name=None)]


def _seal_price_frame(px):
    """Producer-only seal after adjudication/label computation, never annotation of legacy labels."""
    identity = dict(px.attrs["price_contract"])
    owned = set(SOURCE_COLUMNS + SOURCE_IDENTITY_COLUMNS + ADMISSION_COLUMNS)
    owned.update(identity.get("label_columns", []))
    owned.update(identity.get("producer_columns", []))
    identity["certificate_columns"] = [c for c in px if c in owned]
    px.attrs["price_contract"] = identity
    px[CERTIFICATE_COLUMN] = _row_certificates(px, identity)
    return px


def frame_as_of(px, as_of=None):
    identity = px.attrs.get("price_contract", {})
    if as_of is None and identity.get("knowledge_mode") == "AS_OF":
        return datetime.fromisoformat(identity["as_of"])
    return as_of


REGISTRY_PATH = Path(__file__).with_name("corporate_actions.json")


def default_registry():
    # Re-read each run. No cache can survive a revoked/edited registry revision.
    return parse_registry(REGISTRY_PATH.read_bytes())


def independent_price_defects(px, registry, market="REGULAR"):
    """Current source defects, shared before any predecessor/quarantine chaining."""
    fields = ["open", "high", "low", "close", "volume"]
    cross = px.duplicated(["date"] + fields, keep=False) & px[fields].notna().all(axis=1)
    identity = px.duplicated(["ticker", "date"], keep=False)
    segments = pd.Series([sum(e.session <= d for e in registry.matching(t, market))
                          for t, d in zip(px.ticker, px.date)], index=px.index)
    numeric = pd.to_numeric(px.close, errors="coerce")
    numeric = numeric.where(numeric.gt(0) & np.isfinite(numeric))
    median = numeric.groupby([px.ticker, segments]).transform(
        lambda s: s.rolling(21, center=True, min_periods=5).median())
    ratio = numeric / median
    series = ((ratio > 5) | (ratio < 0.2)).fillna(False)
    return pd.DataFrame({"cross_ticker_dup": cross, "duplicate_identity": identity,
                         "series_break": series}, index=px.index)


def annotate_prices(px, *, registry=None, representation=None, market="REGULAR",
                    trusted=None, as_of=None):
    registry = registry or default_registry()
    representation = representation or px.attrs.get("price_contract", {}).get("input_representation", "UNKNOWN")
    as_of = frame_as_of(px, as_of)
    existing = px.attrs.get("price_contract")
    labels = [c for c in px if c.startswith(("fwd_", "lag_", "max_", "mdd_", "gap_"))]
    missing = set(SOURCE_COLUMNS) - set(px.columns)
    if missing:
        raise UnsupportedPriceContract(f"Incomplete OHLCV source: {sorted(missing)}")
    trust = list(trusted) if trusted is not None else [True] * len(px)
    if len(trust) != len(px) or not all(isinstance(v, (bool, np.bool_)) for v in trust):
        raise UnsupportedPriceContract("Price trust must be an aligned Boolean mask")
    if existing or labels:
        identity = require_price_frame(px, registry=registry)
        if (identity.get("input_representation") != representation or identity.get("market") != market
                or identity.get("as_of") != (as_of.isoformat() if as_of else None)):
            raise UnsupportedPriceContract("Changed price/label context; rebuild from source observations")
        if trusted is None or list(px["input_price_trusted"]) == trust:
            return px.copy()  # Retain the original verified adjudication, including filtered baselines.
        if labels:
            raise UnsupportedPriceContract("Changed trust; rebuild labels from source observations")
    px = px.copy()
    source_digest = sha256(px[[c for c in ("ticker", "date", "open", "high", "low", "close", "volume")
                              if c in px]].to_csv(index=False).encode()).hexdigest()
    previous = {}
    records = []
    defects = independent_price_defects(px, registry, market)
    trust = [bool(t) and not bad for t, bad in zip(trust, defects.any(axis=1))]
    for i, row in enumerate(px.to_dict("records")):
        ticker, session = row["ticker"], row["date"]
        prev = previous.get(ticker)
        events = registry.matching(ticker, market, session)
        source_known = True
        for field in ("source", "source_identity", "source_document_id"):
            if field in row:
                v = row[field]
                source_known &= (isinstance(v, str) and v == v.strip()
                                 and v.upper() not in {"", "UNKNOWN", "UNVERIFIED", "NONE", "NULL"})
        if "source_session" in row:
            source_known &= row["source_session"] == session
        if "source_session_status" in row:
            source_known &= row["source_session_status"] == "VERIFIED"
        for field in ("representation", "input_representation"):
            if field in row:
                source_known &= row[field] == representation
        trust[i] &= bool(source_known)
        # Unknown legacy basis still permits narrower ordinary-band diagnostics.
        # They never certify representation or produce economic targets.
        diagnostic_rep = RAW_ACTUAL if representation == "UNKNOWN" and not events else representation
        ref = resolve_limit_reference(ticker, session, market, prev, registry,
                                      input_representation=diagnostic_rep if source_known else "UNKNOWN", as_of=as_of)
        close = validate_actual_price(row.get("close"), ref)
        opened = validate_actual_price(row.get("open"), ref)
        domain = actual_bar_reason(row) is None
        high_admission = validate_actual_price(row.get("high"), ref)
        low_admission = validate_actual_price(row.get("low"), ref)
        complete_in_band = all(a.status == "IN_BAND" for a in (close, opened, high_admission, low_admission))
        bar_status = ("OUT_OF_BAND" if any(a.status == "OUT_OF_BAND" for a in
                      (close, opened, high_admission, low_admission)) else close.status)
        event = events[0] if events else None
        volume = row.get("volume")
        traded = (not isinstance(volume, bool) and isinstance(volume, (int, float))
                  and math.isfinite(float(volume)) and volume > 0)
        try:
            low, opened_price, high = (positive_real(row.get(k)) for k in ("low", "open", "high"))
            open_domain = low <= opened_price <= high
        except PriceContractError:
            open_domain = False
        records.append({
            "previous_actual_close": prev.price if prev else None,
            "previous_actual_session": prev.session if prev else None,
            "limit_reference_price": ref.price,
            "limit_reference_kind": ref.kind,
            "limit_reference_source": ref.source,
            "limit_reference_status": ref.status,
            "limit_unresolved_reason": ref.reason,
            "limit_change": close.limit_change,
            "price_admissibility_status": ((bar_status if representation == RAW_ACTUAL else "UNRESOLVED_REPRESENTATION")
                                           if traded else "UNVERIFIED_TRADING_SESSION") if domain else "INVALID_DOMAIN",
            "limit_admission_status": bar_status,
            "price_step_admissible": complete_in_band and domain and traded and bool(trust[i]),
            "close_anchor_admissible": bool(trust[i]) and actual_predecessor_trusted(
                close, domain_valid=domain and bar_status != "OUT_OF_BAND", traded=traded, event=bool(events)),
            "entry_open_admissible": (opened.status == "IN_BAND" and traded and open_domain
                                      and high_admission.status == "IN_BAND" and low_admission.status == "IN_BAND"
                                      and bool(trust[i])),
            "corporate_action_boundary": bool(event),
            "corporate_action_event_id": event.event_id if event else None,
            "corporate_action_status": event.status if event else None,
            "domain_violation": not domain,
            "input_price_trusted": bool(trust[i]),
        })
        previous[ticker] = PreviousActual(session, row.get("close"), "input-price-snapshot:" + source_digest,
                                          bool(trust[i]) and actual_predecessor_trusted(close, domain_valid=domain and bar_status != "OUT_OF_BAND",
                                                                                     traded=traded, event=bool(events)),
                                          diagnostic_rep)
    for column in (records[0] if records else ()):
        px[column] = [r[column] for r in records]
    if not records:
        for column in ("corporate_action_boundary", "domain_violation", "price_step_admissible", "entry_open_admissible", "close_anchor_admissible", "input_price_trusted"):
            px[column] = False
        for column in ("previous_actual_close", "previous_actual_session", "limit_reference_price",
                       "limit_reference_kind", "limit_reference_source", "limit_reference_status",
                       "limit_unresolved_reason", "limit_change", "price_admissibility_status",
                       "limit_admission_status", "corporate_action_event_id", "corporate_action_status"):
            px[column] = None
    px["price_segment_id"] = [sum(e.session <= d for e in registry.matching(t, market))
                              for t, d in zip(px.ticker, px.date)]
    px.attrs["price_contract"] = registry.identity | {
        "input_representation": representation, "market": market,
        "knowledge_mode": "RETROSPECTIVE" if as_of is None else "AS_OF",
        "as_of": as_of.isoformat() if as_of else None,
        "source_basis_snapshot_sha256": source_digest,
    }
    return _seal_price_frame(px)


def span_result(ticker, start, end, *, registry, representation, market, session_axis,
                start_phase="CLOSE", end_phase="CLOSE", as_of=None):
    if start is None or end is None:
        from price_contract import SpanResult
        return SpanResult("WITHHELD", "MISSING_ANCHOR")
    return return_span_status(ticker, Anchor(start, start_phase), Anchor(end, end_phase),
                              market, registry, session_axis, representation, as_of=as_of)


def require_price_frame(px, required=(), registry=None):
    identity = px.attrs.get("price_contract")
    registry = registry or default_registry()
    if not identity or any(identity.get(k) != v for k, v in registry.identity.items()):
        raise UnsupportedPriceContract("Missing/stale corporate-action frame identity")
    columns = identity.get("certificate_columns")
    semantic = {c for c in px if label_column(c) or c in SOURCE_IDENTITY_COLUMNS}
    if (not isinstance(columns, list) or not set(SOURCE_COLUMNS) <= set(columns)
            or CERTIFICATE_COLUMN not in px or not set(columns) <= set(px.columns)
            or not (set(required) | semantic) <= set(columns)):
        raise UnsupportedPriceContract("Missing value-bound price/label certificate")
    if list(px[CERTIFICATE_COLUMN]) != _row_certificates(px, identity):
        raise UnsupportedPriceContract("Prices, labels or contract context changed after certification")
    if px.duplicated(["ticker", "date"]).any():
        raise UnsupportedPriceContract("Duplicate price observation identity")
    missing = set(required) - set(px.columns)
    if missing:
        raise UnsupportedPriceContract(f"Missing guarded price columns: {sorted(missing)}")
    return identity
