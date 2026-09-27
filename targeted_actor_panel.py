"""Collect the targeted broker actor panel: collection mode TARGETED_SELECTORS.

For each ticker, the dominant brokers per horizon (C5, C20, C50, ALL): the top
5 net buyers and net sellers by lot and by value. This is a separate
observation product for Market Intelligence. It is NOT full-universe broker
data and does not replace it: it observes the brokers the vendor's selectors
chose, and says nothing about the rest.

PER TICKER
----------
  1. request A (C5 + C20, 8 selector tokens) and request B (C50 + ALL, 8
     tokens), both over the same window (targeted_selectors.PLAN), each
     refused before the network if it breaks the 10-token cap or the
     allowlist;
  2. validate each response on its own (validate_response): success true;
     meta.symbol the ticker; meta.brokers echoing exactly the tokens sent (as
     a sorted multiset); the requested dates and investor type echoed where
     echoed; a strict session axis inside the requested window with the OHLC
     rows on it one for one; six broker maps with the same keys, every key a
     known two-letter code (a fake key such as ALL is refused) and every
     series whole; build_inventory_db.strict_ticker_frame's value contract
     (exact lots, finite values, nlot = blot - slot, nval = bval - sval);
  3. check the pair (check_pair): the same axis, the same OHLC, and the same
     six series for every broker both returned. Anything else refuses the
     WHOLE ticker: neither capture is used;
  4. take the union of the brokers (each broker's daily series once, with the
     request(s) that returned it) and derive every selector's membership from
     it (derive_membership);
  5. write one snapshot to targeted_actor_panel.db (targeted_actor_db) in one
     transaction, recording both captures.

A later ticker's OHLC identical to an earlier one's in the same run is a
cross-ticker clone (broker_collect) and refused.

DERIVED MEMBERSHIP
------------------
The vendor returns the union of the selectors' brokers with no rank and no
attribution. For each of the 16 selectors, over the union:

    NB  the brokers with a POSITIVE sum of nlot (LOT) or nval (VAL) over the
        horizon, largest first
    NS  the brokers with a NEGATIVE sum, most negative first
    Ck  exactly the last k sessions of the axis, anchored at discovery_as_of
        (the last session)
    ALL the whole returned window

and the top 5 of each. Every row is labelled DERIVED_FROM_SELECTOR_UNION, and
a broker can hold several rows, one per selection reason. Each selector ends
in exactly one selection_status (targeted_actor_db), and only RESOLVED writes
membership rows:

  RESOLVED                 the top 5 is exact. The rank is 1 + the number of
                           members with a strictly better window value, so
                           members with equal values share a rank and are
                           flagged `tied`. No tie is broken by broker code or
                           by any other rule.
  UNRESOLVED_BOUNDARY_TIE  the 5th and 6th candidates have the same window
                           value, so which of the tied brokers the vendor
                           picked cannot be known (its tie policy is
                           unverified). Nothing is attributed; the status row
                           records the tied brokers and their value.
  INSUFFICIENT_HISTORY     the axis has fewer than k sessions for a Ck
                           horizon. What the vendor does then is unverified,
                           so nothing is derived.

Each selector's own picks are inside the union. So when the top 5 of the union
has no tie across the boundary, it IS the selector's top 5. That is checked
for every RESOLVED selector: a member that its selector's own request did not
return contradicts the contract, and the ticker is refused. A broker is never
attributed to a selector capture that did not return it. A returned broker
that no RESOLVED selection of its request explains is kept as observed and
listed as unexplained on its capture: the vendor padding a short list, or
the pick of an unresolved or insufficient-history selector.

COVERAGE
--------
Only returned brokers are stored. Per broker-session: OBSERVED_NONZERO,
OBSERVED_ZERO (returned, all six values zero; an all-zero broker stays
observed), else UNOBSERVED, which is never stored and never zero
(targeted_actor_db.coverage). The snapshot carries coverage_scope
TARGETED_SELECTOR_UNION, and coverage_guard keeps it out of every metric that
needs all brokers.

MANIFEST
--------
Every attempt is recorded in <manifest root>/_capture_manifest/<run_id>.jsonl
(inventory_capture) with collection_mode TARGETED_SELECTORS, selector_plan,
request_group, the exact ordered tokens (brokers_param), the expanded
returned_brokers, digests and status; the result line of an accepted capture
carries discovery_as_of. The snapshot is announced by a persisting line on
both captures before the commit, and each is OK only after it: a refusal of
the pair is REJECTED on both, a failure of B leaves A ABORTED.

PILOT ONLY
----------
No schedule runs this. collect() refuses more than MAX_PILOT_TICKERS tickers:
a broad run needs review first. The CLI:

    py -3 targeted_actor_panel.py plan BBCA BREN ...     dry run: no network
    py -3 targeted_actor_panel.py collect --db PATH BBCA live, <= 5 tickers
    py -3 targeted_actor_panel.py show --db PATH BBCA    the stored panel
    py -3 targeted_actor_panel.py manifest FILE.jsonl    capture/timing summary

Module scope is standard library plus the stdlib-only modules beside it;
neobdm_scraper and Playwright are imported only inside collect()'s live path,
and build_inventory_db / price_audit (pandas) only when a response is checked.
"""

