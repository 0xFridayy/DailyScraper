"""Synthetic offline acquisition tests. Run python test_bandarmolony_acquire.py.

No BandarmoloNY, Supabase or Azure request is made. The HTTP transport is a
fake. One loopback test drives the real requests transport against a local
127.0.0.1 server to prove urllib3 logging cannot carry a SAS. Every token,
SAS and header value is an invented canary; Parquet fixtures hold invented
executions only.
"""

from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
import ast
import copy
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import pickle
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer

import pyarrow as pa

import bandarmolony_acquire as acquire
import bandarmolony_trade_capture as capture
import bandarmolony_trade_contract as contract
from test_bandarmolony_trade_capture import make_tree_writable, sample_rows, source_row, write_parquet


# A JWT-shaped bearer that also carries every credential marker the scans look for.
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJUT0tFTkNBTkFSWSJ9.TOKENCANARY-api_key=apikey=sig=sv=7f3a"
SIGNATURE = "SIGCANARY9c1d%2Bapi_key%3Dapikey%3D"
UNIQUE_MARKERS = ("TOKENCANARY", "SIGCANARY", "APIKEYCANARY", "REFRESHCANARY", "LOCATIONCANARY", "PATHCANARY")
TEXT_MARKERS = UNIQUE_MARKERS + ("eyJ", "sig=", "sv=", "api_key=", "apikey=", "Bearer")
ENDPOINT = acquire.SasEndpoint(host="api.example.test", path="/api/api/test/sas-token",
                               json_field="sasToken", phase0_confirmed=True)
REQUEST_ID = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
CLEAN_PATH = "/trading-data-v2/done_detail/20261001/STOCK/DEWA.parquet"
START = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def sas_query(*, se="2026-10-01T13:00:00Z", sp="rl", spr="https", extra=""):
    query = f"sv=2025-05-05&sr=c&sp={sp}&se={se.replace(':', '%3A')}"
    if spr is not None:
        query += f"&spr={spr}"
    return query + f"&sig={SIGNATURE}{extra}"


class FakeClock:
    """Strictly increasing synthetic instants before the real wall clock."""

    def __init__(self, start=START, step=timedelta(milliseconds=250)):
        self.now, self.step = start, step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


class FakeResponse:
    def __init__(self, status, headers=None, chunks=(b"",), fail_after=None, failure=None):
        self.status = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.fail_after = fail_after
        self.failure = failure or ConnectionError(
            f"IncompleteRead for {CLEAN_PATH}?sig={SIGNATURE} Bearer {TOKEN}")
        self.closed = False

    def chunks(self):
        for index, chunk in enumerate(self._chunks):
            if self.fail_after == index:
                raise self.failure
            yield chunk
        if self.fail_after == len(self._chunks):
            raise self.failure

    def close(self):
        self.closed = True


