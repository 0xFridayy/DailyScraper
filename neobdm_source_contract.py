"""
NeoBDM market-summary (screener) source contract: field lifecycle, raw capture,
capture manifest, contract checks and signal-source availability.

Standard library only, so check_signal_integrity.py can import it without
playwright.

Why this exists (2026-09-14 incident): NeoBDM removed `is_unusual_volume` from
its column catalog. The screener PATCH still answered success, the stored
DAILY_SCRAPER config silently shrank to 14 columns, and the summary rows simply
omitted the key. The scraper wrote NULL, the Top-2 screen read that as "not
unusual", Telegram said "No data scraped today", and the integrity monitor went
green again the next day because the empty day became part of its own baseline.
No raw response had been kept, so the mechanism took a live probe to establish.

Layers (minimum, additive to the existing tables):
  RAW         immutable response bytes, content-addressed gzip under
              market_summary_raw_fragments/, indexed in ms_raw_response
              (schema-allowlisted: public repository; see RAW_KIND_*)
  NORMALIZED  per-capture presence facts in ms_capture_manifest: for each field
              KEY_ABSENT / EXPLICIT_NULL / FALSE / TRUE / VALUE counts and dtypes
  CANONICAL   market_summary_daily (unchanged; NULL = unavailable, never 0)
              plus source_field_lifecycle (ACTIVE / RETIRED, with provenance)
  DERIVED     konglo_signal_watch hits plus signal_source_status per family
"""

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

SOURCE = "neobdm.market_summary.screener"
CONTRACT_VERSION = "market_summary_contract_v2"
# The contract every capture before this repair ran under. Those captures carry no
# manifest, so a capture WITHOUT a manifest is judged against this request set.
LEGACY_CONTRACT_VERSION = "market_summary_contract_v1"

# ── field lifecycle ───────────────────────────
ACTIVE = "ACTIVE"
RETIRED = "RETIRED"

# Per-capture availability of a field (an observation, not a contract state).
AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"

# Presence of one field in one returned row.
KEY_ABSENT = "KEY_ABSENT"
EXPLICIT_NULL = "EXPLICIT_NULL"
FALSE = "FALSE"
TRUE = "TRUE"
VALUE = "VALUE"

# The screener request set. NeoBDM caps a screener at 15 columns and returns only
# the configured ones. `is_unusual_volume` is retired and deliberately NOT
# replaced: the freed slot stays empty until an explicit contract change.
ACTIVE_REQUEST_COLUMNS = [
    "symbol", "close", "high", "low",
    "m_dn_0", "nr_dn_0", "f_dn_0",
    "m_cn_5", "m_dn_3", "top_5_buyer",
    "clean_score",
    "tval", "market_cap_t", "pct_5",
]
IDENTITY_KEY = "symbol"
LEGACY_REQUEST_COLUMNS = [
    "symbol", "close", "high", "low",
    "m_dn_0", "nr_dn_0", "f_dn_0",
    "m_cn_5", "m_dn_3", "top_5_buyer",
    "is_unusual_volume", "clean_score",
    "tval", "market_cap_t", "pct_5",
]
CONTRACT_REQUEST_COLUMNS = {
    LEGACY_CONTRACT_VERSION: LEGACY_REQUEST_COLUMNS,
    CONTRACT_VERSION: ACTIVE_REQUEST_COLUMNS,
}

# Health of one capture judged against the contract that was ACTIVE when it was
# taken. A later, human-authorised retirement explains a break; it does not turn
# the break into a healthy capture.
HEALTHY = "HEALTHY"
SOURCE_CONTRACT_BREAK_ACKNOWLEDGED = "SOURCE_CONTRACT_BREAK_ACKNOWLEDGED"

# Undeclared keys the screener is KNOWN to add, each with the only condition under
# which it is tolerated. Deliberately an explicit allowlist, not "ignore extras":
# any key not listed here stays an UNEXPECTED_KEY warning.
TOLERATED_ADDITIVE_KEYS_VERSION = "tolerated_additive_keys_v1"
TOLERATED_ADDITIVE_KEYS = {
    "symbol_1": {
        "alias_of": "symbol",
        "rule": "equal to alias_of on every row carrying both; present without alias_of is a failure",
        "evidence": "Live read-only probe 2026-09-15: POST summary page 1 (sha256 0de3da23cbe5ffb22f4694ee903f"
                    "7a913df6a9de1d952c3dab8b073b4376528a) returned symbol_1 == symbol on 20/20 rows",
    },
}

RETIREMENT_EVIDENCE_IUV = (
    "Live read-only probe 2026-09-15T16:11Z: GET /api/market-summary/columns "
    "(sha256 560c9f7c075791e4adbfccbad0131c0c881b56e6b10254da23f61c04af027aa1) lists 430 "
    "fields without is_unusual_volume; GET /api/screeners "
    "(sha256 aa25e6af188441a7d12a4306325df8e7306df6aa8495d2c076d95ee8e4cb7108) shows "
    "DAILY_SCRAPER stored with 14 columns (is_unusual_volume dropped although the "
    "production PATCH sent it and was not rejected); POST summary page 1 "
    "(sha256 0de3da23cbe5ffb22f4694ee903f7a913df6a9de1d952c3dab8b073b4376528a) omits the "
    "key on 20/20 rows. market_summary_daily stored NULL on every row of the "
    "2026-09-14 and 2026-09-15 captures. is_spike_volume_* exist but semantic "
    "equivalence is NOT established; no replacement approved.")

