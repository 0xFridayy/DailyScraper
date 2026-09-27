"""The v1 request plan of the targeted broker actor panel, and its pre-network guard.

NeoBDM's /api/inventory takes broker SELECTORS as well as broker codes in its
repeated `brokers` param. The grammar the vendor lists (GET
/api/brokers/inventory, verified live 2026-09-27 against a 101-broker ground
truth) is

    TOP_{n}_{tx}_{unit}_{period}
        n       3, 5, 8
        tx      NB (net buy), NS (net sell), BUY, SELL (gross)
        unit    LOT, VAL            (VAL, not VALUE: VALUE is a ValidationError)
        period  C1, C3, C5, C10, C20, C50 (the last k sessions), ALL (the window)

168 selectors in all. C60 and TOP_10 are accepted too, but the vendor does not
list them, so nothing here will send them.

What a selector returns (the same verification):
  - the top n brokers by the SUM over the horizon of nlot (LOT) or nval (VAL):
    NB the largest positive sums, NS the most negative. Ck is the last k
    sessions of the returned axis, anchored at its last session (the last one
    on or before end_date); ALL is the whole requested window;
  - each chosen broker's FULL daily series over the requested window,
    identical to what an explicit request for its code returns;
  - with several selectors, the exact UNION of their brokers, keyed by broker
    code and alphabetical: the response says nothing about which selector
    chose a broker, or at what rank. Ranks and per-selector membership have to
    be derived locally (targeted_actor_panel) and are labelled DERIVED;
  - meta.brokers echoes the TOKENS sent (sorted), not the brokers they expanded
    to.

The cap. The server keeps the first 10 raw `brokers` values of a request and
drops the rest without an error (inventory-api truncation, verified
2026-09-27). The cap is on INPUT tokens, not on expanded brokers: eight
selectors can come back with 13 brokers or more, and that is complete. So this
module refuses a request of more than MAX_INPUT_TOKENS values before anything
is sent. It never truncates: a request that would be cut must not go out.

THE V1 PLAN
-----------
Four horizons, four selectors each (NB/NS x LOT/VAL, n = 5):

    C5   TOP_5_NB_LOT_C5   TOP_5_NS_LOT_C5   TOP_5_NB_VAL_C5   TOP_5_NS_VAL_C5
    C20  ... _C20          C50  ... _C50      ALL  ... _ALL

16 tokens, packed into exactly two requests per ticker, grouped by horizon:

    A  C5 + C20    8 tokens
    B  C50 + ALL   8 tokens

The order inside a request is fixed (horizon, then NB_LOT, NS_LOT, NB_VAL,
NS_VAL) so the plan, and every query built from it, is deterministic. Nothing
downstream relies on that order: the vendor's echo is compared as a sorted
multiset and its broker keys are a set.

This module is standard library only and makes no request.
"""

import re
from collections import namedtuple
from urllib.parse import urlencode

import coverage_guard as cg

PLAN_ID = "targeted_actor_v1"
MAX_INPUT_TOKENS = cg.MAX_BROKERS_PER_REQUEST   # the server's cap on raw `brokers` values
INVESTOR_TYPE = "A"            # all investors, as every other inventory collector

# The vendor-listed grammar (module docstring).
VENDOR_N = (3, 5, 8)
VENDOR_TX = ("NB", "NS", "BUY", "SELL")
VENDOR_UNITS = ("LOT", "VAL")
VENDOR_PERIODS = ("C1", "C3", "C5", "C10", "C20", "C50", "ALL")

# The v1 production subset.
V1_N = 5
V1_TX = ("NB", "NS")
V1_UNITS = ("LOT", "VAL")
HORIZONS = ("C5", "C20", "C50", "ALL")
HORIZON_SESSIONS = {"C5": 5, "C20": 20, "C50": 50, "ALL": None}     # None: the whole window
METRICS = ("NB_LOT", "NS_LOT", "NB_VAL", "NS_VAL")                  # the per-horizon order
UNIT_FIELD = {"LOT": "nlot", "VAL": "nval"}                         # what a unit ranks by
REQUEST_GROUPS = (("A", ("C5", "C20")), ("B", ("C50", "ALL")))

