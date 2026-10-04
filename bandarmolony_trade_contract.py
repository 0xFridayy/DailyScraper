"""Offline BandarmoloNY done_detail schemas and exact trade normalization.

Each source row is an executed trade. Quantity is shares and money is integer
rupiah. The supported Parquet representations are explicit in
``SUPPORTED_PARQUET_REPRESENTATIONS``; unknown layouts or types fail closed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import struct
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4


DATASET = "BANDARMOLONY_DONE_DETAIL"
NORMALIZED_SCHEMA_VERSION = "TRADE_TAPE_V1"
RECENT_SCHEMA_VERSION = "RECENT_16COL"
LEGACY_SCHEMA_VERSION = "LEGACY_2025_14COL"
OBSERVATION_STATES = frozenset(
    {"CONTENT_FIRST_SEEN", "CONTENT_CHANGED", "CONTENT_REPEAT", "ABSENT_OBSERVED"}
)
JAKARTA_TIMEZONE = timezone(timedelta(hours=7), "Asia/Jakarta")

RECENT_COLUMNS = frozenset(
    {
        "TRX_CODE", "TRX_SESS", "TRX_TYPE", "BRK_COD1", "INV_TYP1",
        "BRK_COD2", "INV_TYP2", "STK_CODE", "STK_VOLM", "STK_PRIC",
        "TRX_DATE", "TRX_ORD1", "TRX_ORD2", "TRX_TIME", "HAKA_HAKI", "VALUE",
    }
)
LEGACY_COLUMNS = RECENT_COLUMNS - {"STK_CODE", "TRX_DATE"}

# These are supported parser profiles, not a claim that every representation
# was present in the source audit. Nested/repeated fields and unsigned integer
# annotations are deliberately unsupported. Optional fields must contain no
# nulls. Parquet compression and encoding do not change the semantic schema.
SUPPORTED_PARQUET_REPRESENTATIONS = {
    "integers": "INT32/INT64, no logical annotation or matching signed Int",
    "text": "BYTE_ARRAY with String/UTF8 annotation",
    "codes": "text or supported integers for session/investor/vendor codes",
    "trade_date": "INT32 Date, supported integer YYYYMMDD, or ISO date text",
    "trade_time": "supported integer HHMMSS or HHMMSS/HH:MM:SS text",
    "source_value": (
        "supported integer; Decimal on INT32/INT64/BYTE_ARRAY/"
        "FIXED_LEN_BYTE_ARRAY, precision 1..76, scale 0..precision; "
        "unannotated DOUBLE via its shortest decimal roundtrip, with exactly "
        "one integer rupiah compatible with its IEEE-754 rounding interval"
    ),
    "legacy_board": "unannotated or signed Int32 INT32, value exactly zero",
}


class TradeContractError(ValueError):
    """A safe contract rejection, containing no source payload or credential."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    """Canonical UTF-8 JSON text. Canonical trade money is always integer."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError):
        raise TradeContractError("value cannot be represented canonically") from None


def utc_text(value: str | datetime) -> str:
    """Preserve microseconds while converting an aware ISO instant to UTC."""
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, datetime):
            parsed = value
        else:
            raise ValueError
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        raise TradeContractError("timestamp must be a timezone-aware ISO instant") from None


