"""Tests for the NeoBDM market-summary source-contract repair (2026-09-14 incident:
is_unusual_volume retired by NeoBDM). No network, no Playwright session.

    py -3 -m pytest test_neobdm_source_contract.py -v
"""

import gzip
import hashlib
import json
import os
import re
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
    assert lines[1:] == ["b Bandarmologi: ⚠️ unavailable: request failed: x",
                         "n NonRetail: ⚠️ empty — unverified: screener returned no rows",
                         "f Foreign: ABCD(n/a)"]


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


# ── Telegram report truthfulness ──────────────
TOLD_NO_SELL = "Tidak ada retail net sell hari ini"


class FakeCell:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text

    def inner_html(self):
        return self.text


class FakeTr:
    def __init__(self, cells):
        self.cells = cells

    def query_selector(self, sel):
        col = sel.split('data-dash-column="')[1].split('"')[0]
        return FakeCell(self.cells[col]) if col in self.cells else None


OLD_TABLE = "<table><tr><td>left over from before the submit</td></tr></table>"
DASH_URL = "https://neobdm.tech/django_plotly_dash/app/bs_app/_dash-update-component"
DIST, AKUM = "broker-dist-stalker", "broker-akum-stalker"


UNSET = object()


# The live submit callback, as captured 2026-09-29: one request whose structured
# outputs are both side containers' children, answered by a multi-output body
# whose side children are a Label and that side's DataTable.
LIVE_OUTPUT = f"..{AKUM}.children...{DIST}.children.."
LIVE_OUTPUTS = ({"id": AKUM, "property": "children"}, {"id": DIST, "property": "children"})


def dash_label(text):
    return {"props": {"children": text}, "type": "Label", "namespace": "dash_html_components"}


def dash_table(table_id, data=UNSET, type_="DataTable", namespace="dash_table"):
    props = {"id": table_id, "columns": [{"name": "Symbol", "id": "symbol"}]}
    if data is not UNSET:
        props["data"] = data
    return {"props": props, "type": type_, "namespace": namespace}


def stalker_row(symbol, netval, savg="100"):
    """A row as parse_stalker_table reads it from the DOM (the live DOM shows the
    plain ticker and the cells' float text, e.g. "-5000" or "-1234.5")."""
    return {"symbol": symbol, "netval": netval, "bval": "1", "sval": "2", "bavg": "3", "savg": savg}


SELL_ROWS = [stalker_row("AAAA", "-5000"), stalker_row("BBBB", "-3000"), stalker_row("CCCC", "1000")]
# strict=False (broker_flow) reads DOM text through parse_num, which also takes comma-grouped
# numbers; its tests keep that text so the legacy parse stays covered.
LEGACY_DOM_ROWS = [stalker_row("AAAA", "-5,000"), stalker_row("BBBB", "-3,000"), stalker_row("CCCC", "1,000")]
LIVE_ROUTE = "/some_route/"          # any route: only the label and last segment are the contract


def live_entry(row, route=LIVE_ROUTE):
    """The live props.data entry a DOM `row` is rendered from: the symbol as a
    Markdown link [TICKER](/route/TICKER), every other cell a JSON float."""
    return {"symbol": f"[{row['symbol']}]({route}{row['symbol']})",
            **{f: float(row[f].replace(",", "")) for f in ("netval", "bval", "sval", "bavg", "savg")}}


def live_entries(rows):
    return [live_entry(r) for r in rows]


def live_side(side, data=None):
    """One side's live update; its DataTable data defaults to SELL_ROWS' live entries."""
    data = live_entries(SELL_ROWS) if data is None else data
    return {"children": [dash_label(f"{side} stalker"), dash_table(f"stalker-{side}-table", list(data))]}


DROP = object()


def live_body(akum=UNSET, dist=UNSET):
    """The live response body; akum/dist replace that side's update (DROP leaves it out)."""
    response = {}
    for component, side, update in ((AKUM, "akum", akum), (DIST, "dist", dist)):
        if update is not DROP:
            response[component] = live_side(side) if update is UNSET else update
    return {"multi": True, "response": response}


class FakeDashResponse:
    """A Playwright Response as the submit-callback predicate and
    _submit_and_confirm_callback read it. By default: the exact live submit
    callback (LIVE_OUTPUT / LIVE_OUTPUTS request, live_body() response).
    output/outputs=UNSET leaves that key out of the request body; a non-string
    body is sent as its JSON."""
    def __init__(self, changed=("submit-button.n_clicks",), status=200, method="POST", url=DASH_URL,
                 post_data="json", body=UNSET, output=LIVE_OUTPUT, outputs=LIVE_OUTPUTS):
        if post_data == "json":
            payload = {"changedPropIds": list(changed), "inputs": []}
            if output is not UNSET:
                payload["output"] = output
            if outputs is not UNSET:
                payload["outputs"] = list(outputs) if isinstance(outputs, tuple) else outputs
            post_data = json.dumps(payload)
        body = live_body() if body is UNSET else body
        self.url, self.status = url, status
        self._body = body if isinstance(body, str) else json.dumps(body)
        self.request = type("Req", (), {"method": method, "post_data": post_data})()

    def text(self):
        return self._body


SUBMIT_OK = FakeDashResponse()


class FakeStalkerPage:
    """Just enough of the broker_stalker page for get_netflow. `fail` names the
    one step that raises: "duration", "submit" or "parse".

    `rows` are the rows the DOM table shows. The side container's HTML is
    `before` (None = no container) until the submit click; `lag` polls later it
    becomes `after` (default: a fresh table for `rows`). The click emits
    `callbacks` (default: one live submit callback whose DataTable data is the
    props.data entries `response_rows`, by default `rows` in the live shape),
    which only a response waiter armed before the click can see. `dom_reads`
    counts reads of the DOM table."""
    def __init__(self, rows=(), fail=None, before=None, after="fresh", lag=1, callbacks=None,
                 response_rows=UNSET):
        self.rows, self.fail = list(rows), fail
        self.dom_reads = 0
        self.before = before
        self.after = f"<table><tr><td>{len(self.rows)} fresh rows</td></tr></table>" if after == "fresh" else after
        self.lag = lag
        data = live_entries(self.rows) if response_rows is UNSET else list(response_rows)
        self.callbacks = ([FakeDashResponse(body=live_body(akum=live_side("akum", data),
                                                          dist=live_side("dist", data)))]
                          if callbacks is None else list(callbacks))
        self.keyboard = self
        self.gotos, self.waits = [], []
        self.fingerprints = 0
        self.expect_calls = 0
        self._armed = None           # responses seen by the armed waiter, if any
        self.submitted_at = None     # len(self.waits) when submit was clicked

    def expect_response(self, predicate, timeout=None):
        self.expect_calls += 1
        page = self

        class Info:
            value = None

        class Waiter:
            def __enter__(self):
                page._armed = []
                return Info

            def __exit__(self, exc_type, exc, tb):
                seen, page._armed = page._armed, None
                if exc is not None:
                    return False
                match = next((r for r in seen if predicate(r)), None)
                if match is None:
                    raise TimeoutError(f'Timeout {timeout}ms exceeded while waiting for event "response"')
                Info.value = match
                return False
        return Waiter()

    def goto(self, url, **kw):
        self.gotos.append(url)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def type(self, text):
        pass

    def press(self, key):
        pass

    def click(self, sel, timeout=None):
        if sel == "#submit-button":
            if self.fail == "submit":
                raise TimeoutError("page.click: Timeout 5000ms exceeded waiting for #submit-button")
            self.submitted_at = len(self.waits)
            if self._armed is not None:
                self._armed.extend(self.callbacks)

    def query_selector(self, sel):
        self.fingerprints += 1
        refreshed = self.submitted_at is not None and len(self.waits) - self.submitted_at >= self.lag
        html = self.after if refreshed else self.before
        return None if html is None else FakeCell(html)

    def locator(self, sel):
        page = self

        class Loc:
            first = property(lambda self: self)

            def count(self):
                return 0

            def click(self, timeout=None):
                if "duration-picker" in sel and page.fail == "duration":
                    raise TimeoutError("locator.click: Timeout 5000ms exceeded")
        return Loc()

    def wait_for_selector(self, sel, timeout=None):
        self.dom_reads += 1
        if self.fail == "parse":
            raise TimeoutError(f"page.wait_for_selector: Timeout 15000ms waiting for {sel}")

    def query_selector_all(self, sel):
        self.dom_reads += 1
        return [FakeTr(r) for r in self.rows]


def stalker_text(result):
    return "\n".join(ns._broker_stalker_lines(result))


@pytest.mark.parametrize("fail, stage", [("duration", "duration 'Today' switch failed"),
                                         ("submit", "submit failed")])
def test_broker_stalker_strict_scan_failures_are_source_unavailable(fail, stage):
    got = ns.scrape_broker_stalker(FakeStalkerPage(SELL_ROWS, fail=fail))
    assert isinstance(got, nsc.SignalResult)
    assert got.source == "broker_stalker"
    assert got.status == nsc.SOURCE_UNAVAILABLE and got.hits == []
    assert stage in got.detail and "TimeoutError" in got.detail
    text = stalker_text(got)
    assert TOLD_NO_SELL not in text
    assert "Source unavailable" in text and stage in text


def test_broker_stalker_other_scan_failure_is_source_unavailable():
    page = FakeStalkerPage(SELL_ROWS)

    def boom(url, **kw):
        raise RuntimeError("net::ERR_CONNECTION_RESET")
    page.goto = boom
    got = ns.scrape_broker_stalker(page)
    assert got.status == nsc.SOURCE_UNAVAILABLE and "ERR_CONNECTION_RESET" in got.detail


@pytest.mark.parametrize("rows, detail", [([stalker_row("CCCC", "1000"), stalker_row("DDDD", "0")],
                                           "2 row(s) parsed, none with a negative netval"),
                                          ([], "dist table parsed with no rows")])
def test_broker_stalker_parsed_but_no_negative_rows_is_empty_unverified(rows, detail):
    got = ns.scrape_broker_stalker(FakeStalkerPage(rows))
    assert got.status == nsc.EMPTY_UNVERIFIED and got.status != nsc.NO_HITS
    assert got.hits == [] and got.detail == detail
    text = stalker_text(got)
    assert TOLD_NO_SELL not in text
    assert "Empty — unverified" in text and detail in text


