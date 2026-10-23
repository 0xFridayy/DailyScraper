"""Synthetic failure witnesses; no provider calls or production certificates."""
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ticker_registry import load_registry, registry_from_tickers
from sentinel_evidence import check_price_evidence, check_receipt, file_sha256
from test_spectra_sentinel import database, GATES

TODAY = date(2026, 10, 22)
REGISTRY = registry_from_tickers(["BMRI", "BRMS"])

def price_db(path, members=("BMRI", "BRMS")):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE price_history(date TEXT,ticker TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)")
    conn.execute("CREATE TABLE price_quarantine(date TEXT,ticker TEXT)")
    for day in ["2026-10-20", "2026-10-21"]:
        for pos,ticker in enumerate(members):
            conn.execute("INSERT INTO price_history VALUES(?,?,?,?,?,?,?)",
                         (day,ticker,100+pos,110+pos,90+pos,101+pos,1000+pos))
    conn.commit()
    return conn

class FixedRegistryTests(unittest.TestCase):
    def test_source_registry_rejects_duplicate_identity(self):
        with self.assertRaises(ValueError):
            registry_from_tickers(["BMRI", "BMRI"])

    def test_registry_hash_independent_of_json_indent_and_order(self):
        self.assertEqual(registry_from_tickers(["BRMS","BMRI"]).sha256,REGISTRY.sha256)

    def test_default_registry_keeps_all_entries(self):
        r = load_registry()
        self.assertEqual(len(r.tickers),1042)
        self.assertIn("IHSG",r.required_for(TODAY))
        self.assertIn("ISSI",r.required_for(TODAY))
        self.assertEqual(r.metadata()["listing_lifecycle_certification"],"UNVERIFIED")

class PriceEvidenceTests(unittest.TestCase):
    def run_case(self, mutate=None, members=("BMRI","BRMS"), gates=GATES, **kwargs):
        with TemporaryDirectory() as td:
            p=Path(td)/"prices.db"
            c=price_db(p,members)
            if mutate: mutate(c)
            c.commit()
            c.close()
            before=file_sha256(p)
            with patch.dict(sys.modules,gates):
                result=check_price_evidence(p,TODAY,REGISTRY,**kwargs)
            self.assertEqual(file_sha256(p),before)
            return result

    def test_healthy_control_runs_gate_on_same_snapshot(self):
        r=self.run_case()
        self.assertEqual(r["status"],"CURRENT_REQUIRED_COVERAGE")
        self.assertEqual(r["report_admission"]["snapshot_sha256"],r["snapshot_sha256"])

    def test_one_current_ticker_cannot_hide_stale_peer(self):
        r=self.run_case(lambda c:c.execute("UPDATE price_history SET date='2026-10-09' WHERE ticker='BRMS'"))
        self.assertEqual(r["status"],"INCOMPLETE_COVERAGE")
        self.assertEqual(r["coverage"]["2026-10-21"]["missing"],["BRMS"])

    def test_first_run_and_persistent_dropout_keep_denominator(self):
        r=self.run_case(members=("BMRI",))
        for item in r["coverage"].values():
            self.assertEqual(item["required"],2)
            self.assertEqual(item["missing"],["BRMS"])

    def test_raw_latest_quarantined_rows_do_not_count(self):
        r=self.run_case(lambda c:c.execute("INSERT INTO price_quarantine SELECT date,ticker FROM price_history WHERE date='2026-10-21'"))
        self.assertEqual(r["status"],"INCOMPLETE_COVERAGE")
        self.assertEqual(r["coverage"]["2026-10-21"]["raw_present"],2)
        self.assertEqual(r["coverage"]["2026-10-21"]["accepted_present"],0)

    def test_numeric_looking_text_cannot_certify_prices(self):
        r=self.run_case(lambda c:c.execute("UPDATE price_history SET close='not-a-number' WHERE ticker='BRMS'"))
        self.assertEqual(r["status"],"INVALID_DATA")

    def test_duplicate_keys_refused(self):
        r=self.run_case(lambda c:c.execute("INSERT INTO price_history SELECT * FROM price_history WHERE date='2026-10-21'"))
        self.assertEqual(r["status"],"INVALID_DATA")

    def test_missing_gate_refuses_certificate(self):
        r=self.run_case(gates={"signal_integrity_health":None})
        self.assertEqual(r["status"],"REPORT_ADMISSION_NOT_INTEGRATED")
        self.assertEqual(r["severity"],"CRITICAL")

    def test_different_report_snapshot_refused(self):
        gates=dict(GATES)
        gates["signal_integrity_health"]=SimpleNamespace(report_admission=lambda *a,**kw:{"admitted":True,"snapshot_sha256":"0"*64})
        self.assertEqual(self.run_case(gates=gates)["status"],"DIFFERENT_REPORT_SNAPSHOT")

    def test_missing_quarantine_is_not_admission(self):
        r=self.run_case(lambda c:c.execute("DROP TABLE price_quarantine"))
        self.assertEqual(r["status"],"QUARANTINE_NOT_CERTIFIED")

    # An after-close producer stores TODAY's session the same evening; only the
    # caller knows that producer's cutoff, so the default stays strictly-before.
    def same_day(self, members=("BMRI","BRMS")):
        return lambda c:c.executemany("INSERT INTO price_history VALUES('2026-10-22',?,100,110,90,101,1000)",
                                      [(t,) for t in members])

    def test_same_day_bar_stays_future_without_a_closed_session(self):
        self.assertEqual(self.run_case(self.same_day())["status"],"FUTURE_SOURCE_DATE")

    def test_closed_same_day_session_is_required_not_future(self):
        r=self.run_case(self.same_day(),closed_session=TODAY)
        self.assertEqual(r["required_sessions"],["2026-10-22","2026-10-21"])
        self.assertEqual(r["status"],"CURRENT_REQUIRED_COVERAGE")

    def test_closed_session_reports_evening_coverage_instead_of_hiding_it(self):
        r=self.run_case(self.same_day(("BMRI",)),closed_session=TODAY)
        self.assertEqual(r["status"],"INCOMPLETE_COVERAGE")
        self.assertEqual(r["coverage"]["2026-10-22"]["missing"],["BRMS"])

    def test_closed_session_reaches_the_same_snapshot_admission(self):
        seen={}
        def admit(conn,**kw):
            seen.update(kw)
            return GATES["signal_integrity_health"].report_admission(conn,**kw)
        gates=dict(GATES,signal_integrity_health=SimpleNamespace(report_admission=admit))
        self.run_case(self.same_day(),gates=gates,closed_session=TODAY)
        self.assertEqual(seen["closed_session"],TODAY)

