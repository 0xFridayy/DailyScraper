"""Capture manifest for NeoBDM's /api/inventory: what we asked for, what came back.

The inventory caches keep the payload and nothing about the request that
produced it. inventory_raw/<T>.json.gz is the bare `data` dict;
broker_learning_raw/<mode>/<T>.json.gz adds the vendor's `meta`, which is the
vendor's echo of a request, not evidence of what we sent. Without the request,
a broker missing from a payload cannot be told apart from one that was never
asked for, and a fetch that failed leaves no trace once the log scrolls away.
The broker_learning_raw/daily cache of 2026-09-26 shows why that matters: every
one of its 46 payloads holds 10 of the 101 codes requested (the first ten in
request order), and nothing in the cache says so.

This module records both sides of every request attempt, beside the cache and
without touching it:

    <cache root>/_capture_manifest/<run_id>.jsonl      append-only, one run each

The cache files keep their exact existing format, and every cache reader globs
<root>/*.json.gz or <root>/<mode>/*.json.gz, so the manifest directory is
invisible to them. Nothing here imports beyond the standard library:
broker_collect imports this at module scope and must stay light.

Two collectors keep no cache file: backfill_inventory (its rows go to
neobdm.db) and the bag-holder lookup in neobdm_scraper (used in memory for the
Telegram line). They write under NO_CACHE_ROOT, the repository root, so their
manifests sit in <repo>/_capture_manifest/ (gitignored like the caches). Their
request lines say writes_cache false, and their results always carry
cache_ref and cache_sha256 null: there is no cache artifact to point at.

The collectors that fetch /api/inventory and record here: broker_collect,
harvest_inventory, backfill_inventory and neobdm_scraper's bag holders.
discover_inventory_api.py, a manual discovery script, is the one caller left
out on purpose.

EVENTS (schema inventory_capture_v1)
------------------------------------
Each attempt writes two or three JSON lines, joined by capture_id:

  request  written BEFORE the request is sent, from the exact query string sent:
           ticker (the `symbol` param), start_date, end_date, investor_type;
           brokers_param, the `brokers` values exactly as sent, in order;
           broker_request_kind and requested_brokers, the normalised set
           (strip + upper, sorted, unique), which is null unless every value
           is an explicit two-letter code (a vendor selector such as
           TOP_5_NB_LOT_C20 is listed in broker_selectors, never passed off
           as a broker set); other_params, names only, never values; and
           query_sha256 over the exact query. Plus provenance: collector,
           mode, pipeline_run_id, broker_list_source, writes_cache, attempt.
  persisting  written after the response was accepted and just BEFORE the
           collector makes its artifact durable (the cache file write, or
           backfill_inventory's commit to neobdm.db): persisting_at, target
           ("cache" or "neobdm.db"), intended_cache_ref (null for neobdm.db)
           and the response evidence listed under result. The artifact and
           this file are separate resources and cannot be written atomically;
           this line is the write-ahead half that keeps the gap visible.
  result   written once the attempt's outcome is known: status (below),
           a short log-safe reason, captured_at (when the response arrived,
           or when the failure was seen), http_status; response_bytes and
           response_sha256 over the exact body bytes, where the response
           object hands them over (Playwright's APIResponse.body() does), or
           else response_text_sha256 over the UTF-8 encoding of the decoded
           text, named apart because it is not a digest of the bytes received;
           vendor_success, vendor_meta (the echo, allowlisted, kept apart from
           the request), returned_brokers (the union of broker keys with a
           non-null series in blot/bval/slot/sval/nlot/nval, derived from the
           payload), session_count, first_session, last_session, and, for OK
           from a collector that caches, cache_ref (path relative to the cache
           root) and cache_sha256 (over the uncompressed JSON text written to
           that file).

status and http_status answer different questions. http_status is the fact
of the transport: the code the response came with, whatever happened next.
status is the collector's outcome for the attempt, set where the collector
decides it, never inferred from the code alone. HTTP_ERROR means the collector
refused the response because the source answered with an error (a page that is
not JSON, success != true, a body it cannot read) under an HTTP status >= 400
(source_refusal()); broker_collect refuses every HTTP >= 400 outright. A
refusal for the content (REJECTED: wrong symbol, stale series, strict frame)
and a local failure after an accepted response (ERROR, e.g. a failed commit;
CACHE_WRITE_FAILED) keep their own status whatever the code. A response a
collector accepts despite such a code, as harvest_inventory and the no-cache
collectors do with a JSON success=true answer, records what became of it: OK
beside http_status 500 means the payload was cached or used although it came
with a 500.

OK is written only after the artifact is durable, and CACHE_WRITE_FAILED or
ERROR only when it is known not to be (the write raised; the commit was
rolled back). read_captures() reports an attempt with no result line by what
reached the file: INCOMPLETE when there is no persisting line either (it
stopped before persisting anything, so it left no artifact), and
PERSIST_UNCONFIRMED when there is (the collector began writing its artifact and
its outcome was never recorded: the artifact may or may not exist, and for a
cache the intended_cache_ref says where to look).

The manifest states requested vs returned and nothing more. It writes no
per-broker rows and never turns an omitted broker into a zero: a broker in
returned_brokers with all-zero series was observed at zero, a requested
broker absent from returned_brokers was not returned, and deriving coverage
from that is the reader's job.

DURABILITY
----------
The manifest is written wherever the collector runs. On a GitHub Actions runner
that is the runner's own disk, which is discarded with the runner exactly like
the caches beside it: CI captures are recorded during the run but not kept.
Until a follow-up persists them, only local runs leave a lasting manifest, and
this is not yet a durable audit history of the scheduled jobs.

SECRETS
-------
Only the query string, the HTTP status, the response body (raw_body() calls
APIResponse.body() and nothing else) and its text or parsed JSON are handed
to this module; request headers, cookies and the session never are. NeoBDM
authenticates by cookie, so the query holds none of them, and any query
parameter outside the five known ones is recorded by name only. Reasons are
cut at Playwright's 'Call log:' (which lists every request header, the session
cookie among them), scrubbed of key=value pairs that look like credentials,
and truncated. A non-JSON body (the login page, with its CSRF form token) is
kept as a digest only, never as text.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl

SCHEMA_VERSION = "inventory_capture_v1"
MANIFEST_DIR = "_capture_manifest"
SOURCE = "neobdm"
METHOD = "GET"
ENDPOINT = "/api/inventory"
# The manifest root of the collectors that keep no cache file (module docstring).
NO_CACHE_ROOT = os.path.dirname(os.path.abspath(__file__))

# Attempt outcomes: what the collector did with the attempt, not the transport
# code, which is http_status (module docstring). Everything except OK and EMPTY
# is a source failure for the attempt; the reason says which check fired.
OK = "OK"                                  # accepted, and cached (cache_ref) or used, durably
EMPTY = "EMPTY"                            # success, but the collector found nothing to keep
HTTP_ERROR = "HTTP_ERROR"                  # refused: the source answered with an HTTP error
NON_JSON = "NON_JSON"                      # refused: body not JSON (login page), HTTP < 400
VENDOR_ERROR = "VENDOR_ERROR"              # refused: JSON success != true, HTTP < 400
REJECTED = "REJECTED"                      # refused for its content by our checks
ERROR = "ERROR"                            # a local failure or an exception ended the attempt
ABORTED = "ABORTED"                        # accepted, then the run or lookup stopped before using it
CACHE_WRITE_FAILED = "CACHE_WRITE_FAILED"  # accepted, but the cache write failed
STATUSES = (OK, EMPTY, HTTP_ERROR, NON_JSON, VENDOR_ERROR, REJECTED, ERROR, ABORTED,
            CACHE_WRITE_FAILED)
# Read side only: an attempt with no result line (module docstring).
INCOMPLETE = "INCOMPLETE"                  # nothing was being persisted: no artifact
PERSIST_UNCONFIRMED = "PERSIST_UNCONFIRMED"  # persisting began; the artifact may exist

EXPLICIT_CODES = "EXPLICIT_CODES"
SELECTOR = "SELECTOR"
MIXED = "MIXED"
NO_BROKERS = "NONE"

KNOWN_PARAMS = ("symbol", "brokers", "start_date", "end_date", "investor_type")
SERIES_FIELDS = ("blot", "bval", "slot", "sval", "nlot", "nval")
VENDOR_META_KEYS = ("symbol", "brokers", "start_date", "end_date", "investor_type")

BROKER_CODE_RE = re.compile(r"[A-Z]{2}")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SECRETISH = re.compile(
    r"(?i)\b(sessionid|csrftoken|csrfmiddlewaretoken|x-csrftoken|set-cookie|cookie"
    r"|authorization|password|passwd|api[_-]?key|token)\b(\s*[:=]\s*)"
    r"(?:(?:bearer|basic|token)\s+)?[^\s;,&]+")
REASON_MAX = 200
MAX_TEXT = 200          # longest vendor string kept
MAX_LIST = 1000         # longest vendor list kept


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _key_label(key):
    # As neobdm_source_contract._key_label: a name that is itself data is never echoed.
    return key if isinstance(key, str) and _IDENT.fullmatch(key) else "<non-identifier key>"


def safe_reason(reason, limit=REASON_MAX):
    """A short reason fit for a file that may be shared: an exception as
    'Type: message', cut at Playwright's 'Call log:', credential-looking
    key=value pairs redacted, truncated."""
    if reason is None:
        return None
    if isinstance(reason, BaseException):
        reason = f"{type(reason).__name__}: {reason}"
    text = str(reason).split("Call log:")[0].strip()
    return _SECRETISH.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)[:limit]


# ── request side ──────────────────────────────

def _single(values):
    """A param's value when it was sent once, None when never, the list otherwise."""
    if not values:
        return None
    return values[0] if len(values) == 1 else list(values)