def test_broker_stalker_hits_and_bag_holder_wording_are_unchanged(monkeypatch):
    def holders(page, symbol, captures=None):
        if symbol == "BBBB":
            raise RuntimeError("inventory API status=500")
        return [{"code": "AK", "cum": 12345.0, "avg": 1500.0}] if symbol == "AAAA" else []
    monkeypatch.setattr(ns, "get_inventory_bagholders", holders)
    monkeypatch.setattr(ns, "_bagholder_captures", lambda: None)
    rows = SELL_ROWS + [stalker_row("EEEE", "-1000", savg="90")]
    got = ns.scrape_broker_stalker(FakeStalkerPage(rows))
    assert got.status == nsc.HITS
    assert [(r["symbol"], r["holders_failed"]) for r in got] == [
        ("AAAA", False), ("BBBB", True), ("EEEE", False)]
    assert ns._broker_stalker_lines(got) == [
        "🕵️ Broker Stalker — Retail (XL+XC) Net Sell → top 2 bag holder",
        "(observable inventory ~60 hari bursa; bukan beneficial ownership)",
        "1. AAAA | retail jual -5000  savg: 100",
        "   🎒 Bag holder: AK 12k lot @1500",
        "2. BBBB | retail jual -3000  savg: 100",
        "   🎒 Bag holder: ⚠️ gagal ambil",
        "3. EEEE | retail jual -1000  savg: 90",
        "   🎒 Bag holder: tidak ada akumulator",
    ]
    # the HITS rendering is the one the plain row list always had
    assert ns._broker_stalker_lines(got) == ns._broker_stalker_lines(list(got.hits))


def strict_scan(**page_kw):
    """(get_netflow strict result or the RuntimeError it raised, the scrape's SignalResult)."""
    try:
        got = ns.get_netflow(FakeStalkerPage(SELL_ROWS, **page_kw), ["XL"], "Today", side="dist", strict=True)
    except RuntimeError as e:
        got = e
    return got, ns.scrape_broker_stalker(FakeStalkerPage(SELL_ROWS, **page_kw))


NO_SUBMIT_CALLBACK = "no submit-button.n_clicks Dash callback for broker-dist-stalker within 20s of submit"


def test_cosmetic_dom_change_without_a_submit_callback_is_unavailable():
    # The table visibly changes and settles, but the server never answered a
    # submit-triggered callback: the change proves nothing.
    for after in ("<table><tr><td>re-rendered, same old rows</td></tr></table>", "fresh"):
        got, result = strict_scan(before=OLD_TABLE, after=after, callbacks=[])
        assert isinstance(got, RuntimeError) and NO_SUBMIT_CALLBACK in str(got)
        assert result.status == nsc.SOURCE_UNAVAILABLE and result.hits == []
        assert NO_SUBMIT_CALLBACK in result.detail
        assert "AAAA" not in stalker_text(result)            # the table's rows are never shown


def test_confirmed_callback_with_a_byte_identical_table_is_accepted():
    identical = "<table><tr><td>the same rows before and after</td></tr></table>"
    got, result = strict_scan(before=identical, after=identical)
    assert sorted(got) == ["AAAA", "BBBB", "CCCC"]
    assert result.status == nsc.HITS and [r["symbol"] for r in result][:2] == ["AAAA", "BBBB"]


@pytest.mark.parametrize("status, body, reason", [
    (500, '{"message": "Internal Server Error"}', "HTTP 500"),
    (204, "", "HTTP 204"),                                    # Dash PreventUpdate: nothing updated
    (403, "<html>CSRF verification failed</html>", "HTTP 403"),
    (200, "<html>login</html>", "returned no JSON object"),
])
def test_non_success_submit_callback_is_unavailable(status, body, reason):
    got, result = strict_scan(callbacks=[FakeDashResponse(status=status, body=body)])
    assert isinstance(got, RuntimeError) and reason in str(got)
    assert result.status == nsc.SOURCE_UNAVAILABLE and reason in result.detail


@pytest.mark.parametrize("callback", [
    FakeDashResponse(changed=("broker.value",)),                          # chip callback
    FakeDashResponse(changed=("duration-picker.value",)),                 # duration callback
    FakeDashResponse(changed=()),
    FakeDashResponse(post_data='{"output": "x"}'),                        # no changedPropIds
    FakeDashResponse(post_data="not json"),                               # unvalidatable payload
    FakeDashResponse(post_data=None),
    FakeDashResponse(method="GET"),
    FakeDashResponse(url="https://neobdm.tech/api/screeners"),            # not a Dash callback
])
def test_callback_not_proven_submit_triggered_is_ignored_and_the_scan_fails(callback):
    got, result = strict_scan(before=OLD_TABLE, callbacks=[callback])
    assert isinstance(got, RuntimeError) and NO_SUBMIT_CALLBACK in str(got)
    assert result.status == nsc.SOURCE_UNAVAILABLE


def test_submit_callback_among_others_is_found():
    others = [FakeDashResponse(changed=("broker.value",)), SUBMIT_OK]
    for before, lag in ((None, 1), (None, 6), (OLD_TABLE, 6)):
        got, result = strict_scan(before=before, lag=lag, callbacks=others)
        assert sorted(got) == ["AAAA", "BBBB", "CCCC"], (before, lag)
        assert result.status == nsc.HITS
    page = FakeStalkerPage([stalker_row("CCCC", "1000")], lag=4)
    assert ns.scrape_broker_stalker(page).status == nsc.EMPTY_UNVERIFIED
    assert page.expect_calls == 1


# ── strict rows are the validated response's rows, never the DOM's
STALE_DOM_ROWS = [stalker_row("OLDA", "-9000"), stalker_row("OLDB", "-8000")]
NEW_ROWS = [stalker_row("NEWA", "-4000", savg="55.5"), stalker_row("NEWB", "2000")]


def stale_dom_page(response_rows, **kw):
    """A page whose DOM table shows STALE_DOM_ROWS, present and byte-stable before
    and after the submit, forever; the validated response's props.data is `response_rows`."""
    return FakeStalkerPage(STALE_DOM_ROWS, before=OLD_TABLE, after=OLD_TABLE,
                           response_rows=response_rows, **kw)


def by_symbol(rows):
    return {r["symbol"]: r for r in rows}


def test_strict_rows_are_the_response_rows_while_the_dom_stays_stale():          # ROWS-A
    page = stale_dom_page(live_entries(NEW_ROWS))
    assert ns.get_netflow(page, ["XL"], "Today", side="dist", strict=True) == by_symbol(NEW_ROWS)
    assert page.dom_reads == 0 and page.fingerprints == 0                  # the DOM is never read
    unreadable = stale_dom_page(live_entries(NEW_ROWS), fail="parse")                    # nor needed
    assert ns.get_netflow(unreadable, ["XL"], "Today", side="dist", strict=True) == by_symbol(NEW_ROWS)
    result = ns.scrape_broker_stalker(stale_dom_page(live_entries(NEW_ROWS)))
    assert result.status == nsc.HITS and [r["symbol"] for r in result] == ["NEWA"]
    text = stalker_text(result)
    assert "NEWA | retail jual -4000  savg: 55.5" in text and "OLD" not in text


def test_empty_response_data_never_lets_stale_dom_rows_through():                # ROWS-B
    page = stale_dom_page([])
    assert ns.get_netflow(page, ["XL"], "Today", side="dist", strict=True) == {}
    assert page.dom_reads == 0
    for rows, detail in (([], "dist table parsed with no rows"),
                         ([stalker_row("NEWB", "2000")], "1 row(s) parsed, none with a negative netval")):
        result = ns.scrape_broker_stalker(stale_dom_page(live_entries(rows)))
        assert result.status == nsc.EMPTY_UNVERIFIED and result.hits == [] and result.detail == detail
        assert "OLD" not in stalker_text(result)


def test_response_rows_identical_to_the_dom_are_accepted():                      # ROWS-C
    runs = [ns.get_netflow(FakeStalkerPage(SELL_ROWS, before=OLD_TABLE, after=OLD_TABLE),
                           ["XL"], "Today", side="dist", strict=True) for _ in range(2)]
    assert runs[0] == runs[1] == by_symbol(SELL_ROWS)


def test_numeric_response_cells_become_the_dom_text_contract():
    entry = {"symbol": "[AAAA](/some_route/AAAA)", "netval": -5000, "bval": 1.5, "sval": 2.0, "bavg": "(3)", "savg": " 1,234.5 "}
    got = ns.get_netflow(stale_dom_page([entry]), ["XL"], "Today", side="dist", strict=True)
    assert got == {"AAAA": {"symbol": "AAAA", "netval": "-5000", "bval": "1.5", "sval": "2",
                            "bavg": "(3)", "savg": "1,234.5"}}
    assert [ns.parse_num(got["AAAA"][f]) for f in ns.STALKER_ROW_FIELDS[1:]] == [-5000, 1.5, 2, -3, 1234.5]


GOOD_ROW = live_entry(stalker_row("AAAA", "-5000"))
NOT_A_LINK = "is not a Markdown link [TICKER](/path/TICKER)"


@pytest.mark.parametrize("entry, reason", [                                      # ROWS-D
    ("AAAA", "row 0 is not an object"),
    (None, "row 0 is not an object"),
    (["AAAA", "-5,000"], "row 0 is not an object"),
    ({}, "row 0 lacks symbol, netval, bval, sval, bavg, savg"),
    ({"symbol": "AAAA", "netval": "-5,000"}, "row 0 lacks bval, sval, bavg, savg"),
    ({k: v for k, v in GOOD_ROW.items() if k != "savg"}, "row 0 lacks savg"),
    ({**GOOD_ROW, "symbol": ""}, f"row 0 symbol: '' {NOT_A_LINK}"),
    ({**GOOD_ROW, "symbol": "   "}, f"row 0 symbol: '   ' {NOT_A_LINK}"),
    ({**GOOD_ROW, "symbol": None}, f"row 0 symbol: None {NOT_A_LINK}"),
    ({**GOOD_ROW, "symbol": 123}, f"row 0 symbol: 123 {NOT_A_LINK}"),
    ({**GOOD_ROW, "symbol": "ZZZZ"}, f"row 0 symbol: 'ZZZZ' {NOT_A_LINK}"),            # a plain ticker
    ({**GOOD_ROW, "netval": None}, "row 0 netval: None is not a number"),
    ({**GOOD_ROW, "netval": ""}, "row 0 netval: '' is not a number"),
    ({**GOOD_ROW, "netval": "-"}, "row 0 netval: '-' is not a number"),
    ({**GOOD_ROW, "netval": "N/A"}, "row 0 netval: 'N/A' is not a number"),
    ({**GOOD_ROW, "savg": "12abc"}, "row 0 savg: '12abc' is not a number"),
    ({**GOOD_ROW, "netval": True}, "row 0 netval: True is not a number"),
    ({**GOOD_ROW, "bval": [1]}, "row 0 bval: [1] is not a number"),
    ({**GOOD_ROW, "bavg": {"v": 1}}, "row 0 bavg: {'v': 1} is not a number"),
    ({**GOOD_ROW, "netval": "nan"}, "row 0 netval: 'nan' is not a finite number"),
    ({**GOOD_ROW, "sval": float("inf")}, "row 0 sval: inf is not a finite number"),
])
def test_malformed_response_row_fails_closed(entry, reason):
    for rows, where in (([entry], reason), ([GOOD_ROW, entry], reason.replace("row 0", "row 1"))):
        page = stale_dom_page(rows)
        with pytest.raises(RuntimeError) as e:
            ns.get_netflow(page, ["XL"], "Today", side="dist", strict=True)
        assert f"{BAD_RESPONSE} stalker-dist-table {where}" in str(e.value)
        assert page.dom_reads == 0
        result = ns.scrape_broker_stalker(stale_dom_page(rows))
        assert result.status == nsc.SOURCE_UNAVAILABLE and result.hits == []
        assert "OLD" not in stalker_text(result) and "retail jual" not in stalker_text(result)


