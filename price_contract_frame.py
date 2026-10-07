"""Dataframe/registry-file adapter; pure financial rules stay in price_contract."""

from pathlib import Path
from hashlib import sha256
import math

from price_contract import (Anchor, PreviousActual, RAW_ACTUAL, parse_registry,
                            resolve_limit_reference, return_span_status,
                            validate_actual_price, UnsupportedPriceContract, actual_bar_reason,
                            positive_real, PriceContractError)

REGISTRY_PATH = Path(__file__).with_name("corporate_actions.json")


def default_registry():
    # Re-read each run. No cache can survive a revoked/edited registry revision.
    return parse_registry(REGISTRY_PATH.read_bytes())


def annotate_prices(px, *, registry=None, representation=None, market="REGULAR",
                    trusted=None, as_of=None):
    registry = registry or default_registry()
    representation = representation or px.attrs.get("price_contract", {}).get("input_representation", "UNKNOWN")
    px = px.copy()
    source_digest = sha256(px[[c for c in ("ticker", "date", "open", "high", "low", "close", "volume")
                              if c in px]].to_csv(index=False).encode()).hexdigest()
    previous = {}
    records = []
    trust = list(trusted) if trusted is not None else [True] * len(px)
    for i, row in enumerate(px.to_dict("records")):
        ticker, session = row["ticker"], row["date"]
        prev = previous.get(ticker)
        events = registry.matching(ticker, market, session)
        # Unknown legacy basis still permits narrower ordinary-band diagnostics.
        # They never certify representation or produce economic targets.
        diagnostic_rep = RAW_ACTUAL if representation == "UNKNOWN" else representation
        ref = resolve_limit_reference(ticker, session, market, prev, registry,
                                      input_representation=diagnostic_rep, as_of=as_of)
        close = validate_actual_price(row.get("close"), ref)
        opened = validate_actual_price(row.get("open"), ref)
        domain = True
        if all(k in row for k in ("open", "high", "low")):
            domain = actual_bar_reason(row) is None
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
            "price_admissibility_status": ((close.status if representation == RAW_ACTUAL else "UNRESOLVED_REPRESENTATION")
                                           if traded else "UNVERIFIED_TRADING_SESSION") if domain else "INVALID_DOMAIN",
            "limit_admission_status": close.status,
            "price_step_admissible": close.status == "IN_BAND" and domain and traded,
            "entry_open_admissible": opened.status == "IN_BAND" and traded and open_domain,
            "corporate_action_boundary": bool(event),
            "corporate_action_event_id": event.event_id if event else None,
            "corporate_action_status": event.status if event else None,
            "domain_violation": not domain,
        })
        previous[ticker] = PreviousActual(session, row.get("close"), "input-price-snapshot:" + source_digest,
                                          bool(trust[i]) and domain and traded and close.status != "OUT_OF_BAND",
                                          diagnostic_rep)
    for column in (records[0] if records else ()):
        px[column] = [r[column] for r in records]
    if not records:
        for column in ("corporate_action_boundary", "domain_violation", "price_step_admissible", "entry_open_admissible"):
            px[column] = False
        for column in ("previous_actual_close", "previous_actual_session", "limit_reference_price",
                       "limit_reference_kind", "limit_reference_source", "limit_reference_status",
                       "limit_unresolved_reason", "limit_change", "price_admissibility_status",
                       "limit_admission_status", "corporate_action_event_id", "corporate_action_status"):
            px[column] = None
    px["price_segment_id"] = px["corporate_action_boundary"].groupby(px["ticker"]).cumsum()
    px.attrs["price_contract"] = registry.identity | {
        "input_representation": representation, "market": market,
        "knowledge_mode": "RETROSPECTIVE" if as_of is None else "AS_OF",
        "as_of": as_of.isoformat() if as_of else None,
        "source_basis_snapshot_sha256": source_digest,
    }
    return px


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
    missing = set(required) - set(px.columns)
    if missing:
        raise UnsupportedPriceContract(f"Missing guarded price columns: {sorted(missing)}")
    return identity