def _header_time(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return utc_text(value)
    except TradeContractError:
        try:
            return utc_text(parsedate_to_datetime(value))
        except (TradeContractError, ValueError, TypeError, OverflowError):
            raise TradeContractError("header timestamp is invalid") from None


_CREDENTIAL_WORD = re.compile(
    r"(?:^|[^a-z0-9])(?:bearer|token|sas|sig|session|cookie|password|"
    r"authorization|supabase|secret|credential|username)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_JWT = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_LONG_OPAQUE = re.compile(r"[A-Za-z0-9_+=-]{40,}")


def _safe_component(value: str) -> None:
    decoded = value
    for _ in range(4):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    if (
        any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        or _CREDENTIAL_WORD.search(decoded)
        or _JWT.search(decoded)
        or _LONG_OPAQUE.search(decoded)
        or "@" in decoded
        or "%" in decoded
        or ";" in decoded
    ):
        raise TradeContractError("source provenance contains an unsafe component")


def sanitize_source_path(value: str) -> str:
    """Remove queries and URL fragment/userinfo before provenance persistence.

    Windows separators normalize to '/', independently of the running OS.
    A local '#' is a literal filename character. Extended/device Windows paths
    and decoded matrix parameters are refused rather than partially parsed.
    Credential names, JWTs and long opaque path components are rejected, even
    when percent encoded. Rejection messages never include the input.
    """
    if not isinstance(value, str) or len(value) > 4096:
        raise TradeContractError("source provenance path is invalid")
    if not value:
        return ""
    # Raise after leaving the parser's exception handler. ``from None`` alone
    # hides a traceback but retains __context__, which may contain a secret.
    result = None
    try:
        normalized = value.replace("\\", "/")
        if normalized.startswith(("//?/", "//./")):
            raise ValueError
        if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError
        is_windows = bool(re.match(r"^[A-Za-z]:/", normalized))
        scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", normalized)
        if not is_windows and scheme:
            # Backslashes or missing '//' in a URI are ambiguous, not repairs.
            if "\\" in value or "://" not in value:
                raise ValueError
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in {"http", "https", "file"}:
                raise ValueError
            host = parsed.hostname or ""
            if parsed.scheme.lower() in {"http", "https"} and not host:
                raise ValueError
            if parsed.scheme.lower() == "file" and (not parsed.path.startswith("/") or parsed.username):
                raise ValueError
            if any(char.isspace() for char in host) or parsed.netloc.endswith(":"):
                raise ValueError
            _safe_component(host)
            if ":" in host:
                host = f"[{host}]"
            port = parsed.port
            if parsed.scheme.lower() == "file" and port is not None:
                raise ValueError
            netloc = host + (f":{port}" if port is not None else "")
            path = parsed.path
            for component in path.split("/"):
                _safe_component(component)
            result = urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
        else:
            if "://" in normalized or (":" in normalized and not is_windows):
                raise ValueError
            if is_windows and ":" in normalized[2:]:
                raise ValueError
            # Local '?' keeps the established provenance-query convention.
            # Unlike URL fragments, a literal local '#' must survive intact.
            stripped = normalized.split("?", 1)[0]
            if not stripped:
                raise ValueError
            for component in stripped.split("/"):
                _safe_component(component)
            result = stripped
    except (ValueError, UnicodeError):
        pass
    if result is None:
        raise TradeContractError("source provenance path is invalid")
    return result


def _safe_identifier(value: str, name: str, max_length: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
        or len(value) > max_length
        or _CREDENTIAL_WORD.search(value)
        or _JWT.search(value)
        or _LONG_OPAQUE.search(value)
    ):
        raise TradeContractError(f"{name} is not a safe identifier")
    return value


def _trade_date(value: str) -> str:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
        return value
    except ValueError:
        raise TradeContractError("trade_date must be an ISO calendar date") from None


@dataclass(frozen=True)
class CaptureEnvelope:
    ticker: str
    trade_date: str
    requested_at: str
    response_at: str
    capture_id: str = field(default_factory=lambda: str(uuid4()))
    http_status: int = 200
    last_modified: str | None = None
    x_ms_creation_time: str | None = None
    x_ms_request_id: str | None = None
    source_path_without_query_or_token: str = ""
    created_by: str = "local-ingest-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", self.ticker):
            raise TradeContractError("ticker must be an uppercase exchange identifier")
        _trade_date(self.trade_date)
        _safe_identifier(self.capture_id, "capture_id")
        _safe_identifier(self.created_by, "created_by", 64)
        if type(self.http_status) is not int or self.http_status not in {200, 404}:
            raise TradeContractError("only successful tape or absence observations are supported")
        requested_at = utc_text(self.requested_at)
        response_at = utc_text(self.response_at)
        if response_at < requested_at:
            raise TradeContractError("response timestamp precedes request timestamp")
        trade_day_start = utc_text(datetime.combine(
            date.fromisoformat(self.trade_date), time.min, JAKARTA_TIMEZONE,
        ))
        if response_at < trade_day_start:
            raise TradeContractError("response timestamp precedes the Jakarta trade date")
        last_modified = _header_time(self.last_modified)
        creation_time = _header_time(self.x_ms_creation_time)
        if any(stamp > response_at for stamp in (last_modified, creation_time) if stamp is not None):
            raise TradeContractError("source header timestamp follows the response")
        if creation_time is not None and last_modified is not None and creation_time > last_modified:
            raise TradeContractError("source creation timestamp follows last modification")
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "response_at", response_at)
        object.__setattr__(self, "last_modified", last_modified)
        object.__setattr__(self, "x_ms_creation_time", creation_time)
        if self.x_ms_request_id is not None:
            try:
                parsed_id = str(UUID(self.x_ms_request_id))
            except (ValueError, TypeError, AttributeError):
                raise TradeContractError("request ID must be a UUID") from None
            object.__setattr__(self, "x_ms_request_id", parsed_id)
        object.__setattr__(
            self, "source_path_without_query_or_token",
            sanitize_source_path(self.source_path_without_query_or_token),
        )