def test_legacy_netflow_reads_the_dom_even_when_a_response_carries_other_rows():  # ROWS-E
    page = FakeStalkerPage(STALE_DOM_ROWS, response_rows=live_entries(NEW_ROWS))
    assert ns.get_netflow(page, ["XL"], "Today", side="dist") == by_symbol(STALE_DOM_ROWS)
    assert page.expect_calls == 0 and page.dom_reads == 2 and page.waits[page.submitted_at:] == [4000]


# ── strict symbol contract: the live Markdown link [TICKER](/route/TICKER) ──
@pytest.mark.parametrize("cell, ticker", [
    ("[BBCA](/some_route/BBCA)", "BBCA"),                     # SYM-A. the live shape
    ("[BBCA](/other_route/BBCA)", "BBCA"),                    # any route: not the contract
    ("[BBCA](/BBCA)", "BBCA"),
    ("[BBCA](/a/b/c/BBCA)", "BBCA"),
    ("[BBCA](/some_route/BBCA/)", "BBCA"),                    # final NON-EMPTY segment
    ("[BBCA](/some_route/BBCA?tab=flow)", "BBCA"),            # query ignored
    ("[BBCA](/some_route/BBCA#top)", "BBCA"),                 # fragment ignored
    ("[BBCA](/some_route/BBCA?next=/x/ZZZZ#/y/QQQQ)", "BBCA"),
    ("[AB](/some_route/AB)", "AB"),                           # no fixed length
    ("[ABCDEFGH](/some_route/ABCDEFGH)", "ABCDEFGH"),
    ("[AB1C](/some_route/AB1C)", "AB1C"),                     # SYM-B. digits
    ("[1234](/some_route/1234)", "1234"),
    ("[BBCA-R](/some_route/BBCA-R)", "BBCA-R"),               # SYM-C. suffixes
    ("[ABCD-W](/some_route/ABCD-W)", "ABCD-W"),
    ("[AB1C-W2-R](/some_route/AB1C-W2-R)", "AB1C-W2-R"),
])
def test_live_markdown_symbol_becomes_the_canonical_ticker(cell, ticker):
    assert ns._stalker_symbol(cell) == ticker


MISMATCH = "does not end in its label"
NOT_RELATIVE = "is not a site-relative /path"
NOT_TICKER = "is not a ticker"


@pytest.mark.parametrize("cell, reason", [
    # SYM-D. label and path disagree
    ("[BBCA](/some_route/BBRI)", MISMATCH),
    ("[BBCA](/some_route/BBCA-R)", MISMATCH),
    ("[BBCA-R](/some_route/BBCA)", MISMATCH),
    ("[BBCA](/some_route/bbca)", MISMATCH),
    ("[BBCA](/some_route/XBBCA)", MISMATCH),
    ("[BBCA](/some_route/BBCA/extra)", MISMATCH),
    ("[BBCA](/BBCA/some_route)", MISMATCH),
    ("[BBCA](/some_route/BB%43A)", MISMATCH),                 # no decoding
    ("[BBCA](/some_route/)", MISMATCH),
    ("[BBCA](/)", MISMATCH),
    ("[BBCA](/?q=/x/BBCA)", MISMATCH),                        # the label only in the query
    ("[BBCA](/some_route/#/BBCA)", MISMATCH),                 # ... or the fragment
    # SYM-E. malformed Markdown / not a link at all
    ("BBCA", NOT_A_LINK),
    ("", NOT_A_LINK),
    ("[BBCA]", NOT_A_LINK),
    ("[BBCA](/some_route/BBCA", NOT_A_LINK),
    ("BBCA](/some_route/BBCA)", NOT_A_LINK),
    ("[BBCA] (/some_route/BBCA)", NOT_A_LINK),
    ("[BBCA][/some_route/BBCA]", NOT_A_LINK),
    ("[[BBCA]](/some_route/BBCA)", NOT_A_LINK),
    ("[BBCA](/some_route/BBCA) ", NOT_A_LINK),
    (" [BBCA](/some_route/BBCA)", NOT_A_LINK),
    ("[BBCA](/some_route/BBCA)\n", NOT_A_LINK),
    ("x[BBCA](/some_route/BBCA)", NOT_A_LINK),
    ("[BBCA](/some_route/BBCA)[BBRI](/some_route/BBRI)", NOT_A_LINK),
    ("[BBCA]( /some_route/BBCA)", NOT_A_LINK),
    ('[BBCA](/some_route/BBCA "title")', NOT_A_LINK),
    ("[BBCA](/some_route/(BBCA))", NOT_A_LINK),
    ("<a href='/some_route/BBCA'>BBCA</a>", NOT_A_LINK),
    ("[BBCA](/some_route\x01/BBCA)", NOT_A_LINK),           # ASCII control characters
    ("[BBCA](/x\x1b[31m/BBCA)", NOT_A_LINK),
    ("[BBCA](/x\x7f/BBCA)", NOT_A_LINK),
    ("[BBCA](/x\x00y/BBCA)", NOT_A_LINK),
    (None, NOT_A_LINK),
    (123, NOT_A_LINK),
    (["[BBCA](/some_route/BBCA)"], NOT_A_LINK),
    # SYM-F. absolute / external / scheme URLs
    ("[BBCA](https://example.com/some_route/BBCA)", NOT_RELATIVE),
    ("[BBCA](http://example.com/BBCA)", NOT_RELATIVE),
    ("[BBCA](//example.com/BBCA)", NOT_RELATIVE),             # protocol-relative
    ("[BBCA](/\\example.com/BBCA)", NOT_RELATIVE),            # browsers read /\ as //
    ("[BBCA](\\\\example.com\\BBCA)", NOT_RELATIVE),
    ("[BBCA](javascript:void/BBCA)", NOT_RELATIVE),
    ("[BBCA](javascript:alert(1))", NOT_A_LINK),
    ("[BBCA](data:text/html,/BBCA)", NOT_RELATIVE),
    ("[BBCA](mailto:x/BBCA)", NOT_RELATIVE),
    ("[BBCA](some_route/BBCA)", NOT_RELATIVE),                # relative, not site-relative
    ("[BBCA](BBCA)", NOT_RELATIVE),
    ("[BBCA]()", NOT_RELATIVE),
    # SYM-G. not a plausible ticker token
    ("[](/some_route/)", NOT_TICKER),
    ("[bbca](/some_route/bbca)", NOT_TICKER),
    ("[Bbca](/some_route/Bbca)", NOT_TICKER),
    ("[BBCA-r](/some_route/BBCA-r)", NOT_TICKER),
    ("[BBCA-](/some_route/BBCA-)", NOT_TICKER),
    ("[-BBCA](/some_route/-BBCA)", NOT_TICKER),
    ("[BBCA--R](/some_route/BBCA--R)", NOT_TICKER),
    ("[BB CA](/some_route/BBCA)", NOT_TICKER),
    ("[BB.CA](/some_route/BB.CA)", NOT_TICKER),
    ("[BBCA.JK](/some_route/BBCA.JK)", NOT_TICKER),
    ("[BBCA_R](/some_route/BBCA_R)", NOT_TICKER),
    ("[BBÇA](/some_route/BBÇA)", NOT_TICKER),                 # non-ASCII uppercase
    ("[ＢＢＣＡ](/some_route/ＢＢＣＡ)", NOT_TICKER),            # full-width
    ("[BBCA​](/some_route/BBCA​)", NOT_TICKER),     # zero-width space
    ("[AB１](/some_route/AB１)", NOT_TICKER),         # full-width digit
    ("[AB١](/some_route/AB١)", NOT_TICKER),         # Arabic-Indic digit
    ("[BBCA-２](/some_route/BBCA-２)", NOT_TICKER),
    ("[\\BBCA](/some_route/\\BBCA)", NOT_TICKER),
])
def test_anything_but_the_live_symbol_link_fails_closed(cell, reason):
    with pytest.raises(ValueError, match=re.escape(reason)):
        ns._stalker_symbol(cell)
    # ... and through the strict scan it is SOURCE_UNAVAILABLE, never a row
    entry = {**live_entry(stalker_row("AAAA", "-5000")), "symbol": cell}
    page = stale_dom_page([entry])
    with pytest.raises(RuntimeError, match=re.escape(f"{BAD_RESPONSE} stalker-dist-table row 0 symbol: ")):
        ns.get_netflow(page, ["XL"], "Today", side="dist", strict=True)
    assert page.dom_reads == 0
    assert ns.scrape_broker_stalker(stale_dom_page([entry])).status == nsc.SOURCE_UNAVAILABLE


def test_a_raw_markdown_link_never_escapes_as_a_symbol():                        # SYM-H
    routes = ("/some_route/", "/a/b/", "/")
    entries = [live_entry(stalker_row(t, n), route=r) for t, n, r in
               zip(("AAAA", "BB1C", "CCCC-R"), ("-5000", "-3000.5", "1000"), routes)]
    got = ns.get_netflow(stale_dom_page(entries), ["XL"], "Today", side="dist", strict=True)
    assert sorted(got) == ["AAAA", "BB1C", "CCCC-R"]
    for key, row in got.items():
        assert key == row["symbol"]
        assert not any(mark in row["symbol"] for mark in ("[", "]", "(", ")", "](", "/"))
    result = ns.scrape_broker_stalker(stale_dom_page(entries))
    assert result.status == nsc.HITS and [r["symbol"] for r in result] == ["AAAA", "BB1C"]
    text = stalker_text(result)
    assert "](" not in text and "some_route" not in text
    assert "1. AAAA | retail jual -5000" in text and "2. BB1C | retail jual -3000.5" in text


