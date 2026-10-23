"""Offline counterexamples; no source login, messages, or paid acquisition."""
from datetime import date,timedelta
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs

import broker_subset_capture as capture
from ticker_registry import registry_from_tickers

CODES=["AK","AZ","BK","CC","MG","SQ","TP","XL","YP","ZP"]
EXPECTED="2026-10-21"

def payload(ticker,codes=CODES,base=1000):
    days=[date.fromisoformat(EXPECTED)-timedelta(days=n) for n in range(365)]
    days=sorted(d.isoformat() for d in days if d.weekday()<5)
    result={f:{c:[0]*len(days) for c in codes} for f in capture.bc.cg.SERIES_FIELDS}
    result["date"]=days
    result["ohlc"]=[dict(date=d,open=base,high=base+1,low=base-1,close=base,volume=1000)
                    for d in days]
    return dict(success=True,meta=dict(symbol=ticker),data=result)

class Response:
    status=200
    def __init__(self,data):
        self.raw=json.dumps(data).encode()
    def text(self):
        return self.raw.decode()
    def body(self):
        return self.raw

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.subset=dict(name="test-core10",codes=CODES,sha256=capture.digest(capture.encoded(CODES)),
                         coverage_scope="OBSERVED_BROKER_SUBSET",full_broker_universe=False,
                         client_classification="UNKNOWN")
        self.registry=registry_from_tickers(["BMRI","BRMS"],source="synthetic")
        self.queries=[]
    def tearDown(self):
        self.temp.cleanup()
    def get(self,query):
        q=parse_qs(query)
        self.queries.append(q)
        self.assertLessEqual(len(q["brokers"]),10)
        self.assertEqual(q["start_date"],["2025-10-21"])
        self.assertEqual(q["end_date"],[EXPECTED])
        t=q["symbol"][0]
        return Response(payload(t,base=1000 if t=="BMRI" else 2000))
    def run_capture(self,**kwargs):
        return capture.capture(self.root,self.registry,self.subset,EXPECTED,self.get,
                               sleep=lambda _:None,**kwargs)
    def test_all_members_exact_subset_rawbytes_immutable_and_no_full101_claim(self):
        r=self.run_capture()
        self.assertEqual(r["counts"],{"COMPLETE":2})
        self.assertEqual(r["acquisition_status"],"COMPLETE")
        self.assertFalse(r["full_broker_universe"])
        self.assertEqual(r["source_certification"],"OFFLINE_TEST_ONLY")
        self.assertEqual(r["persistence"]["status"],"LOCAL_ONLY")
        for ticker,item in r["per_ticker"].items():
            original=Response(payload(ticker,base=1000 if ticker=="BMRI" else 2000)).raw
            stored=self.root/item["artifacts"][0]["path"]
            self.assertEqual(gzip.decompress(stored.read_bytes()),original)
            self.assertEqual(item["artifacts"][0]["source_sha256"],hashlib.sha256(original).hexdigest())
            self.assertNotEqual(item["history_certification"],"COMPLETE")
    def test_budget_and_persistent_missing_keep_full_registry(self):
        first=self.run_capture(max_requests=1)
        self.assertEqual(first["counts"],{"COMPLETE":1,"MISSING":1})
        self.assertEqual(first["acquisition_status"],"PARTIAL")
        self.assertEqual(len(first["per_ticker"]),2)
        second=self.run_capture(max_requests=1)
        self.assertEqual(second["counts"],{"COMPLETE":2})
        self.assertEqual(len(self.queries),2)
        self.assertGreaterEqual(len(list(self.root.glob("runs/*/receipt_versions/*.json"))),3)
    def test_tampered_successful_object_cannot_be_resumed_as_complete(self):
        r=self.run_capture(max_requests=1)
        artifact=self.root/r["per_ticker"]["BMRI"]["artifacts"][0]["path"]
        artifact.write_bytes(b"tampered")
        again=self.run_capture(max_requests=1)
        self.assertEqual(again["per_ticker"]["BMRI"]["state"],"FAILED")
        self.assertEqual(again["acquisition_status"],"PARTIAL")
    def test_current_global_latest_cannot_hide_one_stale_ticker(self):
        def stale(query):
            q=parse_qs(query);t=q["symbol"][0]
            env=payload(t,base=1000 if t=="BMRI" else 2000)
            if t=="BRMS":
                env["data"]["date"][-1]="2026-10-20"
                env["data"]["ohlc"][-1]["date"]="2026-10-20"
            return Response(env)
        r=capture.capture(self.root,self.registry,self.subset,EXPECTED,stale,sleep=lambda _:None)
        self.assertEqual(r["per_ticker"]["BRMS"]["state"],"FAILED")
        self.assertEqual(r["acquisition_status"],"PARTIAL")

    def test_valid_stale_history_is_preserved_and_remains_incomplete(self):
        def stale(query):
            t=parse_qs(query)["symbol"][0]
            env=payload(t,base=1000 if t=="BMRI" else 2000)
            if t=="BRMS":
                env["data"]["date"].pop()
                env["data"]["ohlc"].pop()
                for field in capture.bc.cg.SERIES_FIELDS:
                    for series in env["data"][field].values(): series.pop()
            return Response(env)
        r=capture.capture(self.root,self.registry,self.subset,EXPECTED,stale,sleep=lambda _:None)
        item=r["per_ticker"]["BRMS"]
        self.assertEqual(item["state"],"SOURCE_STALE")
        self.assertEqual(item["last_session"],"2026-10-20")
        self.assertFalse(item["requested_session_available"])
        self.assertTrue(item["successful_source_answer"])
        self.assertEqual(r["source_response_counts"]["validated_nonempty"],2)
        self.assertFalse(r["eligible_scope_complete"])
        self.assertTrue((self.root/item["artifacts"][0]["path"]).is_file())
        # Simulate the old generic failure. Repair comes from the saved bytes,
        # not the reported last date, and never makes a provider request.
        run=next((self.root/"runs").iterdir())
        item["state"]="FAILED"
        item["last_session"]=EXPECTED
        capture.publish_receipt(self.root,run,r)
        before=list(self.queries)
        audited=capture.audit_archive(self.root,self.registry,self.subset,EXPECTED)
        self.assertEqual(audited["per_ticker"]["BRMS"]["state"],"SOURCE_STALE")
        self.assertEqual(audited["per_ticker"]["BRMS"]["last_session"],"2026-10-20")
        self.assertEqual(audited["archive_audit"]["provider_requests"],0)
        self.assertEqual(audited["request_budget"]["performed"],0)
        self.assertEqual(audited["archive_audit"]["prior_request_budget"]["performed"],2)
        self.assertEqual(self.queries,before)
        self.assertEqual(capture.audit_archive(self.root,self.registry,self.subset,EXPECTED)["counts"],r["counts"])

    def test_source_ahead_is_distinct_and_never_current(self):
        data=payload("BMRI")["data"]
        item=capture.observation_item(data,"BMRI",self.subset,"2026-10-20",[{}])
        self.assertEqual(item["state"],"SOURCE_AHEAD")
        self.assertFalse(item["requested_session_available"])

    def test_archive_audit_refuses_changed_original(self):
        r=self.run_capture()
        (self.root/r["per_ticker"]["BMRI"]["artifacts"][0]["path"]).write_bytes(b"tampered")
        audited=capture.audit_archive(self.root,self.registry,self.subset,EXPECTED)
        self.assertEqual(audited["per_ticker"]["BMRI"]["state"],"FAILED")
        self.assertIn("hash mismatch",audited["per_ticker"]["BMRI"]["reason"])

    def test_archive_audit_does_not_convert_unattempted_to_failure(self):
        self.run_capture(max_requests=1)
        r=capture.audit_archive(self.root,self.registry,self.subset,EXPECTED)
        self.assertEqual(r["per_ticker"]["BRMS"]["state"],"MISSING")
    def test_cloned_ohlcv_is_refused_across_individual_ticker_calls(self):
        r=capture.capture(self.root,self.registry,self.subset,EXPECTED,
                          lambda q:Response(payload(parse_qs(q)["symbol"][0],base=1000)),
                          sleep=lambda _:None)
        self.assertEqual(r["per_ticker"]["BRMS"]["state"],"FAILED")
        self.assertIn("identical",r["per_ticker"]["BRMS"]["reason"])
    def test_missing_requested_broker_is_not_zero(self):
        r=capture.capture(self.root,self.registry,self.subset,EXPECTED,
                          lambda q:Response(payload(parse_qs(q)["symbol"][0],codes=CODES[:-1])),
                          sleep=lambda _:None)
        self.assertEqual(r["counts"],{"FAILED":2})
    def test_no_secret_or_network_dependency_for_plan_and_import(self):
        config=Path(__file__).with_name("broker_subset.json")
        subset=capture.subset_contract(config)
        self.assertEqual(subset["client_classification"],"UNKNOWN")
        self.assertEqual(len(subset["codes"]),10)
    def test_narrow_requested_ticker_does_not_narrow_denominator(self):
        r=self.run_capture(requested_tickers=["BMRI"])
        self.assertEqual(r["per_ticker"]["BRMS"]["state"],"MISSING")
        self.assertEqual(r["acquisition_status"],"PARTIAL")
    def test_failed_first_name_does_not_starve_untouched_next_batch(self):
        def bad_first(query):
            ticker=parse_qs(query)["symbol"][0]
            return Response(payload("WRNG" if ticker=="BMRI" else ticker,base=1000 if ticker=="BMRI" else 2000))
        first=capture.capture(self.root,self.registry,self.subset,EXPECTED,bad_first,
                              max_requests=1,sleep=lambda _:None)
        self.assertEqual(first["per_ticker"]["BMRI"]["state"],"FAILED")
        second=self.run_capture(max_requests=1)
        self.assertEqual(second["per_ticker"]["BRMS"]["state"],"COMPLETE")
        self.assertEqual(second["per_ticker"]["BMRI"]["state"],"FAILED")
    def test_source_throttle_halts_global_requests_after_first_response(self):
        calls=[]
        def throttled(query):
            calls.append(query)
            response=Response(dict(success=False,message="abnormal usage"))
            response.status=429
            return response
        r=capture.capture(self.root,self.registry,self.subset,EXPECTED,throttled,sleep=lambda _:None)
        self.assertEqual(len(calls),1)
        self.assertEqual(r["halt_reason"],"PROVIDER_HTTP_429")
        self.assertEqual(r["counts"],{"FAILED":1,"MISSING":1})
    def test_index_exclusions_are_explicit_even_under_one_request_budget(self):
        registry=registry_from_tickers(["BMRI","IHSG","ISSI"],source="synthetic")
        r=capture.capture(self.root,registry,self.subset,EXPECTED,self.get,
                          max_requests=1,sleep=lambda _:None)
        self.assertEqual(len(r["per_ticker"]),3)
        self.assertEqual(r["counts"],{"COMPLETE":1,"SOURCE_UNAVAILABLE":2})
        self.assertTrue(r["eligible_scope_complete"])
        self.assertEqual(r["acquisition_status"],"PARTIAL")
    def test_checkpoint_failure_stops_before_further_source_acquisition(self):
        def refused(*args):
            raise OSError("private persistence refused")
        with self.assertRaisesRegex(OSError,"private persistence"):
            self.run_capture(checkpoint=refused,checkpoint_every=1)
        self.assertEqual(len(self.queries),1)

if __name__=="__main__":
    unittest.main()
