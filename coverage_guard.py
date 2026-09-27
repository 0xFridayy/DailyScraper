"""What broker coverage a payload has, and who may read it.

/api/inventory silently keeps the first 10 raw `brokers` values of a request
(verified live 2026-09-27). A request for more comes back as a quiet 10, and a
metric that sums, counts or ranks ACROSS brokers then reads every dropped
broker as zero: adv20 becomes the turnover of 10 brokers, the top holder the
top of 10. The targeted broker actor panel (targeted_actor_panel) has the same
shape of problem by design: its payloads hold the UNION of 16 TOP_5 selectors,
typically 10-25 of the 101 codes, and a broker outside the union is
UNOBSERVED, not zero.

So coverage is decided on positive evidence, never on the absence of a mark:

  check_explicit_codes(codes, known)
      the pre-network guard of an explicit-code request: at most
      MAX_BROKERS_PER_REQUEST codes, each a known code, none repeated. More
      than that is refused BEFORE anything is sent, never truncated and never
      split into batches here.
  explicit_coverage_reason(data, requested)
      what an explicit request's response must show: the six broker maps each
      hold exactly the requested codes, with a series for every one. A
      requested code missing from the answer is a refusal, not a zero.
  full_universe_reason(data)
      what a full-universe consumer requires: every code of the broker
      universe (broker_codes.json) present in all six maps with a series.
      That is the only evidence of full coverage a payload can carry. A
      payload that holds fewer, whether a selector union, a 10-of-101
      truncated cache or a partial legacy harvest, does not have it, whether
      or not it carries a mark. Wired into broker_book.frames_from_payload,
      the entry of every broker_book / broker_rules / broker_learning metric.
  refuse_targeted(obj, consumer)
      names targeted-selector data specifically (a declared coverage_scope, or
      an envelope whose meta.brokers echoes selector tokens) so it is refused
      with a clear reason even before its coverage is counted. Wired into
      broker_collect._data_of and broker_book.frames_from_payload.

FULL_UNIVERSE_METRICS lists what needs every broker, where, and what guards it.
One legacy path is deliberately NOT guarded here. harvest_inventory feeds the
weekly ARB veto (arb-veto.yml: harvest -> build_inventory_db -> inventory_features
-> arb_veto), still sends all 101 codes in one request, and caches what comes
back: since 2026-09, 10 of the 101. Failing it would stop the ARB veto, whose
model reads price features only. So instead it checks every response with
explicit_coverage_reason and records "coverage NOT verified" in its capture
manifest; it never claims full coverage. build_inventory_db.py and
normalize_market_data.py cannot carry a check of their own: both are Experiment
#1F DATA PLANE code whose bytes are pinned (experiment_1f_manifest.CODE_IDENTITY,
experiment_1f_gate_b.HELPER_FILES). They and inventory_features still read
inventory_raw/ as before, and gating them is a separate task. The targeted
collector writes no vendor-shaped file there, or anywhere.

Standard library only: broker_collect imports this at module scope.
"""

import json
import os
import re
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_FILE = os.path.join(HERE, "broker_codes.json")

COVERAGE_KEY = "coverage_scope"
TARGETED_SELECTOR_UNION = "TARGETED_SELECTOR_UNION"
MAX_BROKERS_PER_REQUEST = 10     # the server's cap on raw `brokers` values
SERIES_FIELDS = ("blot", "bval", "slot", "sval", "nlot", "nval")

_BROKER_CODE = re.compile(r"[A-Z]{2}")

# (consumer, what it computes across brokers, how incomplete coverage is kept out)
FULL_UNIVERSE_METRICS = (
    ("broker_book.rolling_state", "broker-summed adv20 / val20 (tot_blot, tot_bval), "
     "BUYDAYS60, NL5/NL60 ranks", "full_universe_reason in frames_from_payload"),
    ("broker_book.basis_flags", "market VWAP = sum bval / sum blot over brokers",
     "full_universe_reason in frames_from_payload"),
    ("broker_book.cumulative_curves / average_cost_book", "top buyers/sellers and "
     "position ranks across brokers", "full_universe_reason in frames_from_payload"),
    ("broker_rules.eligibility / evaluate / track_record_events", "val20 eligibility, "
     "R1-R6 holder groups and argmaxes, per-session top/bottom 3 by NL5",
     "full_universe_reason in frames_from_payload (their input is ticker_bundle's)"),
    ("broker_learning.rule_stats / broker_scores / broker_profitability / broker_lift / "
     "alpha_cases", "the weekly learning, profitability and lift over every eligible "
     "ticker and broker", "every bundle passes frames_from_payload; refuse_targeted in "
     "broker_collect._data_of; broker_learning_db refuses a targeted database"),
    ("broker_learning_run.write_history", "broker_history parquet, where a missing "
     "broker row is read as zero", "built from ticker_bundle frames"),
    ("inventory_features.build", "top/bottom accumulators, HHI, n_acc/n_dist, "
     "n_active_brk, conc_today, GROUPS totals, market VWAP",
     "NOT GUARDED (separate task): reads build_inventory_db's parquets of "
     "inventory_raw/, which the legacy ARB-veto harvest fills with unverified "
     "coverage recorded as such in its manifest; the targeted collector never writes there"),
    ("build_inventory_db.main / normalize_market_data", "broker_daily.parquet; "
     "daily broker totals, wrap and basis detection", "NOT GUARDED (separate task): "
     "Experiment #1F pinned DATA PLANE code, globs inventory_raw/ (as above)"),
    ("walk_forward_backtest._broker_day_aggregates and its callers", "broker "
     "concentration, n_brokers, net_flow_total, retail presence over neobdm.db "
     "broker_flow", "isolation: targeted_actor_db refuses neobdm.db"),
)