import argparse
import hashlib
import json
import logging
import math
import os
import random
import subprocess
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone

import broker_collect as bc
import coverage_guard as cg
import inventory_capture as ic
import targeted_actor_db as tdb
import targeted_selectors as ts

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = "targeted_actor_panel"
CAPTURE_MODE = "targeted"
COLLECTION_MODE = ic.TARGETED_SELECTORS
MAX_PILOT_TICKERS = 5
SERIES_FIELDS = tdb.SERIES_FIELDS
LOT_FIELDS = ("blot", "slot", "nlot")

# Pacing: broker_collect's proven values, applied per REQUEST (two per ticker),
# since NeoBDM's "abnormal usage" flag counts requests, not tickers.
PACE, JITTER = bc.PACE, bc.JITTER
REST_EVERY, REST_FOR = bc.REST_EVERY, bc.REST_FOR
MAX_RETRY, THROTTLE_COOLDOWN = bc.MAX_RETRY, bc.THROTTLE_COOLDOWN
RELOGIN_AFTER_NON_JSON = bc.RELOGIN_AFTER_NON_JSON
REQUEST_TIMEOUT_MS = bc.REQUEST_TIMEOUT_MS
EST_SECONDS_PER_REQUEST = 2.7   # observed: 11,440 paced requests ~ 8.6 h (inventory truncation note)

# Where no targeted output may go: the full-universe caches and exports.
FULL_UNIVERSE_DIRS = ("inventory_raw", "broker_learning_raw", "broker_history",
                      "broker_dashboard_out")

log = logging.getLogger("targeted_actor_panel")

Observed = namedtuple("Observed", "dates ohlc series brokers meta_brokers")


class PairRejected(Exception):
    """Both requests answered, but not with a panel this ticker can use."""


# ── validation of one response ───────────────

def _reject(msg):
    return bc.FetchRejected(msg)


def validate_response(env, ticker, values, kind, start_date, end_date, known_codes):
    """The Observed payload of one response to a request for `values`, or
    bc.FetchRejected (bc.EmptyResponse for a successful answer with no
    sessions). `kind` is ic.SELECTOR (values are the tokens sent) or
    ic.EXPLICIT_CODES (values are the codes sent, and the codes returned
    must be exactly those). Module docstring, step 2."""
    if kind not in (ic.SELECTOR, ic.EXPLICIT_CODES):
        raise ValueError(f"unknown request kind {kind!r}")
    if not isinstance(env, dict):
        raise _reject(f"response is {type(env).__name__}, not an object")
    if env.get("success") is not True:
        msg = str(env.get("message"))[:120]
        raise bc.FetchRejected(f"success={env.get('success')!r} message={msg!r}", retryable=True,
                               throttled=bc._looks_throttled(msg), category=ic.VENDOR_ERROR)
    meta = env.get("meta")
    if not isinstance(meta, dict):
        raise _reject("no meta object: the token echo cannot be checked")
    shown = meta.get("symbol")
    if not isinstance(shown, str) or shown.upper() != ticker:
        raise _reject(f"meta.symbol {shown!r} != requested {ticker}")
    for key, sent in (("start_date", start_date), ("end_date", end_date),
                      ("investor_type", ts.INVESTOR_TYPE)):
        if key in meta and meta[key] != sent:
            raise _reject(f"meta.{key} {meta[key]!r} != requested {sent!r}")
    echo = meta.get("brokers")
    if (not isinstance(echo, list) or not all(isinstance(v, str) for v in echo)
            or sorted(echo) != sorted(values)):
        raise _reject(f"meta.brokers {str(echo)[:160]} does not echo the {len(values)} "
                      "values requested")

    data = env.get("data")
    if not isinstance(data, dict):
        raise _reject(f"data is {type(data).__name__}, not an object")
    dates = data.get("date")
    if not isinstance(dates, list):
        raise _reject("data.date is not a list")
    if not dates:
        raise bc.EmptyResponse("empty date axis: 0 sessions (delisted or long-suspended)")

    # pandas comes in with build_inventory_db; module scope stays standard library.
    import build_inventory_db as bidb
    try:
        bidb.strict_dates(dates, ticker)
    except bidb.StrictSourceError as e:
        raise _reject(f"date axis: {e}") from None
    if dates[0] < start_date or dates[-1] > end_date:
        raise _reject(f"sessions {dates[0]}..{dates[-1]} outside the requested "
                      f"{start_date}..{end_date}")
    ohlc = data.get("ohlc")
    if not isinstance(ohlc, list) or not all(isinstance(r, dict) for r in ohlc):
        raise _reject("data.ohlc is not a list of objects")
    if [r.get("date") for r in ohlc] != dates:
        raise _reject(f"OHLC rows ({len(ohlc)}) are not the session axis ({len(dates)}) one for one")
    for r in ohlc:     # strict_ohlc_domain checks the other five; None is absent, not zero
        v = r.get("volume_sma20")
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                              or not math.isfinite(v)):
            raise _reject(f"{r['date']}: volume_sma20 is {v!r}")

    maps = {}
    for f in SERIES_FIELDS:
        if not isinstance(data.get(f), dict):
            raise _reject(f"data.{f} is {type(data.get(f)).__name__}, not a broker map")
        maps[f] = data[f]
    keysets = {f: frozenset(maps[f]) for f in SERIES_FIELDS}
    brokers = sorted(keysets["nlot"])
    if len(set(keysets.values())) != 1:
        union = set().union(*keysets.values())
        odd = sorted(b for b in union if not all(b in k for k in keysets.values()))
        raise _reject(f"broker keys differ across the six maps: {odd[:5]}")
    known = set(known_codes)
    bad = [b for b in brokers if not ts.BROKER_CODE_RE.fullmatch(b) or b not in known]
    if bad:
        raise _reject(f"returned keys that are not known broker codes: {bad[:5]}")
    nulls = sorted({b for f in SERIES_FIELDS for b in brokers if maps[f][b] is None})
    if nulls:
        raise _reject(f"null series for {nulls[:5]}: absent is not zero, and not observed")
    if kind == ic.EXPLICIT_CODES and set(brokers) != set(values):
        raise _reject(f"returned codes {brokers[:12]} != requested {sorted(values)[:12]}")

    try:
        bidb.strict_ohlc_domain(data, ticker)
        bidb.strict_ticker_frame(data, ticker)
    except bidb.StrictSourceError as e:
        raise _reject(f"strict frame: {str(e)[:160]}") from None
    series = {b: {f: [(bidb.strict_lot if f in LOT_FIELDS else bidb.strict_rupiah)(
                      v, ticker, b, f, i) for i, v in enumerate(maps[f][b])]
                  for f in SERIES_FIELDS}
              for b in brokers}
    return Observed(dates=list(dates), ohlc=[dict(r) for r in ohlc], series=series,
                    brokers=tuple(brokers), meta_brokers=tuple(echo))