@dataclass(frozen=True)
class NormalizedTape:
    schema_version: str
    schema_fingerprint: str
    rows: tuple[dict[str, Any], ...]
    duplicate_counts: tuple[tuple[int, int], ...]
    source_row_count: int
    normalized_content_sha256: str
    content_json: str


def normalized_document(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Hash semantic rows in ticker/date/trx_code order, without provenance."""
    ordered = sorted(
        ({key: value for key, value in row.items()
          if key not in {"source_capture_id", "source_schema_version"}} for row in rows),
        key=lambda row: (row["ticker"], row["trade_date"], row["trx_code"]),
    )
    return {"schema_version": NORMALIZED_SCHEMA_VERSION, "rows": ordered}


def normalized_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    return sha256_bytes(canonical_json(normalized_document(rows)).encode("utf-8"))


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise TradeContractError(f"{name} must be a supported integer")
    return value


def _code(value: Any, name: str, allow_integer: bool = False) -> str | int:
    if allow_integer and type(value) is int and value >= 0:
        return value
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", value)
        or _CREDENTIAL_WORD.search(value)
        or _JWT.search(value)
    ):
        raise TradeContractError(f"{name} must be a supported code")
    return value


def _source_date(value: Any) -> str:
    if type(value) is date:
        return value.isoformat()
    if type(value) is int:
        digits = str(value)
        if len(digits) != 8:
            raise TradeContractError("source trade date is invalid")
        return _trade_date(f"{digits[:4]}-{digits[4:6]}-{digits[6:]}")
    return _trade_date(value)


def _trade_time(value: Any) -> str:
    if type(value) is int and 0 <= value <= 235959:
        digits = f"{value:06d}"
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{6}", value):
        digits = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}", value):
        digits = value.replace(":", "")
    else:
        raise TradeContractError("trade time must be HHMMSS to seconds")
    hour, minute, second = int(digits[:2]), int(digits[2:4]), int(digits[4:])
    if hour > 23 or minute > 59 or second > 59:
        raise TradeContractError("trade time is invalid")
    return f"{digits[:2]}:{digits[2:4]}:{digits[4:]}"


def _source_value_rp(value: Any) -> int:
    """Convert source VALUE ×100 without Decimal-context or float arithmetic.

    A DOUBLE is supported only through Python's shortest decimal roundtrip.
    Its decimal result must resolve to integer rupiah and be the only integer
    rupiah in that DOUBLE's exact nearest-even rounding interval. This admits
    ordinary binary representation imprecision, without accepting tolerances
    or a representation that cannot distinguish adjacent integer rupiah.
    Shares×price must independently equal the converted integer. No audited
    source magnitude bound exists; Decimal and integer money remain exact.
    FLOAT and binary-float money multiplication are unsupported.
    """
    if type(value) is int:
        return value * 100
    double_value = None
    if type(value) is float:
        if not math.isfinite(value):
            raise TradeContractError("source VALUE must be finite")
        double_value = value
        value = Decimal(str(value))
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TradeContractError("source VALUE has an unsupported representation")
    parts = value.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    exponent = parts.exponent + 2
    if exponent >= 0:
        rupiah = coefficient * (10 ** exponent)
    else:
        rupiah, remainder = divmod(coefficient, 10 ** -exponent)
        if remainder:
            raise TradeContractError("source VALUE does not resolve to integer rupiah")
    rupiah = -rupiah if parts.sign else rupiah
    if double_value is not None:
        _require_unique_double_rupiah(double_value, rupiah)
    return rupiah


def _require_unique_double_rupiah(value: float, rupiah: int) -> None:
    """Prove unique Rp using exact rational IEEE-754 rounding boundaries."""
    previous = math.nextafter(value, -math.inf)
    following = math.nextafter(value, math.inf)
    if not math.isfinite(previous) or not math.isfinite(following):
        raise TradeContractError("source VALUE DOUBLE cannot uniquely represent integer rupiah")
    exact = Fraction.from_float(value)
    # Midpoints in VALUE units, scaled by 100 to integer-rupiah units.
    lower = (Fraction.from_float(previous) + exact) * 50
    upper = (exact + Fraction.from_float(following)) * 50
    minimum = -(-lower.numerator // lower.denominator)
    maximum = upper.numerator // upper.denominator
    # The least significant significand bit governs nearest-even ties.
    ties_included = int.from_bytes(struct.pack(">d", value), "big") & 1 == 0
    if not ties_included:
        minimum += int(lower.denominator == 1)
        maximum -= int(upper.denominator == 1)
    if minimum != maximum or rupiah != minimum:
        raise TradeContractError("source VALUE DOUBLE cannot uniquely represent integer rupiah")


def _integer_type(column: Any) -> bool:
    logical = json.loads(column.logical_type.to_json())
    if column.physical_type not in {"INT32", "INT64"}:
        return False
    bits = 32 if column.physical_type == "INT32" else 64
    return logical == {"Type": "None"} or logical == {
        "Type": "Int", "bitWidth": bits, "isSigned": True,
    }


def _text_type(column: Any) -> bool:
    return column.physical_type == "BYTE_ARRAY" and json.loads(
        column.logical_type.to_json()
    ) == {"Type": "String"}


def _value_type(column: Any) -> bool:
    if _integer_type(column):
        return True
    logical = json.loads(column.logical_type.to_json())
    if column.physical_type == "DOUBLE" and logical == {"Type": "None"}:
        return True
    if not (
        logical.get("Type") == "Decimal"
        and column.physical_type in {"INT32", "INT64", "BYTE_ARRAY", "FIXED_LEN_BYTE_ARRAY"}
        and 1 <= column.precision <= 76
        and 0 <= column.scale <= column.precision
    ):
        return False
    if column.physical_type == "INT32":
        return column.precision <= 9
    if column.physical_type == "INT64":
        return column.precision <= 18
    if column.physical_type == "FIXED_LEN_BYTE_ARRAY":
        return 1 <= column.length <= 32 and 10 ** column.precision <= 2 ** (8 * column.length - 1) - 1
    return True


def _validated_schema(parquet: Any) -> tuple[str, str]:
    names = parquet.schema.names
    if len(names) != len(set(names)):
        raise TradeContractError("duplicate source column names are unsupported")
    columns = set(names)
    if columns == RECENT_COLUMNS:
        schema_version = RECENT_SCHEMA_VERSION
    elif columns == LEGACY_COLUMNS:
        schema_version = LEGACY_SCHEMA_VERSION
    else:
        raise TradeContractError("unsupported done_detail column layout")
    fingerprint_columns = []
    integer_columns = {"TRX_CODE", "STK_VOLM", "STK_PRIC", "TRX_ORD1", "TRX_ORD2"}
    text_columns = {"BRK_COD1", "BRK_COD2", "STK_CODE"}
    code_columns = {"TRX_SESS", "INV_TYP1", "INV_TYP2", "HAKA_HAKI", "TRX_TIME"}
    for column in parquet.schema:
        if (
            column.path != column.name
            or column.max_repetition_level != 0
            or column.max_definition_level not in {0, 1}
        ):
            raise TradeContractError("nested or repeated source columns are unsupported")
        name = column.name
        logical = json.loads(column.logical_type.to_json())
        if name in integer_columns:
            supported = _integer_type(column)
        elif name in text_columns:
            supported = _text_type(column)
        elif name in code_columns:
            supported = _integer_type(column) or _text_type(column)
        elif name == "TRX_TYPE":
            if schema_version == LEGACY_SCHEMA_VERSION:
                supported = column.physical_type == "INT32" and _integer_type(column)
            else:
                supported = _text_type(column)
        elif name == "TRX_DATE":
            supported = (
                _integer_type(column) or _text_type(column)
                or (column.physical_type == "INT32" and logical == {"Type": "Date"})
            )
        elif name == "VALUE":
            supported = _value_type(column)
        else:
            supported = False
        if not supported:
            raise TradeContractError("unsupported source physical or logical type")
        fingerprint_columns.append({
            "name": name,
            "physical_type": column.physical_type,
            "physical_length": column.length if column.physical_type == "FIXED_LEN_BYTE_ARRAY" else None,
            "logical_type": logical,
            "converted_type": column.converted_type,
            "classification": "required" if column.max_definition_level == 0 else "optional",
            "definition_level": column.max_definition_level,
            "repetition_level": column.max_repetition_level,
        })
    fingerprint = sha256_bytes(canonical_json({
        "schema_version": schema_version,
        "columns": sorted(fingerprint_columns, key=lambda item: item["name"]),
    }).encode("utf-8"))
    return schema_version, fingerprint


def normalize_parquet(
    path: str | Path | bytes, envelope: CaptureEnvelope, *, batch_size: int = 65536,
) -> NormalizedTape:
    """Parse local bytes or a local file and validate source boundary values.

    Stores pass the exact verified byte snapshot so hashing and normalization
    cannot observe different bodies if another process changes a raw object.
    """
    if not isinstance(envelope, CaptureEnvelope) or envelope.http_status != 200:
        raise TradeContractError("a tape requires a successful capture envelope")
    if type(batch_size) is not int or batch_size < 1:
        raise TradeContractError("parser batch size must be a positive integer")
    if isinstance(path, str) and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", path):
        raise TradeContractError("Parquet input must be a local file")
    source = None
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        # Arrow receives local bytes or an open local handle, never a URI.
        source = pa.BufferReader(path) if isinstance(path, bytes) else Path(path).open("rb")
        parquet = pq.ParquetFile(source)
        schema_version, schema_fingerprint = _validated_schema(parquet)
        rows_by_key: dict[int, dict[str, Any]] = {}
        multiplicities: Counter[int] = Counter()
        source_row_count = 0
        for batch in parquet.iter_batches(batch_size=batch_size):
            for row in batch.to_pylist():
                source_row_count += 1
                if any(value is None for value in row.values()):
                    raise TradeContractError("source tape contains a null canonical field")
                if schema_version == RECENT_SCHEMA_VERSION:
                    if row["STK_CODE"] != envelope.ticker:
                        raise TradeContractError("source ticker differs from capture envelope")
                    if _source_date(row["TRX_DATE"]) != envelope.trade_date:
                        raise TradeContractError("source date differs from capture envelope")
                    board = _code(row["TRX_TYPE"], "board")
                else:
                    if type(row["TRX_TYPE"]) is not int or row["TRX_TYPE"] != 0:
                        raise TradeContractError("unsupported legacy TRX_TYPE value")
                    board = "UNKNOWN"
                shares = _integer(row["STK_VOLM"], "shares", 1)
                price_idr = _integer(row["STK_PRIC"], "price_idr", 1)
                value_rp = _source_value_rp(row["VALUE"])
                if value_rp != shares * price_idr:
                    raise TradeContractError("source VALUE disagrees with exact shares times price")
                canonical = {
                    "ticker": envelope.ticker,
                    "trade_date": envelope.trade_date,
                    "trx_code": _integer(row["TRX_CODE"], "trx_code"),
                    "session": _code(row["TRX_SESS"], "session", True),
                    "board": board,
                    "buyer_broker": _code(row["BRK_COD1"], "buyer broker"),
                    "buyer_investor_type": _code(row["INV_TYP1"], "buyer investor type", True),
                    "seller_broker": _code(row["BRK_COD2"], "seller broker"),
                    "seller_investor_type": _code(row["INV_TYP2"], "seller investor type", True),
                    "shares": shares,
                    "price_idr": price_idr,
                    "value_rp": value_rp,
                    "buy_order_no": _integer(row["TRX_ORD1"], "buy order"),
                    "sell_order_no": _integer(row["TRX_ORD2"], "sell order"),
                    "trade_time": _trade_time(row["TRX_TIME"]),
                    "vendor_haka_haki": _code(row["HAKA_HAKI"], "vendor HAKA_HAKI", True),
                    "source_schema_version": schema_version,
                }
                # TRX_TIME proves a lower bound to whole seconds only. It does
                # not prove exchange milliseconds or a market-close cutoff.
                represented_trade_at = utc_text(datetime.combine(
                    date.fromisoformat(envelope.trade_date),
                    time.fromisoformat(canonical["trade_time"]), JAKARTA_TIMEZONE,
                ))
                if envelope.response_at < represented_trade_at:
                    raise TradeContractError("response timestamp precedes a represented trade")
                trx_code = canonical["trx_code"]
                if trx_code in rows_by_key and rows_by_key[trx_code] != canonical:
                    raise TradeContractError("conflicting duplicate natural trade key")
                rows_by_key[trx_code] = canonical
                multiplicities[trx_code] += 1
        if source_row_count == 0:
            raise TradeContractError("zero-row successful tapes have unaudited source semantics")
        rows = tuple(rows_by_key[key] for key in sorted(rows_by_key))
        # Deterministic accounting invariants are not independent source proof.
        broker_totals(rows)
        content_json = canonical_json(normalized_document(rows))
        return NormalizedTape(
            schema_version=schema_version,
            schema_fingerprint=schema_fingerprint,
            rows=rows,
            duplicate_counts=tuple((key, count) for key, count in sorted(multiplicities.items()) if count > 1),
            source_row_count=source_row_count,
            normalized_content_sha256=sha256_bytes(content_json.encode("utf-8")),
            content_json=content_json,
        )
    except TradeContractError:
        raise
    except Exception:
        # Parser exceptions can echo filenames, embedded metadata or payloads.
        raise TradeContractError("local Parquet cannot satisfy the trade contract") from None
    finally:
        if source is not None:
            source.close()


def broker_totals(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Deterministic broker-day accounting invariants, without OHLC inputs."""
    totals: dict[str, dict[str, int]] = {}
    fields = (
        "buy_shares", "sell_shares", "net_shares", "buy_value_rp",
        "sell_value_rp", "net_value_rp", "trade_count_buy", "trade_count_sell",
    )
    for row in rows:
        for side, broker in (("buy", row["buyer_broker"]), ("sell", row["seller_broker"])):
            values = totals.setdefault(broker, dict.fromkeys(fields, 0))
            values[f"{side}_shares"] += row["shares"]
            values[f"{side}_value_rp"] += row["value_rp"]
            values[f"trade_count_{side}"] += 1
    for values in totals.values():
        values["net_shares"] = values["buy_shares"] - values["sell_shares"]
        values["net_value_rp"] = values["buy_value_rp"] - values["sell_value_rp"]
    if (
        sum(value["buy_shares"] for value in totals.values()) != sum(value["sell_shares"] for value in totals.values())
        or sum(value["buy_value_rp"] for value in totals.values()) != sum(value["sell_value_rp"] for value in totals.values())
    ):
        raise TradeContractError("broker-day tape is not balanced")
    return {broker: totals[broker] for broker in sorted(totals)}


def tape_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = tuple(rows)
    buy_orders = Counter(row["buy_order_no"] for row in rows)
    sell_orders = Counter(row["sell_order_no"] for row in rows)
    times = [row["trade_time"] for row in rows]
    return {
        "board_counts": dict(sorted(Counter(row["board"] for row in rows).items())),
        "broker_count": len(broker_totals(rows)),
        "time_range": [min(times), max(times)] if times else None,
        "total_shares": sum(row["shares"] for row in rows),
        "total_value_rp": sum(row["value_rp"] for row in rows),
        "unique_buy_orders": len(buy_orders),
        "unique_sell_orders": len(sell_orders),
        "fills_per_buy_order": dict(sorted(buy_orders.items())),
        "fills_per_sell_order": dict(sorted(sell_orders.items())),
    }