def test_legacy_netflow_keeps_the_dom_symbol_text():                             # SYM-I
    page = FakeStalkerPage(LEGACY_DOM_ROWS)            # the response carries Markdown links
    assert "](" in json.dumps(page.callbacks[0]._body)
    got = ns.get_netflow(page, ["XL"], "Today", side="dist")
    assert got == by_symbol(LEGACY_DOM_ROWS) and page.expect_calls == 0
    assert page.dom_reads == 2 and page.waits[page.submitted_at:] == [4000]


def targeting(component, prop="children"):
    """A successful submit-triggered callback whose outputs are `component` only."""
    return FakeDashResponse(output=f"{component}.{prop}", outputs=({"id": component, "property": prop},))


UNRELATED = targeting("stalker-summary-text")


def test_submit_callback_for_an_unrelated_component_is_ignored_and_the_scan_fails():
    for callbacks in ([UNRELATED], [UNRELATED, targeting("broker-akum-stalker")]):
        got, result = strict_scan(before=OLD_TABLE, after=OLD_TABLE, callbacks=callbacks)
        assert isinstance(got, RuntimeError) and NO_SUBMIT_CALLBACK in str(got)
        assert result.status == nsc.SOURCE_UNAVAILABLE and result.hits == []
        assert "AAAA" not in stalker_text(result)             # the stale dist table is never shown


def test_a_later_callback_that_targets_the_dist_table_is_the_one_accepted():
    targeted = targeting("broker-dist-stalker")
    for callbacks in ([UNRELATED, targeted], [targeting("broker-akum-stalker"), UNRELATED, targeted]):
        got, result = strict_scan(before=None, lag=3, callbacks=callbacks)
        assert sorted(got) == ["AAAA", "BBBB", "CCCC"]
        assert result.status == nsc.HITS


def test_targeted_callback_with_a_byte_identical_table_is_accepted():
    identical = "<table><tr><td>the same rows before and after</td></tr></table>"
    got, result = strict_scan(before=identical, after=identical,
                              callbacks=[UNRELATED, targeting("broker-dist-stalker")])
    assert sorted(got) == ["AAAA", "BBBB", "CCCC"]
    assert result.status == nsc.HITS


@pytest.mark.parametrize("output, outputs", [
    (LIVE_OUTPUT, LIVE_OUTPUTS),                                                     # the live request
    (UNSET, [{"id": DIST, "property": "children"}]),                                # `outputs` alone suffices
    ("x.children", [{"id": "x", "property": "children"}, {"id": DIST, "property": "children"}]),
])
def test_structured_outputs_with_the_exact_side_children_pair_are_accepted(output, outputs):
    got, result = strict_scan(callbacks=[FakeDashResponse(output=output, outputs=outputs)])
    assert sorted(got) == ["AAAA", "BBBB", "CCCC"], (output, outputs)
    assert result.status == nsc.HITS


@pytest.mark.parametrize("output, outputs", [
    # A. the side id is declared, but not as the exact {id, property: children} pair
    (f"{DIST}.data", [{"id": DIST, "property": "data"}]),                            # wrong property
    (LIVE_OUTPUT, [{"id": AKUM, "property": "children"}, {"id": DIST, "property": "data"}]),
    (UNSET, [{"id": DIST, "property": "children.props"}]),
    (UNSET, [{"id": DIST}]),                                                         # no property
    (UNSET, [{"id": DIST, "property": "children", "extra": 1}]),                     # not the exact pair
    (LIVE_OUTPUT, UNSET),                                                            # string only: not the proof
    (f"{DIST}.children", {"id": DIST, "property": "children"}),                      # object, not a list
    (UNSET, [[{"id": DIST, "property": "children"}]]),                               # nested list
    (UNSET, {"group": [{"id": DIST, "property": "children"}]}),
    # the side id is not declared at all
    (UNSET, UNSET),
    (None, None),
    ("", []),
    (123, {"property": "children"}),
    (DIST, UNSET),
    (f"{DIST}-summary.children", [{"id": f"{DIST}-summary", "property": "children"}]),  # prefix, not the id
    (f"x-{DIST}.children", [{"id": f"x-{DIST}", "property": "children"}]),             # suffix, not the id
    (UNSET, [{"id": {"type": DIST, "index": 0}, "property": "children"}]),           # pattern id
    (UNSET, [{"id": "x", "property": DIST}]),                                        # named only as a value
    ("x.children", [{"id": "x", "property": "children", "note": DIST}]),
])
def test_missing_or_malformed_output_target_is_rejected(output, outputs):
    got, result = strict_scan(callbacks=[FakeDashResponse(output=output, outputs=outputs)])
    assert isinstance(got, RuntimeError) and NO_SUBMIT_CALLBACK in str(got), (output, outputs)
    assert result.status == nsc.SOURCE_UNAVAILABLE


def test_a_table_named_only_as_input_or_state_is_not_a_target():
    payload = json.dumps({"changedPropIds": ["submit-button.n_clicks"], "output": "x.children",
                          "inputs": [{"id": DIST, "property": "data"}],
                          "state": [{"id": DIST, "property": "data"}]})
    got, _ = strict_scan(callbacks=[FakeDashResponse(post_data=payload)])
    assert isinstance(got, RuntimeError) and NO_SUBMIT_CALLBACK in str(got)


def test_each_side_accepts_only_its_own_table_callback():
    dist, akum = targeting("broker-dist-stalker"), targeting("broker-akum-stalker")
    for side, own, other in (("dist", dist, akum), ("akum", akum, dist)):
        component = ns.SIDE_CONTAINER[side].lstrip("#")
        with pytest.raises(RuntimeError, match=f"Dash callback for {component} within"):
            ns.get_netflow(FakeStalkerPage(SELL_ROWS, callbacks=[other]), ["XL"], "Today",
                           side=side, strict=True)
        got = ns.get_netflow(FakeStalkerPage(SELL_ROWS, callbacks=[other, own]), ["XL"], "Today",
                             side=side, strict=True)
        assert sorted(got) == ["AAAA", "BBBB", "CCCC"], side


# ── response side: the matched callback must prove it supplied the side table
BAD_RESPONSE = "submit-button.n_clicks Dash callback response"

# The live response text, verbatim in shape (Label + DataTable per side).
LIVE_RESPONSE_TEXT = json.dumps({"multi": True, "response": {
    "broker-akum-stalker": {"children": [
        {"props": {"children": "Top Akumulasi"}, "type": "Label", "namespace": "dash_html_components"},
        {"props": {"id": "stalker-akum-table", "columns": [{"name": "Symbol", "id": "symbol"}],
                   "data": [{"symbol": "[ZZZZ](/some_route/ZZZZ)", "netval": 7000.0, "bval": 9000.0,
                             "sval": 2000.0, "bavg": 150.25, "savg": 148.0}]},
         "type": "DataTable", "namespace": "dash_table"}]},
    "broker-dist-stalker": {"children": [
        {"props": {"children": "Top Distribusi"}, "type": "Label", "namespace": "dash_html_components"},
        {"props": {"id": "stalker-dist-table", "columns": [{"name": "Symbol", "id": "symbol"}],
                   "data": [{"symbol": "[AAAA](/some_route/AAAA)", "netval": -5000.0, "bval": 1000.0,
                             "sval": 6000.0, "bavg": 99.0, "savg": 101.75}]},
         "type": "DataTable", "namespace": "dash_table"}]}}})


# ... and the parse_stalker_table rows it stands for: plain tickers, float text.
LIVE_RESPONSE_ROWS = {
    "akum": {"ZZZZ": {"symbol": "ZZZZ", "netval": "7000", "bval": "9000", "sval": "2000",
                      "bavg": "150.25", "savg": "148"}},
    "dist": {"AAAA": {"symbol": "AAAA", "netval": "-5000", "bval": "1000", "sval": "6000",
                      "bavg": "99", "savg": "101.75"}}}


def response_scan(body, side="dist", before=None, after="fresh"):
    """get_netflow(strict=True) for `side`, the live request answered by `body`:
    its rows, or the RuntimeError it raised."""
    page = FakeStalkerPage(SELL_ROWS, before=before, after=after, callbacks=[FakeDashResponse(body=body)])
    try:
        return ns.get_netflow(page, ["XL"], "Today", side=side, strict=True)
    except RuntimeError as e:
        return e


def assert_response_rejected(body, reason, side="dist"):
    got = response_scan(body, side)
    assert isinstance(got, RuntimeError), (body, got)
    assert f"{BAD_RESPONSE} {reason}" in str(got), str(got)
    if side == "dist":
        result = ns.scrape_broker_stalker(FakeStalkerPage(SELL_ROWS, callbacks=[FakeDashResponse(body=body)]))
        assert result.status == nsc.SOURCE_UNAVAILABLE and result.hits == []
        assert reason in result.detail and "AAAA" not in stalker_text(result)


@pytest.mark.parametrize("side", ["dist", "akum"])                     # F, G
def test_exact_live_response_shape_is_accepted_for_each_side(side):
    assert response_scan(LIVE_RESPONSE_TEXT, side) == LIVE_RESPONSE_ROWS[side]           # the response rows
    assert response_scan(live_body(), side) == by_symbol(SELL_ROWS)
    assert response_scan(live_body(**{side: live_side(side, data=())}), side) == {}   # an empty list is a list


def test_identical_table_and_data_across_runs_are_accepted():         # H
    identical = "<table><tr><td>the same rows on both runs</td></tr></table>"
    runs = [response_scan(LIVE_RESPONSE_TEXT, before=identical, after=identical) for _ in range(2)]
    assert runs[0] == runs[1] and sorted(runs[0]) == ["AAAA"]


@pytest.mark.parametrize("side, other", [("dist", "akum"), ("akum", "dist")])   # B
def test_response_that_updates_only_the_other_side_is_rejected(side, other):
    component = ns.SIDE_CONTAINER[side].lstrip("#")
    assert_response_rejected(live_body(**{side: DROP}), f"does not update {component}", side)
    # the other side's table, moved under this side's key, is still not this side's table
    assert_response_rejected(live_body(**{side: live_side(other)}),
                             f"{component}.children has no stalker-{side}-table DataTable", side)