FIELD_LIFECYCLE = {
    "is_unusual_volume": {
        "state": RETIRED,
        "effective_capture_date": "2026-09-14",
        "evidence": RETIREMENT_EVIDENCE_IUV,
        "replacement": None,
        "signals": ["top_akum_bandar"],
        # The retirement is a human decision taken after the break was observed;
        # captures made before this contract version still count as breaks.
        "acknowledged_in_contract": CONTRACT_VERSION,
        "acknowledgement": "Source-recovery memo and live probe accepted in human review 2026-09-16; "
                           "field retired, no replacement approved",
    },
}

# Fields the daily signal needs populated. is_unusual_volume stays listed as
# HISTORICALLY critical so its retirement is reported explicitly, not forgotten.
ACTIVE_CRITICAL_FIELDS = ["close", "high", "low", "tval", "clean_score",
                          "m_dn_0", "nr_dn_0", "f_dn_0", "market_cap_t", "pct_5"]
HISTORICAL_CRITICAL_FIELDS = ["is_unusual_volume"]


def lifecycle_state(field_name, capture_date=None, lifecycle=None):
    """ACTIVE or RETIRED for `field_name` in the capture regime of `capture_date`
    (YYYY-MM-DD). A retirement never reaches back before its effective date."""
    entry = (FIELD_LIFECYCLE if lifecycle is None else lifecycle).get(field_name)
    if not entry or entry["state"] != RETIRED:
        return ACTIVE
    if capture_date is not None and capture_date < entry["effective_capture_date"]:
        return ACTIVE
    return RETIRED


def retired_fields(capture_date=None, lifecycle=None):
    lc = FIELD_LIFECYCLE if lifecycle is None else lifecycle
    return sorted(f for f in lc if lifecycle_state(f, capture_date, lc) == RETIRED)


# ── presence / flag semantics ─────────────────
def presence_state(row, field_name):
    """Absent key, explicit null, boolean false/true, or another value. Keeps the
    distinctions the old `row.get()` collapsed."""
    if field_name not in row:
        return KEY_ABSENT
    value = row[field_name]
    if value is None:
        return EXPLICIT_NULL
    if value is True:
        return TRUE
    if value is False:
        return FALSE
    return VALUE


def flag_state(value):
    """Tri-state reading of a source boolean: TRUE / FALSE / None (unknown).
    Missing is never False."""
    if value is True:
        return TRUE
    if value is False:
        return FALSE
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "1", "v"):
        return TRUE
    if text in ("false", "0"):
        return FALSE
    return None