def request_fields(query):
    """What the exact query string asked for (module docstring, `request`)."""
    pairs = parse_qsl(query, keep_blank_values=True)
    sent = {k: [v for name, v in pairs if name == k] for k in KNOWN_PARAMS}
    codes, selectors = [], []
    for value in sent["brokers"]:
        norm = value.strip().upper()
        if BROKER_CODE_RE.fullmatch(norm):
            codes.append(norm)
        else:
            selectors.append(value)
    if codes and selectors:
        kind = MIXED
    elif codes:
        kind = EXPLICIT_CODES
    elif selectors:
        kind = SELECTOR
    else:
        kind = NO_BROKERS
    return {
        "ticker": _single(sent["symbol"]),
        "start_date": _single(sent["start_date"]),
        "end_date": _single(sent["end_date"]),
        "investor_type": _single(sent["investor_type"]),
        "brokers_param": sent["brokers"],
        "broker_request_kind": kind,
        "requested_brokers": sorted(set(codes)) if kind == EXPLICIT_CODES else None,
        "broker_selectors": selectors,
        "other_params": sorted({_key_label(k) for k, _ in pairs if k not in KNOWN_PARAMS}),
        "query_sha256": sha256_text(query),
    }


# ── response side ─────────────────────────────

def _plain(value):
    """`value` if it is a short scalar or a list of them, else None."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, list) and len(value) <= MAX_LIST and all(
            v is None or isinstance(v, (bool, int, float, str)) for v in value):
        return [v[:MAX_TEXT] if isinstance(v, str) else v for v in value]
    return None


def returned_brokers(data):
    """Broker codes carrying a non-null series in any of the six series fields.

    An all-zero series counts: the vendor returned the broker and said zero.
    A broker with no series anywhere was not returned, whatever was asked."""
    if not isinstance(data, dict):
        return None
    return sorted({b for f in SERIES_FIELDS if isinstance(data.get(f), dict)
                   for b, series in data[f].items() if series is not None})


def payload_facts(data):
    """returned_brokers and the session range of a `data` dict (Nones otherwise)."""
    dates = data.get("date") if isinstance(data, dict) else None
    if not isinstance(dates, list):
        return {"returned_brokers": returned_brokers(data), "session_count": None,
                "first_session": None, "last_session": None}
    return {"returned_brokers": returned_brokers(data), "session_count": len(dates),
            "first_session": dates[0] if dates and isinstance(dates[0], str) else None,
            "last_session": dates[-1] if dates and isinstance(dates[-1], str) else None}


def raw_body(response):
    """The exact body bytes of `response` where it hands them over
    (Playwright's APIResponse.body()), else None. Calls .body() and nothing
    else, so no header is ever read, and never raises."""
    read = getattr(response, "body", None)
    if not callable(read):
        return None
    try:
        raw = read()
    except Exception:
        return None
    return bytes(raw) if isinstance(raw, (bytes, bytearray)) else None


def response_evidence(http_status, text, body, raw_bytes=None, clock=utc_now):
    """Evidence of one response: digest, vendor echo, payload facts.

    `text` is the response text (None if it was not read), `body` its parsed
    JSON (None if it was not JSON) and `raw_bytes` the exact body bytes where
    they are available (raw_body()). The digest is over raw_bytes when given
    (response_sha256) and over the text otherwise (response_text_sha256).
    Neither the bytes nor the text is kept."""
    meta = body.get("meta") if isinstance(body, dict) else None
    kept, other = None, set()
    if isinstance(meta, dict):
        kept = {}
        for k, v in meta.items():
            if k in VENDOR_META_KEYS and (v is None or _plain(v) is not None):
                kept[k] = _plain(v)
            else:
                other.add(_key_label(k))     # unknown key, or a nested value: name only
    success = body.get("success") if isinstance(body, dict) else None
    exact = raw_bytes is not None
    return {
        "captured_at": clock(),
        "http_status": http_status,
        "response_bytes": len(raw_bytes) if exact else None,
        "response_sha256": hashlib.sha256(raw_bytes).hexdigest() if exact else None,
        "response_text_sha256": (sha256_text(text) if not exact and text is not None
                                 else None),
        "vendor_success": None if isinstance(success, list) else _plain(success),
        "vendor_meta": kept,
        "vendor_meta_unrecorded_keys": sorted(other),
        **payload_facts(body.get("data") if isinstance(body, dict) else None),
    }


def is_http_error(code):
    """An HTTP status of 400 or more (an int, not a bool)."""
    return isinstance(code, int) and not isinstance(code, bool) and code >= 400


def source_refusal(http_status, body):
    """The status of a response a collector refuses because the source answered
    with an error instead of data: HTTP_ERROR under an HTTP status >= 400,
    whatever the body; otherwise NON_JSON for a body that is not JSON (`body`
    None) and VENDOR_ERROR for JSON whose success is not true.

    Only for that refusal. A response refused for its content is REJECTED, and
    a local failure after an accepted response ERROR or CACHE_WRITE_FAILED,
    whatever its code: the cause decides the status, not http_status."""
    if is_http_error(http_status):
        return HTTP_ERROR
    return NON_JSON if body is None else VENDOR_ERROR


_NO_RESPONSE = {"captured_at": None, "http_status": None, "response_bytes": None,
                "response_sha256": None, "response_text_sha256": None, "vendor_success": None,
                "vendor_meta": None, "vendor_meta_unrecorded_keys": [], "returned_brokers": None,
                "session_count": None, "first_session": None, "last_session": None}


class ManifestAppendError(OSError):
    """An append failed, but the manifest was restored and may be retried."""


class ManifestPoisonedError(BaseException):
    """Rollback failed: this manifest may end in a torn line and cannot be appended.

    This bypasses collectors' ordinary ``except Exception`` retry/failure paths.
    The run must stop before it can issue another request or write another event.
    """


# ── the log ───────────────────────────────────

class Capture:
    """One request attempt. Its request line is already on disk when the caller
    gets this; response() attaches the evidence, persisting() records that the
    collector is about to make its artifact durable, and finish() records the
    outcome.

    The first finish() call decides the outcome, so the handler that knows the
    most specific one records it and a catch-all behind it cannot replace it.
    The outcome counts as recorded (`finished`) only once its line is on disk.
    If the append fails, the error propagates, and a later finish() call writes
    that same decided line again, never a different one: after a durable write,
    a catch-all's ERROR must not stand in for an OK whose line did not land."""

    def __init__(self, log, capture_id, ticker):
        self.log, self.capture_id, self.ticker = log, capture_id, ticker
        self.evidence = dict(_NO_RESPONSE)
        self.decided = None          # the outcome line, once decided
        self.finished = False        # ...and on disk

    def _evidence(self):
        evidence = dict(self.evidence)
        if evidence["captured_at"] is None:
            evidence["captured_at"] = self.log.clock()
        return evidence

    def response(self, http_status, text, body, raw_bytes=None):
        """Attach the response: its status, text, parsed JSON and, where
        available, exact bytes (see response_evidence)."""
        self.evidence = response_evidence(http_status, text, body, raw_bytes, self.log.clock)

    def persisting(self, target, cache_ref=None):
        """Record, just before the collector makes its artifact durable, that
        it is about to (target "cache" or "neobdm.db"; cache_ref where a cache
        file goes). If no outcome line follows, read_captures() reports
        PERSIST_UNCONFIRMED instead of INCOMPLETE: the artifact may exist.
        Raises if the line cannot be written, before anything is persisted."""
        self.log._append({
            "schema": SCHEMA_VERSION, "event": "persisting", "capture_id": self.capture_id,
            "run_id": self.log.run_id, "ticker": self.ticker, "persisting_at": self.log.clock(),
            "target": target, "intended_cache_ref": cache_ref, **self._evidence()})

    def finish(self, status, reason=None, cache_ref=None, cache_sha256=None):
        """Decide the outcome (the first call only) and write its line: True
        when this call put the line on disk, False when it already was. Raises
        when the append fails, leaving `finished` False for a later call.

        The status is written as given; it is never inferred from http_status
        (see source_refusal for the refusals where the code decides)."""
        if status not in STATUSES:
            raise ValueError(f"unknown capture status {status!r}")
        if self.finished:
            return False
        if self.decided is None:
            self.decided = {
                "schema": SCHEMA_VERSION, "event": "result", "capture_id": self.capture_id,
                "run_id": self.log.run_id, "ticker": self.ticker, "status": status,
                "reason": safe_reason(reason), **self._evidence(),
                "cache_ref": cache_ref, "cache_sha256": cache_sha256}
        self.log._append(self.decided)
        self.finished = True
        return True


class CaptureLog:
    """The manifest of one run: <root>/_capture_manifest/<run_id>.jsonl.

    Nothing touches the disk until the first begin(), so a run that sends no
    request leaves no file. The run id follows neobdm_source_contract's
    capture ids ("ms-<UTC digits>Z"): "inv-<UTC digits>Z". A second run that
    starts in the same instant gets "-2" rather than the first run's file."""

    def __init__(self, root, collector, *, writes_cache, mode=None, pipeline_run_id=None,
                 broker_list_source=None, clock=utc_now):
        self.root, self.collector, self.mode = root, collector, mode
        self.writes_cache = bool(writes_cache)
        self.pipeline_run_id, self.broker_list_source = pipeline_run_id, broker_list_source
        self.clock = clock
        self.run_id = "inv-" + re.sub(r"[^0-9T]", "", clock()) + "Z"
        self.path = None
        self.seq = 0
        self.poisoned = False

    def _open(self):
        folder = os.path.join(self.root, MANIFEST_DIR)
        os.makedirs(folder, exist_ok=True)
        base = self.run_id
        for n in range(1, 1000):
            run_id = base if n == 1 else f"{base}-{n}"
            path = os.path.join(folder, f"{run_id}.jsonl")
            try:
                with open(path, "x", encoding="utf-8"):
                    pass
            except FileExistsError:
                continue
            self.run_id, self.path = run_id, path
            return
        raise RuntimeError(f"no free capture manifest name for {base} in {folder}")

    def _append(self, event):
        """Append one line. Retry only if a failed write was fully rolled back.

        If rollback fails, the possible torn line must remain the final line:
        read_captures() can then recover the preceding lifecycle events.
        """
        if self.poisoned:
            raise ManifestPoisonedError("capture manifest is poisoned; refusing another append")
        if self.path is None:
            self._open()
        line = json.dumps(event, separators=(",", ":")) + "\n"
        size = os.path.getsize(self.path)
        try:
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
        except BaseException as append_error:
            try:
                os.truncate(self.path, size)
                if os.path.getsize(self.path) != size:
                    raise OSError("capture manifest was not restored to its pre-append size")
            except BaseException as rollback_error:
                self.poisoned = True
                raise ManifestPoisonedError(
                    "capture manifest append and rollback failed; refusing further writes"
                ) from rollback_error
            if isinstance(append_error, Exception):
                raise ManifestAppendError("capture manifest append failed; file restored") from append_error
            raise

    def begin(self, query, attempt=1):
        """Record the request `query` is about to make; its Capture.

        `query` must be the exact query string the request sends."""
        if self.path is None:
            self._open()             # settles run_id before the first capture_id
        self.seq += 1
        capture_id = f"{self.run_id}-{self.seq:05d}"
        fields = request_fields(query)
        self._append({
            "schema": SCHEMA_VERSION, "event": "request", "capture_id": capture_id,
            "run_id": self.run_id, "seq": self.seq, "attempt": attempt,
            "requested_at": self.clock(), "source": SOURCE, "method": METHOD,
            "endpoint": ENDPOINT, "collector": self.collector, "mode": self.mode,
            "pipeline_run_id": self.pipeline_run_id,
            "broker_list_source": self.broker_list_source,
            "writes_cache": self.writes_cache, **fields})
        return Capture(self, capture_id, fields["ticker"])


# ── reading ───────────────────────────────────

def read_captures(path):
    """[capture] from one manifest file, in request order: the request fields
    with the persisting and result fields merged in. An attempt without a
    result is INCOMPLETE, or PERSIST_UNCONFIRMED when a persisting line shows
    its collector had begun writing the artifact (module docstring). A torn last
    line (the process died mid-write) is ignored; any other malformed line, an
    event without its request or out of order, or a repeated one raises
    ValueError."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    captures = {}
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            if i == len(lines):
                break
            raise ValueError(f"{path}:{i}: malformed manifest line") from None
        kind, cid = event.get("event"), event.get("capture_id")
        if kind == "request":
            if cid in captures:
                raise ValueError(f"{path}:{i}: second request for {cid}")
            captures[cid] = dict(event, status=INCOMPLETE)
        elif kind == "persisting":
            if cid not in captures:
                raise ValueError(f"{path}:{i}: persisting for {cid} without its request")
            if captures[cid]["status"] != INCOMPLETE:
                raise ValueError(f"{path}:{i}: persisting for {cid} after its result or twice")
            captures[cid].update(event, status=PERSIST_UNCONFIRMED)
        elif kind == "result":
            if cid not in captures:
                raise ValueError(f"{path}:{i}: result for {cid} without its request")
            if captures[cid]["status"] not in (INCOMPLETE, PERSIST_UNCONFIRMED):
                raise ValueError(f"{path}:{i}: second result for {cid}")
            captures[cid].update(event)
        else:
            raise ValueError(f"{path}:{i}: unknown event {kind!r}")
    for c in captures.values():
        c.pop("event", None)
    return list(captures.values())