@pytest.mark.parametrize("update, reason", [                             # C
    ({}, f"does not update {DIST}.children"),
    ({"data": [{"symbol": "AAAA"}]}, f"does not update {DIST}.children"),
    (None, f"does not update {DIST}"),
    ("children", f"does not update {DIST}"),
    ([live_side("dist")], f"does not update {DIST}"),
    ({"children": None}, f"{DIST}.children is not a list"),
    ({"children": dash_table("stalker-dist-table", [])}, f"{DIST}.children is not a list"),
    ({"children": "stalker-dist-table"}, f"{DIST}.children is not a list"),
])
def test_response_with_the_side_but_no_children_list_is_rejected(update, reason):
    assert_response_rejected(live_body(dist=update), reason)


NO_DIST_TABLE = f"{DIST}.children has no stalker-dist-table DataTable"


@pytest.mark.parametrize("children, reason", [                           # D
    ([], NO_DIST_TABLE),
    ([dash_label("dist stalker")], NO_DIST_TABLE),
    ([dash_label("x"), dash_table("stalker-akum-table", [])], NO_DIST_TABLE),     # wrong side's id
    ([dash_label("x"), dash_table("stalker-dist-table-2", [])], NO_DIST_TABLE),   # not the exact id
    ([dash_label("x"), dash_table(None, [])], NO_DIST_TABLE),
    ([dash_label("x"), dash_table("stalker-dist-table", [], type_="Div",
                                  namespace="dash_html_components")], NO_DIST_TABLE),  # not a DataTable
    ([dash_label("x"), dash_table("stalker-dist-table", [], namespace="other_lib")], NO_DIST_TABLE),
    ([dash_label("x"), {"props": {"children": [dash_table("stalker-dist-table", [])]},
                        "type": "Div", "namespace": "dash_html_components"}], NO_DIST_TABLE),  # nested
    ([dash_label("x"), {"type": "DataTable", "namespace": "dash_table", "props": None}], NO_DIST_TABLE),
    ([dash_label("x"), "stalker-dist-table"], NO_DIST_TABLE),
    ([dash_table("stalker-dist-table", []), dash_table("stalker-dist-table", [])],
     f"{DIST}.children has 2 stalker-dist-table DataTable"),                         # ambiguous
])
def test_children_without_the_side_datatable_are_rejected(children, reason):
    assert_response_rejected(live_body(dist={"children": children}), reason)


@pytest.mark.parametrize("data", [UNSET, None, {}, {"symbol": "AAAA"}, "[]", 0])   # E
def test_side_datatable_without_list_data_is_rejected(data):
    children = [dash_label("dist stalker"), dash_table("stalker-dist-table", data)]
    assert_response_rejected(live_body(dist={"children": children}),
                             "stalker-dist-table props.data is not a list")


@pytest.mark.parametrize("body, reason", [                               # I
    ({**live_body(), "multi": False}, 'is not a multi-output response ("multi" is not true)'),
    ({"response": live_body()["response"]}, 'is not a multi-output response ("multi" is not true)'),
    ({**live_body(), "multi": "true"}, 'is not a multi-output response ("multi" is not true)'),
    ({**live_body(), "multi": 1}, 'is not a multi-output response ("multi" is not true)'),
    ({"multi": True}, 'has no "response" object'),
    ({"multi": True, "response": None}, 'has no "response" object'),
    ({"multi": True, "response": [live_side("dist")]}, 'has no "response" object'),
    ({"multi": True, "response": {}}, f"does not update {DIST}"),
    ({DIST: live_side("dist")}, 'is not a multi-output response ("multi" is not true)'),   # single-output
    ([live_body()], "returned no JSON object"),
    ("null", "returned no JSON object"),
    ("", "returned no JSON object"),
    ('{"multi": true, "response": ', "returned no JSON object"),                    # truncated
    ("<html>login</html>", "returned no JSON object"),
])
def test_malformed_or_non_multi_response_is_rejected(body, reason):
    assert_response_rejected(body, reason)


PATCH = {"__dash_patch_update": "__dash_patch_update", "operations": []}


@pytest.mark.parametrize("body, reason", [                               # J
    ({**live_body(), "job": "a1b2"}, "is a background-callback response (job)"),
    ({**live_body(), "cacheKey": "k"}, "is a background-callback response (cacheKey)"),
    ({**live_body(), "sideUpdate": {}}, "is a background-callback response (sideUpdate)"),
    ({"multi": True, "response": {}, "job": "a1b2", "cacheKey": "k", "progress": None},
     "is a background-callback response (job, cacheKey)"),
    (live_body(dist={"children": PATCH}), "is a Patch (partial) update"),
    (live_body(dist={"children": [dash_label("x"), dash_table("stalker-dist-table", [PATCH])]}),
     "is a Patch (partial) update"),                                                # Patch inside the data
    (live_body(akum={"children": PATCH}), "is a Patch (partial) update"),           # Patch on either side
])
def test_background_or_patch_response_is_rejected(body, reason):
    assert_response_rejected(body, reason)


@pytest.mark.parametrize("before, after, callbacks", [
    (OLD_TABLE, OLD_TABLE, []),                                           # no callback at all
    (None, "fresh", [FakeDashResponse(status=500)]),                      # failed callback
    (None, "fresh", [FakeDashResponse(changed=("broker.value",))]),       # not submit-triggered
    (OLD_TABLE, OLD_TABLE, [UNRELATED]),                                  # not the side table
    (None, "fresh", [FakeDashResponse(outputs=[{"id": DIST, "property": "data"}])]),  # wrong property
    (None, "fresh", [FakeDashResponse(body=live_body(dist=DROP))]),       # response: other side only
    (None, "fresh", [FakeDashResponse(body={**live_body(), "job": "a1b2"})]),         # background
    (None, "fresh", [FakeDashResponse(body=live_body(dist={"children": PATCH}))]),    # Patch
])
def test_legacy_netflow_is_unchanged_by_the_submit_contract(before, after, callbacks):
    page = FakeStalkerPage(LEGACY_DOM_ROWS, before=before, after=after, callbacks=callbacks)
    got = ns.get_netflow(page, ["XL"], "Today", side="dist")
    assert sorted(got) == ["AAAA", "BBBB", "CCCC"]          # no callback/settle check, as before
    assert page.expect_calls == 0 and page.fingerprints == 0
    assert page.waits[page.submitted_at:] == [4000]         # the fixed post-submit wait


@pytest.mark.parametrize("fail", ["duration", "submit"])
def test_legacy_netflow_still_swallows_duration_and_submit_failures(fail):
    got = ns.get_netflow(FakeStalkerPage(LEGACY_DOM_ROWS, fail=fail), ["XL"], "Today", side="dist")
    assert sorted(got) == ["AAAA", "BBBB", "CCCC"]


def test_legacy_netflow_still_turns_a_parse_failure_into_an_empty_table():
    assert ns.get_netflow(FakeStalkerPage(LEGACY_DOM_ROWS, fail="parse"), ["XL"], "Today", side="dist") == {}
    assert ns.get_netflow(FakeStalkerPage(LEGACY_DOM_ROWS, fail="parse"), ["XL"], strict=False) == {}


@pytest.mark.parametrize("fail", ["duration", "submit"])
def test_strict_netflow_raises_on_every_scan_failure(fail):
    with pytest.raises(RuntimeError):
        ns.get_netflow(FakeStalkerPage(SELL_ROWS, fail=fail), ["XL"], "Today", side="dist", strict=True)


# ── broker_flow capture (dash_callback_v1) ─────
# The live contract of 2026-09-30: one submit answers both sides; the request
# carries duration/foreign-only in inputs and the broker list in state; each side
# is a dbc Label naming the session plus the whole DataTable in props.data.
FLOW_SESSION = "29 Sep 2026"
FLOW_TRACKED = {"TRKA", "TRKB", "TRKC"}
FLOW_FIELDS = ("netval", "bval", "sval", "bavg", "savg")


def flow_label(side, day=FLOW_SESSION, text=None, namespace="dash_bootstrap_components"):
    word = {"akum": "Buy", "dist": "Sell"}[side]
    text = f"Stalking Net {word} from {day} to {day}" if text is None else text
    return {"props": {"children": text}, "type": "Label", "namespace": namespace}


def flow_entry(symbol, **cells):
    """A live props.data entry: Markdown ticker link, JSON float cells."""
    return {"symbol": f"[{symbol}]({LIVE_ROUTE}{symbol})",
            **{"netval": 1.5, "bval": 2.0, "sval": 0.5, "bavg": 100.0, "savg": 99.0, **cells}}


def filler(prefix, n):
    return [flow_entry(f"{prefix}{i:03d}") for i in range(n)]


def flow_body(akum=None, dist=None, akum_label=UNSET, dist_label=UNSET, day=FLOW_SESSION):
    akum = filler("A", 3) + [flow_entry("TRKA")] if akum is None else akum
    dist = filler("D", 3) + [flow_entry("TRKB", netval=-1.5)] if dist is None else dist
    labels = {"akum": flow_label("akum", day) if akum_label is UNSET else akum_label,
              "dist": flow_label("dist", day) if dist_label is UNSET else dist_label}
    return {"multi": True, "response": {
        component: {"children": [c for c in (labels[side], dash_table(f"stalker-{side}-table", data))
                                 if c is not None]}
        for component, side, data in ((AKUM, "akum", akum), (DIST, "dist", dist))}}


def flow_request(code, duration="Today", foreign=(), broker=UNSET, outputs=LIVE_OUTPUTS,
                 changed=("submit-button.n_clicks",), drop=()):
    inputs = [{"id": "submit-button", "property": "n_clicks", "value": 1},
              {"id": "duration-picker", "property": "value", "value": duration},
              {"id": "foreign-only-checkbox", "property": "value", "value": list(foreign)}]
    state = [{"id": "broker", "property": "value", "value": [code] if broker is UNSET else broker}]
    return json.dumps({"output": LIVE_OUTPUT, "outputs": list(outputs), "changedPropIds": list(changed),
                       "inputs": [i for i in inputs if i["id"] not in drop],
                       "state": [s for s in state if s["id"] not in drop]})


def flow_response(code, body=None, **request):
    return FakeDashResponse(post_data=flow_request(code, **request), body=flow_body() if body is None else body)


