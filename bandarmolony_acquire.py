"""Operator-attended BandarmoloNY done_detail acquisition for one ticker-day.

The flow is fixed: the operator's browser access token -> one sas-token
request -> SAS validation -> one exact blob GET -> private staging ->
CaptureEnvelope -> TradeCaptureStore.ingest() -> durable acceptance ->
independent read-only verify() -> staging deletion. Nothing lists, ranges,
schedules or repeats a network request. Trade Capture v1 is used unchanged.

The access token is read once from a hidden interactive prompt. The token and
the SAS stay in process memory and travel only to their allowlisted hosts. They
are never logged, printed, persisted or placed in exception text. Python cannot
wipe string memory, so the process is kept short-lived instead.

Phase 0 confirmed the SPA orderbook-replay SAS endpoint and its nested
data.sasToken response. Live acquisition remains operator-attended, with
the browser access token entered through the hidden interactive prompt.

Legacy 14-column files carry no ticker or date. For them the requested blob
path, routed here from the validated ticker and date, is the only ticker-day
evidence. That routing is a precondition of this adapter, and capture cannot
independently prove legacy identity.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
import argparse
import getpass
import http.client
from http.cookiejar import DefaultCookiePolicy
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4
import warnings

from bandarmolony_trade_capture import (
    DEFAULT_DB, SIDECARS, PendingCaptureError, TradeCaptureStore, check_private_output,
)
from bandarmolony_trade_contract import (
    JAKARTA_TIMEZONE, RECENT_SCHEMA_VERSION, CaptureEnvelope, TradeContractError,
    canonical_json, sha256_bytes, utc_text,
)


# Audited 2026-10-03: done_detail/{YYYYMMDD}/STOCK/{TICKER}.parquet in this container.
BLOB_HOST = "storagebandarmolony.blob.core.windows.net"
BLOB_CONTAINER = "trading-data-v2"
CREATED_BY = "bandarmolony-acquisition-v1"
MAX_BLOB_BYTES = 256 * 1024 * 1024
MAX_SMALL_BODY = 64 * 1024
SAS_MIN_REMAINING = timedelta(seconds=60)
TIMEOUT = (10, 120)
ATTEMPT_FORMAT = "BANDARMOLONY_ACQUISITION_ATTEMPT_V1"
CLEANUP_SUFFIX = ".cleanup"
EXIT_CODES = {"ACCEPTED": 0, "ABSENCE_NOT_RECORDED": 3, "REJECTED": 4,
              "UNRESOLVED": 5, "INTEGRITY_MISMATCH": 6}
# Standard Azure SAS signature fields. Anything else is refused, notably
# response-header overrides (rsce, rsct) and listing parameters (comp, restype).
SAS_FIELDS = frozenset({
    "sv", "ss", "srt", "sr", "sp", "st", "se", "sip", "spr", "sig", "si", "sdd", "ses",
    "skoid", "sktid", "skt", "ske", "sks", "skv", "saoid", "suoid", "scid",
})
_HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)+")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_TICKER = re.compile(r"[A-Z][A-Z0-9]{1,11}")
_SAS_TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:T([0-9]{2}:[0-9]{2}(?::[0-9]{2})?)(?:\.[0-9]{1,7})?Z)?")
_ALLOWLISTED_HEADERS = {"last-modified": "last_modified", "x-ms-creation-time": "x_ms_creation_time",
                        "x-ms-request-id": "x_ms_request_id"}
_PATH_ASSIGNMENT = re.compile(
    r"(?:^|[^a-z0-9])(?:sig|sv|api_key|apikey|token|access_token|refresh_token|"
    r"authorization|password|passwd|secret|sas|bearer|session|cookie|supabase|"
    r"credential|username)\s*=", re.IGNORECASE)


class AcquisitionError(Exception):
    """A fixed, input-free failure. Never chained to a transport or parser error."""


class _SingleValue(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_single_value_options", set())
        if self.dest in seen:
            parser.error("repeated single-value option")
        seen.add(self.dest)
        setattr(namespace, "_single_value_options", seen)
        setattr(namespace, self.dest, values)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Refuse ambiguous options without echoing supplied arguments."""

    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def add_argument(self, *args, **kwargs):
        if args and args[0].startswith("--") and "action" not in kwargs:
            kwargs["action"] = _SingleValue
        return super().add_argument(*args, **kwargs)

    def error(self, message):
        self.print_usage()
        self.exit(2, "invalid command arguments; use --help\n")