# ── the pair, the union, the membership ──────

def check_pair(a, b):
    """PairRejected unless requests A and B describe the same sessions, prices
    and, for every broker both returned, the same six series."""
    if a.dates != b.dates:
        raise PairRejected(f"session axes differ: A {a.dates[0]}..{a.dates[-1]} "
                           f"({len(a.dates)}), B {b.dates[0]}..{b.dates[-1]} ({len(b.dates)})")
    if a.ohlc != b.ohlc:
        diff = next(i for i, (x, y) in enumerate(zip(a.ohlc, b.ohlc)) if x != y)
        raise PairRejected(f"OHLC differs between A and B at {a.dates[diff]}")
    for broker in sorted(set(a.brokers) & set(b.brokers)):
        if a.series[broker] != b.series[broker]:
            raise PairRejected(f"{broker}'s series differ between A and B")


def union_series(observed_by_group):
    """{broker: {"returned_by": (groups), "series": {field: [...]}}}, each
    broker once, however many requests and selectors returned it."""
    out = {}
    for group, obs in observed_by_group.items():
        for broker in obs.brokers:
            entry = out.setdefault(broker, {"returned_by": (), "series": obs.series[broker]})
            entry["returned_by"] += (group,)
    return dict(sorted(out.items()))


def window_sum(values, field):
    """Exact for lots (ints); math.fsum for rupiah values."""
    return sum(values) if field in LOT_FIELDS else math.fsum(values)


RESOLVED = tdb.RESOLVED
UNRESOLVED_BOUNDARY_TIE = tdb.UNRESOLVED_BOUNDARY_TIE
INSUFFICIENT_HISTORY = tdb.INSUFFICIENT_HISTORY