class FlowPage(FakeStalkerPage):
    """The broker_stalker page for read_broker_flow_code: the submit emits
    `responses[typed code]` (a FakeDashResponse, or None for no callback).
    `rows` is only the DOM, which the broker_flow path must never read."""
    def __init__(self, responses, rows=LEGACY_DOM_ROWS, fail=None):
        super().__init__(rows, fail=fail, callbacks=[])
        self.responses, self.code, self.submits = responses, None, 0

    def type(self, text):
        self.code = text

    def click(self, sel, timeout=None):
        if sel == "#submit-button" and self.fail != "submit":
            self.submits += 1
            response = self.responses.get(self.code)
            self.callbacks = [] if response is None else [response]
        super().click(sel, timeout)


def flow_clock(monkeypatch, *when):
    import datetime as dt

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return myt(*when)
    monkeypatch.setattr(ns, "datetime", Clock)


def capture(monkeypatch, responses, codes=("XL",), when=(2026, 9, 30, 7, 5), page=None):
    flow_clock(monkeypatch, *when)
    page = FlowPage(responses) if page is None else page
    return ns.capture_broker_flow(page, FLOW_TRACKED, codes=list(codes)), page


def flow_db(live=(), backfill=()):
    """In-memory db with broker_flow rows: live (date, ticker, code, netval) get
    full columns, backfill ones only netval (bval NULL), as the two writers do."""
    conn = sqlite3.connect(":memory:")
    conn.execute(ns.BROKER_FLOW_DDL)
    conn.executemany("INSERT INTO broker_flow VALUES (?,?,?,?,?,?,?,?)",
                     [(d, t, c, 1.0, 1.0, n, 1.0, 1.0) for d, t, c, n in live] +
                     [(d, t, c, None, None, n, None, None) for d, t, c, n in backfill])
    conn.commit()
    return conn


def flow_rows(conn):
    return sorted(conn.execute("SELECT * FROM broker_flow"), key=lambda r: tuple(str(x) for x in r))


def scans(conn):
    return {(r[0], r[1]): r[2:] for r in conn.execute(
        "SELECT broker_code, run_started_utc, status, snapshot, session_date, akum_rows_returned, "
        "dist_rows_returned, tracked_rows, method, detail FROM broker_flow_scan")}


def test_broker_flow_captures_tracked_rows_beyond_the_rendered_page(monkeypatch):
    akum = filler("A", 20) + [flow_entry("TRKA", netval=0.25)]        # row 21: never on DOM page 1
    got, page = capture(monkeypatch, {"XL": flow_response("XL", flow_body(akum=akum))})
    assert got["reason"] is None and page.dom_reads == 0
    assert {(r["ticker"], r["netval"]) for r in got["rows"]} == {("TRKA", 0.25), ("TRKB", -1.5)}
    assert got["scans"][0]["akum_rows_returned"] == 21 and got["scans"][0]["tracked_rows"] == 2


def test_broker_flow_uses_one_submit_per_code_for_both_sides(monkeypatch):
    codes = ("XL", "AK", "IF")
    got, page = capture(monkeypatch, {c: flow_response(c) for c in codes}, codes=codes)
    assert page.submits == 3 and page.expect_calls == 3 and page.dom_reads == 0
    assert sorted((r["broker_code"], r["ticker"]) for r in got["rows"]) == [
        (c, t) for c in sorted(codes) for t in ("TRKA", "TRKB")]


@pytest.mark.parametrize("request_kw", [
    {"broker": ["AK"]},                                   # another broker
    {"broker": ["XL", "AK"]},                             # more than one broker
    {"broker": "XL"},                                     # not the live list form
    {"broker": []},                                       # no broker
    {"duration": "1 Week"},                               # wrong duration
    {"foreign": ["foreign"]},                             # foreign-only checked
    {"drop": ("duration-picker",)},                       # missing input
    {"drop": ("foreign-only-checkbox",)},
    {"drop": ("broker",)},                                # missing state
    {"outputs": [{"id": AKUM, "property": "children"}]},  # only one side
    {"outputs": [{"id": AKUM, "property": "children"}, {"id": DIST, "property": "data"}]},
    {"changed": ("broker.value",)},                       # not the submit
])
def test_broker_flow_rejects_a_callback_that_does_not_prove_its_request(request_kw):
    page = FlowPage({"XL": flow_response("XL", **request_kw)})
    with pytest.raises(RuntimeError):
        ns.read_broker_flow_code(page, "XL", timeout_ms=10)


def test_broker_flow_request_proof_rejects_malformed_request_lists():
    good = json.loads(flow_request("XL"))
    ns._broker_flow_request_proof(good, "XL")
    for bad in ({**good, "state": {"broker": ["XL"]}}, {**good, "inputs": None},
                {**good, "state": good["state"] * 2}, {**good, "inputs": good["inputs"] + ["x"]},
                {**good, "state": [{"id": "broker", "property": "value"}]}, []):
        with pytest.raises(ValueError):
            ns._broker_flow_request_proof(bad, "XL")


@pytest.mark.parametrize("side, text, day", [
    ("akum", "Stalking Net Buy from 29 Sep 2026 to 29 Sep 2026", (2026, 9, 29)),
    ("dist", "Stalking Net Sell from 29 Sep 2026 to 29 Sep 2026", (2026, 9, 29)),
    ("akum", "Stalking Net Buy from 2 Jan 2027 to 2 Jan 2027", (2027, 1, 2)),
])
def test_flow_label_accepts_the_live_shapes(side, text, day):
    import datetime as dt
    assert ns._flow_label_session(text, side) == dt.date(*day)


@pytest.mark.parametrize("side, text", [
    ("akum", "Stalking Net Sell from 29 Sep 2026 to 29 Sep 2026"),    # wrong side word
    ("dist", "Stalking Net Buy from 29 Sep 2026 to 29 Sep 2026"),
    ("akum", "Stalking Net Buy from 26 Sep 2026 to 29 Sep 2026"),     # from != to
    ("akum", "Stalking Net Buy from 31 Feb 2026 to 31 Feb 2026"),     # not a date
    ("akum", "Stalking Net Buy from 29 Agu 2026 to 29 Agu 2026"),     # not an English month
    ("akum", "Stalking Net Buy from 29 sep 2026 to 29 sep 2026"),
    ("akum", "Stalking Net Buy from 2026-09-29 to 2026-09-29"),
    ("akum", "Stalking Net Buy 29 Sep 2026"),
    ("akum", " Stalking Net Buy from 29 Sep 2026 to 29 Sep 2026"),
    ("akum", ""), ("akum", None), ("akum", ["Stalking Net Buy from 29 Sep 2026 to 29 Sep 2026"]),
])
def test_flow_label_rejects_everything_else(side, text):
    with pytest.raises(ValueError):
        ns._flow_label_session(text, side)


@pytest.mark.parametrize("body", [
    flow_body(dist_label=flow_label("dist", "28 Sep 2026")),                   # akum date != dist date
    flow_body(akum_label=None),                                                # no Label
    flow_body(akum_label=flow_label("akum", namespace="dash_html_components")),  # not the dbc Label
    flow_body(dist_label=flow_label("dist", text="Stalking Net Sell today")),  # malformed
])
def test_broker_flow_code_fails_without_one_session_date_for_both_sides(body):
    with pytest.raises(RuntimeError):
        ns.read_broker_flow_code(FlowPage({"XL": flow_response("XL", body)}), "XL", timeout_ms=10)


def test_broker_flow_code_fails_on_a_label_pair_that_repeats():
    body = flow_body()
    body["response"][AKUM]["children"].insert(0, flow_label("akum"))
    with pytest.raises(RuntimeError, match="2 Label"):
        ns.read_broker_flow_code(FlowPage({"XL": flow_response("XL", body)}), "XL", timeout_ms=10)


def test_broker_flow_code_returns_the_label_session():
    import datetime as dt
    got = ns.read_broker_flow_code(FlowPage({"XL": flow_response("XL")}), "XL", timeout_ms=10)
    assert got["session_date"] == dt.date(2026, 9, 29) and len(got["akum"]) == len(got["dist"]) == 4


@pytest.mark.parametrize("fail", ["duration", "submit"])
def test_broker_flow_code_fails_when_the_ui_step_fails(fail):
    with pytest.raises(RuntimeError):
        ns.read_broker_flow_code(FlowPage({"XL": flow_response("XL")}, fail=fail), "XL", timeout_ms=10)


def test_broker_flow_code_fails_on_a_failed_callback():
    for response in (None, FakeDashResponse(status=500, post_data=flow_request("XL"), body=flow_body()),
                     FakeDashResponse(post_data=flow_request("XL"), body={**flow_body(), "job": "a1"})):
        with pytest.raises(RuntimeError):
            ns.read_broker_flow_code(FlowPage({"XL": response}), "XL", timeout_ms=10)


def run_sessions(monkeypatch, sessions, when):
    responses = {c: flow_response(c, flow_body(day=d)) for c, d in sessions.items()}
    got, _ = capture(monkeypatch, responses, codes=tuple(sessions), when=when)
    conn = flow_db(live=[(got["scrape_date"], "OLD1", "XL", 9.0)])
    return got, ns.persist_broker_flow_capture(conn, got), flow_rows(conn)


def test_broker_flow_persists_when_every_code_serves_the_same_prior_session(monkeypatch):
    got, persisted, rows = run_sessions(monkeypatch, {"XL": FLOW_SESSION, "AK": FLOW_SESSION}, (2026, 9, 30, 7, 5))
    assert got["reason"] is None and persisted
    assert {(r[0], r[1], r[2]) for r in rows} == {("2026-09-30", t, c) for c in ("XL", "AK") for t in ("TRKA", "TRKB")}


def test_broker_flow_allows_a_weekend_copy_of_the_last_session(monkeypatch):
    got, persisted, _ = run_sessions(monkeypatch, {"XL": "25 Sep 2026"}, (2026, 9, 27, 7, 5))   # Sun <- Fri
    assert got["scrape_date"] == "2026-09-27" and persisted


@pytest.mark.parametrize("sessions, when, reason", [
    ({"XL": FLOW_SESSION, "AK": "28 Sep 2026"}, (2026, 9, 30, 7, 5), "different source sessions"),
    ({"XL": "30 Sep 2026", "AK": "30 Sep 2026"}, (2026, 9, 30, 18, 0), "is not before scrape date"),
    ({"XL": "1 Oct 2026"}, (2026, 9, 30, 7, 5), "is not before scrape date"),
])
def test_broker_flow_writes_nothing_without_one_prior_session(monkeypatch, sessions, when, reason):
    got, persisted, rows = run_sessions(monkeypatch, sessions, when)
    assert reason in got["reason"] and not persisted
    assert rows == [(got["scrape_date"], "OLD1", "XL", 1.0, 1.0, 9.0, 1.0, 1.0)]