def source_number(row, field_name):
    """Numeric reading of a source-backed field, or None when it is unavailable.
    KEY_ABSENT, EXPLICIT_NULL, booleans, empty and unparseable values are None;
    a genuine source zero stays 0.0. Unlike the scraper's legacy parse_num(), a
    missing value never becomes zero."""
    if field_name not in row:
        return None
    value = row[field_name]
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None     # NaN is unavailable
    text = str(value).replace(",", "").replace("(", "-").replace(")", "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number else None


def dtype_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def field_presence_summary(rows, fields):
    out = {}
    for f in fields:
        rec = {KEY_ABSENT: 0, EXPLICIT_NULL: 0, FALSE: 0, TRUE: 0, VALUE: 0, "dtypes": {}}
        for r in rows:
            state = presence_state(r, f)
            rec[state] += 1
            if state != KEY_ABSENT:
                name = dtype_name(r[f])
                rec["dtypes"][name] = rec["dtypes"].get(name, 0) + 1
        rec["present_non_null"] = rec[FALSE] + rec[TRUE] + rec[VALUE]
        out[f] = rec
    return out


def returned_keys(rows):
    return sorted({k for r in rows for k in r})


# ── contract check ────────────────────────────
FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
BLOCKING_CODES = {"IDENTITY_AMBIGUITY", "DUPLICATE_IDENTITY", "CASE_COLLISION", "ALIAS_WITHOUT_CANONICAL_KEY"}
RETIRED_FIELD_REAPPEARED = "RETIRED_FIELD_REAPPEARED_REQUIRES_HUMAN_REVIEW"
CONTRACT_OK, CONTRACT_DEGRADED, CONTRACT_FAILED = "OK", "DEGRADED", "FAILED"


def _issue(severity, code, field_name=None, detail=None):
    return {"severity": severity, "code": code, "field": field_name, "detail": detail}


def evaluate_capture_contract(requested, catalog_fields, stored_columns, rows,
                              capture_date=None, lifecycle=None, identity_key=IDENTITY_KEY):
    """requested vs catalog vs stored config (after PATCH) vs returned row keys.

    Required-field loss is a FAIL but does not block persisting the other fields:
    the field becomes UNAVAILABLE for this capture and the good data is kept.

    Partial missingness is judged per row, not per capture: explicit nulls on some
    rows are INFO (those rows' values are unavailable and stay NULL; a calculation
    needing one excludes the row), while a key absent from only some rows is WARN,
    because a response whose rows disagree on shape is a structural change. Mass
    loss below the coverage floor fails in check_signal_integrity.py.

    Additive unexpected keys are WARN only, unless they make the row identity or a
    column name ambiguous, which blocks persistence (BLOCKING_CODES). A key in
    TOLERATED_ADDITIVE_KEYS that satisfies its rule is recorded as INFO and does not
    degrade the capture. A RETIRED field seen again in the catalog, the stored
    config or the rows is WARN RETIRED_FIELD_REAPPEARED_REQUIRES_HUMAN_REVIEW.
    `catalog_fields` / `stored_columns` may be None when that read failed; that is
    itself reported, never assumed fine.
    """
    lc = FIELD_LIFECYCLE if lifecycle is None else lifecycle
    issues, availability = [], {}
    n = len(rows)
    presence = field_presence_summary(rows, requested)
    if catalog_fields is None:
        issues.append(_issue(FAIL, "CATALOG_UNREADABLE"))
    if stored_columns is None:
        issues.append(_issue(FAIL, "STORED_CONFIG_UNREADABLE"))
    if not n:
        issues.append(_issue(FAIL, "NO_ROWS"))

    for f in requested:
        if lifecycle_state(f, capture_date, lc) == RETIRED:
            issues.append(_issue(FAIL, "RETIRED_FIELD_REQUESTED", f))
        if catalog_fields is not None and f not in catalog_fields:
            issues.append(_issue(FAIL, "REQUESTED_MISSING_FROM_CATALOG", f))
        if stored_columns is not None and f not in stored_columns:
            issues.append(_issue(FAIL, "REQUESTED_MISSING_FROM_STORED_CONFIG", f))
        p = presence[f]
        if n and p[KEY_ABSENT] == n:
            issues.append(_issue(FAIL, "KEY_ABSENT_IN_RESPONSE", f, f"{n}/{n} rows"))
        elif n and p[EXPLICIT_NULL] == n:
            issues.append(_issue(FAIL, "EXPLICIT_NULL_ALL_ROWS", f, f"{n}/{n} rows"))
        else:
            if p[KEY_ABSENT]:
                issues.append(_issue(WARN, "KEY_ABSENT_SOME_ROWS", f, f"{p[KEY_ABSENT]}/{n} rows"))
            if p[EXPLICIT_NULL]:
                issues.append(_issue(INFO, "EXPLICIT_NULL_SOME_ROWS", f, f"{p[EXPLICIT_NULL]}/{n} rows"))
        availability[f] = AVAILABLE if p["present_non_null"] else UNAVAILABLE

    keys = returned_keys(rows)
    retired_now = retired_fields(capture_date, lc)
    for f in retired_now:
        availability[f] = RETIRED
        # A retired name coming back may carry different semantics: surface it for a
        # human, never reactivate, re-request or bridge it automatically.
        seen_in = [where for where, present in (
            ("catalog", catalog_fields is not None and f in catalog_fields),
            ("stored screener config", stored_columns is not None and f in stored_columns and f not in requested),
            ("returned rows", f in keys and f not in requested)) if present]
        if seen_in:
            issues.append(_issue(WARN, RETIRED_FIELD_REAPPEARED, f,
                                 f"RETIRED since {lc[f]['effective_capture_date']} but present in "
                                 f"{', '.join(seen_in)}; not reactivated, semantics unverified"))

    if stored_columns is not None:
        for f in stored_columns:
            if f not in requested and f not in retired_now:
                issues.append(_issue(WARN, "STORED_CONFIG_EXTRA_COLUMN", f))

    unexpected = [k for k in keys if k not in requested]
    tolerated = {}
    lowered = {}
    for k in keys:
        lowered.setdefault(k.lower(), []).append(k)
    for k in unexpected:
        collides = [o for o in lowered[k.lower()] if o != k]
        if collides:
            issues.append(_issue(FAIL, "CASE_COLLISION", k, f"collides with {collides}"))
        elif k in retired_now:
            continue                                   # reported above
        elif k in TOLERATED_ADDITIVE_KEYS:
            alias_of = TOLERATED_ADDITIVE_KEYS[k]["alias_of"]
            carrying = [r for r in rows if k in r]
            orphaned = sum(1 for r in carrying if alias_of not in r)
            mismatched = sum(1 for r in carrying if alias_of in r and r[k] != r[alias_of])
            if orphaned:
                issues.append(_issue(FAIL, "ALIAS_WITHOUT_CANONICAL_KEY", k,
                                     f"present without {alias_of} on {orphaned} row(s)"))
            if mismatched:
                issues.append(_issue(FAIL, "IDENTITY_AMBIGUITY", k,
                                     f"differs from {alias_of} on {mismatched} row(s)"))
            if not orphaned and not mismatched:
                tolerated[k] = {"alias_of": alias_of, "rows_checked": len(carrying),
                                "allowlist_version": TOLERATED_ADDITIVE_KEYS_VERSION}
                issues.append(_issue(INFO, "TOLERATED_ADDITIVE_ALIAS", k,
                                     f"equals {alias_of} on {len(carrying)}/{len(carrying)} row(s) "
                                     f"({TOLERATED_ADDITIVE_KEYS_VERSION})"))
        elif re.fullmatch(re.escape(identity_key) + r"_\d+", k):
            mismatched = sum(1 for r in rows if k in r and r.get(k) != r.get(identity_key))
            if mismatched:
                issues.append(_issue(FAIL, "IDENTITY_AMBIGUITY", k,
                                     f"differs from {identity_key} on {mismatched} row(s)"))
            else:
                issues.append(_issue(WARN, "UNEXPECTED_KEY", k, f"duplicate of {identity_key} on every "
                                     f"row but not in {TOLERATED_ADDITIVE_KEYS_VERSION}"))
        else:
            issues.append(_issue(WARN, "UNEXPECTED_KEY", k))
    ids = [r.get(identity_key) for r in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1 and i is not None})
    if dupes:
        issues.append(_issue(FAIL, "DUPLICATE_IDENTITY", identity_key, f"{len(dupes)} duplicated: {dupes[:5]}"))
    missing_identity = sum(1 for i in ids if not i)
    if missing_identity:
        issues.append(_issue(WARN, "ROW_WITHOUT_IDENTITY", identity_key, f"{missing_identity} row(s)"))

    severities = {i["severity"] for i in issues}
    status = CONTRACT_FAILED if FAIL in severities else CONTRACT_DEGRADED if WARN in severities else CONTRACT_OK
    blocking = sorted({i["code"] for i in issues if i["code"] in BLOCKING_CODES})
    return {"status": status, "issues": issues, "availability": availability, "presence": presence,
            "returned_keys": keys, "unexpected_keys": unexpected, "tolerated_additive_keys": tolerated,
            "blocking_codes": blocking}