class TargetedCoverageError(ValueError):
    """Targeted-selector data offered to a consumer that needs every broker."""


class CoverageError(ValueError):
    """A payload without the broker coverage its consumer needs."""


class ExplicitRequestError(ValueError):
    """An explicit-code request that must not be sent (module docstring)."""


@lru_cache(maxsize=1)
def universe_codes():
    """The broker universe, broker_codes.json, as a frozenset. Checked: a
    malformed or duplicated list cannot define what full coverage means."""
    with open(UNIVERSE_FILE, encoding="utf-8") as fh:
        codes = json.load(fh)
    if (not isinstance(codes, list) or not codes
            or not all(isinstance(c, str) and _BROKER_CODE.fullmatch(c) for c in codes)
            or len(set(codes)) != len(codes)):
        raise ValueError("broker_codes.json is not a list of unique two-letter codes")
    return frozenset(codes)


def check_explicit_codes(codes, known=None):
    """`codes` as a tuple, or ExplicitRequestError before anything is sent:
    at least one, at most MAX_BROKERS_PER_REQUEST, each a two-letter code of
    `known` (the universe by default), none repeated."""
    codes = tuple(codes)
    known = universe_codes() if known is None else frozenset(known)
    if not codes:
        raise ExplicitRequestError("no broker codes")
    if len(codes) > MAX_BROKERS_PER_REQUEST:
        raise ExplicitRequestError(
            f"{len(codes)} explicit broker codes in one request: the server keeps only the "
            f"first {MAX_BROKERS_PER_REQUEST} and drops the rest silently, so the request "
            "is refused before it is sent, never truncated")
    bad = [c for c in codes if not isinstance(c, str) or not _BROKER_CODE.fullmatch(c)
           or c not in known]
    if bad:
        raise ExplicitRequestError(f"not known broker codes: {bad[:5]}")
    if len(set(codes)) != len(codes):
        raise ExplicitRequestError("repeated broker codes")
    return codes


def _returned(data):
    """{field: set of keys with a non-null series}, or a reason string."""
    if not isinstance(data, dict):
        return f"payload is {type(data).__name__}, not a mapping"
    out = {}
    for f in SERIES_FIELDS:
        m = data.get(f)
        if not isinstance(m, dict):
            return f"{f} is {type(m).__name__}, not a broker map"
        out[f] = {b for b, s in m.items() if s is not None}
    return out


def explicit_coverage_reason(data, requested):
    """None when every broker map holds exactly the `requested` codes, each
    with a series; else why not. Missing is never read as zero."""
    returned = _returned(data)
    if isinstance(returned, str):
        return returned
    want = set(requested)
    for f in SERIES_FIELDS:
        missing, extra = sorted(want - returned[f]), sorted(set(data[f]) - want)
        if missing or extra:
            return (f"{f}: returned brokers differ from the {len(want)} requested "
                    f"(missing {missing[:5]}, not requested {extra[:5]})")
    return None


def full_universe_reason(data, universe=None):
    """None when every code of the universe is present in all six maps with a
    series (the one positive evidence of full coverage); else why not."""
    returned = _returned(data)
    if isinstance(returned, str):
        return returned
    universe = universe_codes() if universe is None else frozenset(universe)
    for f in SERIES_FIELDS:
        missing = universe - returned[f]
        if missing:
            return (f"{f} holds {len(universe) - len(missing)} of the {len(universe)} "
                    f"universe brokers (missing {sorted(missing)[:5]}): not full coverage, "
                    "and a missing broker is not a zero")
    return None


def targeted_reason(obj):
    """Why `obj` is known to be targeted-selector data, or None.

    `obj` is a vendor envelope ({"success", "meta", "data"}), a bare data dict,
    or any dict that declares COVERAGE_KEY. None is NOT evidence of full
    coverage (full_universe_reason decides that)."""
    if not isinstance(obj, dict):
        return None
    layers = [("", obj)]
    if isinstance(obj.get("data"), dict):
        layers.append(("data.", obj["data"]))
    for where, layer in layers:
        if COVERAGE_KEY in layer:
            return f"{where}{COVERAGE_KEY} is {layer[COVERAGE_KEY]!r}"
    meta = obj.get("meta")
    echo = meta.get("brokers") if isinstance(meta, dict) else None
    if isinstance(echo, list):
        tokens = [b for b in echo if not (isinstance(b, str) and _BROKER_CODE.fullmatch(b))]
        if tokens:
            return (f"meta.brokers echoes selector tokens {tokens[:3]}: the payload is the "
                    "union of those selectors, not every broker")
    return None


def refuse_targeted(obj, consumer):
    """`obj` unchanged, or TargetedCoverageError naming `consumer` and why."""
    reason = targeted_reason(obj)
    if reason is not None:
        raise TargetedCoverageError(
            f"{consumer} needs every broker (absent = zero); refused targeted data: {reason}")
    return obj