BAD_CELLS = [None, "", "   ", "-", "N/A", "abc", "1.2.3", float("nan"), float("inf"), float("-inf"), True, [1]]


@pytest.mark.parametrize("field", FLOW_FIELDS)
@pytest.mark.parametrize("bad", BAD_CELLS, ids=repr)
@pytest.mark.parametrize("ticker", ["TRKA", "A001"])                   # tracked or not: the whole table
def test_broker_flow_never_turns_a_missing_or_invalid_cell_into_zero(monkeypatch, field, bad, ticker):
    akum = [flow_entry("A001"), flow_entry("TRKA")]
    akum[1 if ticker == "TRKA" else 0][field] = bad
    got, _ = capture(monkeypatch, {"XL": flow_response("XL", flow_body(akum=akum))})
    assert got["rows"] == [] and got["scans"][0]["status"] == ns.FLOW_SOURCE_FAILURE
    assert f"row {0 if ticker == 'A001' else 1} {field}" in got["scans"][0]["detail"]
    assert got["reason"] and "1/1 code(s) failed: XL" in got["reason"]


@pytest.mark.parametrize("field", FLOW_FIELDS)
def test_broker_flow_fails_a_row_missing_a_column(monkeypatch, field):
    entry = flow_entry("TRKA")
    del entry[field]
    got, _ = capture(monkeypatch, {"XL": flow_response("XL", flow_body(akum=[entry]))})
    assert got["rows"] == [] and f"lacks {field}" in got["scans"][0]["detail"]


@pytest.mark.parametrize("field", FLOW_FIELDS)
@pytest.mark.parametrize("zero", [0.0, 0, "0", "0.0"])
def test_broker_flow_keeps_an_explicit_source_zero(monkeypatch, field, zero):
    got, _ = capture(monkeypatch, {"XL": flow_response("XL", flow_body(akum=[flow_entry("TRKA", **{field: zero})]))})
    row = next(r for r in got["rows"] if r["ticker"] == "TRKA")
    assert got["reason"] is None and row[field] == 0.0 and isinstance(row[field], float)


@pytest.mark.parametrize("field", FLOW_FIELDS)
@pytest.mark.parametrize("text, value", [("1,234.5", 1234.5), ("(1,234.5)", -1234.5), ("-12,000", -12000.0)])
def test_broker_flow_reads_comma_grouped_cells_in_every_column(monkeypatch, field, text, value):
    got, _ = capture(monkeypatch, {"XL": flow_response("XL", flow_body(akum=[flow_entry("TRKA", **{field: text})]))})
    row = next(r for r in got["rows"] if r["ticker"] == "TRKA")
    assert row[field] == value
    assert all(row[f] == flow_entry("TRKA")[f] for f in FLOW_FIELDS if f != field)


@pytest.mark.parametrize("akum, dist", [
    ([flow_entry("TRKA")], [flow_entry("TRKA", netval=-1.0)]),     # tracked, differing
    ([flow_entry("A001")], [flow_entry("A001")]),                   # untracked, identical
    ([flow_entry("A001", netval=0.0)], [flow_entry("A001", netval=0.0)]),
])
def test_broker_flow_fails_a_code_whose_sides_share_a_ticker(monkeypatch, akum, dist):
    got, _ = capture(monkeypatch, {"XL": flow_response("XL", flow_body(akum=akum, dist=dist))})
    assert got["rows"] == [] and "in both the akum and the dist table" in got["scans"][0]["detail"]


def test_broker_flow_fails_a_side_that_repeats_a_ticker(monkeypatch):
    got, _ = capture(monkeypatch, {"XL": flow_response("XL", flow_body(akum=[flow_entry("A001")] * 2))})
    assert got["rows"] == [] and "akum table repeats a ticker" in got["scans"][0]["detail"]


def test_one_failed_code_leaves_broker_flow_untouched(monkeypatch):
    codes = ("XL", "AK", "IF")
    got, _ = capture(monkeypatch, {"XL": flow_response("XL"), "AK": flow_response("AK")}, codes=codes)
    conn = flow_db(live=[("2026-09-30", "OLD1", "XL", 9.0), ("2026-09-29", "TRKA", "XL", 3.0)],
                   backfill=[("2026-07-01", "TRKA", "XL", 4.0)])
    before = flow_rows(conn)
    assert not ns.persist_broker_flow_capture(conn, got)
    assert flow_rows(conn) == before
    status = {code: v for (code, _run), v in scans(conn).items()}
    assert status["IF"][:2] == (ns.FLOW_SOURCE_FAILURE, ns.SNAPSHOT_REJECTED) and "no submit-button" in status["IF"][-1]
    for code in ("XL", "AK"):
        assert status[code][:3] == (ns.FLOW_OK, ns.SNAPSHOT_REJECTED, "2026-09-29")
        assert status[code][-1] == "snapshot rejected: 1/3 code(s) failed: IF"


def test_successful_rerun_replaces_the_date_snapshot_completely(monkeypatch):
    conn = flow_db(live=[("2026-09-30", "OLD1", "XL", 9.0), ("2026-09-30", "TRKA", "XL", 9.0),
                         ("2026-09-29", "OLD1", "XL", 7.0)],
                   backfill=[("2026-09-30", "TRKC", "AK", 5.0), ("2026-07-01", "TRKA", "XL", 4.0)])
    got, _ = capture(monkeypatch, {"XL": flow_response("XL")})
    assert ns.persist_broker_flow_capture(conn, got)
    assert flow_rows(conn) == sorted([
        ("2026-07-01", "TRKA", "XL", None, None, 4.0, None, None),     # backfill kept
        ("2026-09-29", "OLD1", "XL", 1.0, 1.0, 7.0, 1.0, 1.0),         # other date kept
        ("2026-09-30", "TRKA", "XL", 2.0, 0.5, 1.5, 100.0, 99.0),      # new snapshot only
        ("2026-09-30", "TRKB", "XL", 2.0, 0.5, -1.5, 100.0, 99.0),
        ("2026-09-30", "TRKC", "AK", None, None, 5.0, None, None),     # backfill-shaped row kept
    ], key=lambda r: tuple(str(x) for x in r))


def test_a_failed_snapshot_write_rolls_back_and_is_recorded(monkeypatch):
    # A backfill row on the same key makes the INSERT fail after the DELETE ran.
    conn = flow_db(live=[("2026-09-30", "OLD1", "XL", 9.0)], backfill=[("2026-09-30", "TRKA", "XL", 5.0)])
    before = flow_rows(conn)
    got, _ = capture(monkeypatch, {"XL": flow_response("XL")})
    assert not ns.persist_broker_flow_capture(conn, got)
    assert flow_rows(conn) == before
    (status,) = scans(conn).values()
    assert status[:2] == (ns.FLOW_OK, ns.SNAPSHOT_REJECTED) and "broker_flow write failed" in status[-1]


def test_scan_status_records_provenance_and_keeps_the_persisted_run(monkeypatch):
    conn = flow_db()
    first, _ = capture(monkeypatch, {"XL": flow_response("XL")}, when=(2026, 9, 30, 7, 5))
    assert ns.persist_broker_flow_capture(conn, first)
    ((key, row),) = scans(conn).items()
    assert key == ("XL", "2026-09-29T23:05:00+00:00")
    assert row == (ns.FLOW_OK, ns.SNAPSHOT_PERSISTED, "2026-09-29", 4, 4, 2, "dash_callback_v1", "")

    failed, _ = capture(monkeypatch, {}, when=(2026, 9, 30, 8, 0))              # rerun: no callback
    assert not ns.persist_broker_flow_capture(conn, failed)
    got = scans(conn)
    assert got[key][1] == ns.SNAPSHOT_PERSISTED                                  # still the live snapshot
    assert got[("XL", "2026-09-30T00:00:00+00:00")][:2] == (ns.FLOW_SOURCE_FAILURE, ns.SNAPSHOT_REJECTED)

    again, _ = capture(monkeypatch, {"XL": flow_response("XL")}, when=(2026, 9, 30, 9, 0))
    assert ns.persist_broker_flow_capture(conn, again)
    snapshots = {k[1]: v[1] for k, v in scans(conn).items()}
    assert snapshots == {"2026-09-29T23:05:00+00:00": ns.SNAPSHOT_SUPERSEDED,
                         "2026-09-30T00:00:00+00:00": ns.SNAPSHOT_REJECTED,
                         "2026-09-30T01:00:00+00:00": ns.SNAPSHOT_PERSISTED}


def test_save_daily_broker_flow_persists_one_complete_snapshot(monkeypatch, tmpdir_path):
    monkeypatch.setattr(ns, "DB_PATH", os.path.join(tmpdir_path, "neobdm.db"))
    monkeypatch.setattr(ns, "BROKER_FLOW_CODES", ["XL", "AK"])
    monkeypatch.setattr(ns, "TRACKED_TICKERS", sorted(FLOW_TRACKED))
    flow_clock(monkeypatch, 2026, 9, 30, 7, 5)
    page = FlowPage({"XL": flow_response("XL"), "AK": flow_response("AK")})
    summary = ns.save_daily_broker_flow(page)
    conn = sqlite3.connect(ns.DB_PATH)
    assert {r[:3] for r in flow_rows(conn)} == {("2026-09-30", t, c) for c in ("XL", "AK") for t in ("TRKA", "TRKB")}
    assert {v[1] for v in scans(conn).values()} == {ns.SNAPSHOT_PERSISTED}
    conn.close()
    assert "Backfill" in summary and page.dom_reads == 0 and page.submits == 2


def test_save_daily_broker_flow_writes_nothing_on_a_partial_capture(monkeypatch, tmpdir_path):
    monkeypatch.setattr(ns, "DB_PATH", os.path.join(tmpdir_path, "neobdm.db"))
    monkeypatch.setattr(ns, "BROKER_FLOW_CODES", ["XL", "AK"])
    flow_clock(monkeypatch, 2026, 9, 30, 7, 5)
    ns.save_daily_broker_flow(FlowPage({"XL": flow_response("XL")}))
    conn = sqlite3.connect(ns.DB_PATH)
    assert flow_rows(conn) == [] and {v[1] for v in scans(conn).values()} == {ns.SNAPSHOT_REJECTED}
    conn.close()