class FakeTransport:
    """Replays prepared responses and records every request it receives."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def get(self, url, headers):
        self.requests.append((url, dict(headers)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


def sas_response(value=None, *, status=200, body=None, headers=None):
    if body is None:
        body = json.dumps({
            "sasToken": sas_query() if value is None else value,
            "note": "apikey=APIKEYCANARY api_key=APIKEYCANARY",
            "refresh_token": "eyJREFRESHCANARY",
        }).encode()
    return FakeResponse(status, {"Content-Type": "application/json", **(headers or {})}, [body])


def blob_response(data, *, status=200, headers=None, length=True, chunk=997, **options):
    base = {
        "Last-Modified": "Thu, 01 Oct 2026 10:00:00 GMT",
        "x-ms-creation-time": "Thu, 01 Oct 2026 09:30:00 GMT",
        "x-ms-request-id": REQUEST_ID,
        "ETag": '"0x8DEETAGCANARY"',
        "Content-Type": "application/octet-stream",
        "Set-Cookie": "session=TOKENCANARY",
    }
    if length:
        base["Content-Length"] = str(len(data))
    base.update(headers or {})
    chunks = [data[index:index + chunk] for index in range(0, len(data), chunk)] or [b""]
    return FakeResponse(status, base, chunks, **options)


def not_found(code="BlobNotFound"):
    body = (b'<?xml version="1.0" encoding="utf-8"?><Error><Code>' + code.encode()
            + b"</Code><Message>The specified blob does not exist.</Message></Error>")
    headers = {"x-ms-request-id": REQUEST_ID, "Content-Length": str(len(body))}
    if code:
        headers["x-ms-error-code"] = code
    return FakeResponse(404, headers, [body])


class AcquisitionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bandarmolony-acquire-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(make_tree_writable, self.root)
        self.db = self.root / "private" / "trade_capture.db"
        self.raw = self.root / "private" / "trade_raw"
        self.staging = self.root / "private" / "acquisition_staging"
        self.clock = FakeClock()
        self.fixtures = 0
        self.logs = io.StringIO()
        handler = logging.StreamHandler(self.logs)
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        self.addCleanup(root_logger.setLevel, previous_level)
        self.addCleanup(root_logger.removeHandler, handler)

    def parquet(self, rows=None, **options):
        self.fixtures += 1
        path = write_parquet(self.root / "fixtures" / f"fixture-{self.fixtures}.parquet", rows, **options)
        return path.read_bytes()

    def run_acquire(self, *responses, ticker="DEWA", trade_date="2026-10-01", **options):
        self.transport = FakeTransport(responses)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                return acquire.acquire_one(
                    ticker, trade_date, token=acquire.Secret(TOKEN), endpoint=options.pop("endpoint", ENDPOINT),
                    transport=self.transport, db=options.pop("db", self.db),
                    raw_root=options.pop("raw_root", self.raw),
                    staging_root=options.pop("staging_root", self.staging), clock=self.clock, **options)
            finally:
                self.assert_clean_text(out.getvalue(), err.getvalue(), self.logs.getvalue())

    def run_retry(self, capture_id):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                return acquire.retry(capture_id, db=self.db, raw_root=self.raw, staging_root=self.staging)
            finally:
                self.assert_clean_text(out.getvalue(), err.getvalue(), self.logs.getvalue())

    def refused(self, *responses, error=acquire.AcquisitionError, **options):
        with self.assertRaises(error) as caught:
            self.run_acquire(*responses, **options)
        self.assert_safe_error(caught.exception)
        return caught.exception

    def acquire_bytes(self, data, **options):
        return self.run_acquire(sas_response(), blob_response(data), **options)

    def reader_verify(self, capture_id):
        with capture.TradeCaptureStore(self.db, self.raw, read_only=True) as reader:
            return reader.verify(capture_id)

    def counts(self):
        if not self.db.exists():
            return 0, 0
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "trade_captures" not in tables:
                return 0, 0
            return (conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0],
                    conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0])

    def staged(self):
        if not self.staging.exists():
            return []
        return sorted(path.name for path in self.staging.iterdir())

    def raw_objects(self):
        return sorted(self.raw.rglob("*.parquet")) if self.raw.exists() else []

    def assert_clean_text(self, *texts):
        for text in texts:
            for marker in TEXT_MARKERS:
                self.assertNotIn(marker, text)

    def assert_safe_error(self, error):
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assert_clean_text(str(error), repr(error), *(repr(arg) for arg in error.args))

    def assert_no_persisted_secrets(self):
        for path in self.root.rglob("*"):
            relative = str(path.relative_to(self.root))
            self.assert_clean_text(relative)
            if path.is_file() and "fixtures" not in path.parts:
                data = path.read_bytes()
                # Synthetic Parquet holds no canary by construction, but its
                # base64 Arrow schema could contain a short generic marker.
                for marker in UNIQUE_MARKERS if path.suffix == ".parquet" else TEXT_MARKERS:
                    self.assertNotIn(marker.encode(), data, relative)

    def assert_nothing_recorded(self):
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.raw_objects(), [])
        self.assertEqual(self.staged(), [])


class SecretAndSasTests(AcquisitionTestCase):
    def test_secret_wrapper_never_renders_or_serializes(self):
        secret = acquire.Secret(TOKEN)
        self.assertEqual(secret.reveal(), TOKEN)
        self.assert_clean_text(repr(secret), str(secret), f"{secret}", "%s" % secret, repr([secret]))
        for operation in (lambda: pickle.dumps(secret), lambda: copy.copy(secret), lambda: copy.deepcopy(secret)):
            with self.assertRaises(TypeError):
                operation()
        for bad in ("", "has space", "tab\tinside", "nul\x00", 3, None):
            with self.assertRaises(acquire.AcquisitionError) as caught:
                acquire.Secret(bad)
            self.assert_safe_error(caught.exception)

    def test_validate_accepts_container_query_and_container_url(self):
        now = START
        for value in (sas_query(), "?" + sas_query(),
                      f"https://{acquire.BLOB_HOST}/trading-data-v2?{sas_query()}",
                      f"https://{acquire.BLOB_HOST}/trading-data-v2/?{sas_query()}",
                      f"https://{acquire.BLOB_HOST.upper()}:443/trading-data-v2?{sas_query()}"):
            sas = acquire.validate_sas(value, now)
            self.assertEqual(sas.query.reveal(), sas_query())
            self.assertEqual(sas.expires_at, datetime(2026, 10, 1, 13, tzinfo=timezone.utc))
            self.assert_clean_text(repr(sas), str(sas))
        self.assertEqual(acquire.validate_sas(sas_query(se="2026-10-01T13:00:00.1234567Z"), now).expires_at,
                         datetime(2026, 10, 1, 13, tzinfo=timezone.utc))
        self.assertEqual(acquire.validate_sas(sas_query(se="2026-10-02", sp="r"), now).expires_at,
                         datetime(2026, 10, 2, tzinfo=timezone.utc))

    def assert_sas_refused(self, value, message):
        with self.assertRaises(acquire.AcquisitionError) as caught:
            acquire.validate_sas(value, START)
        self.assert_safe_error(caught.exception)
        self.assertIn(message, str(caught.exception))

    def test_wrong_sas_host_refused(self):
        query = sas_query()
        for value in (f"https://evil.example.test/trading-data-v2?{query}",
                      f"https://{acquire.BLOB_HOST}.evil.test/trading-data-v2?{query}",
                      f"https://other.blob.core.windows.net/trading-data-v2?{query}",
                      f"https://user@{acquire.BLOB_HOST}/trading-data-v2?{query}",
                      f"https://{acquire.BLOB_HOST}:8443/trading-data-v2?{query}"):
            self.assert_sas_refused(value, "host")
        for value in (f"https://{acquire.BLOB_HOST}/other-container?{query}",
                      f"https://{acquire.BLOB_HOST}/trading-data-v2/done_detail?{query}",
                      f"https://{acquire.BLOB_HOST}/trading-data-v2#frag?{query}"):
            self.assert_sas_refused(value, "container")

    def test_non_https_sas_refused(self):
        for value in (f"http://{acquire.BLOB_HOST}/trading-data-v2?{sas_query()}",
                      f"ftp://{acquire.BLOB_HOST}/trading-data-v2?{sas_query()}",
                      sas_query(spr="http"), sas_query(spr="https,http"), sas_query(spr=None)):
            self.assert_sas_refused(value, "HTTPS")

    def test_expired_or_unparseable_expiry_refused(self):
        for se in ("2026-10-01T11:59:59Z", "2026-10-01T12:00:30Z", "2026-09-30"):
            self.assert_sas_refused(sas_query(se=se), "expired")
        for se in ("tomorrow", "2026-13-01T00:00:00Z", "2026-10-01T13:00:00+07:00", ""):
            self.assert_sas_refused(sas_query(se=se), "expiry")

    def test_sas_without_read_permission_refused(self):
        for sp in ("l", "wl", "", "R", "r!"):
            self.assert_sas_refused(sas_query(sp=sp), "read")
        self.assert_sas_refused(sas_query().replace("&sp=rl", ""), "read")

    def test_sas_with_unexpected_or_duplicate_parameters_refused(self):
        for extra in ("&comp=list", "&restype=container", "&rsce=gzip", "&rsct=text%2Fhtml",
                      "&sig=second", "&api_key=APIKEYCANARY", "&snapshot=1"):
            self.assert_sas_refused(sas_query(extra=extra), "parameters")
        self.assert_sas_refused(sas_query().replace(f"&sig={SIGNATURE}", ""), "incomplete")
        self.assert_sas_refused(sas_query().replace("sv=2025-05-05&", ""), "incomplete")
        for value in ("", "has space&" + sas_query(), "a" * 9000, None, "sv", "==&&"):
            self.assert_sas_refused(value, "malformed")


class OutputPathSafetyTests(AcquisitionTestCase):
    def assert_path_refused(self, **options):
        error = self.refused(sas_response(), **options)
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(list(self.root.rglob("*")), [])
        self.assertNotIn("PATHCANARY", str(error))
        self.assert_no_persisted_secrets()
        return str(error)

    def test_credential_assignments_in_every_configurable_path_are_refused_early(self):
        assignments = ("sig", "sv", "api_key", "apikey", "token", "access_token",
                       "refresh_token", "authorization", "password", "passwd", "secret", "sas")
        messages = set()
        for option in ("db", "raw_root", "staging_root"):
            for assignment in assignments:
                for spelling in (assignment, assignment.upper()):
                    with self.subTest(path=option, assignment=spelling):
                        unsafe = self.root / (spelling + "=PATHCANARY") / "output"
                        messages.add(self.assert_path_refused(**{option: unsafe}))
        self.assertEqual(len(messages), 1, "path refusals must use one fixed message")

    def test_derived_lock_path_is_validated_independently(self):
        derived = str(self.db.resolve()) + ".acquisition-lock"
        unsafe_lock = self.root / "authorization=PATHCANARY.acquisition-lock"

        def path(value):
            return unsafe_lock if str(value) == derived else Path(value)

        with patch.object(acquire, "Path", side_effect=path):
            self.assert_path_refused()

    def test_offline_retry_refuses_credential_paths_without_creating_artifacts(self):
        capture_id = "04a7fdd2-aef6-4c6d-b2a5-a115ae0c1dc8"
        for option in ("db", "raw_root", "staging_root"):
            with self.subTest(path=option):
                paths = dict(db=self.db, raw_root=self.raw, staging_root=self.staging)
                paths[option] = self.root / "ReFrEsH_ToKeN=PATHCANARY" / "output"
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err), \
                        self.assertRaises(acquire.AcquisitionError) as caught:
                    acquire.retry(capture_id, **paths)
                self.assert_safe_error(caught.exception)
                self.assert_clean_text(out.getvalue(), err.getvalue(), self.logs.getvalue())
                self.assertEqual(list(self.root.rglob("*")), [])


class PublicCaptureApiTests(unittest.TestCase):
    def test_acquisition_uses_no_private_capture_imports_connection_or_schema_sql(self):
        tree = ast.parse(Path(acquire.__file__).read_text(encoding="utf-8"))
        private_imports, connections, capture_sql = [], [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "bandarmolony_trade_capture":
                private_imports.extend(alias.name for alias in node.names if alias.name.startswith("_"))
            if isinstance(node, ast.Attribute) and node.attr == "conn":
                connections.append(node.lineno)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if (re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", node.value, re.I)
                        and re.search(r"\btrade_(captures|acceptances|store_meta)\b", node.value, re.I)):
                    capture_sql.append(node.lineno)
        self.assertEqual(private_imports, [])
        self.assertEqual(connections, [])
        self.assertEqual(capture_sql, [])


class TransportBoundaryTests(AcquisitionTestCase):
    def test_exact_bytes_flow_from_download_to_raw_object(self):
        data = self.parquet()
        staged_snapshots = []
        original_ingest = capture.TradeCaptureStore.ingest

        def spying_ingest(store, file, envelope):
            staged_snapshots.append(Path(file).read_bytes())
            self.assertTrue(str(Path(file).resolve()).startswith(str(self.staging.resolve())))
            return original_ingest(store, file, envelope)

        with patch.object(capture.TradeCaptureStore, "ingest", spying_ingest):
            result = self.acquire_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(staged_snapshots, [data])
        raw_object = self.raw / "sha256" / digest[:2] / f"{digest}.parquet"
        self.assertEqual(raw_object.read_bytes(), data)
        self.assertEqual(self.raw_objects(), [raw_object])
        metadata = self.reader_verify(result["capture_id"])
        self.assertEqual(metadata["raw_response_sha256"], digest)
        self.assertEqual(metadata["content_length"], len(data))
        self.assertEqual(result["raw_response_sha256_prefix"], digest[:12])
        self.assertEqual(result["content_length"], len(data))
        self.assertEqual(self.staged(), [])

    def test_authorization_only_reaches_api_host(self):
        query = sas_query()
        self.acquire_bytes(self.parquet())
        (sas_url, sas_headers), (blob_url, blob_headers) = self.transport.requests
        self.assertEqual(sas_url, "https://api.example.test/api/api/test/sas-token")
        self.assertEqual(sas_headers["Authorization"], "Bearer " + TOKEN)
        parts = urlsplit(blob_url)
        self.assertEqual((parts.scheme, parts.hostname, parts.path), ("https", acquire.BLOB_HOST, CLEAN_PATH))
        self.assertEqual(parts.query, query)
        self.assertFalse({name.lower() for name in blob_headers} & {"authorization", "cookie", "x-csrftoken"})
        self.assertEqual(blob_headers.get("Accept-Encoding"), "identity")
        with self.assertRaises(acquire.AcquisitionError):
            acquire.SasEndpoint(host=acquire.BLOB_HOST, path="/x", json_field="sasToken")
        for host, path, field in (("API.example.test", "/x", "f"), ("api.example.test", "x", "f"),
                                  ("api.example.test", "/x?a=1", "f"), ("api.example.test", "/a/../b", "f"),
                                  ("api.example.test", "/x", "bad field"), ("http://api.example.test", "/x", "f"),
                                  ("api.example.test:443", "/x", "f")):
            with self.assertRaises(acquire.AcquisitionError):
                acquire.SasEndpoint(host=host, path=path, json_field=field)

    def test_sas_endpoint_responses_are_refused_without_blob_request(self):
        location = {"Location": "https://elsewhere.test/?sig=LOCATIONCANARY"}
        cases = [
            (sas_response(status=302, headers=location), "redirect"),
            (sas_response(status=307, headers=location), "redirect"),
            (sas_response(status=401), "not authorized"),
            (sas_response(status=403), "not authorized"),
            (sas_response(status=429), "rate limited"),
            (sas_response(status=500), "HTTP 500"),
            (sas_response(body=b"<html>apikey=APIKEYCANARY</html>"), "pinned shape"),
            (sas_response(body=b'{"other": "eyJREFRESHCANARY"}'), "pinned shape"),
            (sas_response(body=b'["sv=1"]'), "pinned shape"),
            (sas_response(body=b'{"sasToken": 5}'), "pinned shape"),
            (sas_response(f"https://evil.example.test/trading-data-v2?{sas_query()}"), "host"),
            (sas_response(body=b"x" * (acquire.MAX_SMALL_BODY + 1)), "size cap"),
        ]
        for response, message in cases:
            with self.subTest(message=message, status=response.status):
                error = self.refused(response)
                self.assertIn(message, str(error))
                self.assertEqual(len(self.transport.requests), 1)
                self.assertTrue(response.closed)
                self.assert_nothing_recorded()

    def test_blob_redirect_refused(self):
        for status in (301, 302, 307, 308):
            response = blob_response(b"", status=status, headers={"Location": "https://x.test/?sig=LOCATIONCANARY"})
            error = self.refused(sas_response(), response)
            self.assertIn("redirect", str(error))
            self.assertTrue(response.closed)
            self.assert_nothing_recorded()

    def test_content_encoding_refused(self):
        data = self.parquet()
        for encoding in ("gzip", "identity", "zstd", ""):
            response = blob_response(data, headers={"Content-Encoding": encoding})
            error = self.refused(sas_response(), response)
            self.assertIn("Content-Encoding", str(error))
            self.assert_nothing_recorded()

    def test_content_length_mismatch_refused(self):
        data = self.parquet()
        for value, message in ((str(len(data) + 1), "Content-Length"), (str(len(data) - 1), "Content-Length"),
                               ("12a", "Content-Length"), ("-1", "Content-Length"), ("1" * 20, "Content-Length")):
            error = self.refused(sas_response(), blob_response(data, headers={"Content-Length": value}))
            self.assertIn(message, str(error))
            self.assert_nothing_recorded()

    def test_incomplete_or_oversized_body_never_reaches_trade_capture(self):
        data = self.parquet()
        with patch.object(capture.TradeCaptureStore, "ingest", side_effect=AssertionError("ingest reached")):
            for fail_after in (0, 1, 2):
                response = blob_response(data, chunk=512, fail_after=fail_after)
                error = self.refused(sas_response(), response)
                self.assertIn("before completion", str(error))
                self.assertTrue(response.closed)
                self.assert_nothing_recorded()
            # Without Content-Length the size cap still bounds the read.
            error = self.refused(sas_response(), blob_response(data, length=False), max_bytes=len(data) - 1)
            self.assertIn("size cap", str(error))
            error = self.refused(sas_response(), blob_response(data), max_bytes=len(data) - 1)
            self.assertIn("size cap", str(error))
            for chunk in ("text", None):
                response = FakeResponse(200, {"Content-Length": "4"}, [chunk])
                self.refused(sas_response(), response)
            self.assert_nothing_recorded()

    def test_empty_http_200_is_refused_before_stage_or_ingest(self):
        with patch.object(acquire, "_stage", side_effect=KeyboardInterrupt("stage reached")) as stage, \
                patch.object(capture.TradeCaptureStore, "ingest",
                             side_effect=AssertionError("ingest reached")) as ingest:
            for length in (True, False):
                with self.subTest(content_length=length):
                    response = blob_response(b"", length=length)
                    error = self.refused(sas_response(), response)
                    self.assertIn("empty", str(error).lower())
                    self.assertTrue(response.closed)
                    self.assertEqual(len(self.transport.requests), 2)
                    self.assert_nothing_recorded()
                    self.assertFalse(self.staging.exists())
            stage.assert_not_called()
            ingest.assert_not_called()

    def test_transport_exceptions_are_replaced_without_context(self):
        leaky = ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {CLEAN_PATH}?sig={SIGNATURE}")
        error = self.refused(leaky)
        self.assertIn("failed before a response", str(error))
        error = self.refused(sas_response(), leaky)
        self.assertIn("failed before a response", str(error))
        self.assert_nothing_recorded()

    def test_status_routing(self):
        data = self.parquet()
        for status in (400, 401, 403, 409, 410, 412, 416, 429, 500, 502, 503, 206, 204):
            with self.subTest(status=status):
                error = self.refused(sas_response(), blob_response(data, status=status), record_absence=True)
                self.assertIn(f"HTTP {status}", str(error))
                self.assert_nothing_recorded()
        for code in ("ContainerNotFound", "ResourceNotFound", ""):
            with self.subTest(code=code):
                error = self.refused(sas_response(), not_found(code), record_absence=True)
                self.assertIn("BlobNotFound", str(error))
                self.assert_nothing_recorded()
        result = self.run_acquire(sas_response(), not_found())
        self.assertEqual(result["outcome"], "ABSENCE_NOT_RECORDED")
        self.assertIsNone(result["capture_id"])
        self.assertEqual(result["http_status"], 404)
        self.assert_nothing_recorded()
        result = self.run_acquire(sas_response(), not_found(), record_absence=True)
        self.assertEqual((result["outcome"], result["observation_state"]), ("ACCEPTED", "ABSENT_OBSERVED"))
        self.assertIsNone(result["content_version"])
        self.assertIsNone(result["row_count"])
        self.assertIsNone(result["ticker_date_evidence"])
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.raw_objects(), [])
        self.assertEqual(self.staged(), [])
        result = self.acquire_bytes(data)
        self.assertEqual((result["outcome"], result["observation_state"]), ("ACCEPTED", "CONTENT_FIRST_SEEN"))


class CaptureHandoffTests(AcquisitionTestCase):
    def test_contract_failures_preserve_staging_as_unresolved(self):
        raised = []
        original_ingest = capture.TradeCaptureStore.ingest

        def recording_ingest(store, file, envelope):
            try:
                return original_ingest(store, file, envelope)
            except contract.TradeContractError as error:
                raised.append(type(error))
                raise

        cases = {
            "malformed": b"PAR1" + b"\x00" * 64 + b"PAR1",
            "html": b"<!doctype html><html><body>Sign in</body></html>",
            "wrong ticker": self.parquet([source_row(STK_CODE="BBCA")]),
            "wrong date": self.parquet([source_row(TRX_DATE=datetime(2026, 10, 2).date())]),
            "unsupported schema": self.parquet(extra={"UNEXPECTED": (pa.int32(), 7)}),
            "zero rows": self.parquet([]),
        }
        with patch.object(capture.TradeCaptureStore, "ingest", recording_ingest):
            for index, (label, data) in enumerate(cases.items()):
                with self.subTest(label):
                    self.staging = self.root / "private" / f"acquisition_staging_{index}"
                    raised.clear()
                    result = self.acquire_bytes(data)
                    self.assertEqual(result["outcome"], "UNRESOLVED")
                    self.assertTrue(result["staging_preserved"])
                    self.assertEqual(raised, [contract.TradeContractError])
                    directory = self.staging / result["capture_id"]
                    record = (directory / "attempt.json").read_bytes()
                    self.assertEqual((directory / "body.parquet").read_bytes(), data)
                    self.assertEqual(self.counts(), (0, 0))
                    self.assertEqual(self.raw_objects(), [])
                    self.assertEqual(self.staged(), [result["capture_id"]])
                    raised.clear()
                    retried = self.run_retry(result["capture_id"])
                    self.assertEqual((retried["outcome"], retried["capture_id"]),
                                     ("UNRESOLVED", result["capture_id"]))
                    self.assertEqual(raised, [contract.TradeContractError])
                    self.assertEqual((directory / "body.parquet").read_bytes(), data)
                    self.assertEqual((directory / "attempt.json").read_bytes(), record)
        self.assert_no_persisted_secrets()

    def test_new_http_observation_receives_new_capture_id(self):
        data = self.parquet()
        first = self.acquire_bytes(data)
        second = self.acquire_bytes(data)
        self.assertNotEqual(first["capture_id"], second["capture_id"])
        self.assertGreater(second["requested_at"], first["response_at"])
        self.assertEqual((first["observation_seq"], second["observation_seq"]), (1, 2))
        self.assertEqual(second["observation_state"], "CONTENT_REPEAT")

    def test_observation_sequence_and_content_versions(self):
        original = self.parquet()
        reencoded = self.parquet(compression="snappy", use_dictionary=False)
        changed = self.parquet(sample_rows() + [source_row(TRX_CODE=1004, TRX_TIME=160002)])
        self.assertNotEqual(original, reencoded)
        results = [self.acquire_bytes(original), self.acquire_bytes(original), self.acquire_bytes(reencoded),
                   self.acquire_bytes(changed),
                   self.run_acquire(sas_response(), not_found(), record_absence=True)]
        self.assertEqual([r["observation_seq"] for r in results], [1, 2, 3, 4, 5])
        self.assertEqual([r["content_version"] for r in results], [1, 1, 1, 2, None])
        self.assertEqual([r["observation_state"] for r in results],
                         ["CONTENT_FIRST_SEEN", "CONTENT_REPEAT", "CONTENT_REPEAT", "CONTENT_CHANGED",
                          "ABSENT_OBSERVED"])
        self.assertEqual(len({r["capture_id"] for r in results}), 5)
        self.assertNotEqual(results[0]["raw_response_sha256_prefix"], results[2]["raw_response_sha256_prefix"])
        self.assertEqual(self.staged(), [])
        self.assert_no_persisted_secrets()

    def test_persisted_provenance_is_allowlisted(self):
        sanitized = []
        original_sanitize = contract.sanitize_source_path

        def spying_sanitize(value):
            sanitized.append(value)
            return original_sanitize(value)

        container_url = f"https://{acquire.BLOB_HOST}/trading-data-v2?{sas_query()}"
        with patch.object(contract, "sanitize_source_path", spying_sanitize):
            result = self.run_acquire(sas_response(container_url), blob_response(self.parquet()))
        self.assertTrue(sanitized)
        self.assertTrue(all("?" not in value and "#" not in value for value in sanitized))
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as conn:
            stored = json.loads(conn.execute("SELECT metadata_json FROM trade_captures").fetchone()[0])
        self.assertEqual(set(stored), {field.name for field in fields(contract.CaptureEnvelope)})
        self.assertEqual(stored["source_path_without_query_or_token"], f"https://{acquire.BLOB_HOST}{CLEAN_PATH}")
        self.assertEqual(stored["created_by"], "bandarmolony-acquisition-v1")
        self.assertEqual(stored["x_ms_request_id"], REQUEST_ID)
        self.assertEqual(stored["last_modified"], "2026-10-01T10:00:00.000000Z")
        self.assertEqual(stored["x_ms_creation_time"], "2026-10-01T09:30:00.000000Z")
        self.assertEqual(stored["capture_id"], result["capture_id"])
        self.assertEqual(stored["http_status"], 200)
        # Clock reads: future-date guard, SAS expiry check, blob dispatch, then
        # completion of the full body. requested_at and response_at are the last two.
        self.assertEqual(stored["requested_at"], "2026-10-01T12:00:00.500000Z")
        self.assertEqual(stored["response_at"], "2026-10-01T12:00:00.750000Z")
        self.assert_no_persisted_secrets()

    def test_legacy_identity_comes_only_from_requested_path_routing(self):
        legacy = self.parquet(legacy=True)
        result = self.run_acquire(sas_response(), blob_response(legacy), trade_date="2025-12-30")
        self.assertEqual(result["schema_version"], contract.LEGACY_SCHEMA_VERSION)
        self.assertEqual(result["ticker_date_evidence"], "REQUESTED_PATH_ONLY")
        self.assertEqual(urlsplit(self.transport.requests[1][0]).path,
                         "/trading-data-v2/done_detail/20251230/STOCK/DEWA.parquet")
        recent = self.acquire_bytes(self.parquet())
        self.assertEqual(recent["ticker_date_evidence"], "SOURCE_COLUMNS_AND_REQUESTED_PATH")

    def test_invalid_locator_and_future_date_make_no_request(self):
        for ticker, trade_date in (("dewa", "2026-10-01"), ("DEWA/../X", "2026-10-01"), ("D", "2026-10-01"),
                                   ("DEWA", "2026-10-1"), ("DEWA", "2026-02-30"), ("DEWA", "2026-10-02")):
            with self.subTest(ticker=ticker, trade_date=trade_date):
                self.refused(sas_response(), ticker=ticker, trade_date=trade_date)
                self.assertEqual(self.transport.requests, [])

    def test_endpoint_must_be_confirmed(self):
        unconfirmed = acquire.SasEndpoint(host="api.example.test", path="/x", json_field="sasToken")
        for endpoint in (None, unconfirmed):
            error = self.refused(sas_response(), endpoint=endpoint)
            self.assertIn("Phase-0", str(error))
            self.assertEqual(self.transport.requests, [])

    def test_attempts_are_serialized_per_store(self):
        lock = Path(str(self.db) + ".acquisition-lock")
        lock.parent.mkdir(parents=True)
        with closing(sqlite3.connect(lock, timeout=0, isolation_level=None)) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            error = self.refused(sas_response(), blob_response(self.parquet()))
            self.assertIn("another acquisition", str(error))
            self.assertEqual(self.transport.requests, [])
            holder.execute("ROLLBACK")
        self.assertEqual(self.acquire_bytes(self.parquet())["outcome"], "ACCEPTED")


class RecoveryTests(AcquisitionTestCase):
    def unresolved_first_attempt(self, data):
        """Fail the writer before any body commit, leaving one staged attempt."""
        with patch.object(capture.TradeCaptureStore, "ingest",
                          side_effect=sqlite3.OperationalError("disk I/O error")):
            result = self.acquire_bytes(data)
        self.assertEqual(result["outcome"], "UNRESOLVED")
        self.assertTrue(result["staging_preserved"])
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.staged(), [result["capture_id"]])
        return result

    def assert_parser_failure_recovers_after_repair(self, data, failure_patch):
        raised = []
        original_ingest = capture.TradeCaptureStore.ingest

        def recording_ingest(store, file, envelope):
            try:
                return original_ingest(store, file, envelope)
            except contract.TradeContractError as error:
                raised.append(type(error))
                raise

        with failure_patch, \
                patch.object(capture.TradeCaptureStore, "ingest", recording_ingest), \
                patch.object(capture, "normalize_parquet", wraps=contract.normalize_parquet) as normalize:
            first = self.acquire_bytes(data)
            self.assertEqual(first["outcome"], "UNRESOLVED")
            self.assertTrue(first["staging_preserved"])
            self.assertEqual(raised, [contract.TradeContractError])
            self.assertEqual(normalize.call_count, 1, "reconciliation must not reparse to classify rejection")
            directory = self.staging / first["capture_id"]
            record_bytes = (directory / "attempt.json").read_bytes()
            record = json.loads(record_bytes)
            self.assertEqual((directory / "body.parquet").read_bytes(), data)
            self.assertEqual(self.counts(), (0, 0))
            retried = self.run_retry(first["capture_id"])
            self.assertEqual((retried["outcome"], retried["capture_id"]),
                             ("UNRESOLVED", first["capture_id"]))
            self.assertEqual(normalize.call_count, 2)
            self.assertEqual((directory / "body.parquet").read_bytes(), data)
            self.assertEqual((directory / "attempt.json").read_bytes(), record_bytes)
            self.assertEqual(self.staged(), [first["capture_id"]])
            self.assert_no_persisted_secrets()

        result = self.run_retry(first["capture_id"])
        self.assertEqual((result["outcome"], result["capture_id"]), ("ACCEPTED", first["capture_id"]))
        metadata = self.reader_verify(first["capture_id"])
        self.assertEqual({key: metadata[key] for key in record["envelope"]}, record["envelope"])
        self.assertEqual(metadata["raw_response_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(metadata["content_length"], len(data))
        self.assertEqual(self.raw_objects()[0].read_bytes(), data)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.staged(), [])

    def test_missing_parser_dependency_preserves_bytes_until_environment_repair(self):
        data = self.parquet()
        self.assert_parser_failure_recovers_after_repair(
            data, patch.dict(sys.modules, {"pyarrow": None, "pyarrow.parquet": None}))

    def test_parser_resource_failure_preserves_bytes_until_environment_repair(self):
        import pyarrow.parquet as pq

        data = self.parquet()
        self.assert_parser_failure_recovers_after_repair(
            data, patch.object(pq, "ParquetFile", side_effect=MemoryError(f"parser sig={SIGNATURE}")))

    def test_stable_capture_id_retry(self):
        data = self.parquet()
        first = self.unresolved_first_attempt(data)
        attempt_dir = self.staging / first["capture_id"]
        self.assertEqual((attempt_dir / "body.parquet").read_bytes(), data)
        staged_record = json.loads((attempt_dir / "attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(staged_record["envelope"]["capture_id"], first["capture_id"])
        self.assert_no_persisted_secrets()
        # A new acquisition of the same ticker-day waits for the staged attempt.
        error = self.refused(sas_response(), blob_response(data))
        self.assertIn("retry", str(error))
        self.assertEqual(self.transport.requests, [])
        result = self.run_retry(first["capture_id"])
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(result["capture_id"], first["capture_id"])
        self.assertEqual((result["requested_at"], result["response_at"]),
                         (first["requested_at"], first["response_at"]))
        metadata = self.reader_verify(first["capture_id"])
        self.assertEqual(metadata["raw_response_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual({key: metadata[key] for key in staged_record["envelope"]}, staged_record["envelope"])
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.staged(), [])
        with self.assertRaises(acquire.AcquisitionError) as caught:
            self.run_retry(first["capture_id"])
        self.assert_safe_error(caught.exception)
        for bad in ("../x", "a b", "TOKEN=eyJx", ""):
            with self.assertRaises((acquire.AcquisitionError, contract.TradeContractError)):
                self.run_retry(bad)

    def test_retry_refuses_altered_staged_bytes(self):
        first = self.unresolved_first_attempt(self.parquet())
        body = self.staging / first["capture_id"] / "body.parquet"
        body.write_bytes(self.parquet([source_row(STK_VOLM=200)]))
        with self.assertRaises(acquire.AcquisitionError) as caught:
            self.run_retry(first["capture_id"])
        self.assertIn("staged", str(caught.exception))
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.staged(), [first["capture_id"]])

    def test_interrupted_acceptance_is_reconciled_in_the_same_run(self):
        original_resume = capture.TradeCaptureStore.resume
        calls = []

        def interrupted_resume(store, capture_id):
            calls.append(capture_id)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return original_resume(store, capture_id)

        with patch.object(capture.TradeCaptureStore, "resume", interrupted_resume):
            result = self.acquire_bytes(self.parquet())
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(calls, [result["capture_id"], result["capture_id"]])
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.staged(), [])

    def test_crash_before_acknowledgement_preserves_attempt_until_retry(self):
        data = self.parquet()
        with patch.object(acquire, "_independent_verify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.acquire_bytes(data)
        self.assertEqual(self.counts(), (1, 1))
        (capture_id,) = self.staged()
        result = self.run_retry(capture_id)
        self.assertEqual((result["outcome"], result["capture_id"], result["observation_seq"]),
                         ("ACCEPTED", capture_id, 1))
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.staged(), [])

    def test_pending_predecessor_is_resumed_before_new_observation(self):
        data = self.parquet()
        predecessor = contract.CaptureEnvelope(
            ticker="DEWA", trade_date="2026-10-01", capture_id="manual-pending",
            requested_at="2026-10-01T11:00:00Z", response_at="2026-10-01T11:00:01Z")
        fixture = self.root / "fixtures" / "pending.parquet"
        fixture.write_bytes(data)
        with capture.TradeCaptureStore(self.db, self.raw) as store:
            with patch.object(capture.TradeCaptureStore, "resume",
                              side_effect=sqlite3.OperationalError("crash before acceptance")):
                with self.assertRaises(sqlite3.OperationalError):
                    store.ingest(fixture, predecessor)
        self.assertEqual(self.counts(), (1, 0))
        result = self.acquire_bytes(data)
        self.assertEqual((result["outcome"], result["observation_seq"], result["observation_state"]),
                         ("ACCEPTED", 2, "CONTENT_REPEAT"))
        self.assertEqual(self.counts(), (2, 2))
        self.assertIsNotNone(self.reader_verify("manual-pending")["durable_accepted_at"])

    def test_writer_closes_before_independent_read_only_verify(self):
        events = []

        class RecordingStore(capture.TradeCaptureStore):
            def __init__(store, db, raw_root=None, *, read_only=False):
                super().__init__(db, raw_root, read_only=read_only)
                events.append(("open", read_only, store.db, store.raw_root))

            def close(store):
                events.append(("close", store.read_only))
                super().close()

            def verify(store, capture_id):
                events.append(("verify", store.read_only))
                return super().verify(capture_id)

        with patch.object(acquire, "TradeCaptureStore", RecordingStore):
            result = self.acquire_bytes(self.parquet())
        self.assertEqual(result["outcome"], "ACCEPTED")
        opens = [event for event in events if event[0] == "open"]
        self.assertEqual([event[1] for event in opens], [False, True])
        self.assertTrue(all(event[2:] == (self.db.resolve(), self.raw.resolve()) for event in opens))
        reader_open = events.index(opens[1])
        self.assertIn(("close", False), events[:reader_open])
        self.assertEqual(events[reader_open + 1:], [("verify", True), ("close", True)])

    def test_failed_or_mismatched_independent_verify_preserves_staging(self):
        data = self.parquet()
        refusal = contract.TradeContractError("read-only inspection requires a quiescent checkpointed database")
        with patch.object(acquire, "_read_only_verify", side_effect=refusal):
            result = self.acquire_bytes(data)
        self.assertEqual(result["outcome"], "UNRESOLVED")
        self.assertEqual(self.staged(), [result["capture_id"]])
        self.assertEqual(self.run_retry(result["capture_id"])["outcome"], "ACCEPTED")
        original = acquire._read_only_verify

        def tampered(db, raw_root, capture_id):
            metadata = original(db, raw_root, capture_id)
            return dict(metadata, raw_response_sha256="0" * 64)

        with patch.object(acquire, "_read_only_verify", tampered):
            result = self.acquire_bytes(data)
        self.assertEqual(result["outcome"], "INTEGRITY_MISMATCH")
        self.assertEqual(self.staged(), [result["capture_id"]])
        self.assertEqual(self.run_retry(result["capture_id"])["outcome"], "ACCEPTED")
        self.assertEqual(self.staged(), [])


class TerminalCleanupTests(AcquisitionTestCase):
    def exercise_cleanup_interruption(self, boundary):
        data = self.parquet()
        attempts = []
        original_discard = acquire._discard
        original_replace = os.replace
        original_unlink = Path.unlink
        original_rmdir = Path.rmdir
        original_fsync = acquire._fsync_dir

        def discard(attempt):
            attempts.append(attempt)
            return original_discard(attempt)

        def replace(source, destination):
            if (boundary == "rename" and Path(source).parent == self.staging
                    and Path(destination).name.endswith(".cleanup")):
                raise KeyboardInterrupt
            return original_replace(source, destination)

        def unlink(path, *args, **kwargs):
            result = original_unlink(path, *args, **kwargs)
            if path.parent.name.endswith(".cleanup") and (
                    (boundary == "body deletion" and path.name == "body.parquet")
                    or (boundary == "record deletion" and path.name == "attempt.json")):
                raise KeyboardInterrupt
            return result

        def rmdir(path):
            if boundary == "directory removal" and path.name.endswith(".cleanup"):
                raise KeyboardInterrupt
            return original_rmdir(path)

        def fsync(path):
            if Path(path) == self.staging and attempts:
                tombstone = self.staging / (attempts[0].envelope.capture_id + ".cleanup")
                if ((boundary == "after rename" and tombstone.is_dir())
                        or (boundary == "final fsync" and not any(self.staging.iterdir()))):
                    raise KeyboardInterrupt
            return original_fsync(path)

        with patch.object(acquire, "_discard", discard), \
                patch.object(os, "replace", replace), patch.object(Path, "unlink", unlink), \
                patch.object(Path, "rmdir", rmdir), patch.object(acquire, "_fsync_dir", fsync):
            with self.assertRaises(KeyboardInterrupt):
                self.acquire_bytes(data)

        self.assertEqual(len(attempts), 1)
        capture_id = attempts[0].envelope.capture_id
        active = self.staging / capture_id
        tombstone = self.staging / (capture_id + ".cleanup")
        verified = self.reader_verify(capture_id)
        self.assertEqual(verified["raw_response_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(verified["content_length"], len(data))
        self.assertIsNotNone(verified["durable_accepted_at"])
        self.assertEqual(self.counts(), (1, 1))
        discovered = acquire._staged_attempts(self.staging)
        self.assertEqual([attempt.envelope.capture_id for attempt in discovered],
                         [capture_id] if boundary == "rename" else [])
        if boundary == "rename":
            self.assertTrue(active.is_dir())
            self.assertFalse(tombstone.exists())
            self.assertEqual((active / "body.parquet").read_bytes(), data)
        else:
            self.assertFalse(active.exists())
            self.assertEqual(tombstone.exists(), boundary != "final fsync")
        if boundary == "after rename":
            self.assertEqual((tombstone / "body.parquet").read_bytes(), data)
            self.assertTrue((tombstone / "attempt.json").is_file())
        if boundary == "body deletion":
            self.assertFalse((tombstone / "body.parquet").exists())
            self.assertTrue((tombstone / "attempt.json").is_file())
        if boundary in ("record deletion", "directory removal"):
            self.assertEqual(list(tombstone.iterdir()), [])

        unrelated = self.parquet([source_row(STK_CODE="BBCA")])
        if tombstone.exists():
            # A permanently unavailable cleanup residue must never enter active
            # discovery or prevent a different ticker-day from being captured.
            def deny_unlink(path, *args, **kwargs):
                if path.parent == tombstone:
                    raise PermissionError("synthetic unavailable cleanup")
                return original_unlink(path, *args, **kwargs)

            def deny_rmdir(path):
                if path == tombstone:
                    raise PermissionError("synthetic unavailable cleanup")
                return original_rmdir(path)

            with patch.object(Path, "unlink", deny_unlink), patch.object(Path, "rmdir", deny_rmdir):
                result = self.acquire_bytes(unrelated, ticker="BBCA")
            self.assertEqual(result["outcome"], "ACCEPTED")
            self.assertTrue(tombstone.is_dir())
            self.assertEqual(acquire._staged_attempts(self.staging), [])
            # On a subsequent clean restart, the same tombstone is removed.
            self.assertEqual(self.acquire_bytes(unrelated, ticker="BBCA")["outcome"], "ACCEPTED")
        else:
            self.assertEqual(self.acquire_bytes(unrelated, ticker="BBCA")["outcome"], "ACCEPTED")
        if active.exists():
            result = self.run_retry(capture_id)
            self.assertEqual((result["outcome"], result["capture_id"]), ("ACCEPTED", capture_id))
        self.assertEqual(self.reader_verify(capture_id), verified)
        self.assertEqual(self.staged(), [])
        self.assert_no_persisted_secrets()

    def test_terminal_rename_interruption_keeps_a_readable_active_attempt(self):
        self.exercise_cleanup_interruption("rename")

    def test_interruption_after_terminal_rename_uses_recoverable_tombstone(self):
        self.exercise_cleanup_interruption("after rename")

    def test_interruption_after_body_deletion_uses_recoverable_tombstone(self):
        self.exercise_cleanup_interruption("body deletion")

    def test_interruption_after_record_deletion_uses_recoverable_tombstone(self):
        self.exercise_cleanup_interruption("record deletion")

    def test_directory_removal_failure_uses_recoverable_tombstone(self):
        self.exercise_cleanup_interruption("directory removal")

    def test_final_cleanup_fsync_interruption_preserves_accepted_evidence(self):
        self.exercise_cleanup_interruption("final fsync")

    def test_terminal_discard_without_committed_capture_uses_the_same_safe_namespace(self):
        capture_id = "e4f779a3-08f5-47a1-86f6-4fded77da6a9"
        envelope = contract.CaptureEnvelope(
            ticker="DEWA", trade_date="2026-10-01", capture_id=capture_id,
            requested_at="2026-10-01T12:00:00Z", response_at="2026-10-01T12:00:01Z",
            created_by=acquire.CREATED_BY)
        attempt = acquire._stage(self.staging, envelope, b"malformed staged input")
        original_unlink = Path.unlink

        def interrupt(path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            if path.name == "attempt.json":
                raise KeyboardInterrupt

        with patch.object(Path, "unlink", interrupt), self.assertRaises(KeyboardInterrupt):
            acquire._discard(attempt)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(acquire._staged_attempts(self.staging), [])
        self.assertEqual(self.staged(), [capture_id + ".cleanup"])
        result = self.acquire_bytes(self.parquet([source_row(STK_CODE="BBCA")]), ticker="BBCA")
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.staged(), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_cleanup_tombstone_symlink_never_deletes_external_files(self):
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "attempt.json"
        sentinel.write_bytes(b"external evidence")
        self.staging.mkdir(parents=True)
        tombstone = self.staging / "b89bcf13-546d-4772-b37c-cb926a1fc900.cleanup"
        try:
            tombstone.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        result = self.acquire_bytes(self.parquet())
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(sentinel.read_bytes(), b"external evidence")
        self.assertTrue(tombstone.is_symlink())
        self.assertEqual(acquire._staged_attempts(self.staging), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_cleanup_unlinks_reserved_child_symlink_without_following_it(self):
        sentinel = self.root / "external-evidence"
        sentinel.write_bytes(b"external evidence")
        tombstone = self.staging / "668fd95b-f4d3-412f-bb2a-cc47d973cda3.cleanup"
        tombstone.mkdir(parents=True)
        try:
            (tombstone / "body.parquet").symlink_to(sentinel)
        except OSError:
            self.skipTest("symlink creation unavailable")
        (tombstone / "attempt.json").write_bytes(b"discarded record")
        result = self.acquire_bytes(self.parquet())
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(sentinel.read_bytes(), b"external evidence")
        self.assertEqual(self.staged(), [])

    def test_cleanup_does_not_recurse_through_unknown_children_or_block_acquisition(self):
        tombstone = self.staging / "61f862bb-c809-4d74-b9b3-c1a226da8fe5.cleanup"
        unknown = tombstone / "unexpected-directory"
        unknown.mkdir(parents=True)
        sentinel = unknown / "keep"
        sentinel.write_bytes(b"unrecognized contents")
        result = self.acquire_bytes(self.parquet())
        self.assertEqual(result["outcome"], "ACCEPTED")
        self.assertEqual(sentinel.read_bytes(), b"unrecognized contents")
        self.assertEqual(acquire._staged_attempts(self.staging), [])
        self.assertEqual(self.staged(), [tombstone.name])


class CommandLineTests(AcquisitionTestCase):
    def main(self, argv, *, stdin=None, transport=None):
        out, err = io.StringIO(), io.StringIO()
        factory_calls = []

        def factory():
            factory_calls.append(True)
            return transport

        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            if stdin is not None:
                stack.enter_context(patch.object(sys, "stdin", stdin))
            try:
                code = acquire.main(argv, transport_factory=factory)
            except SystemExit as exit_:
                code = exit_.code
        self.assert_clean_text(out.getvalue(), err.getvalue(), self.logs.getvalue())
        return code, out.getvalue(), err.getvalue(), factory_calls

    def acquire_argv(self, *extra):
        return ["acquire", "--ticker", "DEWA", "--trade-date", "2026-10-01", "--db", str(self.db),
                "--raw-root", str(self.raw), "--staging-dir", str(self.staging), *extra]

    def test_shipped_live_endpoint_is_unconfigured(self):
        self.assertIsNone(acquire.LIVE_SAS_ENDPOINT)

    def test_token_arguments_are_refused_without_echo(self):
        with patch.object(getpass, "getpass", side_effect=AssertionError("prompted")):
            for extra in (["--token", TOKEN], ["--token=" + TOKEN], [TOKEN], ["--access-token", TOKEN],
                          ["--tok", TOKEN]):
                code, _, _, calls = self.main(self.acquire_argv(*extra))
                self.assertEqual(code, 2)
                self.assertEqual(calls, [])
        self.assertFalse(self.db.exists())

    def test_unconfigured_or_unconfirmed_endpoint_refused_before_prompt(self):
        unconfirmed = acquire.SasEndpoint(host="api.example.test", path="/x", json_field="sasToken")
        for endpoint in (None, unconfirmed):
            with patch.object(acquire, "LIVE_SAS_ENDPOINT", endpoint), \
                    patch.object(getpass, "getpass", side_effect=AssertionError("prompted")):
                code, _, err, calls = self.main(self.acquire_argv())
            self.assertEqual(code, 2)
            self.assertIn("Phase-0", err)
            self.assertEqual(calls, [])
        self.assertFalse(self.db.exists())

    def test_non_interactive_token_input_refused(self):
        with patch.object(acquire, "LIVE_SAS_ENDPOINT", ENDPOINT), \
                patch.object(getpass, "getpass", side_effect=AssertionError("prompted")):
            code, _, err, calls = self.main(self.acquire_argv(), stdin=io.StringIO(TOKEN + "\n"))
        self.assertEqual(code, 2)
        self.assertIn("interactive", err)
        self.assertEqual(calls, [])

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        def echoing_getpass(prompt=""):
            import warnings
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
            return TOKEN

        with patch.object(acquire, "LIVE_SAS_ENDPOINT", ENDPOINT), \
                patch.object(getpass, "getpass", echoing_getpass):
            code, _, err, calls = self.main(self.acquire_argv(), stdin=Terminal())
        self.assertEqual(code, 2)
        self.assertIn("hidden", err)
        self.assertEqual(calls, [])

    def test_interactive_acquisition_and_offline_retry(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        transport = FakeTransport([sas_response(), blob_response(self.parquet())])
        with patch.object(acquire, "LIVE_SAS_ENDPOINT", ENDPOINT), \
                patch.object(acquire, "_utc_clock", self.clock), \
                patch.object(getpass, "getpass", return_value=TOKEN) as prompt:
            code, out, _, calls = self.main(self.acquire_argv(), stdin=Terminal(), transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(transport.closed)
        self.assertEqual(prompt.call_count, 1)
        result = json.loads(out)
        self.assertEqual((result["outcome"], result["ticker"], result["observation_seq"]), ("ACCEPTED", "DEWA", 1))

        with patch.object(capture.TradeCaptureStore, "ingest", side_effect=OSError("disk full")):
            unresolved = self.acquire_bytes(self.parquet([source_row(STK_VOLM=500)]))
        with patch.object(getpass, "getpass", side_effect=AssertionError("prompted")):
            code, out, _, calls = self.main(["retry", "--capture-id", unresolved["capture_id"],
                                             "--db", str(self.db), "--raw-root", str(self.raw),
                                             "--staging-dir", str(self.staging)])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(json.loads(out)["outcome"], "ACCEPTED")
        self.assert_no_persisted_secrets()

    def test_cli_failures_print_fixed_messages(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        transport = FakeTransport([sas_response(status=401)])
        with patch.object(acquire, "LIVE_SAS_ENDPOINT", ENDPOINT), \
                patch.object(acquire, "_utc_clock", self.clock), \
                patch.object(getpass, "getpass", return_value=TOKEN):
            code, out, err, _ = self.main(self.acquire_argv(), stdin=Terminal(), transport=transport)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not authorized", err)
        transport = FakeTransport([sas_response(), not_found()])
        with patch.object(acquire, "LIVE_SAS_ENDPOINT", ENDPOINT), \
                patch.object(acquire, "_utc_clock", self.clock), \
                patch.object(getpass, "getpass", return_value=TOKEN):
            code, out, _, _ = self.main(self.acquire_argv(), stdin=Terminal(), transport=transport)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["outcome"], "ABSENCE_NOT_RECORDED")
        self.assertEqual(self.counts(), (0, 0))

    def test_cli_cleanup_failure_returns_one_after_durable_acceptance(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        data = self.parquet()
        transport = FakeTransport([sas_response(), blob_response(data)])
        original_rmdir = Path.rmdir

        def fail_terminal_rmdir(path):
            if path.name.endswith(".cleanup"):
                raise OSError(f"synthetic cleanup failure sig={SIGNATURE}")
            return original_rmdir(path)

        with patch.object(acquire, "LIVE_SAS_ENDPOINT", ENDPOINT), \
                patch.object(acquire, "_utc_clock", self.clock), \
                patch.object(getpass, "getpass", return_value=TOKEN), \
                patch.object(Path, "rmdir", fail_terminal_rmdir):
            code, out, err, calls = self.main(self.acquire_argv(), stdin=Terminal(), transport=transport)
        self.assertEqual((code, out, err), (1, "", "acquisition failed\n"))
        self.assertEqual(len(calls), 1)
        self.assertTrue(transport.closed)
        self.assertEqual(self.counts(), (1, 1))
        (tombstone_name,) = self.staged()
        self.assertTrue(tombstone_name.endswith(".cleanup"))
        self.assertEqual(list((self.staging / tombstone_name).iterdir()), [])
        capture_id = tombstone_name.removesuffix(".cleanup")
        verified = self.reader_verify(capture_id)
        self.assertIsNotNone(verified["durable_accepted_at"])
        self.assertEqual(verified["raw_response_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(verified["content_length"], len(data))

        unrelated = self.acquire_bytes(self.parquet([source_row(STK_CODE="BBCA")]), ticker="BBCA")
        self.assertEqual(unrelated["outcome"], "ACCEPTED")
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.reader_verify(capture_id), verified)
        self.assertEqual(self.staged(), [])
        self.assert_no_persisted_secrets()


class _QuietHandler(BaseHTTPRequestHandler):
    received = []

    def do_GET(self):
        self.received.append({name.lower(): value for name, value in self.headers.items()})
        self.send_response(200)
        self.send_header("Set-Cookie", "session=TOKENCANARY; Path=/")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class RequestsTransportTests(AcquisitionTestCase):
    def test_session_is_environment_blind_redirect_free_and_verified(self):
        import requests

        seen = {}

        def fake_get(session, url, **kwargs):
            seen.update(kwargs, trust_env=session.trust_env, url=url)
            raise requests.ConnectionError(f"Max retries exceeded with url: {CLEAN_PATH}?sig={SIGNATURE}")

        with patch.object(requests.Session, "get", fake_get):
            transport = acquire.RequestsTransport()
            try:
                with self.assertRaises(acquire.AcquisitionError) as caught:
                    acquire._dispatch(transport, f"https://{acquire.BLOB_HOST}{CLEAN_PATH}?{sas_query()}", {})
            finally:
                transport.close()
        self.assert_safe_error(caught.exception)
        self.assertFalse(seen["trust_env"])
        self.assertIs(seen["allow_redirects"], False)
        self.assertIs(seen["verify"], True)
        self.assertIs(seen["stream"], True)
        self.assertTrue(seen["timeout"])
        for name in ("urllib3", "urllib3.connectionpool"):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING)

    def test_loopback_request_logs_no_signed_url_and_body_errors_are_replaced(self):
        import requests

        _QuietHandler.received = []
        server = HTTPServer(("127.0.0.1", 0), _QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        logging.getLogger("urllib3").setLevel(logging.DEBUG)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.DEBUG)
        transport = acquire.RequestsTransport()
        try:
            url = f"http://127.0.0.1:{server.server_port}{CLEAN_PATH}?{sas_query()}"
            response = acquire._dispatch(transport, url, {"Accept-Encoding": "identity"})
            try:
                self.assertEqual(response.status, 200)
                self.assertEqual(acquire._read_body(response, 16), b"ok")
            finally:
                response.close()

            def broken(*args, **kwargs):
                raise requests.exceptions.ChunkedEncodingError(f"IncompleteRead {CLEAN_PATH}?sig={SIGNATURE}")

            response = acquire._dispatch(transport, url, {})
            try:
                with patch.object(response._response, "iter_content", broken):
                    with self.assertRaises(acquire.AcquisitionError) as caught:
                        acquire._read_body(response, 16)
            finally:
                response.close()
            self.assert_safe_error(caught.exception)
        finally:
            transport.close()
        self.assert_clean_text(self.logs.getvalue())
        first, second = _QuietHandler.received[-2:]
        self.assertEqual(first.get("accept-encoding"), "identity")
        self.assertNotIn("cookie", second)
        self.assertNotIn("authorization", second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
