"""Dataframe/registry-file adapter; pure financial rules stay in price_contract."""

from pathlib import Path
from hashlib import sha256
import math
import json
import re
from datetime import datetime

import pandas as pd
import numpy as np

from price_contract import (Anchor, RAW_ACTUAL, parse_registry, return_span_status,
                            UnsupportedPriceContract, adjudicate_series, canonical_session,
                            is_idx_session, IdxCalendarUnavailable, PriceContractError,
                            SERIES_BREAK_WINDOW, SERIES_CONTEXT_ROWS)

SOURCE_COLUMNS = ("ticker", "date", "open", "high", "low", "close", "volume")
CERTIFICATE_COLUMN = "price_contract_row_sha256"
SOURCE_IDENTITY_COLUMNS = ("source", "source_identity", "source_document_id", "source_session",
                           "source_session_status", "representation", "input_representation")
ADMISSION_COLUMNS = (
    "previous_actual_close", "previous_actual_session", "limit_reference_price", "limit_reference_kind",
    "limit_reference_source", "limit_reference_status", "limit_unresolved_reason", "limit_change",
    "price_admissibility_status", "limit_admission_status", "consistency_reference_price",
    "consistency_status", "consistency_violation", "price_step_admissible", "close_anchor_admissible",
    "entry_open_admissible",
    "corporate_action_boundary", "corporate_action_event_id", "corporate_action_status",
    "domain_violation", "price_segment_id", "input_price_trusted", "anchor_trust_status",
    "anchor_trust_reason", "restart_window_sessions",
)
# Placeholder-shaped identities are absence of evidence, never a source claim.
PLACEHOLDER_TOKENS = frozenset({"", "UNKNOWN", "UNVERIFIED", "NONE", "NULL", "N/A", "NA", "TBD",
                                "TODO", "PLACEHOLDER", "-", "--", "?", "MISSING", "PENDING"})


def placeholder_text(value):
    return (not isinstance(value, str) or value != value.strip()
            or value.strip().upper() in PLACEHOLDER_TOKENS
            or re.fullmatch(r"[-_.?/\s0]*", value) is not None)


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


def calendar_supported(session):
    """Inside the pinned calendar's coverage, whether or not it is a session."""
    try:
        is_idx_session(canonical_session(session))
        return True
    except (IdxCalendarUnavailable, PriceContractError, ValueError, TypeError):
        return False


def independent_price_defects(px, registry, market="REGULAR"):
    """Current source defects, shared before any predecessor/quarantine chaining.

    Series-break medians run in date order within one ticker, one registry
    segment and one side of the calendar-coverage boundary, so unsupported
    history never shapes the evidence for a verified session. A row's backward
    median context is complete once SERIES_CONTEXT_ROWS earlier rows of its
    group are present; only such rows can enter a restart window.
    """
    fields = ["open", "high", "low", "close", "volume"]
    cross = px.duplicated(["date"] + fields, keep=False) & px[fields].notna().all(axis=1)
    identity = px.duplicated(["ticker", "date"], keep=False)
    segments = pd.Series([sum(e.session <= d for e in registry.matching(t, market))
                          for t, d in zip(px.ticker, px.date)], index=px.index)
    supported = pd.Series([calendar_supported(d) for d in px.date], index=px.index, dtype=bool)
    numeric = pd.to_numeric(px.close, errors="coerce")
    numeric = numeric.where(numeric.gt(0) & np.isfinite(numeric))
    work = pd.DataFrame({"ticker": px.ticker.values, "date": px.date.astype(str).values,
                         "segment": segments.values, "supported": supported.values,
                         "close": numeric.values}, index=px.index)
    work = work.sort_values(["ticker", "segment", "supported", "date"], kind="mergesort")
    groups = work.groupby(["ticker", "segment", "supported"], sort=False)
    median = groups["close"].transform(
        lambda s: s.rolling(SERIES_BREAK_WINDOW, center=True, min_periods=5).median())
    context = groups.cumcount().ge(SERIES_CONTEXT_ROWS)
    ratio = work["close"] / median
    series = ((ratio > 5) | (ratio < 0.2)).fillna(False)
    return pd.DataFrame({"cross_ticker_dup": cross, "duplicate_identity": identity,
                         "series_break": series.reindex(px.index).astype(bool),
                         "series_context_complete": context.reindex(px.index).astype(bool)},
                        index=px.index)


def source_context_known(row, representation):
    """Optional per-row source metadata must be meaningful and must agree.

    Absent columns claim nothing. A present column holding a placeholder, a
    different session, an unverified session status or another representation
    withdraws the observation; matching prices never substitute for it.
    """
    for field in ("source", "source_identity", "source_document_id"):
        if field in row and placeholder_text(row[field]):
            return False
    if "source_session" in row and row["source_session"] != row["date"]:
        return False
    if "source_session_status" in row and row["source_session_status"] != "VERIFIED":
        return False
    return all(row[field] == representation for field in ("representation", "input_representation")
               if field in row)


def external_reasons(defects, trust):
    """Why an observation itself is unusable, before any predecessor logic."""
    reasons = []
    for dup, identity, series, trusted in zip(defects.cross_ticker_dup, defects.duplicate_identity,
                                              defects.series_break, trust):
        reasons.append("DUPLICATE_IDENTITY" if identity else "CROSS_TICKER_DUPLICATE" if dup
                       else "SERIES_BREAK" if series else None if trusted else "EXTERNALLY_UNTRUSTED")
    return reasons


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
    defects = independent_price_defects(px, registry, market)
    external = external_reasons(defects, trust)
    rows = px.to_dict("records")
    for row, reason, complete in zip(rows, external, defects.series_context_complete):
        row["external_reason"] = reason
        row["source_known"] = source_context_known(row, representation)
        row["context_complete"] = bool(complete)
    records = [None] * len(rows)
    by_ticker = {}
    for i, row in enumerate(rows):
        by_ticker.setdefault(row["ticker"], []).append(i)
    for ticker, positions in by_ticker.items():
        adjudicated = adjudicate_series(ticker, [rows[i] for i in positions], registry, market=market,
                                        representation=representation, as_of=as_of,
                                        source="input-price-snapshot:" + source_digest)
        for i, record in zip(positions, adjudicated):
            records[i] = record
    for column in ADMISSION_COLUMNS:
        if column != "price_segment_id":
            px[column] = [r[column] for r in records] if records else None
    for column in ("corporate_action_boundary", "domain_violation", "price_step_admissible",
                   "entry_open_admissible", "close_anchor_admissible", "input_price_trusted",
                   "consistency_violation"):
        px[column] = px[column].astype(bool) if records else False
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