def derive_membership(dates, brokers, plan, captured_ids):
    """(membership rows, selection status rows, {group: unexplained brokers})
    from the union (module docstring, DERIVED MEMBERSHIP). PairRejected when a
    member of a RESOLVED selector is missing from its own selector's request.

    `brokers` is union_series()'s mapping, `captured_ids` {group: capture_id}.
    The result depends only on the window values: never on the order the
    brokers arrive in, and never on a tie-break."""
    n = len(dates)
    rows, statuses = [], []
    explained = {spec.group: set() for spec in plan}
    union_ids = tuple(captured_ids[spec.group] for spec in plan)
    for spec in plan:
        for sel in ts.selectors_of(spec):
            status = {"horizon": sel.period, "metric": sel.metric, "selector_token": sel.token,
                      "request_group": spec.group,
                      "selector_capture_id": captured_ids[spec.group],
                      "sessions_required": sel.sessions, "sessions_available": n,
                      "window_first_session": None, "window_last_session": None,
                      "qualifying_count": None, "member_count": 0,
                      "boundary_tie_value": None, "boundary_tie_brokers": None}
            statuses.append(status)
            if sel.sessions is not None and n < sel.sessions:
                status["status"] = INSUFFICIENT_HISTORY
                continue
            lo = 0 if sel.sessions is None else n - sel.sessions
            status.update(window_first_session=dates[lo], window_last_session=dates[-1])
            sums = {b: window_sum(e["series"][sel.field][lo:], sel.field)
                    for b, e in brokers.items()}
            # Best first; equal values are left in code order only to make the
            # listing deterministic. No rank or membership below depends on it.
            cands = sorted(((v, b) for b, v in sums.items() if v * sel.sign > 0),
                           key=lambda vb: (-sel.sign * vb[0], vb[1]))
            status["qualifying_count"] = len(cands)
            if len(cands) > sel.n and cands[sel.n - 1][0] == cands[sel.n][0]:
                edge = cands[sel.n][0]
                status.update(status=UNRESOLVED_BOUNDARY_TIE, boundary_tie_value=edge,
                              boundary_tie_brokers=[b for v, b in cands if v == edge])
                continue
            top = cands[:sel.n]
            for value, broker in top:
                if spec.group not in brokers[broker]["returned_by"]:
                    raise PairRejected(
                        f"{sel.token}: {broker} is an exact top-{sel.n} member but was not "
                        f"returned by request {spec.group}, which carried the selector; the "
                        "selector semantics do not hold for this response")
                explained[spec.group].add(broker)
                rows.append({
                    "horizon": sel.period, "metric": sel.metric,
                    "rank": 1 + sum(1 for v, _ in top if sel.sign * v > sel.sign * value),
                    "broker": broker, "window_value": value,
                    "window_first_session": dates[lo], "window_last_session": dates[-1],
                    "window_sessions": n - lo,
                    "tied": sum(1 for v, _ in top if v == value) > 1,
                    "selector_token": sel.token, "request_group": spec.group,
                    "selector_capture_id": captured_ids[spec.group],
                    "union_capture_ids": union_ids, "provenance": tdb.PROVENANCE})
            status.update(status=RESOLVED, member_count=len(top))
    unexplained = {spec.group: sorted(b for b, e in brokers.items()
                                      if spec.group in e["returned_by"]
                                      and b not in explained[spec.group])
                   for spec in plan}
    return rows, statuses, unexplained


def content_sha256(dates, ohlc, brokers):
    """Digest of what a snapshot asserts: axis, OHLC, every observed series and
    which request returned each broker. Equal digests, equal snapshots."""
    doc = {"dates": dates, "ohlc": ohlc,
           "brokers": {b: {"returned_by": list(e["returned_by"]), "series": e["series"]}
                       for b, e in sorted(brokers.items())}}
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_snapshot(ticker, run_id, start_date, end_date, plan, fetched, manifest_run_id):
    """The snapshot targeted_actor_db.record_snapshot writes, from both
    validated requests: {group: (Capture, attempt, query, Observed)}.
    PairRejected when the pair or the derived membership does not hold."""
    a, b = (fetched[spec.group][3] for spec in plan)
    check_pair(a, b)
    observed = {spec.group: fetched[spec.group][3] for spec in plan}
    brokers = union_series(observed)
    if not brokers:
        raise PairRejected("no broker selected by any of the 16 selectors")
    ids = {spec.group: fetched[spec.group][0].capture_id for spec in plan}
    membership, statuses, unexplained = derive_membership(a.dates, brokers, plan, ids)
    captures = []
    for spec in plan:
        cap, attempt, query, obs = fetched[spec.group]
        ev = cap.evidence
        captures.append({
            "request_group": spec.group, "horizons": spec.horizons,
            "selector_tokens": spec.tokens, "capture_id": cap.capture_id,
            "manifest_run_id": manifest_run_id, "attempt": attempt,
            "query_sha256": ic.sha256_text(query), "http_status": ev["http_status"],
            "response_bytes": ev["response_bytes"], "response_sha256": ev["response_sha256"],
            "response_text_sha256": ev["response_text_sha256"],
            "captured_at": ev["captured_at"], "vendor_success": ev["vendor_success"],
            "vendor_meta_brokers": obs.meta_brokers, "expanded_brokers": obs.brokers,
            "unexplained_brokers": unexplained[spec.group], "source_status": "ACCEPTED"})
    return {
        cg.COVERAGE_KEY: cg.TARGETED_SELECTOR_UNION,
        "ticker": ticker, "discovery_as_of": a.dates[-1], "run_id": run_id,
        "selector_plan": ts.PLAN_ID, "requested_start_date": start_date,
        "requested_end_date": end_date, "investor_type": ts.INVESTOR_TYPE,
        "dates": a.dates, "ohlc": a.ohlc, "brokers": brokers, "captures": captures,
        "membership": membership, "selection_status": statuses,
        "content_sha256": content_sha256(a.dates, a.ohlc, brokers),
    }


# ── fetching ─────────────────────────────────

class _TickerFailed(Exception):
    def __init__(self, reason, empty=False):
        super().__init__(reason)
        self.reason, self.empty = reason, empty