TICKER_RE = re.compile(r"[A-Z]{4}")
BROKER_CODE_RE = re.compile(r"[A-Z]{2}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SELECTOR_RE = re.compile(r"TOP_(?P<n>[1-9][0-9]*)_(?P<tx>[A-Z]+)_(?P<unit>[A-Z]+)_(?P<period>C[1-9][0-9]*|ALL)")

Selector = namedtuple("Selector", "token n tx unit period metric field sign sessions")
RequestSpec = namedtuple("RequestSpec", "group horizons tokens")


class SelectorPlanError(ValueError):
    """A request that must not be sent: over the token cap, off the allowlist,
    or otherwise malformed. Raised before any network call."""


def selector_token(n, tx, unit, period):
    return f"TOP_{n}_{tx}_{unit}_{period}"


VENDOR_LISTED_SELECTORS = frozenset(
    selector_token(n, tx, unit, period)
    for n in VENDOR_N for tx in VENDOR_TX for unit in VENDOR_UNITS for period in VENDOR_PERIODS)


def _horizon_tokens(period):
    """The four v1 tokens of one horizon, in METRICS order."""
    return tuple(selector_token(V1_N, *metric.split("_"), period) for metric in METRICS)


V1_ALLOWLIST = frozenset(t for h in HORIZONS for t in _horizon_tokens(h))


def parse_selector(token):
    """The Selector a v1 token names; SelectorPlanError for anything else.

    Exact strings only: no case folding, no whitespace stripping. A token that
    needs normalising is not the token that was verified."""
    if not isinstance(token, str) or token not in V1_ALLOWLIST:
        raise SelectorPlanError(f"{token!r} is not a v1 targeted selector")
    m = SELECTOR_RE.fullmatch(token)
    tx, unit, period = m.group("tx"), m.group("unit"), m.group("period")
    return Selector(token=token, n=int(m.group("n")), tx=tx, unit=unit, period=period,
                    metric=f"{tx}_{unit}", field=UNIT_FIELD[unit],
                    sign=1 if tx == "NB" else -1, sessions=HORIZON_SESSIONS[period])


def check_selector_tokens(tokens, allowlist=V1_ALLOWLIST):
    """The pre-network guard of a selector request: `tokens` as a tuple, or
    SelectorPlanError. At least one, at most MAX_INPUT_TOKENS, every one on
    `allowlist` exactly as written, none repeated (a repeat spends a slot of
    the cap and changes nothing but the echo)."""
    tokens = tuple(tokens)
    if not tokens:
        raise SelectorPlanError("no selector tokens")
    if len(tokens) > MAX_INPUT_TOKENS:
        raise SelectorPlanError(
            f"{len(tokens)} broker tokens in one request; the server keeps only the first "
            f"{MAX_INPUT_TOKENS} and drops the rest silently, so this request is refused, "
            "never truncated")
    off = [t for t in tokens if not isinstance(t, str) or t not in allowlist]
    if off:
        raise SelectorPlanError(f"tokens off the allowlist: {off[:5]}")
    if len(set(tokens)) != len(tokens):
        raise SelectorPlanError(f"repeated tokens: {sorted({t for t in tokens if tokens.count(t) > 1})}")
    return tokens


def check_explicit_codes(codes, known_codes):
    """The pre-network guard of an explicit-code request: `codes` as a tuple,
    or SelectorPlanError. The rule is coverage_guard.check_explicit_codes, the
    one every explicit-code collector uses: at least one, at most
    MAX_INPUT_TOKENS, each a code of `known_codes`, none repeated."""
    try:
        return cg.check_explicit_codes(codes, known_codes)
    except cg.ExplicitRequestError as e:
        raise SelectorPlanError(str(e)) from None


def build_plan():
    """The v1 plan: (RequestSpec A, RequestSpec B). Checked on every call, so
    an edit that breaks the cap, the allowlist or the coverage of the 16
    tokens fails here, not at the vendor."""
    plan = tuple(RequestSpec(group, horizons, tuple(t for h in horizons for t in _horizon_tokens(h)))
                 for group, horizons in REQUEST_GROUPS)
    for spec in plan:
        check_selector_tokens(spec.tokens)
    flat = [t for spec in plan for t in spec.tokens]
    if len(flat) != len(set(flat)) or set(flat) != V1_ALLOWLIST:
        raise SelectorPlanError("the plan must request every v1 token exactly once")
    if tuple(h for spec in plan for h in spec.horizons) != HORIZONS:
        raise SelectorPlanError("the plan must cover every v1 horizon exactly once")
    return plan


PLAN = build_plan()


def build_query(ticker, tokens, start_date, end_date, investor_type=INVESTOR_TYPE):
    """The /api/inventory query string of one selector request, or
    SelectorPlanError before anything is built. `brokers` is repeated, once
    per token, in plan order: a comma-joined list answers HTTP 200 with empty
    series (harvest_inventory)."""
    tokens = check_selector_tokens(tokens)
    return _query(ticker, tokens, start_date, end_date, investor_type)


def build_explicit_query(ticker, codes, known_codes, start_date, end_date,
                         investor_type=INVESTOR_TYPE):
    """As build_query, for at most MAX_INPUT_TOKENS explicit broker codes."""
    codes = check_explicit_codes(codes, known_codes)
    return _query(ticker, codes, start_date, end_date, investor_type)


def _query(ticker, values, start_date, end_date, investor_type):
    if not isinstance(ticker, str) or not TICKER_RE.fullmatch(ticker):
        raise SelectorPlanError(f"{ticker!r} is not a four-letter ticker")
    for name, d in (("start_date", start_date), ("end_date", end_date)):
        if not isinstance(d, str) or not DATE_RE.fullmatch(d):
            raise SelectorPlanError(f"{name} {d!r} is not YYYY-MM-DD")
    if start_date > end_date:
        raise SelectorPlanError(f"start_date {start_date} after end_date {end_date}")
    query = [("symbol", ticker)] + [("brokers", v) for v in values]
    query += [("start_date", start_date), ("end_date", end_date), ("investor_type", investor_type)]
    return urlencode(query)


def selectors_of(spec):
    """[Selector] of a RequestSpec, in its token order."""
    return [parse_selector(t) for t in spec.tokens]


def describe_plan(plan=PLAN):
    """Human-readable lines for the dry run."""
    lines = [f"plan {PLAN_ID}: {len(plan)} requests per ticker, cap {MAX_INPUT_TOKENS} tokens each"]
    for spec in plan:
        lines.append(f"  {spec.group} {'+'.join(spec.horizons):9} {len(spec.tokens)} tokens: "
                     + " ".join(spec.tokens))
    return lines