def catalog_field_names(catalog_json):
    if not isinstance(catalog_json, dict) or not isinstance(catalog_json.get("data"), list):
        return None
    return [c["field"] for c in catalog_json["data"] if isinstance(c, dict) and c.get("field")]


def stored_config_columns(screeners_json, screener_id):
    """Columns of the stored screener `screener_id`, or None if not determinable."""
    if not isinstance(screeners_json, dict) or not isinstance(screeners_json.get("data"), list):
        return None
    for s in screeners_json["data"]:
        if isinstance(s, dict) and s.get("id") == screener_id:
            cols = s.get("columns")
            if not isinstance(cols, list):
                return None
            return [c if isinstance(c, str) else (c or {}).get("field") for c in cols]
    return None


def json_sha256(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


# ── raw capture ───────────────────────────────
RAW_DIR_NAME = "market_summary_raw_fragments"


class RawIntegrityError(RuntimeError):
    pass


def raw_rel_path(sha):
    return f"{RAW_DIR_NAME}/{sha[:2]}/{sha}.json.gz"


def store_raw_bytes(raw, root=HERE):
    """Write `raw` once, content-addressed by its SHA-256. gzip mtime=0 keeps the
    compressed bytes deterministic. An existing object is verified, never
    overwritten; a mismatch raises. Returns (sha256, rel_path, written)."""
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("raw response must be bytes")
    raw = bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    rel = raw_rel_path(sha)
    path = os.path.join(root, *rel.split("/"))
    if os.path.exists(path):
        with open(path, "rb") as fh:
            existing = gzip.decompress(fh.read())
        if hashlib.sha256(existing).hexdigest() != sha:
            raise RawIntegrityError(f"{rel} exists with different content; refusing to overwrite")
        return sha, rel, False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(gzip.compress(raw, compresslevel=9, mtime=0))
    if os.path.exists(path):          # a concurrent writer won: verify, never replace
        os.remove(tmp)
        return store_raw_bytes(raw, root)
    os.replace(tmp, path)
    return sha, rel, True


def read_raw_bytes(rel, root=HERE):
    with open(os.path.join(root, *rel.split("/")), "rb") as fh:
        return gzip.decompress(fh.read())


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# How a response body is kept. The fragment store is committed to a PUBLIC
# repository, so nothing reaches it unless an explicit schema allows it. Every
# body is checked against a nested key allowlist (the *_RESPONSE_SCHEMA below); a key the
# schema does not name is dropped and only its path is recorded.
#   RAW         exact response bytes: the body passed its allowlist with nothing
#               dropped (column catalog, summary pages on a normal day)
#   SANITIZED   the allowlisted subset, re-serialised: the body carried keys the
#               schema does not name (a source-contract change to review)
#   PROJECTED   an ACCOUNT-SCOPED endpoint (screeners, stock universes) narrowed to
#               the one object the capture depends on, then allowlisted; the
#               account's other screeners/universes are never written
#   NOT_STORED  no schema given, or a body that is not a JSON object (e.g. the HTML
#               login page with its CSRF form token after a lost session): only its
#               hash, size and content type are kept
RAW_KIND_RAW, RAW_KIND_SANITIZED, RAW_KIND_PROJECTED, RAW_KIND_NOT_STORED = (
    "RAW", "SANITIZED", "PROJECTED", "NOT_STORED")

SCALAR = "scalar"                     # str / int / float / bool / null only
SCALARS = "scalar_or_scalar_list"     # a scalar, or a list of scalars
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def list_of(spec):
    return ("list", spec)


ENVELOPE_SCHEMA = {
    "success": SCALAR, "status": SCALAR, "message": SCALAR, "errors": list_of(SCALAR),
    "timestamp": SCALAR, "trace_id": SCALAR,
    "meta": {"last_page": SCALAR, "current_page": SCALAR, "per_page": SCALAR, "total": SCALAR},
}
CATALOG_ITEM_SCHEMA = {"field": SCALAR, "title": SCALAR, "desc": SCALAR, "format": SCALAR,
                       "filter": {"ops": list_of(SCALAR), "type": SCALAR, "unit": SCALAR}}
SCREENER_SCHEMA = {
    "id": SCALAR, "name": SCALAR, "columns": list_of(SCALAR), "column_defs": list_of(CATALOG_ITEM_SCHEMA),
    "filters": list_of({"field": SCALAR, "op": SCALAR, "type": SCALAR, "unit": SCALAR, "value": SCALARS}),
    "sort_field": SCALAR, "sort_direction": SCALAR,
    "stock_universe_id": SCALAR, "stock_universe_name": SCALAR, "stock_universe": {"id": SCALAR, "name": SCALAR},
}


def response_schema(data_spec):
    return dict(ENVELOPE_SCHEMA, data=data_spec)


CATALOG_RESPONSE_SCHEMA = response_schema(list_of(CATALOG_ITEM_SCHEMA))
SCREENERS_RESPONSE_SCHEMA = response_schema(list_of(SCREENER_SCHEMA))
SCREENER_RESPONSE_SCHEMA = response_schema(SCREENER_SCHEMA)
UNIVERSE_RESPONSE_SCHEMA = response_schema(list_of({"id": SCALAR, "name": SCALAR}))


def summary_response_schema(row_fields):
    """Summary pages: rows may carry only the named fields (the request set and the
    tolerated aliases), each a scalar or a list of scalars."""
    return response_schema(list_of({f: SCALARS for f in row_fields}))


def _is_scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def _key_label(key):
    # A dropped key's NAME is recorded, unless it is not an identifier (a key that is
    # itself data -- an email, an account id -- is never echoed).
    return key if isinstance(key, str) and _IDENT.fullmatch(key) else "<non-identifier key>"


def sanitize(value, spec, path="$", dropped=None):
    """(allowlisted copy of `value`, sorted dropped paths). Fail-closed: a value whose
    shape does not match its spec is dropped, never passed through."""
    dropped = [] if dropped is None else dropped
    if spec == SCALAR:
        if _is_scalar(value):
            return value, dropped
        dropped.append(path)
        return None, dropped
    if spec == SCALARS:
        if _is_scalar(value):
            return value, dropped
        if isinstance(value, list) and all(_is_scalar(v) for v in value):
            return list(value), dropped
        dropped.append(path)
        return None, dropped
    if isinstance(spec, tuple) and spec[0] == "list":
        if not isinstance(value, list):
            if value is not None:
                dropped.append(path)
            return None, dropped
        return [sanitize(v, spec[1], f"{path}[]", dropped)[0] for v in value], dropped
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            if value is not None:
                dropped.append(path)
            return None, dropped
        out = {}
        for k, v in value.items():
            if k in spec:
                out[k] = sanitize(v, spec[k], f"{path}.{k}", dropped)[0]
            else:
                dropped.append(f"{path}.{_key_label(k)}")
        return out, dropped
    raise ValueError(f"unknown schema spec at {path}")


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON object key")   # raw bytes would hide the shadowed value
    return dict(pairs)


def select_data(parsed, keep):
    """`parsed` with `data` narrowed to the object(s) for which keep(obj) is true."""
    out = dict(parsed)
    data = parsed.get("data")
    if isinstance(data, list):
        out["data"] = [o for o in data if isinstance(o, dict) and keep(o)]
    elif isinstance(data, dict):
        out["data"] = data if keep(data) else None
    else:
        out["data"] = None
    return out


class CaptureRecorder:
    """Records every API response of one capture: stored bytes to the fragment
    store, metadata in memory until `records`. Never sees request headers, cookies
    or the CSRF token; only the response object is passed in. See RAW_KIND_*."""

    MAX_DROPPED_PATHS = 50

    def __init__(self, root=HERE, clock=utc_now):
        self.root, self.clock = root, clock
        self.started_utc = clock()
        self.capture_id = "ms-" + re.sub(r"[^0-9TZ]", "", self.started_utc.split("+")[0]) + "Z"
        self.records = []

    def record(self, method, endpoint, response, schema=None, select=None, screener_id=None, page=None):
        """Keep the response (see RAW_KIND_*); return its full parsed JSON (or None)
        for the caller. `schema` is the key allowlist for what may be stored; without
        one nothing is stored. `select` = (label, keep) marks an account-scoped
        endpoint whose `data` is narrowed before the allowlist applies."""
        body = response.body()
        try:
            parsed = json.loads(body)
        except Exception:  # noqa: BLE001 — non-JSON is recorded (hash only), not fatal
            parsed = None
        try:
            json.loads(body, object_pairs_hook=_no_duplicate_keys)
            duplicate_keys = False
        except Exception:  # noqa: BLE001
            duplicate_keys = isinstance(parsed, dict)
        label, dropped, stored = None, [], None
        if schema is None or not isinstance(parsed, dict):
            kind = RAW_KIND_NOT_STORED
        else:
            candidate = parsed
            if select is not None:
                label, keep = select
                candidate = select_data(parsed, keep)
            clean, dropped = sanitize(candidate, schema)
            if select is None and not dropped and not duplicate_keys:
                kind, stored = RAW_KIND_RAW, body
            else:
                kind = RAW_KIND_PROJECTED if select is not None else RAW_KIND_SANITIZED
                stored = json.dumps(clean, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")
        sha, rel = (None, None) if stored is None else store_raw_bytes(stored, self.root)[:2]
        headers = getattr(response, "headers", None) or {}
        dropped = sorted(set(dropped))
        self.records.append({
            "seq": len(self.records) + 1, "method": method, "endpoint": endpoint,
            "screener_id": screener_id, "page": page, "http_status": getattr(response, "status", None),
            "content_type": headers.get("content-type"), "captured_utc": self.clock(),
            "source_timestamp": parsed.get("timestamp") if isinstance(parsed, dict) else None,
            "trace_id": parsed.get("trace_id") if isinstance(parsed, dict) else None,
            "raw_kind": kind, "projection": label,
            "dropped_paths": dropped[:self.MAX_DROPPED_PATHS] + (["…"] if len(dropped) > self.MAX_DROPPED_PATHS else []),
            "body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body),
            "raw_sha256": sha, "raw_bytes": None if stored is None else len(stored), "fragment_path": rel})
        return parsed


# ── schema (additive) ─────────────────────────
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS source_field_lifecycle (
        source TEXT NOT NULL, field TEXT NOT NULL, state TEXT NOT NULL,
        effective_capture_date TEXT NOT NULL, contract_version TEXT NOT NULL,
        replacement TEXT, evidence TEXT NOT NULL, recorded_utc TEXT NOT NULL,
        acknowledged_in_contract TEXT, acknowledgement TEXT,
        PRIMARY KEY (source, field, state, effective_capture_date))""",
    """CREATE TABLE IF NOT EXISTS ms_raw_response (
        capture_id TEXT NOT NULL, seq INTEGER NOT NULL, method TEXT NOT NULL, endpoint TEXT NOT NULL,
        screener_id TEXT, page INTEGER, http_status INTEGER, content_type TEXT,
        captured_utc TEXT NOT NULL, source_timestamp TEXT, trace_id TEXT,
        raw_kind TEXT NOT NULL, projection TEXT, dropped_paths TEXT NOT NULL,
        body_sha256 TEXT NOT NULL, body_bytes INTEGER NOT NULL,
        raw_sha256 TEXT, raw_bytes INTEGER, fragment_path TEXT,
        PRIMARY KEY (capture_id, seq))""",
    """CREATE TABLE IF NOT EXISTS ms_capture_manifest (
        capture_id TEXT PRIMARY KEY, source TEXT NOT NULL, contract_version TEXT NOT NULL,
        capture_started_utc TEXT NOT NULL, capture_finished_utc TEXT NOT NULL,
        capture_date TEXT NOT NULL, session_date TEXT, session_date_status TEXT NOT NULL,
        persisted_to_market_summary_daily INTEGER NOT NULL, screener_id TEXT, universe_id TEXT,
        requested_columns TEXT NOT NULL, catalog_column_count INTEGER, catalog_columns_sha256 TEXT,
        stored_config_columns TEXT, returned_keys TEXT NOT NULL, unexpected_keys TEXT NOT NULL,
        field_presence TEXT NOT NULL, field_availability TEXT NOT NULL,
        rows INTEGER NOT NULL, pages_fetched INTEGER NOT NULL, last_page INTEGER,
        source_timestamps TEXT NOT NULL, trace_ids TEXT NOT NULL, raw_sha256s TEXT NOT NULL,
        contract_status TEXT NOT NULL, contract_issues TEXT NOT NULL, recorded_utc TEXT NOT NULL,
        tolerated_additive_keys TEXT)""",
    """CREATE TABLE IF NOT EXISTS signal_source_status (
        flag_date TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, hits INTEGER,
        capture_id TEXT, detail TEXT, recorded_by TEXT NOT NULL, recorded_utc TEXT NOT NULL,
        status_at_capture TEXT,
        PRIMARY KEY (flag_date, source))""",
]

MANIFEST_COLUMNS = (
    "capture_id", "source", "contract_version", "capture_started_utc", "capture_finished_utc",
    "capture_date", "session_date", "session_date_status", "persisted_to_market_summary_daily",
    "screener_id", "universe_id", "requested_columns", "catalog_column_count", "catalog_columns_sha256",
    "stored_config_columns", "returned_keys", "unexpected_keys", "field_presence", "field_availability",
    "rows", "pages_fetched", "last_page", "source_timestamps", "trace_ids", "raw_sha256s",
    "contract_status", "contract_issues", "recorded_utc", "tolerated_additive_keys")

# Signal-source statuses.
HITS = "HITS"
NO_HITS = "NO_HITS"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
RETIRED_SOURCE = "RETIRED_SOURCE"
EMPTY_UNVERIFIED = "EMPTY_UNVERIFIED"   # zero rows from a scraper that cannot tell "no hits" from "failed"


def ensure_schema(conn, lifecycle=None, recorded_utc=None, commit=True):
    """Create the additive tables and mirror the lifecycle registry. Idempotent;
    touches no existing table or row."""
    for ddl in SCHEMA:
        conn.execute(ddl)
    lc = FIELD_LIFECYCLE if lifecycle is None else lifecycle
    for f, entry in sorted(lc.items()):
        conn.execute(
            "INSERT OR IGNORE INTO source_field_lifecycle (source, field, state, effective_capture_date, "
            "contract_version, replacement, evidence, recorded_utc, acknowledged_in_contract, acknowledgement) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (SOURCE, f, entry["state"], entry["effective_capture_date"], CONTRACT_VERSION,
             entry.get("replacement"), entry["evidence"], recorded_utc or utc_now(),
             entry.get("acknowledged_in_contract"), entry.get("acknowledgement")))
    if commit:
        conn.commit()


def annotate_retired_signal_days(conn, lifecycle=None, recorded_utc=None, commit=True):
    """Mark signal families fed by a retired field as RETIRED_SOURCE on flag dates
    from the effective date that predate provenance recording. Metadata only; no
    konglo_signal_watch or market_summary_daily row is touched. Existing status
    rows win (INSERT OR IGNORE). Returns the (flag_date, source) pairs added.

    `status` is the CURRENT lifecycle interpretation (retired). `status_at_capture`
    keeps what the capture was under its then-active contract, which still
    requested the field: SOURCE_UNAVAILABLE when market_summary_daily shows the
    field unpopulated that day (a source-contract break), NULL when that cannot be
    established."""
    lc = FIELD_LIFECYCLE if lifecycle is None else lifecycle
    have = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='konglo_signal_watch'").fetchone()
    if not have:
        return []
    dates = [r[0] for r in conn.execute("SELECT DISTINCT flag_date FROM konglo_signal_watch ORDER BY flag_date")]
    panel = table_exists(conn, "market_summary_daily")
    added = []
    for f, entry in sorted(lc.items()):
        if entry["state"] != RETIRED:
            continue
        for signal in entry.get("signals", []):
            for d in dates:
                if d < entry["effective_capture_date"]:
                    continue
                at_capture, observed = None, "field population on that capture unknown"
                if panel:
                    n, populated = conn.execute(
                        f'SELECT COUNT(*), COUNT("{f}") FROM market_summary_daily WHERE date=?', (d,)).fetchone()
                    if n and not populated:
                        at_capture = SOURCE_UNAVAILABLE
                        observed = (f"then-active {LEGACY_CONTRACT_VERSION} requested {f}; NULL on all {n} "
                                    f"captured rows (source-contract break at capture)")
                    elif n:
                        observed = f"{f} populated on {populated}/{n} captured rows"
                cur = conn.execute(
                    "INSERT OR IGNORE INTO signal_source_status (flag_date, source, status, hits, capture_id, "
                    "detail, recorded_by, recorded_utc, status_at_capture) VALUES (?,?,?,?,?,?,?,?,?)",
                    (d, signal, RETIRED_SOURCE, None, None,
                     f"{f} RETIRED from capture regime {entry['effective_capture_date']} "
                     f"({entry.get('acknowledged_in_contract') or 'unacknowledged'}); pre-provenance capture, "
                     f"status annotated from the lifecycle registry, no value backfilled; {observed}",
                     "lifecycle_migration", recorded_utc or utc_now(), at_capture))
                if cur.rowcount:
                    added.append((d, signal))
    if commit:
        conn.commit()
    return added


# Live signal_source_status rows later established to be wrong. Each entry
# corrects one (flag_date, source) that still carries `recorded_status`; the
# row's status_at_capture keeps what the scraper recorded at the time.
SOURCE_STATUS_CORRECTIONS = [
    {"flag_date": "2026-09-17", "source": "dashboard_Bandarmologi",
     "recorded_status": EMPTY_UNVERIFIED, "status": SOURCE_UNAVAILABLE,
     "evidence": "'Top Akum Bandar' returned 7 tickers (Actions run 35169122052, 01:07:15Z) but its "
                 "response never carries m_dn_0, and the PR #45 missing-value rule dropped all of them. "
                 "Not a no-hits day; the tickers were never persisted and cannot be recovered."},
]


def apply_source_status_corrections(conn, corrections=None, recorded_utc=None, commit=True):
    """Apply SOURCE_STATUS_CORRECTIONS. Metadata only: no konglo_signal_watch or
    market_summary_daily row is touched. A row is changed only while it still says
    `recorded_status`, so this is idempotent and never overrides a later re-record.
    Returns the (flag_date, source) pairs changed."""
    cs = SOURCE_STATUS_CORRECTIONS if corrections is None else corrections
    if not table_exists(conn, "signal_source_status"):
        return []
    changed = []
    for c in cs:
        cur = conn.execute(
            "UPDATE signal_source_status SET status=?, detail=?, recorded_by=?, recorded_utc=?, "
            "status_at_capture=COALESCE(status_at_capture, ?) WHERE flag_date=? AND source=? AND status=?",
            (c["status"], f"corrected from {c['recorded_status']}: {c['evidence']}", "status_correction",
             recorded_utc or utc_now(), c["recorded_status"], c["flag_date"], c["source"], c["recorded_status"]))
        if cur.rowcount:
            changed.append((c["flag_date"], c["source"]))
    if commit:
        conn.commit()
    return changed


def _public_issue(issue):
    field_name = issue.get("field")
    if field_name is None or _key_label(field_name) == field_name:
        return issue
    return dict(issue, field=_key_label(field_name), detail="redacted: non-identifier key")


def write_capture(conn, recorder, *, capture_date, persisted, screener_id, universe_id, requested,
                  catalog_fields, stored_columns, rows, pages_fetched, last_page, contract):
    """Persist ms_raw_response rows and the ms_capture_manifest row. Append-only.
    neobdm.db is committed to a public repository too: key NAMES taken from a response
    are stored only if they are identifiers (see _key_label)."""
    for r in recorder.records:
        conn.execute(
            "INSERT INTO ms_raw_response (capture_id, seq, method, endpoint, screener_id, page, http_status, "
            "content_type, captured_utc, source_timestamp, trace_id, raw_kind, projection, dropped_paths, "
            "body_sha256, body_bytes, raw_sha256, raw_bytes, fragment_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (recorder.capture_id, r["seq"], r["method"], r["endpoint"], r["screener_id"], r["page"],
             r["http_status"], r["content_type"], r["captured_utc"], r["source_timestamp"], r["trace_id"],
             r["raw_kind"], r["projection"], json.dumps(r["dropped_paths"]), r["body_sha256"], r["body_bytes"],
             r["raw_sha256"], r["raw_bytes"], r["fragment_path"]))
    pages = [r for r in recorder.records if r["page"] is not None]
    conn.execute(
        f"INSERT INTO ms_capture_manifest ({', '.join(MANIFEST_COLUMNS)}) "
        f"VALUES ({','.join('?' * len(MANIFEST_COLUMNS))})",
        (recorder.capture_id, SOURCE, CONTRACT_VERSION, recorder.started_utc, utc_now(),
         capture_date, None, "UNKNOWN", 1 if persisted else 0, screener_id, universe_id,
         json.dumps(requested), len(catalog_fields) if catalog_fields is not None else None,
         json_sha256(sorted(catalog_fields)) if catalog_fields is not None else None,
         json.dumps([_key_label(c) for c in stored_columns]) if stored_columns is not None else None,
         json.dumps([_key_label(k) for k in contract["returned_keys"]]),
         json.dumps([_key_label(k) for k in contract["unexpected_keys"]]),
         json.dumps(contract["presence"], sort_keys=True), json.dumps(contract["availability"], sort_keys=True),
         len(rows), pages_fetched, last_page,
         json.dumps([p["source_timestamp"] for p in pages]), json.dumps([p["trace_id"] for p in pages]),
         json.dumps([r["raw_sha256"] for r in recorder.records]),
         contract["status"], json.dumps([_public_issue(i) for i in contract["issues"]]), utc_now(),
         json.dumps(contract.get("tolerated_additive_keys") or {}, sort_keys=True)))
    conn.commit()


# ── signal-source availability ────────────────
@dataclass
class SignalResult:
    source: str
    status: str
    hits: list = field(default_factory=list)
    detail: str = ""
    capture_id: str = None

    def __iter__(self):            # legacy callers iterate the hit rows
        return iter(self.hits)

    def __len__(self):
        return len(self.hits)


def record_signal_source_status(conn, flag_date, results, recorded_by="scraper"):
    """One row per signal family per flag date: HITS / NO_HITS / SOURCE_UNAVAILABLE /
    RETIRED_SOURCE / EMPTY_UNVERIFIED. Written even when there are zero hits. A live
    capture runs under the current contract, so its status at capture is its status."""
    for ddl in SCHEMA:
        conn.execute(ddl)
    now = utc_now()
    for r in results:
        conn.execute(
            "INSERT OR REPLACE INTO signal_source_status (flag_date, source, status, hits, capture_id, detail, "
            "recorded_by, recorded_utc, status_at_capture) VALUES (?,?,?,?,?,?,?,?,?)",
            (flag_date, r.source, r.status, len(r.hits), r.capture_id, r.detail, recorded_by, now, r.status))
    conn.commit()


# The combined ALL SIGNALS strategy is the union of the radar signal families. A
# retirement removes a family, so the union after it is a different strategy:
# statistics must say which version they describe instead of pooling silently.
SIGNAL_STRATEGY = "konglo_radar_all_signals"


def signal_strategy_regime(flag_date, lifecycle=None):
    """{"strategy", "since", "retired_sources"} for the combined strategy on
    `flag_date`: version 1, plus one per distinct retirement date (of a field that
    feeds a signal family) reached. Derived from the lifecycle registry only."""
    lc = FIELD_LIFECYCLE if lifecycle is None else lifecycle
    changes = sorted({e["effective_capture_date"] for e in lc.values()
                      if e["state"] == RETIRED and e.get("signals")})
    reached = [d for d in changes if flag_date >= d]
    retired = sorted({s for f, e in lc.items() if lifecycle_state(f, flag_date, lc) == RETIRED
                      for s in e.get("signals") or []})
    return {"strategy": f"{SIGNAL_STRATEGY}_v{1 + len(reached)}", "since": reached[-1] if reached else None,
            "retired_sources": retired}


def signal_status_from_rows(source, rows):
    """For scrapers that return an empty list both on failure and on a genuine empty
    screen: zero rows are EMPTY_UNVERIFIED, never NO_HITS."""
    return SignalResult(source, HITS if rows else EMPTY_UNVERIFIED, list(rows),
                        "" if rows else "scraper cannot distinguish no hits from failure")


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