class _Session:
    """Request state across one run: pacing, the non-JSON streak, the one
    re-login. `requests` counts actual outbound GET attempts, retries
    included."""

    def __init__(self, get, relogin, safe_error, sleep, captures):
        self.get, self.relogin, self.safe_error = get, relogin, safe_error
        self.sleep, self.captures = sleep, captures
        self.non_json, self.relogged, self.requests = 0, False, 0
        self.latencies = []

    def before_attempt(self, first_attempt):
        """Called before every GET, retries included. It rests after every
        REST_EVERY attempts sent, and paces before a plan request's first
        attempt (not the run's first). A retry is already spaced by its
        backoff, so it is not paced again."""
        if self.requests:
            if self.requests % REST_EVERY == 0:
                self.sleep(REST_FOR)
            if first_attempt:
                self.sleep(PACE + random.random() * JITTER)

    def fetch(self, ticker, spec, query, validate):
        """(Capture, attempt, Observed) for one plan request, retried like
        broker_collect; _TickerFailed when no attempt yields a usable payload."""
        reason, delay = None, 1.0
        for attempt in range(1, MAX_RETRY + 1):
            throttled = False
            self.before_attempt(attempt == 1)
            cap = self.captures.begin(query, attempt, request_group=spec.group)
            try:
                started = time.monotonic()
                self.requests += 1           # an actual GET, whatever it returns
                resp = self.get(query)
                status, body, is_json, text = bc._status_and_json(resp)
                self.latencies.append(time.monotonic() - started)
                cap.response(status, text, body, ic.raw_body(resp))
                if is_json:
                    self.non_json = 0
                else:
                    self.non_json += 1
                    if self.non_json >= RELOGIN_AFTER_NON_JSON and not self.relogged:
                        self.relogged, self.non_json = True, 0
                        log.warning(f"{ticker} {spec.group}: {RELOGIN_AFTER_NON_JSON} non-JSON "
                                    "bodies in a row, logging in again (once per run)")
                        try:
                            self.relogin()
                        except Exception as e:
                            log.error(f"re-login failed: {self.safe_error(e)}")
                if status >= 400:
                    raise bc.FetchRejected(
                        f"HTTP {status}", retryable=True,
                        throttled=status == 429 or (isinstance(body, dict) and
                                                    bc._looks_throttled(body.get("message"))),
                        category=ic.HTTP_ERROR)
                if not is_json:
                    raise bc.FetchRejected("non-JSON body", retryable=True, category=ic.NON_JSON)
                return cap, attempt, validate(body)
            except bc.FetchRejected as e:
                reason, throttled = str(e), e.throttled
                cap.finish(e.category, reason)
                if isinstance(e, bc.EmptyResponse):
                    raise _TickerFailed(reason, empty=True) from None
                if not e.retryable:
                    raise _TickerFailed(reason) from None
            except Exception as e:
                reason = self.safe_error(e)
                throttled = bc._looks_throttled(reason)
                cap.finish(ic.ERROR, reason)
            if attempt < MAX_RETRY:
                log.info(f"{ticker} {spec.group} retry {attempt}: {reason}")
                self.sleep(delay)
                delay *= 3
                if throttled:
                    log.warning(f"rate limit hit - cooling down {THROTTLE_COOLDOWN}s")
                    self.sleep(THROTTLE_COOLDOWN)
        raise _TickerFailed(reason or "no usable response")


def _finish_all(caps, status, reason, **kw):
    for cap in caps:
        cap.finish(status, reason, **kw)