def test_record_konglo_signals_keeps_the_broker_stalker_status_and_reason():
    conn = sqlite3.connect(":memory:")
    ms = nsc.SignalResult("top_akum_bandar", nsc.RETIRED_SOURCE)
    cases = {
        "2026-09-21": nsc.SignalResult("broker_stalker", nsc.SOURCE_UNAVAILABLE, [],
                                       "retail net sell scan failed: RuntimeError: submit failed: x"),
        "2026-09-22": nsc.SignalResult("broker_stalker", nsc.EMPTY_UNVERIFIED, [],
                                       "3 row(s) parsed, none with a negative netval"),
        "2026-09-23": nsc.SignalResult("broker_stalker", nsc.HITS,
                                       [{"symbol": "AAAA", "netval": "-5", "holders": []}], ""),
    }
    for day, bs in cases.items():
        ns.record_konglo_signals(conn, day, ms, [], bs)
    got = {d: (st, det, h) for d, st, det, h in conn.execute(
        "SELECT flag_date, status, detail, hits FROM signal_source_status WHERE source='broker_stalker'")}
    assert got == {d: (bs.status, bs.detail, len(bs.hits)) for d, bs in cases.items()}
    assert conn.execute("SELECT flag_date, ticker, sources FROM konglo_signal_watch").fetchall() == [
        ("2026-09-23", "AAAA", "broker_stalker")]


def test_market_summary_empty_unverified_is_explicit_and_never_a_zero_or_no_data_claim():
    for detail in ("screener returned no rows", ""):
        text = "\n".join(ns._market_summary_lines(nsc.SignalResult("top_akum_bandar", nsc.EMPTY_UNVERIFIED,
                                                                   [], detail)))
        assert "Empty — unverified" in text
        assert (detail or "no rows returned") in text
        assert "No data scraped today" not in text
        assert "No unusual-volume candidates" not in text


def test_dashboard_statuses_render_explicitly_and_never_as_a_bare_dash():
    dash = [("Bandarmologi", "b", nsc.SignalResult("dashboard_Bandarmologi", nsc.HITS,
                                                   [{"tick": "ABCD", "tx": "1.0%"}, {"tick": "EFGH", "tx": "n/a"}])),
            ("NonRetail", "n", nsc.SignalResult("dashboard_NonRetail", nsc.EMPTY_UNVERIFIED, [], "")),
            ("Foreign", "f", nsc.SignalResult("dashboard_Foreign", nsc.SOURCE_UNAVAILABLE, [],
                                              "request failed: RuntimeError: HTTP 429"))]
    lines = ns._dashboard_lines(dash)
    assert lines[1:] == ["b Bandarmologi: ABCD(1.0%) EFGH(n/a)",
                         "n NonRetail: ⚠️ empty — unverified",
                         "f Foreign: ⚠️ unavailable: request failed: RuntimeError: HTTP 429"]
    assert not any(line.rstrip().endswith(": -") for line in lines)
    assert ns._dashboard_lines([("Foreign", "f", [])])[1] == "f Foreign: ⚠️ empty — unverified"


def myt(*args):
    import datetime as dt
    import pytz
    return pytz.timezone(ns.TIMEZONE).localize(dt.datetime(*args))


PRE_OPEN_BASIS = "📅 Basis: latest completed session served by NeoBDM (pre-open capture)"
WEEKDAY_OR_DATE = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b|\d")


def report(started, completed, ms=None, bs=None):
    ms = ms or nsc.SignalResult("top_akum_bandar", nsc.NO_HITS, [])
    dash = [("Bandarmologi", "b", nsc.SignalResult("dashboard_Bandarmologi", nsc.HITS, [{"tick": "ABCD", "tx": "1.0%"}])),
            ("NonRetail", "n", nsc.SignalResult("dashboard_NonRetail", nsc.EMPTY_UNVERIFIED, [], "")),
            ("Foreign", "f", nsc.SignalResult("dashboard_Foreign", nsc.SOURCE_UNAVAILABLE, [], "request failed: x"))]
    bs = bs or nsc.SignalResult("broker_stalker", nsc.EMPTY_UNVERIFIED, [], "dist table parsed with no rows")
    return ns.format_combined_message(ms, dash, bs, started, completed)


def test_pre_open_report_states_the_scrape_span_and_the_latest_completed_session_basis():
    text = report(myt(2026, 9, 29, 7, 2), myt(2026, 9, 29, 7, 14))      # Tuesday, all pre-open
    lines = text.splitlines()
    assert lines[0] == "📈 NeoBDM Daily Signal"
    assert lines[1] == "🕗 Scraped 29 Sep 2026, 07:02 AM – 07:14 AM MYT"
    assert lines[2] == PRE_OPEN_BASIS
    assert "(Daily)" in text and "(EOD)" in text
    assert "UNVERIFIED" not in text


def test_the_basis_never_names_a_session_weekday_or_date():
    # The previous weekday can be an IDX holiday; nothing here proves which session
    # NeoBDM served, so the basis line names none (Monday after a Friday holiday).
    for started, completed in ((myt(2026, 9, 29, 7, 2), myt(2026, 9, 29, 7, 14)),
                               (myt(2026, 12, 28, 7, 0), myt(2026, 12, 28, 7, 9)),
                               (myt(2026, 9, 29, 14, 15), myt(2026, 9, 29, 14, 30))):
        basis = report(started, completed).splitlines()[2]
        assert not WEEKDAY_OR_DATE.search(basis.replace(f"{ns.IDX_OPEN_HOUR_LOCAL:02d}:00", "")), basis
        assert "session close" not in basis
    assert not hasattr(ns, "_session_close_label")


@pytest.mark.parametrize("started, completed", [
    ((2026, 9, 29, 9, 59), (2026, 9, 29, 10, 1)),        # crosses IDX open mid-run
    ((2026, 9, 29, 10, 30), (2026, 9, 29, 10, 45)),      # starts after open
    ((2026, 9, 29, 14, 15), (2026, 9, 29, 14, 30)),      # afternoon rerun
    ((2026, 9, 28, 23, 50), (2026, 9, 29, 0, 20)),       # late-night run, crosses midnight
    ((2026, 9, 28, 9, 30), (2026, 9, 29, 7, 0)),         # both pre-open, different days
])
def test_a_scrape_not_wholly_pre_open_is_session_unverified_with_no_daily_or_eod_claim(started, completed):
    assert not ns.report_is_pre_open(myt(*started), myt(*completed))
    for ms in (nsc.SignalResult("top_akum_bandar", nsc.NO_HITS, []),
               nsc.SignalResult("top_akum_bandar", nsc.HITS,
                                [{"symbol": "BBBB", "dn-0": -0.1, "price": 1, "_caution": True}]),
               nsc.SignalResult("top_akum_bandar", nsc.RETIRED_SOURCE, [], "x")):
        text = report(myt(*started), myt(*completed), ms=ms)
        assert text.splitlines()[0] == "📈 NeoBDM Signal — Session Unverified"
        assert "after the safe morning window" in text and "after IDX open" in text
        assert "session UNVERIFIED" in text
        assert "daily" not in text.lower() and "eod" not in text.lower(), text
        for claim in ("Basis:", "pre-open capture", "candidates today", "hari ini"):
            assert claim not in text, claim


def test_a_wholly_pre_open_scrape_is_verified():
    assert ns.report_is_pre_open(myt(2026, 9, 29, 7, 0), myt(2026, 9, 29, 7, 20))
    assert ns.report_is_pre_open(myt(2026, 9, 29, 9, 0), myt(2026, 9, 29, 9, 59))


def run_jobs(monkeypatch, times):
    """run_all_jobs with every source stubbed. `times` maps the step after which
    the clock moves to that MYT time: "start" (the initial read), "stalker"
    (Broker Stalker done) and "broker_flow" (persistence done)."""
    from unittest import mock
    import datetime as dt
    clock = {"now": myt(*times["start"])}

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    def step(name, result):
        def run(*a, **k):
            if name in times:
                clock["now"] = myt(*times[name])
            return result
        return run
    sent = []
    monkeypatch.setattr(ns, "datetime", Clock)
    monkeypatch.setattr(ns, "sync_playwright", mock.MagicMock())
    for name, stub in {"login": lambda page: None,
                       "scrape_market_summary": step("market", nsc.SignalResult("top_akum_bandar", nsc.NO_HITS, [])),
                       "scrape_dashboard_presets": step("dashboard", []),
                       "scrape_broker_stalker": step("stalker", nsc.SignalResult(
                           "broker_stalker", nsc.SOURCE_UNAVAILABLE, [], "retail net sell scan failed: x")),
                       "save_daily_broker_flow": step("broker_flow", None), "_offset_safe": lambda what: False,
                       "send_telegram": sent.append}.items():
        monkeypatch.setattr(ns, name, stub)
    ns.run_all_jobs()
    assert len(sent) == 1
    return sent[0]


def test_run_that_starts_and_finishes_its_report_sources_pre_open_is_verified(monkeypatch):
    # broker_flow persistence finishing after open does not move the report basis.
    text = run_jobs(monkeypatch, {"start": (2026, 9, 29, 7, 2), "stalker": (2026, 9, 29, 7, 14),
                                  "broker_flow": (2026, 9, 29, 10, 40)})
    assert "🕗 Scraped 29 Sep 2026, 07:02 AM – 07:14 AM MYT" in text
    assert PRE_OPEN_BASIS in text and "10:40" not in text
    assert "Source unavailable: retail net sell scan failed: x" in text
    assert TOLD_NO_SELL not in text


def test_run_crossing_idx_open_is_unverified(monkeypatch):
    text = run_jobs(monkeypatch, {"start": (2026, 9, 29, 9, 59), "market": (2026, 9, 29, 10, 0),
                                  "stalker": (2026, 9, 29, 10, 1)})
    assert "🕗 Scraped 29 Sep 2026, 09:59 AM – 10:01 AM MYT" in text
    assert "session UNVERIFIED" in text and PRE_OPEN_BASIS not in text
    assert "daily" not in text.lower() and "eod" not in text.lower()


def test_run_starting_after_idx_open_is_unverified(monkeypatch):
    text = run_jobs(monkeypatch, {"start": (2026, 9, 29, 10, 5), "stalker": (2026, 9, 29, 10, 20)})
    assert "session UNVERIFIED" in text and PRE_OPEN_BASIS not in text


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
