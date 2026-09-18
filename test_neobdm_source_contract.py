"""Tests for the NeoBDM market-summary source-contract repair (2026-09-14 incident:
is_unusual_volume retired by NeoBDM). No network, no Playwright session.

    py -3 -m pytest test_neobdm_source_contract.py -v
"""

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _secret in ("NEOBDM_USERNAME", "NEOBDM_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.setdefault(_secret, "test-placeholder")

import check_signal_integrity as csi  # noqa: E402
import migrate_neobdm_source_lifecycle as mig  # noqa: E402
import neobdm_scraper as ns  # noqa: E402
import neobdm_source_contract as nsc  # noqa: E402

ACTIVE = list(nsc.ACTIVE_REQUEST_COLUMNS)
BEFORE, AFTER = "2026-09-10", "2026-09-15"   # capture regimes either side of the retirement
SECRET = "SECRET-CSRF-TOKEN-XYZ"


@pytest.fixture
def tmpdir_path():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def row(symbol, **extra):
    base = {"symbol": symbol, "close": 1000, "high": 1010, "low": 990, "m_dn_0": 0.01, "nr_dn_0": -0.02,
            "f_dn_0": 0.0, "m_cn_5": 0.03, "m_dn_3": 0.02, "top_5_buyer": ["AK", "BK"], "clean_score": 3,
            "tval": 1.5, "market_cap_t": 2.1, "pct_5": 0.04}
    base.update(extra)
    return base


class FakeResponse:
    def __init__(self, obj, status=200, content_type="application/json"):
        self._body = json.dumps(obj).encode("utf-8") if not isinstance(obj, bytes) else obj
        self.status = status
        self.headers = {"content-type": content_type, "set-cookie": f"sessionid={SECRET}"}

    def body(self):
        return self._body

    def text(self):
        return self._body.decode("utf-8")


# ── source presence ───────────────────────────
def test_presence_distinguishes_absent_null_false_true():
    r = {"a": None, "b": False, "c": True, "d": 0}
    assert [nsc.presence_state(r, k) for k in ("missing", "a", "b", "c", "d")] == [
        nsc.KEY_ABSENT, nsc.EXPLICIT_NULL, nsc.FALSE, nsc.TRUE, nsc.VALUE]
    assert nsc.flag_state(None) is None and nsc.flag_state(False) == nsc.FALSE and nsc.flag_state(True) == nsc.TRUE
    assert nsc.flag_state(0) == nsc.FALSE and nsc.flag_state(1) == nsc.TRUE and nsc.flag_state("maybe") is None


def test_retired_is_not_false():
    assert nsc.lifecycle_state("is_unusual_volume", AFTER) == nsc.RETIRED
    assert nsc.lifecycle_state("is_unusual_volume", BEFORE) == nsc.ACTIVE     # never reaches back
    healthy_false = ns.screen_market_summary([row("AAAA", is_unusual_volume=False)], nsc.AVAILABLE, BEFORE)
    retired = ns.screen_market_summary([row("AAAA")], nsc.RETIRED, AFTER)
    assert healthy_false.status == nsc.NO_HITS and retired.status == nsc.RETIRED_SOURCE


# ── raw capture ───────────────────────────────
def test_raw_capture_is_content_addressed_deterministic_and_write_once(tmpdir_path):
    raw = b'{"success": true, "data": [], "trace_id": "t1"}'
    sha, rel, written = nsc.store_raw_bytes(raw, tmpdir_path)
    assert sha == hashlib.sha256(raw).hexdigest() and rel == f"market_summary_raw_fragments/{sha[:2]}/{sha}.json.gz"
    assert written and nsc.read_raw_bytes(rel, tmpdir_path) == raw
    first = open(os.path.join(tmpdir_path, *rel.split("/")), "rb").read()
    assert nsc.store_raw_bytes(raw, tmpdir_path) == (sha, rel, False)              # idempotent
    other = tempfile.mkdtemp()
    try:
        nsc.store_raw_bytes(raw, other)
        assert open(os.path.join(other, *rel.split("/")), "rb").read() == first   # deterministic gzip
    finally:
        shutil.rmtree(other)


def test_raw_capture_never_overwrites_different_bytes(tmpdir_path):
    raw = b'{"a": 1}'
    sha, rel, _ = nsc.store_raw_bytes(raw, tmpdir_path)
    path = os.path.join(tmpdir_path, *rel.split("/"))
    with open(path, "wb") as fh:
        fh.write(gzip.compress(b'{"a": 2}', mtime=0))
    tampered = open(path, "rb").read()
    with pytest.raises(nsc.RawIntegrityError):
        nsc.store_raw_bytes(raw, tmpdir_path)
    assert open(path, "rb").read() == tampered


def test_recorder_writes_no_secret_bearing_metadata(tmpdir_path):
    rec = nsc.CaptureRecorder(root=tmpdir_path)
    parsed = rec.record("POST", "/market-summary/summary/sid", FakeResponse(
        {"success": True, "timestamp": "2026-09-15T16:11:31Z", "trace_id": "abc", "data": [row("AAAA")]}),
        schema=nsc.summary_response_schema(ACTIVE + ["symbol_1"]), screener_id="sid", page=1)
    assert parsed["trace_id"] == "abc"
    meta = rec.records[0]
    assert (meta["source_timestamp"], meta["trace_id"], meta["http_status"], meta["page"]) == \
        ("2026-09-15T16:11:31Z", "abc", 200, 1)
    assert set(meta) == {"seq", "method", "endpoint", "screener_id", "page", "http_status", "content_type",
                         "captured_utc", "source_timestamp", "trace_id", "raw_kind", "projection", "dropped_paths",
                         "body_sha256", "body_bytes", "raw_sha256", "raw_bytes", "fragment_path"}
    assert meta["dropped_paths"] == []
    assert meta["raw_kind"] == nsc.RAW_KIND_RAW and meta["body_sha256"] == meta["raw_sha256"]
    stored = b"".join(gzip.decompress(open(os.path.join(dp, f), "rb").read())
                      for dp, _, fs in os.walk(tmpdir_path) for f in fs)
    assert SECRET.encode() not in stored and SECRET not in json.dumps(rec.records)


def stored_fragments(root):
    return b"".join(gzip.decompress(open(os.path.join(dp, f), "rb").read())
                    for dp, _, fs in os.walk(root) for f in fs if f.endswith(".json.gz"))


def test_account_scoped_list_endpoint_keeps_only_allowlisted_fields_of_the_capture_object(tmpdir_path):
    rec = nsc.CaptureRecorder(root=tmpdir_path)
    body = {"success": True, "timestamp": "2026-09-16T00:00:00Z", "trace_id": None,
            "meta": {"last_page": 1, "user": {"email": "someone@example.com"}, "someone@example.com": 1},
            "profile": {"email": "someone@example.com"},
            "data": [{"id": "sid", "name": "DAILY_SCRAPER", "columns": ACTIVE, "owner_email": "someone@example.com",
                      "created_by": {"username": "acct-user"}, "filters": [{"field": "is_liquid", "op": "=",
                      "value": "true", "note": "private-note"}]},
                     {"id": "w", "name": "OtherPrivateScreener", "columns": ["symbol"], "filters": [{"field": "private"}]}]}
    parsed = rec.record("GET", "/screeners", FakeResponse(body), schema=nsc.SCREENERS_RESPONSE_SCHEMA,
                        select=ns._named("DAILY_SCRAPER"))
    assert [s["name"] for s in parsed["data"]] == ["DAILY_SCRAPER", "OtherPrivateScreener"]     # caller still sees everything
    meta, stored = rec.records[0], stored_fragments(tmpdir_path)
    assert meta["raw_kind"] == nsc.RAW_KIND_PROJECTED and meta["projection"] == "data[name=DAILY_SCRAPER]"
    assert meta["body_sha256"] == hashlib.sha256(FakeResponse(body).body()).hexdigest() != meta["raw_sha256"]
    assert json.loads(stored) == {
        "success": True, "timestamp": "2026-09-16T00:00:00Z", "trace_id": None, "meta": {"last_page": 1},
        "data": [{"id": "sid", "name": "DAILY_SCRAPER", "columns": ACTIVE,
                  "filters": [{"field": "is_liquid", "op": "=", "value": "true"}]}]}
    for leak in (b"OtherPrivateScreener", b"private", b"someone@example.com", b"profile", b"acct-user", b"owner_email"):
        assert leak not in stored
    assert meta["dropped_paths"] == ["$.data[].created_by", "$.data[].filters[].note", "$.data[].owner_email",
                                     "$.meta.<non-identifier key>", "$.meta.user", "$.profile"]
    assert "someone@example.com" not in json.dumps(rec.records)


def test_recorder_is_fail_closed_without_a_schema(tmpdir_path):
    rec = nsc.CaptureRecorder(root=tmpdir_path)
    rec.record("GET", "/anything", FakeResponse({"success": True, "data": [{"email": "someone@example.com"}]}))
    assert rec.records[0]["raw_kind"] == nsc.RAW_KIND_NOT_STORED and not any(fs for _, _, fs in os.walk(tmpdir_path))
    import inspect
    assert inspect.signature(ns._json_of).parameters["schema"].default is inspect.Parameter.empty


def test_market_data_body_with_an_unexpected_field_is_sanitized_not_published_raw(tmpdir_path):
    rec = nsc.CaptureRecorder(root=tmpdir_path)
    page = {"success": True, "timestamp": "t", "trace_id": None, "meta": {"last_page": 1},
            "data": [row("AAAA", symbol_1="AAAA", watchlisted_by="acct-123", top_5_buyer={"user": "acct-456"})]}
    rec.record("POST", "/market-summary/summary/sid", FakeResponse(page), schema=nsc.summary_response_schema(ACTIVE + ["symbol_1"]), page=1)
    meta, stored = rec.records[0], stored_fragments(tmpdir_path)
    assert meta["raw_kind"] == nsc.RAW_KIND_SANITIZED
    assert meta["dropped_paths"] == ["$.data[].top_5_buyer", "$.data[].watchlisted_by"]
    assert b"acct-123" not in stored and b"acct-456" not in stored and json.loads(stored)["data"][0]["close"] == 1000
    clean = {"success": True, "timestamp": "t", "trace_id": None, "meta": {"last_page": 1},
             "data": [row("BBBB", symbol_1="BBBB")]}
    rec.record("POST", "/market-summary/summary/sid", FakeResponse(clean), schema=nsc.summary_response_schema(ACTIVE + ["symbol_1"]), page=2)
    assert rec.records[1]["raw_kind"] == nsc.RAW_KIND_RAW
    assert nsc.read_raw_bytes(rec.records[1]["fragment_path"], tmpdir_path) == FakeResponse(clean).body()


def test_duplicate_json_keys_are_never_published_as_raw_bytes(tmpdir_path):
    rec = nsc.CaptureRecorder(root=tmpdir_path)
    body = b'{"success": true, "message": "hidden-account-text", "message": "ok", "data": []}'
    rec.record("GET", "/market-summary/columns", FakeResponse(body), schema=nsc.CATALOG_RESPONSE_SCHEMA)
    assert rec.records[0]["raw_kind"] == nsc.RAW_KIND_SANITIZED
    assert b"hidden-account-text" not in stored_fragments(tmpdir_path)


def test_workflow_stages_only_the_database_and_the_raw_fragment_store():
    import re
    text = "\n".join(line for line in open(os.path.join(HERE, ".github", "workflows", "daily-scrape.yml"),
                                           encoding="utf-8").read().splitlines()
                     if not line.lstrip().startswith("#"))
    adds = re.findall(r"^\s*(?:if .*then\s+)?git add (.+?)\s*(?:;|$)", text, flags=re.M)
    assert [a.strip() for a in adds] == ["neobdm.db", "market_summary_raw_fragments/"]
    assert not re.search(r"git add (-A|--all|\.(\s|$)|-u)", text) and "git commit -a" not in text


def test_non_json_body_such_as_a_login_page_is_never_stored(tmpdir_path):
    rec = nsc.CaptureRecorder(root=tmpdir_path)
    page = (f'<html><form><input name="csrfmiddlewaretoken" value="{SECRET}">'
            f'<input name="password"></form></html>').encode()
    assert rec.record("POST", "/market-summary/summary/sid", FakeResponse(page, content_type="text/html"),
                      schema=nsc.summary_response_schema(ACTIVE), screener_id="sid", page=1) is None
    meta = rec.records[0]
    assert meta["raw_kind"] == nsc.RAW_KIND_NOT_STORED and meta["fragment_path"] is None and meta["raw_sha256"] is None
    assert meta["body_sha256"] == hashlib.sha256(page).hexdigest() and meta["content_type"] == "text/html"
    assert not any(fs for _, _, fs in os.walk(tmpdir_path))
    assert SECRET not in json.dumps(rec.records)


# ── contract ──────────────────────────────────
def contract(rows, catalog=None, stored=None, requested=None, capture_date=AFTER):
    requested = ACTIVE if requested is None else requested
    return nsc.evaluate_capture_contract(requested, ACTIVE if catalog is None else catalog,
                                         ACTIVE if stored is None else stored, rows, capture_date=capture_date)


def codes(result, severity=None):
    return {(i["code"], i["field"]) for i in result["issues"] if severity in (None, i["severity"])}


def test_contract_ok_for_the_active_14_column_regime():
    c = contract([row("AAAA"), row("BBBB")])
    assert c["status"] == nsc.CONTRACT_OK and c["availability"]["is_unusual_volume"] == nsc.RETIRED
    assert all(c["availability"][f] == nsc.AVAILABLE for f in ACTIVE) and not c["blocking_codes"]


def test_contract_detects_requested_field_missing_from_catalog_config_and_rows():
    requested = ACTIVE + ["new_flag"]
    rows = [row("AAAA"), row("BBBB")]
    c = contract(rows, catalog=ACTIVE, stored=ACTIVE, requested=requested)
    assert {("REQUESTED_MISSING_FROM_CATALOG", "new_flag"), ("REQUESTED_MISSING_FROM_STORED_CONFIG", "new_flag"),
            ("KEY_ABSENT_IN_RESPONSE", "new_flag")} <= codes(c, nsc.FAIL)
    assert c["status"] == nsc.CONTRACT_FAILED and c["availability"]["new_flag"] == nsc.UNAVAILABLE
    assert not c["blocking_codes"]                        # the other fields are still kept


def test_contract_reports_explicit_null_separately_from_absent_key():
    requested = ACTIVE + ["new_flag"]
    c = contract([row("AAAA", new_flag=None), row("BBBB", new_flag=None)], catalog=requested, stored=requested,
                 requested=requested)
    assert ("EXPLICIT_NULL_ALL_ROWS", "new_flag") in codes(c) and ("KEY_ABSENT_IN_RESPONSE", "new_flag") not in codes(c)
    assert c["presence"]["new_flag"][nsc.EXPLICIT_NULL] == 2 and c["presence"]["new_flag"][nsc.KEY_ABSENT] == 0


def test_contract_flags_a_retired_field_still_requested():
    requested = ACTIVE + ["is_unusual_volume"]
    c = contract([row("AAAA", is_unusual_volume=True)], catalog=requested, stored=requested, requested=requested)
    assert ("RETIRED_FIELD_REQUESTED", "is_unusual_volume") in codes(c, nsc.FAIL)


def test_tolerated_additive_key_allowlist_is_explicit_and_versioned():
    assert set(nsc.TOLERATED_ADDITIVE_KEYS) == {"symbol_1"}
    assert nsc.TOLERATED_ADDITIVE_KEYS["symbol_1"]["alias_of"] == "symbol"
    assert nsc.TOLERATED_ADDITIVE_KEYS_VERSION == "tolerated_additive_keys_v1"


def test_equal_symbol_1_alias_is_recorded_but_does_not_degrade_the_capture():
    c = contract([row("AAAA", symbol_1="AAAA"), row("BBBB", symbol_1="BBBB"), row("CCCC")])
    assert c["status"] == nsc.CONTRACT_OK and not c["blocking_codes"]
    assert c["unexpected_keys"] == ["symbol_1"]                                      # still recorded
    assert ("TOLERATED_ADDITIVE_ALIAS", "symbol_1") in codes(c, nsc.INFO)
    assert not codes(c, nsc.WARN)
    assert c["tolerated_additive_keys"] == {"symbol_1": {"alias_of": "symbol", "rows_checked": 2,
                                                         "allowlist_version": "tolerated_additive_keys_v1"}}


def test_unequal_symbol_1_alias_is_a_blocking_source_contract_failure():
    c = contract([row("AAAA", symbol_1="AAAA"), row("BBBB", symbol_1="XXXX")])
    assert c["status"] == nsc.CONTRACT_FAILED and ("IDENTITY_AMBIGUITY", "symbol_1") in codes(c, nsc.FAIL)
    assert c["blocking_codes"] == ["IDENTITY_AMBIGUITY"] and c["tolerated_additive_keys"] == {}


def test_symbol_1_without_symbol_is_a_blocking_failure():
    orphan = {k: v for k, v in row("AAAA", symbol_1="AAAA").items() if k != "symbol"}
    c = contract([row("BBBB", symbol_1="BBBB"), orphan])
    assert ("ALIAS_WITHOUT_CANONICAL_KEY", "symbol_1") in codes(c, nsc.FAIL)
    assert "ALIAS_WITHOUT_CANONICAL_KEY" in c["blocking_codes"] and c["tolerated_additive_keys"] == {}


def test_genuinely_new_additive_field_still_warns_and_degrades():
    c = contract([row("AAAA", symbol_1="AAAA", is_spike_volume_1=True), row("BBBB", symbol_1="BBBB")])
    assert c["status"] == nsc.CONTRACT_DEGRADED and not c["blocking_codes"]
    assert c["unexpected_keys"] == ["is_spike_volume_1", "symbol_1"]
    assert codes(c, nsc.WARN) == {("UNEXPECTED_KEY", "is_spike_volume_1")}
    other_alias = contract([row("AAAA", symbol_2="AAAA")])        # same shape, but not on the allowlist
    assert codes(other_alias, nsc.WARN) == {("UNEXPECTED_KEY", "symbol_2")}
    assert other_alias["status"] == nsc.CONTRACT_DEGRADED


def test_alias_case_collision_blocks():
    c = contract([row("AAAA", symbol_1="AAAA", SYMBOL_1="AAAA")])
    assert ("CASE_COLLISION", "SYMBOL_1") in codes(c, nsc.FAIL) and "CASE_COLLISION" in c["blocking_codes"]


def test_retired_field_reappearing_in_catalog_config_or_rows_requires_human_review():
    code = nsc.RETIRED_FIELD_REAPPEARED
    assert code == "RETIRED_FIELD_REAPPEARED_REQUIRES_HUMAN_REVIEW"
    in_catalog = contract([row("AAAA")], catalog=ACTIVE + ["is_unusual_volume"])
    in_config = contract([row("AAAA")], stored=ACTIVE + ["is_unusual_volume"])
    in_rows = contract([row("AAAA", is_unusual_volume=True)])
    for c, where in ((in_catalog, "catalog"), (in_config, "stored screener config"), (in_rows, "returned rows")):
        issue = [i for i in c["issues"] if i["code"] == code]
        assert len(issue) == 1 and issue[0]["field"] == "is_unusual_volume" and where in issue[0]["detail"], where
        assert c["status"] == nsc.CONTRACT_DEGRADED and not c["blocking_codes"]        # active contract unaffected
        assert c["availability"]["is_unusual_volume"] == nsc.RETIRED                   # never reactivated
        assert ("STORED_CONFIG_EXTRA_COLUMN", "is_unusual_volume") not in codes(c)
        assert ("UNEXPECTED_KEY", "is_unusual_volume") not in codes(c)
    assert "is_unusual_volume" not in ns.ML_COLUMNS
    assert ns.screen_market_summary([row("AAAA", is_unusual_volume=True)], None, AFTER).status == nsc.RETIRED_SOURCE
    before = contract([row("AAAA")], catalog=ACTIVE + ["is_unusual_volume"], capture_date=BEFORE)
    assert code not in {i["code"] for i in before["issues"]}                        # not retired then


def test_partial_nulls_are_row_level_but_partially_absent_keys_degrade():
    nulls = contract([row("AAAA", m_dn_0=None), row("BBBB"), row("CCCC", m_dn_0=0)])
    assert nulls["status"] == nsc.CONTRACT_OK and nulls["availability"]["m_dn_0"] == nsc.AVAILABLE
    assert ("EXPLICIT_NULL_SOME_ROWS", "m_dn_0") in codes(nulls, nsc.INFO)
    assert nulls["presence"]["m_dn_0"][nsc.EXPLICIT_NULL] == 1 and nulls["presence"]["m_dn_0"]["present_non_null"] == 2
    absent = contract([{k: v for k, v in row("AAAA").items() if k != "m_dn_0"}, row("BBBB")])
    assert absent["status"] == nsc.CONTRACT_DEGRADED and ("KEY_ABSENT_SOME_ROWS", "m_dn_0") in codes(absent, nsc.WARN)


def test_identity_ambiguity_case_collision_and_duplicate_identity_fail_and_block():
    ambiguous = contract([row("AAAA", symbol_1="BBBB")])
    assert ("IDENTITY_AMBIGUITY", "symbol_1") in codes(ambiguous, nsc.FAIL)
    assert ambiguous["blocking_codes"] == ["IDENTITY_AMBIGUITY"]
    case = contract([row("AAAA", Close=5)])
    assert ("CASE_COLLISION", "Close") in codes(case, nsc.FAIL) and case["blocking_codes"] == ["CASE_COLLISION"]
    dupes = contract([row("AAAA"), row("AAAA")])
    assert dupes["blocking_codes"] == ["DUPLICATE_IDENTITY"]


def test_unreadable_catalog_or_config_is_never_assumed_fine():
    c = nsc.evaluate_capture_contract(ACTIVE, None, None, [row("AAAA")], capture_date=AFTER)
    assert {("CATALOG_UNREADABLE", None), ("STORED_CONFIG_UNREADABLE", None)} <= codes(c, nsc.FAIL)


# ── signals ───────────────────────────────────
def test_signal_statuses_follow_source_availability():
    healthy = [row("AAAA", is_unusual_volume=False), row("BBBB", is_unusual_volume=True, m_dn_0=0.5),
               row("CCCC", is_unusual_volume=True)]
    hit = ns.screen_market_summary(healthy, nsc.AVAILABLE, BEFORE)
    assert hit.status == nsc.HITS and [h["symbol"] for h in hit.hits] == ["BBBB", "CCCC"]
    no_hit = ns.screen_market_summary([row("AAAA", is_unusual_volume=False)], nsc.AVAILABLE, BEFORE)
    assert no_hit.status == nsc.NO_HITS and no_hit.hits == []
    for rows in ([row("AAAA")], [row("AAAA", is_unusual_volume=None)]):      # absent / explicit null
        assert ns.screen_market_summary(rows, None, BEFORE).status == nsc.SOURCE_UNAVAILABLE
    assert ns.screen_market_summary([], None, BEFORE).status == nsc.SOURCE_UNAVAILABLE
    assert ns.screen_market_summary(healthy, nsc.AVAILABLE, AFTER).status == nsc.RETIRED_SOURCE
    partial = ns.screen_market_summary([row("AAAA", is_unusual_volume=True), row("BBBB")], nsc.AVAILABLE, BEFORE)
    assert partial.status == nsc.HITS and "1 row(s) with unknown" in partial.detail


def test_source_number_never_turns_missing_into_zero():
    r = {"null": None, "empty": "", "text": "n/a", "flag": True, "zero": 0, "zero_s": "0", "neg": "(1,234.5)",
         "f": 0.25, "nan": float("nan")}
    for key in ("absent", "null", "empty", "text", "flag", "nan"):
        assert nsc.source_number(r, key) is None, key
    assert nsc.source_number(r, "zero") == 0.0 and nsc.source_number(r, "zero_s") == 0.0
    assert nsc.source_number(r, "neg") == -1234.5 and nsc.source_number(r, "f") == 0.25
    assert ns.parse_num(None) == 0.0                       # legacy helper unchanged for its DOM callers


def test_top2_ranking_excludes_unavailable_numeric_inputs_and_keeps_true_zero():
    rows = [row("NULL", is_unusual_volume=True, m_dn_0=None, m_dn_3=0.9),
            {k: v for k, v in row("ABSENT", is_unusual_volume=True, m_dn_0=0.5).items() if k != "m_dn_3"},
            row("ZERO", is_unusual_volume=True, m_dn_0=0.0, m_dn_3=0.0),
            row("NEG", is_unusual_volume=True, m_dn_0=-0.5, m_dn_3=0.1)]
    result = ns.screen_market_summary(rows, nsc.AVAILABLE, BEFORE)
    # parse_num would have ranked NULL (0.0, 0.9) and ABSENT (0.5, 0.0) on invented zeros: ABSENT, NULL.
    assert result.status == nsc.HITS and [h["symbol"] for h in result.hits] == ["ZERO", "NEG"]
    assert result.hits[0]["dn-0"] == 0.0 and result.hits[0]["dn-3"] == 0.0
    assert "2 unusual row(s) with unavailable m_dn_0/m_dn_3 excluded" in result.detail
    only_unrankable = ns.screen_market_summary(rows[:2], nsc.AVAILABLE, BEFORE)
    assert only_unrankable.status == nsc.SOURCE_UNAVAILABLE and only_unrankable.hits == []


class FakeDashboard:
    """GET /screeners/dashboard + POST /market-summary/summary/{id}. `post_rows(field)`
    builds each screener's batch; a callable `get`/`post` override raises or returns
    another response."""
    def __init__(self, post_rows, names=None, get=None, post=None):
        self.post_rows, self.get_override, self.post_override = post_rows, get, post
        self.names = [n for _l, n, _f, _e in ns.DASHBOARD_PRESETS] if names is None else names

    def get(self, url):
        if self.get_override:
            return self.get_override(url)
        return FakeResponse({"success": True, "data": [{"id": name, "name": name} for name in self.names]})

    def post(self, url, data=None, headers=None):
        if self.post_override:
            return self.post_override(url)
        return FakeResponse({"success": True, "data": self.post_rows(json.loads(data)["sort_field"])})


def dashboard(monkeypatch, fake, capture_rows=None, capture_id=None):
    monkeypatch.setattr(ns, "_api_session", lambda page: (fake, {}))
    monkeypatch.setattr(ns, "API_PAGE_PAUSE", 0)
    out = ns.scrape_dashboard_presets(page=None, capture_rows=capture_rows, capture_id=capture_id)
    assert [(label, emoji) for label, emoji, _r in out] == [(l, e) for l, _n, _f, e in ns.DASHBOARD_PRESETS]
    for label, _emoji, result in out:
        assert isinstance(result, nsc.SignalResult) and result.source == f"dashboard_{label}"
    return {label: result for label, _emoji, result in out}


def captured(symbol, m=0.2, nr=0.3, f=-0.05, **extra):
    """A capture row whose three rank fields differ, so a lookup on the wrong one shows."""
    return dict({"symbol": symbol, "m_dn_0": m, "nr_dn_0": nr, "f_dn_0": f}, **extra)


def test_dashboard_rank_value_missing_is_kept_from_the_capture_or_na_never_zero(monkeypatch):
    fake = FakeDashboard(lambda field: [{"symbol": "NULL", field: None}, {"symbol": "ABSN"},
                                        {"symbol": "ZERO", field: 0}, {"symbol": "GOOD", field: 0.123}])
    got = dashboard(monkeypatch, fake, [captured("ABSN")], "ms-cap")
    expected_absn = {"Bandarmologi": "20.0%", "NonRetail": "30.0%", "Foreign": "-5.0%"}
    for label, result in got.items():
        assert result.hits == [{"tick": "NULL", "tx": "n/a"}, {"tick": "ABSN", "tx": expected_absn[label]},
                               {"tick": "ZERO", "tx": "0.0%"}, {"tick": "GOOD", "tx": "12.3%"}]
        assert result.status == nsc.HITS and result.capture_id == "ms-cap"
        assert "['NULL', 'ABSN']" in result.detail and "ms-cap: ['ABSN']" in result.detail
        assert "n/a (absent/null in that capture too): ['NULL']" in result.detail


def test_dashboard_value_null_or_absent_in_the_capture_too_stays_na(monkeypatch):
    fake = FakeDashboard(lambda field: [{"symbol": "NULL"}, {"symbol": "ABSN"}])
    capture = [captured("NULL", m=None, nr=None, f=None), {"symbol": "ABSN", "close": 1000}]
    for result in dashboard(monkeypatch, fake, capture, "ms-cap").values():
        assert [r["tx"] for r in result.hits] == ["n/a", "n/a"]
        assert "valued from market-summary capture ms-cap: []" in result.detail
        assert "n/a (absent/null in that capture too): ['NULL', 'ABSN']" in result.detail


def test_dashboard_list_whose_response_never_carries_the_rank_field_is_not_emptied(monkeypatch):
    """2026-09-17: 'Top Akum Bandar' returned 7 tickers, none with m_dn_0, and the
    list came out empty. The tickers are server-ranked, so they stay; the values
    come from the same run's capture (where the dashboard values matched exactly)."""
    tickers = ["MAPI", "AMRT", "INTP", "PEGE", "TOSK", "MBSS", "TLDN"]
    fake = FakeDashboard(lambda field: [{"symbol": t, "close": 1000} for t in tickers])
    capture = [captured(t, m=v) for t, v in zip(tickers, [0.1803, 0.1573, 0.1569, 0.1498, 0.1020, 0.09, 0.08])]
    with_capture = dashboard(monkeypatch, fake, capture, "ms-1")["Bandarmologi"]
    assert with_capture.status == nsc.HITS and with_capture.capture_id == "ms-1"
    assert [(r["tick"], r["tx"]) for r in with_capture.hits] == [
        ("MAPI", "18.0%"), ("AMRT", "15.7%"), ("INTP", "15.7%"), ("PEGE", "15.0%"), ("TOSK", "10.2%")]
    without_capture = dashboard(monkeypatch, fake)["Bandarmologi"]
    assert without_capture.status == nsc.HITS
    assert [r["tx"] for r in without_capture.hits] == ["n/a"] * ns.DASH_TOP_N
    assert without_capture.capture_id is None
    assert "no usable market-summary capture this run" in without_capture.detail
    assert "None" not in without_capture.detail


def test_dashboard_value_present_in_the_response_wins_and_names_no_capture(monkeypatch):
    fake = FakeDashboard(lambda field: [{"symbol": "GOOD", field: 0.123}])
    for result in dashboard(monkeypatch, fake, [captured("GOOD", m=0.9, nr=0.9, f=0.9)], "ms-1").values():
        assert result.hits == [{"tick": "GOOD", "tx": "12.3%"}]
        assert result.capture_id is None and result.detail == ""


def test_dashboard_never_reads_an_ambiguous_capture_symbol(monkeypatch):
    fake = FakeDashboard(lambda field: [{"symbol": "DUPE"}])
    capture = [captured("DUPE", m=0.1), captured("DUPE", m=0.9)]
    assert dashboard(monkeypatch, fake, capture, "ms-1")["Bandarmologi"].hits == [{"tick": "DUPE", "tx": "n/a"}]


def test_dashboard_request_failures_are_unavailable_and_an_empty_response_is_unverified(monkeypatch):
    def boom(url):
        raise RuntimeError("connection reset")

    def respond(obj, **kw):
        return lambda url: FakeResponse(obj, **kw)

    login_page = respond(b"<html>login</html>", content_type="text/html")
    api_error = respond({"success": False, "message": "abnormal usage detected", "data": None})
    throttled = respond({"detail": "Request was throttled."}, status=429)
    drf_error = respond({"detail": "Authentication credentials were not provided."})
    ok_rows = lambda field: [{"symbol": "GOOD", field: 0.1}]   # noqa: E731

    def all_unavailable(got, text):
        assert {r.status for r in got.values()} == {nsc.SOURCE_UNAVAILABLE}
        assert all(text in r.detail for r in got.values()), [r.detail for r in got.values()]

    for bad in (boom, login_page, api_error, throttled, drf_error):
        all_unavailable(dashboard(monkeypatch, FakeDashboard(ok_rows, get=bad)), "screeners list failed")
        all_unavailable(dashboard(monkeypatch, FakeDashboard(ok_rows, post=bad)), "request failed")
    all_unavailable(dashboard(monkeypatch, FakeDashboard(ok_rows, post=api_error)), "abnormal usage detected")
    all_unavailable(dashboard(monkeypatch, FakeDashboard(ok_rows, post=throttled)), "HTTP 429")

    no_identity = FakeDashboard(lambda field: [{field: 0.1}, {"symbol": None, "close": 1}])
    all_unavailable(dashboard(monkeypatch, no_identity), "identity key symbol absent on 2/2 rows")

    got = dashboard(monkeypatch, FakeDashboard(ok_rows, names=["Top Akum Asing"]))
    assert got["Foreign"].status == nsc.HITS
    assert got["Bandarmologi"].status == nsc.SOURCE_UNAVAILABLE and "not found" in got["Bandarmologi"].detail

    empty = dashboard(monkeypatch, FakeDashboard(lambda field: []))
    assert {r.status for r in empty.values()} == {nsc.EMPTY_UNVERIFIED}
    warrants = dashboard(monkeypatch, FakeDashboard(lambda field: [{"symbol": "AADIBQCQ6A", field: 0.1}]))
    assert {r.status for r in warrants.values()} == {nsc.EMPTY_UNVERIFIED}
    assert "no plain equity ticker" in warrants["Foreign"].detail


def test_dashboard_session_failure_is_unavailable_not_a_lost_run(monkeypatch):
    def no_session(page):
        raise TimeoutError("page.goto: Timeout 60000ms exceeded.")
    monkeypatch.setattr(ns, "_api_session", no_session)
    got = ns.scrape_dashboard_presets(page=None)
    assert [r.status for _l, _e, r in got] == [nsc.SOURCE_UNAVAILABLE] * len(ns.DASHBOARD_PRESETS)
    assert all("TimeoutError" in r.detail for _l, _e, r in got)


def test_request_errors_never_print_the_playwright_call_log(monkeypatch, caplog):
    """A failed Playwright API request lists every request header in its message:
    the CSRF token and the session cookie would land in the PUBLIC Actions log."""
    leaky = ("APIRequestContext.post: connect ECONNREFUSED 1.2.3.4:443\nCall log:\n  - -> POST /api/x\n"
             f"    - X-CSRFToken: {SECRET}\n    - cookie: sessionid={SECRET}; csrftoken={SECRET}")
    assert ns._safe_error(RuntimeError(leaky)) == "RuntimeError: APIRequestContext.post: connect ECONNREFUSED 1.2.3.4:443"

    def boom(url):
        raise RuntimeError(leaky)
    for fake in (FakeDashboard(lambda f: [], get=boom), FakeDashboard(lambda f: [], post=boom)):
        caplog.clear()
        got = dashboard(monkeypatch, fake)
        assert all(SECRET not in r.detail and "ECONNREFUSED" in r.detail for r in got.values())
        assert SECRET not in caplog.text and "ECONNREFUSED" in caplog.text


def test_run_all_jobs_hands_the_capture_to_the_dashboard(monkeypatch):
    from unittest import mock
    seen, sent = {}, []

    def fake_market_summary(page, capture):
        capture.update(rows=[captured("ABCD")], capture_id="ms-t")
        return nsc.SignalResult("top_akum_bandar", nsc.RETIRED_SOURCE, [], "retired")

    def fake_dashboard(page, capture_rows=None, capture_id=None):
        seen.update(rows=capture_rows, capture_id=capture_id)
        return [(l, e, nsc.SignalResult(f"dashboard_{l}", nsc.HITS, [{"tick": "ABCD", "tx": "20.0%"}]))
                for l, _n, _f, e in ns.DASHBOARD_PRESETS]

    monkeypatch.setattr(ns, "sync_playwright", mock.MagicMock())
    for name, stub in {"login": lambda page: None, "scrape_market_summary": fake_market_summary,
                       "scrape_dashboard_presets": fake_dashboard, "scrape_broker_stalker": lambda page: [],
                       "save_daily_broker_flow": lambda page: None, "_offset_safe": lambda what: False,
                       "send_telegram": sent.append}.items():
        monkeypatch.setattr(ns, name, stub)
    ns.run_all_jobs()
    assert seen == {"rows": [captured("ABCD")], "capture_id": "ms-t"}
    assert len(sent) == 1 and "Bandarmologi: ABCD(20.0%)" in sent[0]


def test_dashboard_status_reaches_the_record_and_the_telegram_line():
    conn = sqlite3.connect(":memory:")
    dash = [("Bandarmologi", "b", nsc.SignalResult("dashboard_Bandarmologi", nsc.SOURCE_UNAVAILABLE, [], "request failed: x")),
            ("NonRetail", "n", nsc.SignalResult("dashboard_NonRetail", nsc.EMPTY_UNVERIFIED, [], "screener returned no rows")),
            ("Foreign", "f", nsc.SignalResult("dashboard_Foreign", nsc.HITS, [{"tick": "ABCD", "tx": "n/a"}], "d", "ms-1"))]
    ns.record_konglo_signals(conn, "2026-09-18", nsc.SignalResult("top_akum_bandar", nsc.RETIRED_SOURCE), dash, [])
    got = {s: (st, d, c) for s, st, d, c in conn.execute(
        "SELECT source, status, detail, capture_id FROM signal_source_status WHERE source LIKE 'dashboard_%'")}
    assert got == {"dashboard_Bandarmologi": (nsc.SOURCE_UNAVAILABLE, "request failed: x", None),
                   "dashboard_NonRetail": (nsc.EMPTY_UNVERIFIED, "screener returned no rows", None),
                   "dashboard_Foreign": (nsc.HITS, "d", "ms-1")}
    assert conn.execute("SELECT sources FROM konglo_signal_watch WHERE ticker='ABCD'").fetchone() == ("dashboard_Foreign",)
    lines = ns._dashboard_lines(dash)
    assert lines[1:] == ["b Bandarmologi: ⚠️ unavailable", "n NonRetail: -", "f Foreign: ABCD(n/a)"]


def test_persisted_source_null_stays_null_and_true_zero_stays_zero(tmpdir_path, monkeypatch):
    db = os.path.join(tmpdir_path, "neobdm.db")
    monkeypatch.setattr(ns, "DB_PATH", db)
    absent = {k: v for k, v in row("ABSN").items() if k != "m_dn_0"}
    ns.save_market_summary_daily("2026-09-16", ACTIVE, [row("NULL", m_dn_0=None), absent, row("ZERO", m_dn_0=0)], None)
    got = dict(sqlite3.connect(db).execute("SELECT ticker, m_dn_0 FROM market_summary_daily"))
    assert got == {"NULL": None, "ABSN": None, "ZERO": 0}


def test_telegram_wording_distinguishes_no_hits_from_unavailable_and_retired():
    def text(result):
        return "\n".join(ns._market_summary_lines(result))
    retired = text(nsc.SignalResult("top_akum_bandar", nsc.RETIRED_SOURCE, [], "x"))
    unavailable = text(nsc.SignalResult("top_akum_bandar", nsc.SOURCE_UNAVAILABLE, [], "capture failed"))
    no_hits = text(nsc.SignalResult("top_akum_bandar", nsc.NO_HITS, []))
    for t in (retired, unavailable, no_hits):
        assert "No data scraped today" not in t
    assert "retired" in retired.lower() and "not a zero-candidate day" in retired
    assert "unavailable" in unavailable.lower() and "capture failed" in unavailable
    assert "No unusual-volume candidates today (source healthy)" in no_hits
    hits = text(nsc.SignalResult("top_akum_bandar", nsc.HITS, [{"symbol": "BBBB", "dn-0": 0.5, "price": 1}]))
    assert "1. BBBB" in hits


def test_signal_recording_preserves_source_availability_even_without_hits():
    conn = sqlite3.connect(":memory:")
    retired = nsc.SignalResult("top_akum_bandar", nsc.RETIRED_SOURCE, [], "retired")
    ns.record_konglo_signals(conn, "2026-09-16", retired, [("Foreign", "x", [{"tick": "ABCD"}]),
                                                          ("NonRetail", "y", [])], [])
    got = dict(conn.execute("SELECT source, status FROM signal_source_status WHERE flag_date='2026-09-16'"))
    assert got == {"top_akum_bandar": nsc.RETIRED_SOURCE, "dashboard_Foreign": nsc.HITS,
                   "dashboard_NonRetail": nsc.EMPTY_UNVERIFIED, "broker_stalker": nsc.EMPTY_UNVERIFIED}
    ns.record_konglo_signals(conn, "2026-09-17", nsc.SignalResult("top_akum_bandar", nsc.NO_HITS, []),
                             [("Foreign", "x", [])], [])
    assert dict(conn.execute("SELECT source, status FROM signal_source_status WHERE flag_date='2026-09-17'"))[
        "top_akum_bandar"] == nsc.NO_HITS
    assert conn.execute("SELECT COUNT(*) FROM konglo_signal_watch WHERE flag_date='2026-09-17'").fetchone()[0] == 0


# ── integrity checker ─────────────────────────
def make_panel(days, fill, rows_per_day=30, extra_cols=()):
    """days: list of dates; fill(date_index, field) -> value or None."""
    conn = sqlite3.connect(":memory:")
    cols = ACTIVE + ["is_unusual_volume"] + list(extra_cols)
    conn.execute("CREATE TABLE market_summary_daily (date TEXT, ticker TEXT, "
                 + ", ".join(f'"{c}"' for c in cols) + ", PRIMARY KEY(date, ticker))")
    for i, d in enumerate(days):
        for k in range(rows_per_day):
            vals = [d, f"T{k:03d}"] + [fill(i, c, k) for c in cols]
            conn.execute(f"INSERT INTO market_summary_daily VALUES ({','.join('?' * len(vals))})", vals)
    return conn


def default_fill(i, col, k):
    if col == "symbol":
        return f"T{k:03d}"
    if col == "top_5_buyer":
        return '["AK"]'
    if col == "is_unusual_volume":
        return k % 2
    return float(k + i)


DAYS = [f"2026-08-{d:02d}" for d in range(1, 11)]   # all before the retirement regime


def run_schema(conn, lifecycle=None):
    problems, notes, stats = [], [], {}
    csi.check_schema_and_coverage(conn, problems, notes, stats, lifecycle=lifecycle)
    return problems, notes, stats


def test_integrity_fails_on_disappearance_and_keeps_failing_without_self_poisoning():
    for days_missing in (1, 2, 3):            # day 9 missing, then days 9-10, then 8-10
        cut = len(DAYS) - days_missing
        conn = make_panel(DAYS, lambda i, c, k: None if c == "is_unusual_volume" and i >= cut else default_fill(i, c, k))
        problems, _, _ = run_schema(conn, lifecycle={})          # not (yet) registered as retired
        assert any("is_unusual_volume" in p for p in problems), days_missing


def test_generic_sweep_column_cannot_poison_its_own_baseline():
    fill = lambda i, c, k: (None if i >= 8 else 1.0) if c == "rel_vol_20" else default_fill(i, c, k)  # noqa: E731
    for n_days in (9, 10):                    # day 9 empty; day 10 still empty
        conn = make_panel(DAYS[:n_days], fill, extra_cols=("rel_vol_20",))
        problems, _, _ = run_schema(conn, lifecycle=nsc.FIELD_LIFECYCLE)
        assert any("rel_vol_20" in p for p in problems), n_days


INCIDENT_DAYS = [f"2026-09-{d:02d}" for d in range(4, 17)]      # 09-04 .. 09-16


def incident_panel(last_day):
    """Legacy captures (no manifest) with is_unusual_volume NULL from 09-14, as in production."""
    days = [d for d in INCIDENT_DAYS if d <= last_day]
    return make_panel(days, lambda i, c, k: None if c == "is_unusual_volume" and days[i] >= "2026-09-14"
                      else default_fill(i, c, k))


def insert_manifest(conn, capture_id, capture_date, started, status=nsc.CONTRACT_OK, issues=(), requested=None,
                    contract_version=nsc.CONTRACT_VERSION):
    nsc.ensure_schema(conn)
    values = {c: None for c in nsc.MANIFEST_COLUMNS}
    values.update(capture_id=capture_id, source=nsc.SOURCE, contract_version=contract_version,
                  capture_started_utc=started, capture_finished_utc=started, capture_date=capture_date,
                  session_date_status="UNKNOWN", persisted_to_market_summary_daily=1, screener_id="sid",
                  universe_id="u", requested_columns=json.dumps(requested or ACTIVE), catalog_column_count=430,
                  catalog_columns_sha256="abc", stored_config_columns=json.dumps(ACTIVE), returned_keys="[]",
                  unexpected_keys="[]", field_presence="{}", field_availability="{}", rows=30, pages_fetched=2,
                  last_page=2, source_timestamps="[]", trace_ids="[]", raw_sha256s="[]", contract_status=status,
                  contract_issues=json.dumps(list(issues)), recorded_utc=started, tolerated_additive_keys="{}")
    conn.execute(f"INSERT INTO ms_capture_manifest ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
                 list(values.values()))


def test_pre_break_legacy_capture_stays_plain_healthy():
    problems, notes, stats = run_schema(incident_panel("2026-09-13"))
    assert problems == [] and stats["capture_health"] == nsc.HEALTHY
    assert stats["contract_at_capture"] == nsc.LEGACY_CONTRACT_VERSION and stats["retired_fields"] == []
    assert csi.format_report(problems, notes, stats).startswith("🟢 signal integrity OK\n")
    assert csi.exit_code(problems, stats) == 0


def test_historical_break_captures_are_not_retrospectively_green():
    for day in ("2026-09-14", "2026-09-15"):
        problems, notes, stats = run_schema(incident_panel(day))
        assert problems == [], day
        assert stats["capture_health"] == nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED, day
        assert stats["contract_breaks"] == ["is_unusual_volume"]
        report = csi.format_report(problems, notes, stats)
        assert report.startswith("🟠 SOURCE_CONTRACT_BREAK_ACKNOWLEDGED") and "🟢" not in report, day
        assert any("market_summary_contract_v1, which requested is_unusual_volume" in n
                   and "does not make this capture healthy" in n for n in notes)
        assert csi.exit_code(problems, stats) == 2
        # Without the human retirement the same capture is an unexplained failure.
        unexplained, _, stats_u = run_schema(incident_panel(day), lifecycle={})
        assert any("is_unusual_volume" in p for p in unexplained) and stats_u["capture_health"] == nsc.HEALTHY


def test_future_valid_14_field_capture_is_healthy_with_the_retirement_noted():
    conn = incident_panel("2026-09-16")
    insert_manifest(conn, "ms-20260916T000000Z", "2026-09-16", "2026-09-15T23:00:00Z")
    problems, notes, stats = run_schema(conn)
    assert problems == [] and stats["capture_health"] == nsc.HEALTHY
    assert stats["contract_at_capture"] == nsc.CONTRACT_VERSION and stats["contract_breaks"] == []
    assert any("RETIRED by NeoBDM" in n and "is_unusual_volume" in n for n in notes)
    report = csi.format_report(problems, notes, stats)
    assert report.startswith("🟢 signal integrity OK — active contract valid; RETIRED source field(s): is_unusual_volume")
    assert csi.exit_code(problems, stats) == 0
    # The earlier break captures are still breaks when judged on their own.
    conn.execute("DELETE FROM market_summary_daily WHERE date='2026-09-16'")
    assert run_schema(conn)[2]["capture_health"] == nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED


class FrozenDateTime(__import__("datetime").datetime):
    @classmethod
    def now(cls, tz=None):
        base = cls(2026, 9, 16, 7, 0, 0)
        return tz.localize(base) if hasattr(tz, "localize") else base


def run_capture_checks(conn):
    problems, notes, stats = [], [], {}
    csi.check_schema_and_coverage(conn, problems, notes, stats)
    csi.check_capture_contract(conn, problems, notes, stats)
    csi.check_signal_sources(conn, problems, notes, stats)
    return problems, notes, stats


def test_first_v2_capture_turns_current_health_green_without_rewriting_history(tmpdir_path, monkeypatch):
    db = os.path.join(tmpdir_path, "neobdm.db")
    mem = incident_panel("2026-09-15")
    mem.execute("CREATE TABLE konglo_signal_watch (flag_date TEXT, ticker TEXT, sources TEXT, is_tracked INTEGER, "
                "PRIMARY KEY (flag_date, ticker))")
    for d in ("2026-09-13", "2026-09-14", "2026-09-15"):
        mem.execute("INSERT INTO konglo_signal_watch VALUES (?,?,?,?)", (d, "T003", "dashboard_Foreign", 0))
    mem.commit()
    conn = sqlite3.connect(db)
    mem.backup(conn)
    mig.migrate(conn, apply=True)

    # BEFORE the first v2 capture: the latest capture is an acknowledged break.
    problems, notes, stats = run_capture_checks(conn)
    assert problems == [] and stats["capture_health"] == nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED
    assert csi.exit_code(problems, stats) == 2
    history_status = conn.execute("SELECT * FROM signal_source_status ORDER BY 1, 2").fetchall()
    history_iuv = conn.execute("SELECT date, ticker, is_unusual_volume FROM market_summary_daily ORDER BY 1, 2").fetchall()
    conn.close()

    # FIRST v2 capture, through the real scraper path against a fake NeoBDM.
    rows = [dict({c: default_fill(12, c, k) for c in ACTIVE}, top_5_buyer=["AK"], symbol_1=f"T{k:03d}")
            for k in range(30)]
    fake = FakeNeoBDM([rows[:20], rows[20:]], ACTIVE + ["is_spike_volume_1"], stored=ACTIVE)
    monkeypatch.setattr(ns, "DB_PATH", db)
    monkeypatch.setattr(ns, "API_PAGE_PAUSE", 0)
    monkeypatch.setattr(ns, "_offset_safe", lambda what: True)
    monkeypatch.setattr(ns, "datetime", FrozenDateTime)
    monkeypatch.setattr(ns, "_api_session", lambda page: (fake, {"X-CSRFToken": SECRET}))
    result = ns.scrape_market_summary(page=None)
    conn = sqlite3.connect(db)
    ns.record_konglo_signals(conn, "2026-09-16", result, [("Foreign", "x", [{"tick": "T003"}])], [])

    manifest = conn.execute("SELECT capture_date, contract_version, requested_columns, contract_status "
                            "FROM ms_capture_manifest").fetchall()
    assert manifest == [("2026-09-16", nsc.CONTRACT_VERSION, json.dumps(ACTIVE), nsc.CONTRACT_OK)]
    problems, notes, stats = run_capture_checks(conn)
    assert problems == [], problems
    assert stats["capture_health"] == nsc.HEALTHY and stats["contract_at_capture"] == nsc.CONTRACT_VERSION
    assert stats["contract"].startswith(nsc.CONTRACT_OK)
    assert any("RETIRED by NeoBDM" in n and "is_unusual_volume" in n for n in notes)
    assert csi.format_report(problems, notes, stats).startswith(
        "🟢 signal integrity OK — active contract valid; RETIRED source field(s): is_unusual_volume")
    assert csi.exit_code(problems, stats) == 0

    # History is untouched and still judged as it was captured.
    assert conn.execute("SELECT * FROM signal_source_status WHERE flag_date < '2026-09-16' ORDER BY 1, 2"
                        ).fetchall() == history_status
    assert conn.execute("SELECT date, ticker, is_unusual_volume FROM market_summary_daily WHERE date < '2026-09-16' "
                        "ORDER BY 1, 2").fetchall() == history_iuv
    assert csi._contract_at_capture(conn, "2026-09-15")[0] == nsc.LEGACY_CONTRACT_VERSION
    as_of_0915 = sqlite3.connect(":memory:")
    conn.backup(as_of_0915)
    as_of_0915.execute("DELETE FROM market_summary_daily WHERE date > '2026-09-15'")
    assert run_schema(as_of_0915)[2]["capture_health"] == nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED


def test_no_manifest_is_fabricated_for_historical_captures():
    conn = history_db()
    mig.migrate(conn, apply=True)
    assert conn.execute("SELECT COUNT(*) FROM ms_capture_manifest").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ms_raw_response").fetchone()[0] == 0


def test_retired_field_populated_again_fails():
    days = [f"2026-09-{d:02d}" for d in range(6, 16)]
    problems, _, _ = run_schema(make_panel(days, default_fill))
    assert any("RETIRED" in p and "populated again" in p for p in problems)


def test_active_field_absence_type_change_constant_collapse_and_cardinality_fail():
    missing = make_panel(DAYS, lambda i, c, k: None if c == "m_dn_0" and i == 9 else default_fill(i, c, k))
    assert any("m_dn_0" in p for p in run_schema(missing)[0])
    typed = make_panel(DAYS, lambda i, c, k: "x" if c == "tval" and i == 9 else default_fill(i, c, k))
    assert any("type change" in p and "tval" in p for p in run_schema(typed)[0])
    flat = make_panel(DAYS, lambda i, c, k: 7.0 if c == "pct_5" and i == 9 else default_fill(i, c, k))
    assert any("collapse" in p and "pct_5" in p for p in run_schema(flat)[0])
    conn = make_panel(DAYS[:9], default_fill)
    for k in range(10):
        vals = [DAYS[9], f"T{k:03d}"] + [default_fill(9, c, k) for c in ACTIVE + ["is_unusual_volume"]]
        conn.execute(f"INSERT INTO market_summary_daily VALUES ({','.join('?' * len(vals))})", vals)
    assert any("panel size changed" in p for p in run_schema(conn)[0])


def manifest_db(status, issues, requested=None, capture_date="2026-09-15", extra_capture=True):
    conn = make_panel(["2026-09-14", "2026-09-15"], default_fill)
    nsc.ensure_schema(conn)
    if extra_capture:
        insert_manifest(conn, "ms-old", "2026-09-14", "2026-09-13T23:00:00Z")
    if capture_date:
        insert_manifest(conn, "ms-new", capture_date, "2026-09-14T23:00:00Z", status, issues, requested)
    return conn


def run_contract(conn):
    problems, notes, stats = [], [], {}
    csi.check_capture_contract(conn, problems, notes, stats)
    return problems, notes, stats


def test_integrity_surfaces_manifest_contract_state():
    fail = [{"severity": "FAIL", "code": "KEY_ABSENT_IN_RESPONSE", "field": "m_dn_0", "detail": "30/30"}]
    assert any("FAILED" in p and "m_dn_0" in p for p in run_contract(manifest_db(nsc.CONTRACT_FAILED, fail))[0])
    warn = [{"severity": "WARN", "code": "UNEXPECTED_KEY", "field": "is_spike_volume_1", "detail": None}]
    problems, notes, _ = run_contract(manifest_db(nsc.CONTRACT_DEGRADED, warn))
    assert problems == [] and any("is_spike_volume_1" in n for n in notes)
    alias = [{"severity": "INFO", "code": "TOLERATED_ADDITIVE_ALIAS", "field": "symbol_1", "detail": "equals symbol"}]
    assert run_contract(manifest_db(nsc.CONTRACT_OK, alias))[:2] == ([], [])        # no daily noise
    back = [{"severity": "WARN", "code": nsc.RETIRED_FIELD_REAPPEARED, "field": "is_unusual_volume",
             "detail": "present in catalog"}]
    problems, _, _ = run_contract(manifest_db(nsc.CONTRACT_DEGRADED, back))
    assert any(p.startswith("RETIRED_FIELD_REAPPEARED_REQUIRES_HUMAN_REVIEW: is_unusual_volume") for p in problems)
    assert any("no capture manifest for 2026-09-15" in p for p in run_contract(manifest_db(nsc.CONTRACT_OK, [], capture_date=None))[0])
    drift = manifest_db(nsc.CONTRACT_OK, [], requested=ACTIVE + ["is_unusual_volume"])
    assert any("differ from the active contract" in p for p in run_contract(drift)[0])


def test_integrity_signal_sources():
    conn = sqlite3.connect(":memory:")
    nsc.record_signal_source_status(conn, "2026-09-16", [
        nsc.SignalResult("top_akum_bandar", nsc.RETIRED_SOURCE), nsc.SignalResult("dashboard_Foreign", nsc.HITS, [1])])
    problems, notes = [], []
    csi.check_signal_sources(conn, problems, notes, {})
    assert problems == []
    nsc.record_signal_source_status(conn, "2026-09-16", [nsc.SignalResult("dashboard_Foreign", nsc.SOURCE_UNAVAILABLE)])
    csi.check_signal_sources(conn, problems, notes, {})
    assert any("dashboard_Foreign UNAVAILABLE" in p for p in problems)


# ── persistence / migration ───────────────────
def history_db():
    conn = make_panel(["2026-09-12", "2026-09-13", "2026-09-14", "2026-09-15"],
                      lambda i, c, k: (None if i >= 2 else k % 2) if c == "is_unusual_volume" else default_fill(i, c, k))
    conn.execute("CREATE TABLE konglo_signal_watch (flag_date TEXT, ticker TEXT, sources TEXT, is_tracked INTEGER, "
                 "PRIMARY KEY (flag_date, ticker))")
    for d in ("2026-09-12", "2026-09-13", "2026-09-14", "2026-09-15"):
        conn.execute("INSERT INTO konglo_signal_watch VALUES (?,?,?,?)", (d, "T001", "dashboard_Foreign", 0))
    conn.commit()
    return conn


def test_migration_is_additive_idempotent_and_never_backfills():
    conn = history_db()
    before = conn.execute("SELECT date, ticker, is_unusual_volume FROM market_summary_daily ORDER BY 1, 2").fetchall()
    report = mig.migrate(conn, apply=True)
    assert report["retired_signal_days_added"] == [("2026-09-14", "top_akum_bandar"), ("2026-09-15", "top_akum_bandar")]
    after = conn.execute("SELECT date, ticker, is_unusual_volume FROM market_summary_daily ORDER BY 1, 2").fetchall()
    assert after == before
    assert {v for d, _, v in after if d >= "2026-09-14"} == {None}                  # no backfill
    assert {v for d, _, v in after if d < "2026-09-14"} == {0, 1}                    # history untouched
    assert mig.migrate(conn, apply=True)["retired_signal_days_added"] == []
    assert conn.execute("SELECT state, effective_capture_date, acknowledged_in_contract FROM source_field_lifecycle"
                        ).fetchall() == [("RETIRED", "2026-09-14", nsc.CONTRACT_VERSION)]
    # Current interpretation RETIRED_SOURCE; at capture the then-active contract broke.
    assert conn.execute("SELECT flag_date, status, status_at_capture FROM signal_source_status ORDER BY 1").fetchall() == [
        ("2026-09-14", nsc.RETIRED_SOURCE, nsc.SOURCE_UNAVAILABLE),
        ("2026-09-15", nsc.RETIRED_SOURCE, nsc.SOURCE_UNAVAILABLE)]
    problems, notes = [], []
    csi.check_signal_sources(conn, problems, notes, {})
    assert problems == [] and any("SOURCE_UNAVAILABLE at capture, RETIRED_SOURCE under the current lifecycle" in n
                                  for n in notes)


def test_2026_09_17_bandarmologi_is_corrected_to_unavailable_once_and_only_there():
    import evaluate_signals as ev
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE konglo_signal_watch (flag_date TEXT, ticker TEXT, sources TEXT, is_tracked INTEGER)")
    conn.execute("INSERT INTO konglo_signal_watch VALUES ('2026-09-17', 'RANS', 'dashboard_NonRetail', 0)")
    as_recorded = [nsc.signal_status_from_rows("dashboard_Bandarmologi", []),
                   nsc.SignalResult("dashboard_Foreign", nsc.HITS, [1]),
                   nsc.signal_status_from_rows("broker_stalker", [])]
    nsc.record_signal_source_status(conn, "2026-09-17", as_recorded)
    nsc.record_signal_source_status(conn, "2026-09-16", [nsc.signal_status_from_rows("dashboard_Bandarmologi", [])])
    watch_before = conn.execute("SELECT * FROM konglo_signal_watch").fetchall()

    assert nsc.apply_source_status_corrections(conn) == [("2026-09-17", "dashboard_Bandarmologi")]
    assert nsc.apply_source_status_corrections(conn) == []                              # idempotent
    rows = {(d, s): (st, at, by) for d, s, st, at, by in conn.execute(
        "SELECT flag_date, source, status, status_at_capture, recorded_by FROM signal_source_status")}
    assert rows == {
        ("2026-09-17", "dashboard_Bandarmologi"): (nsc.SOURCE_UNAVAILABLE, nsc.EMPTY_UNVERIFIED, "status_correction"),
        ("2026-09-17", "dashboard_Foreign"): (nsc.HITS, nsc.HITS, "scraper"),
        ("2026-09-17", "broker_stalker"): (nsc.EMPTY_UNVERIFIED, nsc.EMPTY_UNVERIFIED, "scraper"),
        ("2026-09-16", "dashboard_Bandarmologi"): (nsc.EMPTY_UNVERIFIED, nsc.EMPTY_UNVERIFIED, "scraper")}
    assert conn.execute("SELECT * FROM konglo_signal_watch").fetchall() == watch_before
    status = ev.load_source_status(conn)
    assert ev.source_state(status, "dashboard_Bandarmologi", "2026-09-17") == nsc.SOURCE_UNAVAILABLE
    assert ev.source_state(status, "dashboard_Foreign", "2026-09-17") is None

    # A later re-record of that day is never overridden.
    nsc.record_signal_source_status(conn, "2026-09-17", [nsc.SignalResult("dashboard_Bandarmologi", nsc.HITS, [1])])
    assert nsc.apply_source_status_corrections(conn) == []
    assert conn.execute("SELECT status FROM signal_source_status WHERE flag_date='2026-09-17' "
                        "AND source='dashboard_Bandarmologi'").fetchone() == (nsc.HITS,)
    assert nsc.apply_source_status_corrections(sqlite3.connect(":memory:")) == []        # no table, no-op


def test_migration_dry_run_writes_nothing():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    src = history_db()
    src.backup(conn)
    mig.migrate(conn, apply=False)
    assert not nsc.table_exists(conn, "source_field_lifecycle")


def test_migration_on_the_real_database_copy_changes_no_existing_row():
    path = os.path.join(HERE, "neobdm.db")
    if not os.path.exists(path):
        pytest.skip("neobdm.db not present")
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    source.backup(conn)
    source.close()
    profile = mig.unusual_volume_profile(conn)
    report = mig.migrate(conn, apply=True)
    assert mig.unusual_volume_profile(conn) == profile
    assert {t: mig.fingerprint(conn, t) for t in mig.PROTECTED} == report["protected_fingerprints"]
    for d in ("2026-09-14", "2026-09-15"):
        if d in profile:
            n, t, f, nulls = profile[d]
            assert nulls == n and not t and not f


def test_save_keeps_historical_column_and_stores_null_for_retired_field(tmpdir_path, monkeypatch):
    db = os.path.join(tmpdir_path, "neobdm.db")
    monkeypatch.setattr(ns, "DB_PATH", db)
    ns.save_market_summary_daily("2026-09-13", ACTIVE + ["is_unusual_volume"], [row("AAAA", is_unusual_volume=True)], None)
    fresh = row("BBBB", is_unusual_volume=True, symbol_1="BBBB")
    ns.save_market_summary_daily("2026-09-16", ACTIVE, [fresh], None)
    conn = sqlite3.connect(db)
    old = conn.execute("SELECT is_unusual_volume FROM market_summary_daily WHERE date='2026-09-13'").fetchone()[0]
    new = dict(zip(["date", "ticker"] + ACTIVE + ["is_unusual_volume"], conn.execute(
        "SELECT date, ticker, " + ",".join(f'"{c}"' for c in ACTIVE) + ", is_unusual_volume FROM market_summary_daily "
        "WHERE date='2026-09-16'").fetchone()))
    assert old == 1 and new["is_unusual_volume"] is None
    for c in ACTIVE:
        expected = json.dumps(fresh[c], ensure_ascii=False) if isinstance(fresh[c], list) else fresh[c]
        assert new[c] == expected, c
    assert "symbol_1" not in [r[1] for r in conn.execute("PRAGMA table_info(market_summary_daily)")]


# ── end-to-end capture with a fake NeoBDM ─────
class FakeNeoBDM:
    def __init__(self, pages, catalog, stored, patch_success=True):
        self.pages, self.catalog, self.stored, self.patch_success = pages, catalog, stored, patch_success
        self.calls, self.patched = [], None

    def get(self, url):
        self.calls.append(("GET", url))
        if url.endswith("/stock-universe"):
            return FakeResponse({"data": [{"id": "u1", "name": "COMPOSITE"}]})
        if url.endswith("/screeners"):
            cols = self.patched["columns"] if self.patched else self.stored
            stored = [c for c in cols if c in self.catalog]           # NeoBDM silently drops unknown columns
            return FakeResponse({"data": [{"id": "sid", "name": ns.SCRAPER_SCREENER_NAME, "columns": stored},
                                          {"id": "w", "name": "OtherPrivateScreener", "columns": ["symbol"]}]})
        if url.endswith("/market-summary/columns"):
            return FakeResponse({"data": [{"field": f} for f in self.catalog]})
        raise AssertionError(url)

    def patch(self, url, data=None, headers=None):
        assert headers["X-CSRFToken"] == SECRET
        self.calls.append(("PATCH", url))
        self.patched = json.loads(data)
        return FakeResponse({"success": self.patch_success})

    def post(self, url, data=None, headers=None):
        self.calls.append(("POST", url))
        page = json.loads(data)["page"]
        return FakeResponse({"success": True, "timestamp": f"2026-09-16T00:0{page}:00Z", "trace_id": f"tr{page}",
                             "meta": {"last_page": len(self.pages)}, "data": self.pages[page - 1]})


def test_end_to_end_capture_persists_active_fields_raw_and_manifest(tmpdir_path, monkeypatch):
    catalog = ACTIVE + ["is_spike_volume_1", "rel_vol_20"]
    pages = [[row("AAAA", symbol_1="AAAA"), row("BBBB", symbol_1="BBBB")], [row("CCCC", symbol_1="CCCC")]]
    fake = FakeNeoBDM(pages, catalog, stored=ACTIVE)
    db = os.path.join(tmpdir_path, "neobdm.db")
    monkeypatch.setattr(ns, "DB_PATH", db)
    monkeypatch.setattr(ns, "API_PAGE_PAUSE", 0)
    monkeypatch.setattr(ns, "_offset_safe", lambda what: True)
    monkeypatch.setattr(ns, "_api_session", lambda page: (fake, {"Content-Type": "application/json",
                                                                 "X-CSRFToken": SECRET}))
    capture = {}
    result = ns.scrape_market_summary(page=None, capture=capture)

    assert [r["symbol"] for r in capture["rows"]] == ["AAAA", "BBBB", "CCCC"]    # handed to the dashboard presets
    assert capture["capture_id"] and capture["capture_id"].startswith("ms-")
    assert fake.patched["columns"] == ACTIVE                       # retired field no longer requested, no replacement
    assert "is_unusual_volume" not in fake.patched["columns"] and "is_spike_volume_1" not in fake.patched["columns"]
    assert result.status == nsc.RETIRED_SOURCE and result.hits == []
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT ticker, close, top_5_buyer FROM market_summary_daily ORDER BY ticker").fetchall()
    assert stored == [("AAAA", 1000, '["AK", "BK"]'), ("BBBB", 1000, '["AK", "BK"]'), ("CCCC", 1000, '["AK", "BK"]')]
    m = dict(zip([d[0] for d in conn.execute("SELECT * FROM ms_capture_manifest").description],
                 conn.execute("SELECT * FROM ms_capture_manifest").fetchone()))
    assert m["contract_status"] == nsc.CONTRACT_OK and json.loads(m["unexpected_keys"]) == ["symbol_1"]
    assert json.loads(m["tolerated_additive_keys"])["symbol_1"]["rows_checked"] == 3
    assert (m["rows"], m["pages_fetched"], m["last_page"], m["session_date"], m["session_date_status"]) == (3, 2, 2, None, "UNKNOWN")
    assert json.loads(m["stored_config_columns"]) == ACTIVE and m["catalog_column_count"] == len(catalog)
    assert json.loads(m["source_timestamps"]) == ["2026-09-16T00:01:00Z", "2026-09-16T00:02:00Z"]
    assert json.loads(m["field_availability"])["is_unusual_volume"] == nsc.RETIRED
    presence = json.loads(m["field_presence"])
    assert presence["close"]["present_non_null"] == 3 and presence["close"][nsc.KEY_ABSENT] == 0
    raws = conn.execute("SELECT method, endpoint, page, raw_sha256, fragment_path FROM ms_raw_response ORDER BY seq").fetchall()
    assert [r[0] for r in raws] == ["GET", "GET", "PATCH", "GET", "GET", "POST", "POST"]
    assert [r[0] for r in conn.execute("SELECT raw_kind FROM ms_raw_response ORDER BY seq")] == [
        "PROJECTED", "PROJECTED", "PROJECTED", "RAW", "PROJECTED", "RAW", "RAW"]
    assert b"OtherPrivateScreener" not in stored_fragments(tmpdir_path)          # the account's other screener never lands
    assert {r[0] for r in conn.execute("SELECT dropped_paths FROM ms_raw_response")} == {"[]"}
    for _, _, _, sha, rel in raws:
        assert hashlib.sha256(nsc.read_raw_bytes(rel, tmpdir_path)).hexdigest() == sha
    everything = b"".join(open(os.path.join(dp, f), "rb").read() for dp, _, fs in os.walk(tmpdir_path) for f in fs
                          if not f.endswith(".gz")) + b"".join(
        nsc.read_raw_bytes(rel, tmpdir_path) for *_, rel in raws)
    assert SECRET.encode() not in everything
    assert conn.execute("SELECT status FROM signal_source_status").fetchall() == []   # recorded by record_konglo_signals


def test_end_to_end_required_field_loss_keeps_good_fields_and_reports_unavailable(tmpdir_path, monkeypatch):
    catalog = [c for c in ACTIVE if c != "clean_score"]           # NeoBDM drops an ACTIVE field
    pages = [[{k: v for k, v in row("AAAA").items() if k != "clean_score"}]]
    fake = FakeNeoBDM(pages, catalog, stored=ACTIVE)
    db = os.path.join(tmpdir_path, "neobdm.db")
    monkeypatch.setattr(ns, "DB_PATH", db)
    monkeypatch.setattr(ns, "API_PAGE_PAUSE", 0)
    monkeypatch.setattr(ns, "_offset_safe", lambda what: True)
    monkeypatch.setattr(ns, "_api_session", lambda page: (fake, {"X-CSRFToken": SECRET}))
    capture = {}
    ns.scrape_market_summary(page=None, capture=capture)
    assert [r["symbol"] for r in capture["rows"]] == ["AAAA"]     # a degraded, non-blocking capture is still usable
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT close, clean_score FROM market_summary_daily").fetchone() == (1000, None)
    status, issues, availability = conn.execute(
        "SELECT contract_status, contract_issues, field_availability FROM ms_capture_manifest").fetchone()
    found = {(i["code"], i["field"]) for i in json.loads(issues)}
    assert status == nsc.CONTRACT_FAILED and json.loads(availability)["clean_score"] == nsc.UNAVAILABLE
    assert {("REQUESTED_MISSING_FROM_CATALOG", "clean_score"), ("REQUESTED_MISSING_FROM_STORED_CONFIG", "clean_score"),
            ("KEY_ABSENT_IN_RESPONSE", "clean_score")} <= found


def test_end_to_end_identity_ambiguity_blocks_the_panel_write_but_keeps_provenance(tmpdir_path, monkeypatch):
    pages = [[row("AAAA", symbol_1="ZZZZ")]]
    fake = FakeNeoBDM(pages, ACTIVE, stored=ACTIVE)
    db = os.path.join(tmpdir_path, "neobdm.db")
    monkeypatch.setattr(ns, "DB_PATH", db)
    monkeypatch.setattr(ns, "API_PAGE_PAUSE", 0)
    monkeypatch.setattr(ns, "_offset_safe", lambda what: True)
    monkeypatch.setattr(ns, "_api_session", lambda page: (fake, {"X-CSRFToken": SECRET}))
    capture = {}
    ns.scrape_market_summary(page=None, capture=capture)
    assert capture == {}                          # ambiguous identity: no dashboard value is read from it
    conn = sqlite3.connect(db)
    assert not nsc.table_exists(conn, "market_summary_daily")
    assert conn.execute("SELECT persisted_to_market_summary_daily, contract_status FROM ms_capture_manifest").fetchone() == (
        0, nsc.CONTRACT_FAILED)
    assert conn.execute("SELECT COUNT(*) FROM ms_raw_response").fetchone()[0] == 6


def test_end_to_end_retired_field_back_in_catalog_is_surfaced_not_reactivated(tmpdir_path, monkeypatch):
    catalog = ACTIVE + ["is_unusual_volume"]                     # NeoBDM re-adds the name
    fake = FakeNeoBDM([[row("AAAA", symbol_1="AAAA")]], catalog, stored=ACTIVE)
    db = os.path.join(tmpdir_path, "neobdm.db")
    monkeypatch.setattr(ns, "DB_PATH", db)
    monkeypatch.setattr(ns, "API_PAGE_PAUSE", 0)
    monkeypatch.setattr(ns, "_offset_safe", lambda what: True)
    monkeypatch.setattr(ns, "_api_session", lambda page: (fake, {"X-CSRFToken": SECRET}))
    result = ns.scrape_market_summary(page=None)
    assert fake.patched["columns"] == ACTIVE and result.status == nsc.RETIRED_SOURCE
    conn = sqlite3.connect(db)
    capture_date = conn.execute("SELECT capture_date FROM ms_capture_manifest").fetchone()[0]
    assert conn.execute("SELECT persisted_to_market_summary_daily FROM ms_capture_manifest").fetchone()[0] == 1
    problems, notes, stats = [], [], {}
    csi.check_capture_contract(conn, problems, notes, stats)
    assert any(p.startswith(nsc.RETIRED_FIELD_REAPPEARED) and "catalog" in p for p in problems), capture_date
    assert "is_unusual_volume" not in [r[1] for r in conn.execute("PRAGMA table_info(market_summary_daily)")]


# ── downstream consumers ──────────────────────
V1, V2 = "konglo_radar_all_signals_v1", "konglo_radar_all_signals_v2"


def downstream_db(retired_days=12, outage_day=None, start="2026-08-01"):
    """40 panel days x 12 names. top_akum_bandar flags T01 until it is RETIRED for
    the last `retired_days`; dashboard_Foreign flags T02 every day, except a
    SOURCE_UNAVAILABLE outage on `outage_day`."""
    from datetime import date, timedelta
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE market_summary_daily (date TEXT, ticker TEXT, close REAL, high REAL, low REAL, "
                 "PRIMARY KEY(date, ticker))")
    conn.execute("CREATE TABLE konglo_signal_watch (flag_date TEXT, ticker TEXT, sources TEXT, is_tracked INTEGER, "
                 "PRIMARY KEY (flag_date, ticker))")
    days = [(date.fromisoformat(start) + timedelta(days=i)).isoformat() for i in range(40)]
    for i, d in enumerate(days):
        for k in range(12):
            close = 100 + i * (1 + k % 3) - (k % 5) + (7 * ((i * k) % 5))
            conn.execute("INSERT INTO market_summary_daily VALUES (?,?,?,?,?)",
                         (d, f"T{k:02d}", close, close + 1, close - 1))
    cut = len(days) - retired_days
    for i, d in enumerate(days):
        statuses = [nsc.SignalResult("top_akum_bandar", nsc.HITS if i < cut else nsc.RETIRED_SOURCE,
                                     [1] if i < cut else [])]
        if i < cut:
            conn.execute("INSERT INTO konglo_signal_watch VALUES (?,?,?,?)", (d, "T01", "top_akum_bandar", 0))
        if d == outage_day:
            statuses.append(nsc.SignalResult("dashboard_Foreign", nsc.SOURCE_UNAVAILABLE))
        else:
            conn.execute("INSERT INTO konglo_signal_watch VALUES (?,?,?,?)", (d, "T02", "dashboard_Foreign", 0))
            statuses.append(nsc.SignalResult("dashboard_Foreign", nsc.HITS, [1]))
        nsc.record_signal_source_status(conn, d, statuses)
    return conn, days, days[cut:]


def expected_control(ev, conn, keep_day, h):
    panel, dates = ev.load_panel(conn)
    flagged = {}
    for d, t, _s in ev.load_signals(conn):
        flagged.setdefault(d, set()).add(t)
    samples = [ev.outcome(panel, dates, t, d, h) for (d, t) in panel
               if keep_day(d) and t not in flagged.get(d, ())]
    return ev.summarise([o for o in samples if o])


def test_evaluate_signals_retired_source_days_are_not_negative_evidence_for_that_source():
    import evaluate_signals as ev
    conn, days, retired = downstream_db(retired_days=12)
    res = ev.evaluate(conn)
    for h in ev.HORIZONS:
        own = res["source_control"]["top_akum_bandar"][h]
        # top_akum_bandar is compared only with names from days it could have flagged ...
        assert own == expected_control(ev, conn, lambda d: d not in retired, h)
        # ... not with the combined control, which (correctly) keeps retired days.
        combined = expected_control(ev, conn, lambda d: True, h)
        assert res["control"][V1][h] == combined and own["n"] < combined["n"]
        hits = [ev.outcome(*ev.load_panel(conn), "T01", d, h) for d in days if d not in retired]
        assert res["by_source"]["top_akum_bandar"][h] == ev.summarise([o for o in hits if o])
        assert res["source_control"]["dashboard_Foreign"][h] == combined          # dashboard was available
    assert res["unavailable_days"] == {"top_akum_bandar": 12} and res["incomplete_days"] == 0
    report = ev.format_report(res)
    assert "source top_akum_bandar unavailable/retired on 12 panel day(s)" in report
    edge = res["by_source"]["top_akum_bandar"][1]["mean"] - res["source_control"]["top_akum_bandar"][1]["mean"]
    assert f"BY SOURCE — top_akum_bandar (vs control on days top_akum_bandar was available)" in report
    assert f"vs mkt {edge:+.2f}%" in report.split("BY SOURCE — top_akum_bandar")[1].splitlines()[1]


def test_evaluate_signals_retired_day_prices_cannot_move_the_source_comparison():
    import evaluate_signals as ev
    conn, days, retired = downstream_db(retired_days=12)
    before = ev.evaluate(conn)
    # Make would-be top_akum names on retired days wildly profitable: no effect on
    # top_akum_bandar's comparison may leak in through its control.
    horizon_safe = retired[max(ev.HORIZONS) + 1:]
    conn.execute("UPDATE market_summary_daily SET close = close * 3, high = high * 3, low = low * 3 "
                 f"WHERE date IN ({','.join('?' * len(horizon_safe))})", horizon_safe)
    after = ev.evaluate(conn)
    signal_days = [d for d in days if d not in retired]
    last_window = days.index(signal_days[-1]) + 1 + max(ev.HORIZONS)
    assert days[last_window] < horizon_safe[0]            # no top_akum outcome window reaches the edited days
    assert after["by_source"]["top_akum_bandar"] == before["by_source"]["top_akum_bandar"]
    assert after["source_control"]["top_akum_bandar"] == before["source_control"]["top_akum_bandar"]
    assert after["control"] != before["control"]           # the combined control does see them


def test_evaluate_signals_live_source_outage_drops_the_day_from_combined_but_not_other_sources():
    import evaluate_signals as ev
    conn, days, retired = downstream_db(retired_days=12, outage_day="2026-08-05")
    res = ev.evaluate(conn)
    for h in ev.HORIZONS:
        assert res["control"][V1][h] == expected_control(ev, conn, lambda d: d != "2026-08-05", h)
        assert res["source_control"]["dashboard_Foreign"][h] == res["control"][V1][h]
        assert res["source_control"]["top_akum_bandar"][h] == expected_control(
            ev, conn, lambda d: d not in retired, h)                      # top_akum was available that day
        panel, dates = ev.load_panel(conn)
        overall = [ev.outcome(panel, dates, t, d, h) for d, t, _s in ev.load_signals(conn) if d != "2026-08-05"]
        assert res["overall"][V1][h] == ev.summarise([o for o in overall if o])
    assert res["incomplete_days"] == 1


def test_evaluate_signals_uses_the_lifecycle_registry_where_no_status_row_exists():
    import evaluate_signals as ev
    assert ev.source_state({}, "top_akum_bandar", "2026-09-13") is None
    assert ev.source_state({}, "top_akum_bandar", "2026-09-14") == nsc.RETIRED_SOURCE
    assert ev.source_state({}, "dashboard_Foreign", "2026-09-14") is None
    assert ev.source_state({"dashboard_Foreign": {"2026-09-01": nsc.EMPTY_UNVERIFIED}},
                           "dashboard_Foreign", "2026-09-01") is None


def test_signal_strategy_regime_is_versioned_by_the_lifecycle_registry():
    assert nsc.signal_strategy_regime("2026-09-13") == {"strategy": V1, "since": None, "retired_sources": []}
    assert nsc.signal_strategy_regime("2026-09-14") == {"strategy": V2, "since": "2026-09-14",
                                                        "retired_sources": ["top_akum_bandar"]}
    assert nsc.signal_strategy_regime("2026-09-30", lifecycle={})["strategy"] == V1


def test_evaluate_signals_never_pools_all_signals_across_strategy_versions():
    import evaluate_signals as ev
    conn, days, retired = downstream_db(retired_days=21, start="2026-08-26")     # retirement from 2026-09-14
    assert retired[0] == "2026-09-14"
    res = ev.evaluate(conn)
    assert set(res["overall"]) == set(res["control"]) == {V1, V2}
    assert res["strategies"][V1] == {"since": None, "retired_sources": [], "first": "2026-08-26", "last": "2026-09-13"}
    assert res["strategies"][V2] == {"since": "2026-09-14", "retired_sources": ["top_akum_bandar"],
                                     "first": "2026-09-14", "last": days[-1]}
    panel, dates = ev.load_panel(conn)
    for h in ev.HORIZONS:
        for version, keep in ((V1, lambda d: d < "2026-09-14"), (V2, lambda d: d >= "2026-09-14")):
            hits = [ev.outcome(panel, dates, t, d, h) for d, t, _s in ev.load_signals(conn) if keep(d)]
            assert res["overall"][version][h] == ev.summarise([o for o in hits if o])
            assert res["control"][version][h] == expected_control(ev, conn, keep, h)
    report = ev.format_report(res)
    assert f"ALL SIGNALS [{V1}: 2026-08-26 → 2026-09-13] vs market" in report
    assert f"ALL SIGNALS [{V2}: 2026-09-14 → {days[-1]}; without retired top_akum_bandar since 2026-09-14]" in report
    assert "\nALL SIGNALS vs market" not in report


def test_run_ml_reports_tags_strategy_versions_and_never_pools_them_silently(monkeypatch):
    import pandas as pd
    import run_ml_reports as rmr
    conn, days, retired = downstream_db(retired_days=21, start="2026-08-26")
    px = pd.read_sql("SELECT date, ticker, close, high, low FROM market_summary_daily ORDER BY ticker, date", conn)
    px["fwd_1"] = px.groupby("ticker")["close"].shift(-1) / px["close"] - 1
    monkeypatch.setattr(rmr, "clean_panel", lambda conn, **kw: px.copy())
    res = rmr.run_konglo_watch_report(conn)
    assert all(e["strategy"] == (V2 if e["flag_date"] >= "2026-09-14" else V1) for e in res["signals"])
    assert set(res["resolved_by_strategy"]) == {V1, V2}
    note = rmr.strategy_mix_note(res)
    assert V1 in note and V2 in note and "not one unchanged strategy" in note
    assert rmr.strategy_mix_note({"resolved_by_strategy": {V1: 5}}) == ""


def test_run_ml_reports_konglo_watch_only_measures_recorded_hits(monkeypatch):
    import pandas as pd
    import run_ml_reports as rmr
    conn, days, retired = downstream_db(retired_days=12)
    px = pd.read_sql("SELECT date, ticker, close, high, low FROM market_summary_daily ORDER BY ticker, date", conn)
    px["fwd_1"] = px.groupby("ticker")["close"].shift(-1) / px["close"] - 1
    monkeypatch.setattr(rmr, "clean_panel", lambda conn, **kw: px.copy())
    base = rmr.run_konglo_watch_report(conn)
    flagged = set(conn.execute("SELECT flag_date, ticker FROM konglo_signal_watch").fetchall())
    assert base["signals"] and base["resolved"]["n_trades"] > 0
    assert {(e["flag_date"], e["ticker"]) for e in base["signals"]} <= flagged
    assert not any(e["ticker"] == "T01" and e["flag_date"] in retired for e in base["signals"])
    # No status, lifecycle or unsignalled population enters the report: changing them changes nothing.
    conn.execute("DROP TABLE signal_source_status")
    for d in days:
        nsc.record_signal_source_status(conn, d, [nsc.SignalResult("top_akum_bandar", nsc.SOURCE_UNAVAILABLE)])
    assert rmr.run_konglo_watch_report(conn) == base


def test_end_to_end_account_like_content_reaches_neither_fragments_nor_the_public_database(tmpdir_path, monkeypatch):
    email = "someone@example.com"
    pages = [[row("AAAA", symbol_1="AAAA", **{email: True}), row("BBBB", symbol_1="BBBB", owner_note="acct-789")]]
    fake = FakeNeoBDM(pages, ACTIVE, stored=ACTIVE)
    db = os.path.join(tmpdir_path, "neobdm.db")
    monkeypatch.setattr(ns, "DB_PATH", db)
    monkeypatch.setattr(ns, "API_PAGE_PAUSE", 0)
    monkeypatch.setattr(ns, "_offset_safe", lambda what: True)
    monkeypatch.setattr(ns, "_api_session", lambda page: (fake, {"X-CSRFToken": SECRET}))
    ns.scrape_market_summary(page=None)
    conn = sqlite3.connect(db)
    everything = "\n".join(repr(r) for t in ("ms_capture_manifest", "ms_raw_response", "market_summary_daily")
                           for r in conn.execute(f"SELECT * FROM {t}"))
    for leak in (email, "acct-789", SECRET, "OtherPrivateScreener"):
        assert leak not in everything, leak
        assert leak.encode() not in stored_fragments(tmpdir_path), leak
    assert json.loads(conn.execute("SELECT unexpected_keys FROM ms_capture_manifest").fetchone()[0]) == [
        "owner_note", "<non-identifier key>", "symbol_1"]
    page_kind, dropped = conn.execute("SELECT raw_kind, dropped_paths FROM ms_raw_response WHERE page = 1").fetchone()
    assert page_kind == nsc.RAW_KIND_SANITIZED
    assert json.loads(dropped) == ["$.data[].<non-identifier key>", "$.data[].owner_note"]


def test_raw_fragment_directory_is_not_gitignored():
    import subprocess
    out = subprocess.run(["git", "check-ignore", "-q", "market_summary_raw_fragments/ab/abc.json.gz"], cwd=HERE)
    assert out.returncode == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