def _run(tickers, conn, session, now, plan, known_codes, run_id, target=tdb.DB_NAME):
    sd, ed = bc.start_date(now), bc.end_date(now)
    result = {"ok": [], "identical": [], "failed": {}, "empty": {}, "sessions": {},
              "observed_brokers": {}, "discovery_as_of": {}, "seconds": {},
              "requested_start_date": sd, "requested_end_date": ed}
    seen = {}               # OHLC signature -> ticker, across the run
    accepted = []           # session counts of accepted pairs, in order
    log.info(f"targeted panel: {len(tickers)} tickers x {len(plan)} requests, {sd}..{ed}")
    for i, t in enumerate(tickers, 1):
        started = time.monotonic()
        fetched, caps = {}, []
        try:
            for spec in plan:
                query = ts.build_query(t, spec.tokens, sd, ed)
                validate = (lambda env, spec=spec: validate_response(
                    env, t, spec.tokens, ic.SELECTOR, sd, ed, known_codes))
                try:
                    cap, attempt, obs = session.fetch(t, spec, query, validate)
                except _TickerFailed as e:
                    if caps:     # an earlier request of the pair was accepted, never used
                        _finish_all(caps, ic.ABORTED, f"request {spec.group} failed: {e.reason}")
                        raise _TickerFailed(f"request {spec.group}: {e.reason}") from None
                    raise
                caps.append(cap)
                fetched[spec.group] = (cap, attempt, query, obs)
            try:
                snap = build_snapshot(t, run_id, sd, ed, plan, fetched, session.captures.run_id)
                sig = bc.ohlc_signature({"ohlc": snap["ohlc"]})
                if sig is not None and sig in seen:
                    raise PairRejected(f"OHLC identical to {seen[sig]} (cross-ticker clone)")
            except PairRejected as e:
                _finish_all(caps, ic.REJECTED, f"pair refused: {e}")
                raise _TickerFailed(f"pair refused: {e}") from None
            if sig is not None:
                seen[sig] = t
            accepted.append(len(snap["dates"]))
            if bc.short_window_abort(accepted, bc.MODE_DAILY):
                message = (f"{sum(1 for n in accepted[:bc.DAILY_PROBE] if n < bc.MIN_SESSIONS)} "
                           f"of the first {min(len(accepted), bc.DAILY_PROBE)} tickers came back "
                           f"under {bc.MIN_SESSIONS} sessions for {sd}..{ed}: the API's rolling "
                           "window has probably moved; C50 and ALL would not mean what they say")
                _finish_all(caps, ic.ABORTED, message)
                raise SystemExit(message)
            for cap in caps:
                cap.persisting(target)
            try:
                outcome = tdb.record_snapshot(conn, snap)
            except Exception as e:
                reason = f"panel write failed, rolled back: {session.safe_error(e)}"
                _finish_all(caps, ic.ERROR, reason)
                raise _TickerFailed(reason) from None
            as_of = snap["discovery_as_of"]
            if outcome == tdb.CONFLICT:
                reason = (f"differs from the snapshot already recorded for {as_of}; "
                          "insert-only, nothing written")
                _finish_all(caps, ic.REJECTED, reason)
                raise _TickerFailed(reason)
            note = None if outcome == tdb.INSERTED else "identical snapshot already recorded"
            _finish_all(caps, ic.OK, note, discovery_as_of=as_of)
            (result["ok"] if outcome == tdb.INSERTED else result["identical"]).append(t)
            result["sessions"][t] = len(snap["dates"])
            result["observed_brokers"][t] = len(snap["brokers"])
            result["discovery_as_of"][t] = as_of
        except _TickerFailed as e:
            (result["empty"] if e.empty else result["failed"])[t] = e.reason
            log.warning(f"[{i}/{len(tickers)}] {t}: {'EMPTY' if e.empty else 'FAILED'} {e.reason}")
        result["seconds"][t] = round(time.monotonic() - started, 3)
    result["requests"] = session.requests
    result["latencies"] = [round(x, 3) for x in session.latencies]
    return result


def _outside_full_universe(path):
    """`path` unless it lies inside one of the full-universe caches/exports."""
    real = os.path.realpath(path)
    for d in FULL_UNIVERSE_DIRS:
        base = os.path.realpath(os.path.join(HERE, d))
        if real == base or real.startswith(base + os.sep):
            raise ValueError(f"{path} is inside {d}/, a full-universe store; targeted "
                             "output never goes there")
    return path