def _fsync_dir(path):
    """Persist directory entries where Python supports directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_mkdir(path):
    """Create private parents and persist each new directory entry."""
    missing = []
    current = Path(path)
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not directory.is_dir():
            raise AcquisitionError("output parent is not a directory")
        _fsync_dir(directory.parent)


@dataclass(frozen=True)
class SasEndpoint:
    """The SPA sas-token request. Only Phase-0 browser discovery may confirm it."""
    host: str
    path: str
    json_field: str
    phase0_confirmed: bool = False

    def __post_init__(self):
        valid = (isinstance(self.host, str) and _HOST.fullmatch(self.host) is not None
                 and self.host != BLOB_HOST
                 and isinstance(self.path, str) and _PATH.fullmatch(self.path) is not None
                 and not {".", ".."} & set(self.path.split("/"))
                 and isinstance(self.json_field, str) and _FIELD.fullmatch(self.json_field) is not None
                 and type(self.phase0_confirmed) is bool)
        if not valid:
            raise AcquisitionError("SAS endpoint configuration is invalid")

    @property
    def url(self):
        return f"https://{self.host}{self.path}"


# Confirmed by production SPA discovery on 2026-10-06; see issue #82.
LIVE_SAS_ENDPOINT = SasEndpoint(
    host="bandarmolony.com",
    path="/api/api/orderbook-replay/sas-token",
    json_field="sasToken",
    phase0_confirmed=True,
)


class Secret:
    """Bearer material that renders as <redacted> and refuses serialization."""
    __slots__ = ("_value",)

    def __init__(self, value):
        if (not isinstance(value, str) or not 0 < len(value) <= 16384
                or any(char.isspace() or not char.isprintable() for char in value)):
            raise AcquisitionError("secret input is malformed")
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("secrets cannot be serialized or copied")


@dataclass(frozen=True)
class BlobLocator:
    """The only blob this adapter requests, routed from validated identity."""
    ticker: str
    trade_date: str

    def __post_init__(self):
        valid = isinstance(self.ticker, str) and _TICKER.fullmatch(self.ticker) is not None
        try:
            valid = valid and date.fromisoformat(self.trade_date).isoformat() == self.trade_date
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise AcquisitionError("ticker or trade date is invalid")

    @property
    def clean_url(self):
        day = self.trade_date.replace("-", "")
        return f"https://{BLOB_HOST}/{BLOB_CONTAINER}/done_detail/{day}/STOCK/{self.ticker}.parquet"


@dataclass(frozen=True, repr=False)
class BlobSas:
    query: Secret
    expires_at: datetime

    def __repr__(self):
        return "<BlobSas redacted>"


def _sas_time(value):
    match = _SAS_TIME.fullmatch(value)
    if match is None:
        return None
    stamp = None
    try:
        text = match[1] + "T" + (match[2] or "00:00:00")
        stamp = datetime.fromisoformat(text + (":00" if text.count(":") == 1 else "")).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return stamp


def _sas_facts(value, now):
    if (not isinstance(value, str) or not 0 < len(value) <= 8192
            or any(char.isspace() or not char.isprintable() for char in value)):
        return "SAS is malformed", None, None
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme.lower() != "https":
            return "SAS must use HTTPS", None, None
        if (parts.username is not None or parts.password is not None
                or parts.hostname != BLOB_HOST or parts.port not in (None, 443)):
            return "SAS host is not the allowlisted blob host", None, None
        if parts.path.rstrip("/") != "/" + BLOB_CONTAINER or parts.fragment:
            return "SAS is not scoped to the allowlisted container", None, None
        query = parts.query
    else:
        query = value[1:] if value.startswith("?") else value
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    names = [name for name, _ in pairs]
    if len(names) != len(set(names)) or not set(names) <= SAS_FIELDS:
        return "SAS has unexpected or repeated parameters", None, None
    facts = dict(pairs)
    if not facts.get("sv") or not facts.get("sig"):
        return "SAS is incomplete", None, None
    if "spr" in facts and facts["spr"] != "https":
        return "SAS must be restricted to HTTPS", None, None
    if not re.fullmatch(r"[a-z]+", facts.get("sp", "")) or "r" not in facts["sp"]:
        return "SAS lacks read permission", None, None
    expires = _sas_time(facts.get("se", ""))
    if expires is None:
        return "SAS expiry is invalid", None, None
    if expires <= now + SAS_MIN_REMAINING:
        return "SAS is expired or about to expire", None, None
    return None, query, expires


def validate_sas(value, now):
    """Accept SAS text, with any supplied URL pinned to the account/container.

    The original query text is kept verbatim: re-encoding could alter the
    signed fields. Validation reads a parsed copy only. Phase-0 discovery
    observed container-scoped SAS query text from the production SPA.
    """
    problem, query, expires = "SAS is malformed", None, None
    try:
        problem, query, expires = _sas_facts(value, now)
    except (TypeError, ValueError, UnicodeError):
        pass
    if problem is not None:
        raise AcquisitionError(problem)
    return BlobSas(Secret(query), expires)


class RequestsTransport:
    """HTTPS GETs that verify TLS, never redirect, keep no cookies and ignore
    proxy, netrc and CA-bundle environment settings."""

    def __init__(self, timeout=TIMEOUT):
        import requests

        # urllib3 logs each request line, signed query included, at DEBUG, and
        # http.client debug output would print request headers to stdout.
        for name in ("urllib3", "urllib3.connectionpool"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        http.client.HTTPConnection.debuglevel = 0
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        self._timeout = timeout

    def get(self, url, headers):
        return _RequestsResponse(self._session.get(
            url, headers=dict(headers), stream=True, allow_redirects=False,
            verify=True, timeout=self._timeout))

    def close(self):
        self._session.close()


class _RequestsResponse:
    def __init__(self, response):
        self._response = response
        self.status = response.status_code
        self.headers = response.headers

    def chunks(self):
        return self._response.iter_content(chunk_size=65536)

    def close(self):
        self._response.close()


def _dispatch(transport, url, headers):
    """Send one GET. Transport errors can quote the signed URL; drop them."""
    response = None
    try:
        response = transport.get(url, headers)
    except Exception:
        pass
    if response is None:
        raise AcquisitionError("HTTPS request failed before a response")
    return response


def _close(response):
    try:
        response.close()
    except Exception:
        pass


def _status(response):
    status = getattr(response, "status", None)
    if type(status) is not int or not 100 <= status <= 599:
        raise AcquisitionError("response status is malformed")
    return status


def _headers(response):
    items = None
    try:
        items = list(response.headers.items())
    except Exception:
        pass
    if items is None or not all(isinstance(name, str) and isinstance(value, str) for name, value in items):
        raise AcquisitionError("response headers are malformed")
    return {name.lower(): value for name, value in items}


def _status_problem(what, status):
    if 300 <= status < 400:
        return f"{what} redirect refused (HTTP {status})"
    if status in (401, 403):
        return f"{what} was not authorized (HTTP {status})"
    if status == 429:
        return f"{what} was rate limited (HTTP 429)"
    return f"{what} failed (HTTP {status})"


def _read_body(response, limit, *, decoded=False):
    """Read the complete body under a hard cap.

    Content-Length is compared with the received bytes unless the transport
    decoded a Content-Encoding, which only the small SAS response allows.
    """
    declared = _headers(response).get("content-length")
    if declared is not None and not re.fullmatch(r"[0-9]{1,15}", declared):
        raise AcquisitionError("Content-Length is malformed")
    if declared is not None and not decoded and int(declared) > limit:
        raise AcquisitionError("response exceeds the size cap")
    chunks = None
    try:
        chunks = iter(response.chunks())
    except Exception:
        pass
    if chunks is None:
        raise AcquisitionError("response body ended before completion")
    body = bytearray()
    while True:
        chunk, failed = None, False
        try:
            chunk = next(chunks)
        except StopIteration:
            break
        except Exception:
            failed = True
        if failed or not isinstance(chunk, (bytes, bytearray)):
            raise AcquisitionError("response body ended before completion")
        body += chunk
        if len(body) > limit:
            raise AcquisitionError("response exceeds the size cap")
    if declared is not None and not decoded and len(body) != int(declared):
        raise AcquisitionError("response length differs from Content-Length")
    return bytes(body)


def fetch_sas(transport, endpoint, token, clock):
    """Exchange the access token for one SAS at the pinned endpoint."""
    if not isinstance(endpoint, SasEndpoint) or not isinstance(token, Secret):
        raise AcquisitionError("SAS request requires a confirmed endpoint and a token")
    headers = {"Authorization": "Bearer " + token.reveal(), "Accept": "application/json",
               "Accept-Encoding": "identity"}
    response = _dispatch(transport, endpoint.url, headers)
    try:
        status = _status(response)
        if status != 200:
            raise AcquisitionError(_status_problem("SAS request", status))
        decoded = "content-encoding" in _headers(response)
        body = _read_body(response, MAX_SMALL_BODY, decoded=decoded)
    finally:
        _close(response)
    document = None
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        pass
    data = document.get("data") if isinstance(document, dict) else None
    value = data.get(endpoint.json_field) if isinstance(data, dict) else None
    if not isinstance(value, str):
        raise AcquisitionError("SAS response does not match the pinned shape")
    return validate_sas(value, clock())


@dataclass(frozen=True)
class BlobObservation:
    http_status: int
    requested_at: str
    response_at: str
    last_modified: str | None
    x_ms_creation_time: str | None
    x_ms_request_id: str | None
    body: bytes | None = field(default=None, repr=False)


def fetch_blob(transport, locator, sas, clock, *, max_bytes=MAX_BLOB_BYTES):
    """GET the exact blob once, without Authorization, and observe it completely.

    requested_at is read immediately before dispatch. response_at is read after
    the complete 200 body, or the complete 404 response, has been received.
    """
    url = locator.clean_url + "?" + sas.query.reveal()
    requested_at = clock()
    response = _dispatch(transport, url, {"Accept-Encoding": "identity"})
    try:
        status = _status(response)
        headers = _headers(response)
        if status == 200:
            if "content-encoding" in headers:
                raise AcquisitionError("Content-Encoding is refused; exact stored bytes are required")
            body = _read_body(response, max_bytes)
            if not body:
                raise AcquisitionError("HTTP 200 body is empty")
        elif status == 404:
            if headers.get("x-ms-error-code") != "BlobNotFound":
                raise AcquisitionError("HTTP 404 without BlobNotFound is not an absence observation")
            _read_body(response, MAX_SMALL_BODY)
            body = None
        else:
            raise AcquisitionError(_status_problem("blob request", status))
        response_at = clock()
    finally:
        _close(response)
    facts = {}
    for header, name in _ALLOWLISTED_HEADERS.items():
        value = headers.get(header)
        if value is not None and (len(value) > 64 or not value.isascii() or not value.isprintable()):
            raise AcquisitionError("source header is malformed")
        facts[name] = value
    return BlobObservation(status, utc_text(requested_at), utc_text(response_at), body=body, **facts)


@dataclass(frozen=True)
class _Paths:
    db: Path
    raw_root: Path
    staging: Path
    lock: Path


def _paths(db, raw_root, staging_root):
    # Validate both operator spelling and resolved destinations. No path may
    # create artifacts or dispatch a request before every destination passes.
    db_path = _output_path(db)
    raw_path = None if raw_root is None else _output_path(raw_root)
    staging_path = None if staging_root is None else _output_path(staging_root)
    resolved = None
    try:
        db = db_path.resolve()
        raw = raw_path.resolve() if raw_path is not None else db.parent / "trade_raw"
        staging = staging_path.resolve() if staging_path is not None else db.parent / "acquisition_staging"
        resolved = db, raw, staging
    except (OSError, RuntimeError, ValueError):
        pass
    if resolved is None:
        raise AcquisitionError("output path is invalid")
    db, raw, staging = resolved
    lock = _output_path(str(db) + ".acquisition-lock")
    for path in (db, raw, staging, lock):
        _output_path(path)
    if db == raw or raw in db.parents:
        raise AcquisitionError("database must be separate from the raw object store")
    for inner, outer in ((staging, raw), (raw, staging), (db, staging)):
        if inner == outer or outer in inner.parents:
            raise AcquisitionError("staging must be separate from the trade store")
    check_private_output(db, SIDECARS)
    check_private_output(raw)
    check_private_output(raw / ".private-output-probe")
    check_private_output(staging)
    check_private_output(lock, SIDECARS)
    return _Paths(db, raw, staging, lock)


def _output_path(value):
    text, path = None, None
    try:
        text = os.fspath(value)
        if isinstance(text, str):
            path = Path(text)
    except (TypeError, ValueError, OSError):
        pass
    if not isinstance(text, str) or _PATH_ASSIGNMENT.search(text):
        raise AcquisitionError("output paths cannot contain credential assignments")
    if path is None:
        raise AcquisitionError("output path is invalid")
    return path


@contextmanager
def _exclusive(lock):
    """Serialize acquisition windows per store. A held lock refuses; it never waits."""
    _private_mkdir(lock.parent)
    connection = sqlite3.connect(lock, timeout=0, isolation_level=None)
    try:
        locked = False
        try:
            connection.execute("BEGIN EXCLUSIVE")
            locked = True
        except sqlite3.OperationalError:
            pass
        if not locked:
            raise AcquisitionError("another acquisition holds this store's lock")
        try:
            yield
        finally:
            connection.execute("ROLLBACK")
    finally:
        connection.close()


@dataclass(frozen=True)
class StagedAttempt:
    directory: Path
    envelope: CaptureEnvelope
    raw_sha256: str | None
    content_length: int | None

    @property
    def body(self):
        return self.directory / "body.parquet"


def _is_real_directory(path):
    """Inspect the entry itself; Windows junctions can have directory mode."""
    metadata = None
    try:
        metadata = path.lstat()
    except OSError:
        pass
    return (metadata is not None and stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and not (getattr(metadata, "st_file_attributes", 0)
                     & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _stage(staging, envelope, body):
    """Write <capture_id>.partial completely, then rename it into place.

    The attempt record holds only CaptureEnvelope fields, which are exactly
    what Trade Capture persists, plus the received hash and length.
    """
    if envelope.http_status == 200 and not body:
        raise AcquisitionError("HTTP 200 body is empty")
    digest = None if body is None else sha256_bytes(body)
    record = {"format": ATTEMPT_FORMAT, "envelope": asdict(envelope), "raw_sha256": digest,
              "content_length": None if body is None else len(body)}
    _private_mkdir(staging)
    partial = staging / (envelope.capture_id + ".partial")
    partial.mkdir(mode=0o700)
    if not _is_real_directory(partial):
        raise AcquisitionError("staged attempt directory is invalid")
    if body is not None:
        _write_new(partial / "body.parquet", body)
        if sha256_bytes((partial / "body.parquet").read_bytes()) != digest:
            raise AcquisitionError("staged bytes differ from received bytes")
    _write_new(partial / "attempt.json", canonical_json(record).encode("utf-8"))
    _fsync_dir(partial)
    final = staging / envelope.capture_id
    if not _is_real_directory(partial):
        raise AcquisitionError("staged attempt directory is invalid")
    os.replace(partial, final)
    _fsync_dir(staging)
    return StagedAttempt(final, envelope, digest, record["content_length"])


def _load_attempt(directory):
    if not _is_real_directory(directory):
        raise AcquisitionError("staged attempt directory is invalid")
    record = None
    try:
        record = json.loads((directory / "attempt.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        pass
    envelope = None
    if (isinstance(record, dict) and set(record) == {"format", "envelope", "raw_sha256", "content_length"}
            and record["format"] == ATTEMPT_FORMAT and isinstance(record["envelope"], dict)):
        try:
            envelope = CaptureEnvelope(**record["envelope"])
        except (TypeError, TradeContractError):
            pass
    if (envelope is None or canonical_json(asdict(envelope)) != canonical_json(record["envelope"])
            or envelope.capture_id != directory.name or envelope.created_by != CREATED_BY):
        raise AcquisitionError("staged attempt record is invalid")
    digest, length = record["raw_sha256"], record["content_length"]
    if envelope.http_status == 200:
        valid = (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
                 and type(length) is int and length > 0)
    else:
        valid = digest is None and length is None
    if not valid:
        raise AcquisitionError("staged attempt record is invalid")
    return StagedAttempt(directory, envelope, digest, length)


def _staged_attempts(staging):
    if not _is_real_directory(staging):
        return []
    return [_load_attempt(path) for path in sorted(staging.iterdir())
            if not path.name.endswith(".partial") and not _is_cleanup_name(path.name)
            and _is_real_directory(path)]


def _is_cleanup_name(name):
    if not name.endswith(CLEANUP_SUFFIX):
        return False
    identifier = name[:-len(CLEANUP_SUFFIX)]
    try:
        return str(UUID(identifier)) == identifier
    except ValueError:
        return False


def _cleanup_tombstone(directory):
    """Delete only the two reserved files in a real, non-reparse directory."""
    if not _is_cleanup_name(directory.name) or not _is_real_directory(directory):
        raise AcquisitionError("terminal cleanup directory is invalid")
    for child in sorted(directory.iterdir()):
        if child.name not in {"body.parquet", "attempt.json"}:
            raise AcquisitionError("terminal cleanup contains unexpected entries")
        mode = child.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise AcquisitionError("terminal cleanup contains unexpected entries")
    for name in ("body.parquet", "attempt.json"):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
    directory.rmdir()
    _fsync_dir(directory.parent)


def _remove_cleanup_tombstones(staging):
    """Best-effort startup cleanup; residue never participates in discovery."""
    if not _is_real_directory(staging):
        return
    try:
        directories = sorted(staging.iterdir())
    except OSError:
        return
    for directory in directories:
        if _is_cleanup_name(directory.name):
            try:
                _cleanup_tombstone(directory)
            except (OSError, AcquisitionError):
                pass


def _remove_partials(staging):
    """A .partial directory was never renamed, so it was never handed off."""
    if _is_real_directory(staging):
        for partial in staging.glob("*.partial"):
            if not _is_real_directory(partial):
                continue
            for child in partial.iterdir():
                child.unlink()
            partial.rmdir()


def _discard(attempt):
    # Once renamed, interrupted deletion cannot leave a malformed active attempt.
    tombstone = attempt.directory.with_name(attempt.directory.name + CLEANUP_SUFFIX)
    if not _is_cleanup_name(tombstone.name) or not _is_real_directory(attempt.directory):
        raise AcquisitionError("terminal cleanup directory is invalid")
    os.replace(attempt.directory, tombstone)
    _fsync_dir(tombstone.parent)
    _cleanup_tombstone(tombstone)


def _record(store, attempt):
    """Ingest or observe once. A pending predecessor is resumed, then retried once."""
    envelope = attempt.envelope
    for _ in range(2):
        predecessor = None
        try:
            if envelope.http_status == 200:
                return store.ingest(attempt.body, envelope)
            return store.observe_absence(envelope)
        except PendingCaptureError as pending:
            predecessor = pending.capture_id
        store.resume(predecessor)
    raise AcquisitionError("observation chain stayed pending after recovery")


def _reconcile(attempt, paths):
    """After any writer failure, commit is unknown until the store answers.

    COMMITTED: public resume() made or confirmed durable acceptance. Every
    other failure is ambiguous and keeps the attempt as UNRESOLVED, including
    parser failures that could succeed after an environment repair.
    """
    capture_id = attempt.envelope.capture_id
    try:
        with TradeCaptureStore(paths.db, paths.raw_root) as store:
            store.resume(capture_id)
        return "COMMITTED"
    except Exception:
        pass
    return "UNRESOLVED"


def _read_only_verify(db, raw_root, capture_id):
    with TradeCaptureStore(db, raw_root, read_only=True) as reader:
        return reader.verify(capture_id)


def _independent_verify(attempt, paths):
    """A fresh read-only store must reproduce the staged identity and bytes."""
    metadata = _read_only_verify(paths.db, paths.raw_root, attempt.envelope.capture_id)
    expected = dict(asdict(attempt.envelope), raw_response_sha256=attempt.raw_sha256,
                    content_length=attempt.content_length)
    matches = (all(metadata.get(key) == value for key, value in expected.items())
               and metadata.get("durable_accepted_at") is not None)
    return metadata, matches


_RESULT_KEYS = (
    "outcome", "capture_id", "ticker", "trade_date", "http_status", "requested_at", "response_at",
    "last_modified", "observation_state", "observation_seq", "content_version", "schema_version",
    "ticker_date_evidence", "row_count", "content_length", "raw_response_sha256_prefix",
    "durable_accepted_at", "staging_preserved",
)


def _result(outcome, *, envelope=None, digest=None, length=None, metadata=None, staged=False, **facts):
    result = dict.fromkeys(_RESULT_KEYS)
    if envelope is not None:
        result.update({key: getattr(envelope, key) for key in (
            "capture_id", "ticker", "trade_date", "http_status", "requested_at", "response_at", "last_modified")})
    result.update(facts)
    if metadata is not None:
        result.update({key: metadata.get(key) for key in (
            "observation_state", "observation_seq", "content_version", "schema_version",
            "row_count", "durable_accepted_at")})
    schema = result["schema_version"]
    result.update(
        outcome=outcome, staging_preserved=staged, content_length=length,
        raw_response_sha256_prefix=None if digest is None else digest[:12],
        ticker_date_evidence=(None if schema is None else "SOURCE_COLUMNS_AND_REQUESTED_PATH"
                              if schema == RECENT_SCHEMA_VERSION else "REQUESTED_PATH_ONLY"))
    return result


def _handoff(attempt, paths):
    """Record a staged attempt, then acknowledge it by independent verification.

    Staging leaves the active namespace only after a fresh read-only verify()
    matches the attempt (ACCEPTED). Every other outcome keeps its capture_id for
    `retry`, which reuses the same bytes and envelope.
    """
    if not _is_real_directory(attempt.directory):
        raise AcquisitionError("staged attempt directory is invalid")
    envelope = attempt.envelope
    facts = dict(envelope=envelope, digest=attempt.raw_sha256, length=attempt.content_length)
    # Only an acknowledged 200 attempt has lost its body: verify it from the record.
    present = envelope.http_status == 404 or attempt.body.is_file()
    if envelope.http_status == 200 and present and sha256_bytes(attempt.body.read_bytes()) != attempt.raw_sha256:
        raise AcquisitionError("staged bytes differ from the attempt record")
    if present:
        recorded = False
        try:
            with TradeCaptureStore(paths.db, paths.raw_root) as store:
                _record(store, attempt)
            recorded = True
        except Exception:
            pass
        if not recorded:
            state = _reconcile(attempt, paths)
            if state != "COMMITTED":
                return _result("UNRESOLVED", staged=True, **facts)
    # The writer is closed. Only a fresh read-only store acknowledges the attempt.
    verified = None
    try:
        verified = _independent_verify(attempt, paths)
    except Exception:
        pass
    if verified is None:
        return _result("UNRESOLVED", staged=True, **facts)
    metadata, matches = verified
    if not matches:
        return _result("INTEGRITY_MISMATCH", staged=True, **facts)
    _discard(attempt)
    return _result("ACCEPTED", metadata=metadata, **facts)


def _utc_clock():
    return datetime.now(timezone.utc)


def acquire_one(ticker, trade_date, *, token, endpoint, transport, db=DEFAULT_DB, raw_root=None,
                staging_root=None, clock=None, record_absence=False, max_bytes=MAX_BLOB_BYTES):
    """Acquire one ticker-day: one SAS, one exact blob GET, one capture handoff.

    Errors before staging raise AcquisitionError or TradeContractError and
    record nothing. After staging, the result's outcome is ACCEPTED,
    UNRESOLVED or INTEGRITY_MISMATCH. A 404 is recorded only with
    record_absence=True and otherwise reports ABSENCE_NOT_RECORDED.
    """
    clock = _utc_clock if clock is None else clock
    if not isinstance(endpoint, SasEndpoint) or not endpoint.phase0_confirmed:
        raise AcquisitionError("SAS endpoint is not confirmed by Phase-0 browser discovery")
    locator = BlobLocator(ticker, trade_date)
    if date.fromisoformat(trade_date) > clock().astimezone(JAKARTA_TIMEZONE).date():
        raise AcquisitionError("trade date is in the future")
    paths = _paths(db, raw_root, staging_root)
    with _exclusive(paths.lock):
        _remove_cleanup_tombstones(paths.staging)
        _remove_partials(paths.staging)
        if any((attempt.envelope.ticker, attempt.envelope.trade_date) == (ticker, trade_date)
               for attempt in _staged_attempts(paths.staging)):
            raise AcquisitionError("a staged attempt for this ticker-day is unresolved; run retry first")
        sas = fetch_sas(transport, endpoint, token, clock)
        observation = fetch_blob(transport, locator, sas, clock, max_bytes=max_bytes)
        if observation.http_status == 404 and not record_absence:
            return _result("ABSENCE_NOT_RECORDED", ticker=ticker, trade_date=trade_date, http_status=404,
                           requested_at=observation.requested_at, response_at=observation.response_at)
        envelope = CaptureEnvelope(
            ticker=ticker, trade_date=trade_date, capture_id=str(uuid4()),
            requested_at=observation.requested_at, response_at=observation.response_at,
            http_status=observation.http_status, last_modified=observation.last_modified,
            x_ms_creation_time=observation.x_ms_creation_time, x_ms_request_id=observation.x_ms_request_id,
            source_path_without_query_or_token=locator.clean_url, created_by=CREATED_BY)
        return _handoff(_stage(paths.staging, envelope, observation.body), paths)


def retry(capture_id, *, db=DEFAULT_DB, raw_root=None, staging_root=None):
    """Re-run the handoff of one staged attempt with its original identity. Offline."""
    valid = False
    try:
        valid = isinstance(capture_id, str) and str(UUID(capture_id)) == capture_id
    except ValueError:
        pass
    if not valid:
        raise AcquisitionError("capture ID is not a staged attempt identifier")
    paths = _paths(db, raw_root, staging_root)
    with _exclusive(paths.lock):
        _remove_cleanup_tombstones(paths.staging)
        directory = paths.staging / capture_id
        if not _is_real_directory(directory):
            raise AcquisitionError("no staged attempt has this capture ID")
        return _handoff(_load_attempt(directory), paths)


def _read_token():
    interactive = False
    try:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, OSError, ValueError):
        pass
    if not interactive:
        raise AcquisitionError("the access token must be typed at an interactive terminal")
    value = None
    with warnings.catch_warnings():
        # getpass warns, then echoes, when it cannot hide input. Refuse instead.
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass("BandarmoloNY access token (input hidden): ")
        except getpass.GetPassWarning:
            pass
    if value is None:
        raise AcquisitionError("hidden token input is unavailable on this terminal")
    return Secret(value.strip())


def main(argv=None, *, transport_factory=None):
    parser = _SafeArgumentParser(description="Acquire one BandarmoloNY done_detail ticker-day.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_SafeArgumentParser)
    live = sub.add_parser("acquire", help="acquire one ticker-day; prompts for the access token")
    live.add_argument("--ticker", required=True)
    live.add_argument("--trade-date", required=True)
    live.add_argument("--record-absence", action="store_true")
    again = sub.add_parser("retry", help="re-run the handoff of one staged attempt, offline")
    again.add_argument("--capture-id", required=True)
    for child in (live, again):
        child.add_argument("--db", default=str(DEFAULT_DB))
        child.add_argument("--raw-root")
        child.add_argument("--staging-dir")
    args = parser.parse_args(argv)
    token = endpoint = None
    if args.command == "acquire":
        endpoint = LIVE_SAS_ENDPOINT
        if not isinstance(endpoint, SasEndpoint) or not endpoint.phase0_confirmed:
            print("live acquisition is disabled until Phase-0 discovery confirms the SAS endpoint",
                  file=sys.stderr)
            return 2
        message = None
        try:
            token = _read_token()
        except AcquisitionError as refusal:
            message = str(refusal)
        if token is None:
            print(message, file=sys.stderr)
            return 2
    result, message = None, "acquisition failed"
    try:
        if args.command == "acquire":
            transport = (RequestsTransport if transport_factory is None else transport_factory)()
            try:
                result = acquire_one(
                    args.ticker, args.trade_date, token=token, endpoint=endpoint, transport=transport,
                    db=args.db, raw_root=args.raw_root, staging_root=args.staging_dir,
                    clock=_utc_clock, record_absence=args.record_absence)
            finally:
                transport.close()
        else:
            result = retry(args.capture_id, db=args.db, raw_root=args.raw_root, staging_root=args.staging_dir)
    except (AcquisitionError, TradeContractError) as error:
        message = str(error)
    except KeyboardInterrupt:
        message = "cancelled; any staged attempt is kept for retry"
    except Exception:
        pass
    if result is None:
        print(message, file=sys.stderr)
        return 1
    print(canonical_json(result))
    return EXIT_CODES[result["outcome"]]


if __name__ == "__main__":
    raise SystemExit(main())
