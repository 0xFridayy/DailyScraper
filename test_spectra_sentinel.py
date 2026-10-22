"""Offline deterministic tests for the Sentinel; no GitHub or vendor calls."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

import spectra_sentinel as s

NOW = datetime(2026, 10, 22, 15, 30, tzinfo=timezone.utc)
BASE = "https://api.github.com/repos/0xFridayy/DailyScraper"

class FakeGitHub:
    def __init__(self, state=None, conclusions=None, age_hours=None):
        self.state = state or {}
        self.conclusions = conclusions or {}
        self.age_hours = age_hours or {}
        self.ids = {name: i + 101 for i, name in enumerate(s.POLICY)}

    def __call__(self, url):
        if url == BASE + "/actions/workflows?per_page=100":
            return {"workflows": [
                {"id": self.ids[name], "path": ".github/workflows/" + name,
                 "state": self.state.get(name, "disabled_manually" if policy[1] == "paused" else "active")}
                for name, policy in s.POLICY.items()]}
        if "/runs?" in url:
            wid = int(url.split("/workflows/")[1].split("/")[0])
            name = next(k for k, v in self.ids.items() if v == wid)
            when = (NOW - timedelta(hours=self.age_hours.get(name, 4))).isoformat()
            return {"workflow_runs": [{
                "created_at": when, "status": "completed",
                "conclusion": self.conclusions.get(name, "success"),
                "html_url": "https://github.com/0xFridayy/DailyScraper/actions/runs/1"}]}
        raise AssertionError("Unexpected call")

def database(p, dt="2026-10-21", table=True):
    d = sqlite3.connect(p)
    if table:
        d.execute("CREATE TABLE price_history(ticker TEXT, date TEXT, close REAL)")
        d.execute("INSERT INTO price_history VALUES('BRMS', ?, 400)", (dt,))
    else:
        d.execute("CREATE TABLE unrelated(x TEXT)")
    d.commit()
    d.close()

class SentinelTests(unittest.TestCase):
    def run_audit(self, *, date_str="2026-10-21", state=None, conclusions=None, age_hours=None):
        with TemporaryDirectory() as td:
            p = Path(td) / "neobdm.db"
            database(p, date_str)
            return s.audit(FakeGitHub(state, conclusions, age_hours),
                           "0xFridayy/DailyScraper", NOW, p)

    def test_all_scheduled_success_does_not_claim_vendor_access(self):
        r = self.run_audit()
        self.assertEqual(r["overall"], "GREEN_MONITORED_ONLY")
        self.assertEqual(r["external_not_certified"]["stockbit_broker_distribution"],
                         "WAITING_AUTHORIZED_CAPTURE_METHOD")

    def test_required_disabled_is_critical(self):
        r = self.run_audit(state={"price-history-topup.yml": "disabled_manually"})
        self.assertEqual(r["overall"], "RED")
        self.assertTrue(any("price-history-topup.yml" in x for x in r["critical"]))

    def test_broker_learning_intentionally_paused(self):
        r = self.run_audit()
        rows = {x["name"]: x for x in r["workflows"]}
        self.assertEqual(rows["broker-learning-daily.yml"]["severity"], "INFO")
        self.assertEqual(rows["broker-learning-weekly.yml"]["status"], "INTENTIONALLY_PAUSED")

    def test_broker_learning_unexpected_reenable_warning(self):
        r = self.run_audit(state={"broker-learning-weekly.yml": "active"})
        self.assertEqual(r["overall"], "DEGRADED")

    def test_failed_signal_integrity_is_critical(self):
        r = self.run_audit(conclusions={"signal-integrity.yml": "failure"})
        self.assertEqual(r["overall"], "RED")
        self.assertTrue(any("signal-integrity.yml" in x for x in r["critical"]))

    def test_stale_successful_weekly_workflow_detected(self):
        r = self.run_audit(age_hours={"arb-veto.yml": 240})
        self.assertTrue(any("STALE_SCHEDULED_RUN" in x for x in r["critical"]))

    def test_source_stale_even_with_green_workflows(self):
        r = self.run_audit(date_str="2026-10-09")
        self.assertTrue(any("price_history: STALE" in x for x in r["critical"]))

    def test_source_missing_table(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "neobdm.db"
            database(p, table=False)
            self.assertEqual(s.check_price_history(p, NOW.date())["status"], "MISSING_TABLE")

    def test_missing_db(self):
        with TemporaryDirectory() as td:
            self.assertEqual(s.check_price_history(Path(td) / "absent", NOW.date())["status"], "MISSING_DATABASE")

    def test_future_date(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "data.db"
            database(p, "2026-11-01")
            self.assertEqual(s.check_price_history(p, NOW.date())["status"], "FUTURE_SOURCE_DATE")

    def test_weekend_gap(self):
        self.assertEqual(s.weekdays_since(date(2026, 10, 16), date(2026, 10, 19)), 1)

    def test_missing_run_history_is_red(self):
        def get(url):
            if "/runs?" in url:
                return {"workflow_runs": []}
            return FakeGitHub()(url)
        with TemporaryDirectory() as td:
            p = Path(td) / "d.db"
            database(p)
            r = s.audit(get, "0xFridayy/DailyScraper", NOW, p)
        self.assertEqual(r["overall"], "RED")

    def test_api_error_fails_closed(self):
        def get(_):
            raise s.SentinelError("no")
        with self.assertRaises(s.SentinelError):
            s.audit(get, "0xFridayy/DailyScraper", NOW, Path("missing"))

    def test_markdown_has_freshness_and_disabled_context(self):
        r = self.run_audit()
        md = s.markdown(r)
        self.assertIn("broker-learning-daily.yml", md)
        self.assertIn("neobdm.db/price_history", md)
        self.assertIn("NOT_OBSERVABLE_FROM_GITHUB", md)

if __name__ == "__main__":
    unittest.main()