def _git(args, cwd):
    """A read-only git command's CompletedProcess, or None where git cannot run."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def _nearest_dir(path):
    d = os.path.dirname(path)
    while not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return d


def check_private_output(path, sidecars=(), repo_local=None):
    """`path` if the files written there cannot become committable in a git
    repository; ValueError otherwise, before anything is written.

    The panel holds paid per-broker series and this repository is public.
    Inside this repository, a database may only be `repo_local` (the
    canonical targeted_actor_panel.db, gitignored with its sidecars). Anywhere
    inside a git work tree, `path` and every sidecar (SQLite's -journal,
    -wal, -shm) must be ignored there (`git check-ignore`). Outside any work
    tree nothing can be committed by accident, and the path is allowed."""
    real = os.path.realpath(path)
    here = os.path.realpath(HERE)
    if real == here or real.startswith(here + os.sep):
        if repo_local is not None and real != os.path.realpath(repo_local):
            raise ValueError(
                f"{path} is inside this public repository; the only repository-local "
                f"targeted database is {os.path.basename(repo_local)} (gitignored with its "
                "sidecars). Write the pilot database outside the repository.")
    anchor = _nearest_dir(real)
    top = _git(["rev-parse", "--show-toplevel"], anchor)
    if top is None or top.returncode != 0:
        return path                       # no git work tree here: nothing to commit into
    for p in (real,) + tuple(real + s for s in sidecars):
        ignored = _git(["check-ignore", "-q", p], anchor)
        if ignored is None or ignored.returncode != 0:
            raise ValueError(
                f"{path} is inside the git work tree {top.stdout.strip()} and "
                f"{os.path.basename(p)} is not ignored there; paid broker data must never be "
                "committable. Write outside the work tree or use an ignored path.")
    return path


def run_status(result, n_tickers):
    """ok: every ticker stored (inserted or identical); empty: every ticker
    answered with zero sessions; failed: none stored otherwise; partial: some
    stored, the rest failed or empty."""
    stored = len(result["ok"]) + len(result["identical"])
    if stored == n_tickers:
        return "ok"
    if stored == 0:
        return "empty" if len(result["empty"]) == n_tickers else "failed"
    return "partial"


def _run_id(clock):
    return "tap-" + "".join(c for c in clock() if c.isdigit() or c == "T") + "Z"


def collect(tickers, db_path, manifest_root=None, sleep=time.sleep, now=None,
            request_get=None, relogin=None, max_tickers=MAX_PILOT_TICKERS, clock=ic.utc_now):
    """Collect the targeted panel for `tickers` into db_path; the result dict
    ({"ok", "identical", "failed", "empty", "sessions", "observed_brokers",
    "discovery_as_of", "seconds", "requests", "latencies", "run_id",
    "manifest_path", ...}).

    Refuses more than `max_tickers` tickers (a pilot guard: no broad run without
    review), a db_path that is another product's database, any output inside a
    full-universe cache, and any output that could become committable
    (check_private_output), all before anything is written. The manifest goes
    to <manifest_root>/_capture_manifest/, by default beside the database.
    result["status"] is run_status(): ok, partial, empty or failed.
    Raises SystemExit, after recording the run, when the rolling window has
    moved (broker_collect.short_window_abort, daily semantics).

    request_get and relogin exist for the offline tests, as in
    broker_collect.collect: request_get(qs) returns an object with .status,
    .text() and optionally .body(). Left None, collect() logs in to NeoBDM
    through Playwright."""
    tickers = bc._dedupe([str(t).strip().upper() for t in tickers])
    for t in tickers:
        if not ts.TICKER_RE.fullmatch(t) or t in bc.NOT_A_TICKER:
            raise ValueError(f"{t!r} is not a ticker")
    if not tickers:
        raise ValueError("no tickers")
    if len(tickers) > max_tickers:
        raise ValueError(f"{len(tickers)} tickers: the targeted panel is pilot-only, at most "
                         f"{max_tickers} per run until a broad run has been reviewed")
    db_path = _outside_full_universe(tdb.check_path(os.path.abspath(db_path)))
    check_private_output(db_path, sidecars=tdb.SIDECARS, repo_local=tdb.DB_PATH)
    manifest_root = _outside_full_universe(os.path.abspath(
        manifest_root or os.path.dirname(db_path)))
    check_private_output(os.path.join(manifest_root, ic.MANIFEST_DIR, "inv-probe.jsonl"))
    plan, known_codes = ts.PLAN, bc.load_codes()
    run_id = _run_id(clock)
    captures = ic.CaptureLog(manifest_root, COLLECTOR, writes_cache=False, mode=CAPTURE_MODE,
                             pipeline_run_id=run_id,
                             broker_list_source=f"targeted_selectors.PLAN ({ts.PLAN_ID})",
                             collection_mode=COLLECTION_MODE, selector_plan=ts.PLAN_ID,
                             clock=clock)
    conn = tdb.connect(db_path)
    result = None
    try:
        tdb.start_run(conn, run_id, ts.PLAN_ID, clock(), tickers_requested=len(tickers),
                      requested_start_date=bc.start_date(now), requested_end_date=bc.end_date(now))
        status, note = "failed", None
        try:
            if request_get is not None:
                session = _Session(request_get, relogin or (lambda: None), bc._safe_error_local,
                                   sleep, captures)
                result = _run(tickers, conn, session, now, plan, known_codes, run_id,
                              os.path.basename(db_path))
            else:
                result = _live(tickers, conn, sleep, now, plan, known_codes, run_id, captures,
                               os.path.basename(db_path))
            status = run_status(result, len(tickers))
        except SystemExit as e:
            status, note = "aborted", str(e)[:500]
            raise
        except BaseException as e:
            note = bc._safe_error_local(e, 300)
            raise
        finally:
            fields = {"finished_utc": clock(), "status": status,
                      "manifest_run_id": captures.run_id if captures.path else None,
                      "manifest_path": captures.path}
            if result is not None:
                fields.update(tickers_ok=len(result["ok"]),
                              tickers_identical=len(result["identical"]),
                              tickers_failed=len(result["failed"]),
                              tickers_empty=len(result["empty"]),
                              note=json.dumps({"requests": result["requests"],
                                               "seconds": result["seconds"],
                                               "failed": result["failed"],
                                               "empty": result["empty"]})[:4000])
            elif note is not None:
                fields["note"] = note
            tdb.finish_run(conn, run_id, **fields)
    finally:
        conn.close()
    result.update(run_id=run_id, manifest_path=captures.path, db_path=db_path, status=status)
    return result


def _live(tickers, conn, sleep, now, plan, known_codes, run_id, captures, target):
    # Imported here, never at module scope: neobdm_scraper raises on import
    # without all four secrets, and nothing else here needs a browser.
    from neobdm_scraper import API_BASE, INVENTORY_CHART_URL, _safe_error, login
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()

            def sign_in():
                login(page)
                page.goto(INVENTORY_CHART_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)

            def get(qs):
                return ctx.request.get(f"{API_BASE}/inventory?{qs}", timeout=REQUEST_TIMEOUT_MS)

            try:
                sign_in()
            except Exception as e:
                raise RuntimeError(f"NeoBDM login failed: {_safe_error(e)}") from None
            session = _Session(get, sign_in, _safe_error, sleep, captures)
            return _run(tickers, conn, session, now, plan, known_codes, run_id, target)
        finally:
            browser.close()


# ── CLI ──────────────────────────────────────

def _plan_lines(tickers, now=None):
    sd, ed = bc.start_date(now), bc.end_date(now)
    lines = ts.describe_plan()
    n_req = len(ts.PLAN) * len(tickers)
    lines.append(f"window {sd}..{ed} (broker_collect.LOOKBACK_DAYS={bc.LOOKBACK_DAYS}), "
                 f"investor_type {ts.INVESTOR_TYPE}")
    for t in tickers:
        for spec in ts.PLAN:
            lines.append(f"  {t} {spec.group}: /api/inventory?{ts.build_query(t, spec.tokens, sd, ed)}")
    lines.append(f"{len(tickers)} tickers -> {n_req} requests, about "
                 f"{n_req * EST_SECONDS_PER_REQUEST / 60:.1f} min at the paced rate "
                 f"(~{EST_SECONDS_PER_REQUEST}s per request); no network in this dry run")
    return lines


def _show_lines(conn, ticker, as_of=None):
    snap = tdb.snapshot(conn, ticker, as_of)
    if snap is None:
        return [f"{ticker}: no snapshot"]
    as_of = snap["discovery_as_of"]
    lines = [f"{ticker} discovery_as_of {as_of}: {snap['observed_broker_count']} brokers observed "
             f"over {snap['session_count']} sessions ({snap['coverage_scope']}; NOT full "
             "universe: every other broker is UNOBSERVED, not zero)"]
    for c in snap["captures"]:
        lines.append(f"  capture {c['request_group']} {c['capture_id']} http {c['http_status']} "
                     f"{len(c['selector_tokens'])} tokens -> {len(c['expanded_brokers'])} brokers "
                     f"{c['expanded_brokers']} unexplained {c['unexplained_brokers']}")
    lines.append(f"  {'horizon':7} {'metric':6} rank broker {'window value':>20} window  "
                 "(DERIVED_FROM_SELECTOR_UNION)")
    for m in tdb.membership(conn, ticker, as_of):
        lines.append(f"  {m['horizon']:7} {m['metric']:6} {m['rank']:4} {m['broker']:6} "
                     f"{m['window_value']:>20,.0f} {m['window_first_session']}..{m['window_last_session']}"
                     f" ({m['window_sessions']}){' tied' if m['tied'] else ''}")
    for st in tdb.selection_status(conn, ticker, as_of):
        if st["status"] != tdb.RESOLVED:
            lines.append(f"  {st['horizon']:7} {st['metric']:6} {st['status']}: no membership "
                         f"(sessions {st['sessions_available']}/{st['sessions_required']}, tied "
                         f"{st['boundary_tie_brokers']} at {st['boundary_tie_value']})")
    return lines


def _manifest_lines(path):
    caps = ic.read_captures(path)
    lines = [f"{path}: {len(caps)} captures"]
    for c in caps:
        took = ""
        if c.get("captured_at") and c.get("requested_at"):
            fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
            try:
                took = f"{(datetime.strptime(c['captured_at'], fmt) - datetime.strptime(c['requested_at'], fmt)).total_seconds():.2f}s"
            except ValueError:
                took = ""
        lines.append(
            f"  {c['capture_id']} {c.get('ticker')} {c.get('request_group')} try {c.get('attempt')} "
            f"{c['status']:<19} http {c.get('http_status')} {c.get('collection_mode')} "
            f"{len(c.get('brokers_param') or [])} tokens -> "
            f"{len(c.get('returned_brokers') or [])} brokers as_of {c.get('discovery_as_of')} "
            f"{took} {(c.get('response_sha256') or c.get('response_text_sha256') or '')[:12]} "
            f"{c.get('reason') or ''}")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Targeted broker actor panel (pilot only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="dry run: print the request plan; no network, no database")
    p.add_argument("tickers", nargs="+")
    c = sub.add_parser("collect", help=f"live collection, at most {MAX_PILOT_TICKERS} tickers")
    c.add_argument("--db", required=True, help="the targeted panel database to write")
    c.add_argument("--manifest-root", default=None, help="default: the database's directory")
    c.add_argument("tickers", nargs="+")
    s = sub.add_parser("show", help="print a stored snapshot's membership")
    s.add_argument("--db", required=True)
    s.add_argument("--as-of", default=None)
    s.add_argument("tickers", nargs="+")
    m = sub.add_parser("manifest", help="summarise a capture manifest file")
    m.add_argument("path")
    a = ap.parse_args(argv)

    if a.cmd == "plan":
        print("\n".join(_plan_lines([t.strip().upper() for t in a.tickers])))
        return 0
    if a.cmd == "manifest":
        print("\n".join(_manifest_lines(a.path)))
        return 0
    if a.cmd == "show":
        if not os.path.isfile(a.db):           # show never creates a database file
            raise SystemExit(f"{a.db}: no such database")
        conn = tdb.connect(a.db)
        try:
            for t in a.tickers:
                print("\n".join(_show_lines(conn, t.strip().upper(), a.as_of)))
        finally:
            conn.close()
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    res = collect(a.tickers, a.db, manifest_root=a.manifest_root)
    print(json.dumps({k: res[k] for k in ("run_id", "status", "ok", "identical", "failed", "empty",
                                          "discovery_as_of", "observed_brokers", "sessions",
                                          "requests", "seconds", "latencies", "manifest_path",
                                          "db_path")}, indent=1))
    return 0 if res["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