class ReceiptEvidenceTests(unittest.TestCase):
    def fixture(self,root):
        artifact=root/"original.bin"
        artifact.write_bytes(b"explicit synthetic opaque evidence")
        receipt=dict(schema="spectra-collector-receipt.v1",source="synthetic",
            registry_sha256=REGISTRY.sha256,trade_date="2026-10-21",captured_at="2026-10-22T08:00:00+07:00",
            acquisition_status="COMPLETE",processing_status="COMPLETE",
            source_certification="VERIFIED_REQUESTED_SCOPE",
            coverage_scope="OBSERVED_BROKER_SUBSET",full_broker_universe=False,
            per_ticker={t:dict(state="COMPLETE",last_session="2026-10-21") for t in REGISTRY.tickers},
            persistence=dict(status="PERSISTED",retrieval_status="VERIFIED"),
            artifacts=[dict(path="original.bin",sha256=file_sha256(artifact))])
        return receipt

    def check_case(self,mutate=None,artifact_mutate=None,root_available=True):
        with TemporaryDirectory() as td:
            root=Path(td)
            receipt=self.fixture(root)
            if mutate: mutate(receipt)
            if artifact_mutate: artifact_mutate(root)
            p=root/"receipt.json"
            p.write_text(json.dumps(receipt))
            return check_receipt(p,REGISTRY,date(2026,10,21),root if root_available else None)

    def test_complete_requested_subset_is_not_all_broker_certificate(self):
        r=self.check_case()
        self.assertEqual(r["status"],"VERIFIED_REQUESTED_SCOPE_ONLY")
        self.assertFalse(r["full_broker_universe"])
        self.assertEqual(r["history_completeness"],"NOT_CERTIFIED")

    def test_missing_batch_or_ticker_refused(self):
        r=self.check_case(lambda r:r["per_ticker"].pop("BRMS"))
        self.assertEqual(r["status"],"INCOMPLETE_COVERAGE")
        self.assertEqual(r["missing"],["BRMS"])

    def test_failed_persistence_even_after_successful_capture(self):
        r=self.check_case(lambda r:r["persistence"].update(status="FAILED"))
        self.assertEqual(r["status"],"PRIVATE_PERSISTENCE_NOT_VERIFIED")

    def test_retrieval_declaration_without_readable_bytes_not_proof(self):
        self.assertEqual(self.check_case(root_available=False)["status"],"RETRIEVED_ARTIFACTS_NOT_AVAILABLE")

    def test_tampered_retrieved_artifact_is_refused(self):
        r=self.check_case(artifact_mutate=lambda root:(root/"original.bin").write_bytes(b"tampered"))
        self.assertEqual(r["status"],"ARTIFACT_HASH_MISMATCH")

    def test_blocked_image_acquisition_cannot_pass_processing_success(self):
        r=self.check_case(lambda r:r.update(acquisition_status="WAITING_AUTHORIZED_CAPTURE_METHOD"))
        self.assertEqual(r["status"],"ACQUISITION_NOT_COMPLETE")

    def test_retrospective_history_scope_cannot_become_live_certificate(self):
        r=self.check_case(lambda r:r.update(source_certification="UNVALIDATED_LIVE"))
        self.assertEqual(r["status"],"SOURCE_NOT_CERTIFIED")

    def test_stale_receipt_refused(self):
        self.assertEqual(self.check_case(lambda r:r.update(trade_date="2026-10-20"))["status"],"STALE_RECEIPT")

if __name__=="__main__":
    unittest.main()
