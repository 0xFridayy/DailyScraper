"""Offline writer regressions with synthetic VKTR observations, never live data."""

import contextlib
import copy
from datetime import date, timedelta
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile

import pytest

from idx_calendar import COVERED_FROM, is_idx_session
from price_contract import RAW_ACTUAL
from test_inventory_capture import bf, price_db, price_payload, price_bar
import price_history_revision as revision


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("historical revision tests must stay offline")
    monkeypatch.setattr(socket, "create_connection", refuse)


def observations(n=30, ticker="VKTR"):
    day, bars = COVERED_FROM, []
    while len(bars) < n:
        if is_idx_session(day):
            bar = price_bar(day.isoformat(), 100 + len(bars))
            bar["volume"] = 1000 + len(bars)
            bars.append(bar)
        day += timedelta(days=1)
    body = price_payload(bars, ticker)
    body["data"]["nlot"] = {"AK": [100] * n, "BK": [-50] * n}
    return body


def adjusted(body, factor, count=None):
    result = copy.deepcopy(body)
    for bar in result["data"]["ohlc"][:count]:
        for field in ("open", "high", "low", "close"):
            bar[field] *= factor
        bar["volume"] /= factor
    return result


def financial_snapshot(conn):
    return {table: conn.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid").fetchall()
            for table in ("price_history", "broker_flow")}


def duplicate_count(conn):
    from price_audit import detect, load
    return int(detect(load(conn))["cross_ticker_dup"].sum())


def seed(conn, body):
    bf.insert_inventory(conn, body["meta"]["symbol"], body, representation=RAW_ACTUAL)
    conn.commit()


def vktr_witness(factor):
    """Run unchanged on base and fix; report actual persisted writer effects."""
    body = observations()
    # The 1.10 case deliberately keeps actual and adjusted bars in one ticker.
    proposal = adjusted(body, factor, 8 if factor == 1.10 else None)
    with tempfile.TemporaryDirectory(prefix="vktr-revision-") as folder:
        path = str(Path(folder) / "synthetic.sqlite")
        with price_db(path) as conn, contextlib.redirect_stdout(io.StringIO()):
            seed(conn, body)
            before, dup_before = financial_snapshot(conn), duplicate_count(conn)
            changes_before = conn.total_changes
            refusal = None
            try:
                bf.insert_inventory(conn, "VKTR", proposal, representation=RAW_ACTUAL)
            except bf.InventoryError as exc:
                refusal = str(exc)
            conn.commit()
            after, dup_after = financial_snapshot(conn), duplicate_count(conn)
            changes = conn.total_changes - changes_before
        with sqlite3.connect(path) as persisted:
            assert financial_snapshot(persisted) == after
        return {"factor": factor, "first_session": body["data"]["date"][0],
                "accepted": refusal is None, "refusal": refusal,
                "price_rows_changed": sum(a != b for a, b in zip(before["price_history"], after["price_history"])),
                "broker_rows_changed": sum(a != b for a, b in zip(before["broker_flow"], after["broker_flow"])),
                "price_observations_changed": sum(a[1:] != b[1:] for a, b in zip(before["price_history"], after["price_history"])),
                "netval_values_changed": sum(a[6] != b[6] for a, b in zip(before["broker_flow"], after["broker_flow"])),
                "first_close_before": before["price_history"][0][6],
                "first_close_after": after["price_history"][0][6],
                "first_netval_before": before["broker_flow"][0][6],
                "first_netval_after": after["broker_flow"][0][6],
                "cross_ticker_dup_before": dup_before, "cross_ticker_dup_after": dup_after,
                "mixed_actual_and_adjusted": factor == 1.10 and refusal is None,
                "total_changes_delta": changes, "financial_snapshot_unchanged": before == after}


@pytest.mark.parametrize("factor", [1.10, 1.24])
def test_vktr_historical_rewrites_are_refused_before_mutation(factor):
    result = vktr_witness(factor)
    assert not result["accepted"], result
    assert result["financial_snapshot_unchanged"], result
    assert result["total_changes_delta"] == 0, result
    assert result["cross_ticker_dup_before"] == result["cross_ticker_dup_after"] == 0


@pytest.mark.parametrize("representation", [RAW_ACTUAL, "UNKNOWN", "ADJUSTED", "MIXED"])
def test_plausible_partial_mass_revision_cannot_authorize_itself(representation):
    with price_db() as conn:
        body = observations()
        seed(conn, body)
        before, changes = financial_snapshot(conn), conn.total_changes
        proposal = adjusted(body, 1.10, 12)
        # Broker lots also differ, so a rejected price cannot refresh netval.
        proposal["data"]["nlot"]["AK"] = [900] * 30
        with pytest.raises(bf.InventoryError, match="historical_revision"):
            bf.insert_inventory(conn, "VKTR", proposal, representation=representation)
        conn.commit()
        assert conn.total_changes == changes
        assert financial_snapshot(conn) == before


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_each_nonnull_field_is_protected_including_null_repair(field):
    with price_db() as conn:
        body = observations()
        seed(conn, body)
        bar = copy.deepcopy(body["data"]["ohlc"][5])
        # Keep the bar's domain valid while changing just one non-NULL field.
        if field in {"open", "close"}:
            conn.execute("UPDATE price_history SET high=high+1 WHERE date=?", (bar["date"],))
            bar["high"] += 1
        bar[field] += -1 if field == "low" else 1
        if field != "volume":
            conn.execute("UPDATE price_history SET volume=NULL WHERE date=?", (bar["date"],))
        conn.commit()
        before, changes = financial_snapshot(conn), conn.total_changes
        with pytest.raises(bf.InventoryError, match="historical_revision"):
            bf.insert_inventory(conn, "VKTR", price_payload([bar], "VKTR"))
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes


def test_identical_repeat_preserves_rowids_and_does_not_fire_price_triggers():
    with price_db() as conn:
        body = observations()
        seed(conn, body)
        before, changes = financial_snapshot(conn), conn.total_changes
        conn.execute("CREATE TRIGGER forbid_price_update BEFORE UPDATE ON price_history "
                     "BEGIN SELECT RAISE(ABORT,'identical price updated'); END")
        conn.execute("CREATE TRIGGER forbid_price_insert BEFORE INSERT ON price_history "
                     "BEGIN SELECT RAISE(ABORT,'identical price replaced'); END")
        try:
            result = bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        except sqlite3.Error as exc:
            raise AssertionError("identical repeat attempted a price mutation") from exc
        assert result[1] == 30
        conn.commit()
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes


def test_new_session_and_unrelated_ticker_still_insert():
    with price_db() as conn:
        body = observations()
        seed(conn, body)
        old_prices = financial_snapshot(conn)["price_history"]
        extension = observations(31)
        assert bf.insert_inventory(conn, "VKTR", extension, representation=RAW_ACTUAL)[1] == 31
        other = observations(ticker="BBBB")
        for bar in other["data"]["ohlc"]:
            bar["volume"] += 10000
        assert bf.insert_inventory(conn, "BBBB", other, representation=RAW_ACTUAL)[1] == 30
        conn.commit()
        assert financial_snapshot(conn)["price_history"][:30] == old_prices
        assert conn.execute("SELECT count(*) FROM price_history").fetchone() == (61,)


@pytest.mark.parametrize("fields", [("open",), ("high", "low"), ("volume",), ("close",),
                                   ("open", "high", "low", "close", "volume")])
def test_legitimate_null_repair_preserves_nonnull_observations(fields):
    with price_db() as conn:
        body = observations()
        seed(conn, body)
        bar = body["data"]["ohlc"][5]
        conn.execute("UPDATE price_history SET " + ",".join(f"{f}=NULL" for f in fields) +
                     " WHERE date=? AND ticker='VKTR'", (bar["date"],))
        conn.commit()
        rid = conn.execute("SELECT rowid FROM price_history WHERE date=?", (bar["date"],)).fetchone()
        assert bf.insert_inventory(conn, "VKTR", price_payload([bar], "VKTR"), representation=RAW_ACTUAL)[1] == 1
        conn.commit()
        assert conn.execute("SELECT rowid FROM price_history WHERE date=?", (bar["date"],)).fetchone() == rid
        assert conn.execute("SELECT open,high,low,close,volume FROM price_history WHERE date=?",
                            (bar["date"],)).fetchone() == tuple(bar[f] for f in ("open", "high", "low", "close", "volume"))


@pytest.mark.parametrize("autocommit", [False, True])
def test_broker_failure_rolls_back_prices_and_earlier_broker_writes(tmp_path, autocommit):
    path = str(tmp_path / "atomic.sqlite")
    with price_db(path) as conn:
        conn.execute("CREATE TRIGGER fail_second_broker BEFORE INSERT ON broker_flow "
                     "WHEN NEW.broker_code='BK' BEGIN SELECT RAISE(ABORT,'injected broker failure'); END")
        conn.commit()
        if autocommit:
            conn.isolation_level = None
        before = financial_snapshot(conn)
        with pytest.raises(sqlite3.IntegrityError, match="injected broker failure"):
            bf.insert_inventory(conn, "VKTR", observations(), representation=RAW_ACTUAL)
        assert financial_snapshot(conn) == before
        # A caller that catches the error and commits must not leak partial writes.
        conn.commit()
    with sqlite3.connect(path) as persisted:
        assert financial_snapshot(persisted) == before


def test_price_failure_preserves_unrelated_pending_transaction():
    with price_db() as conn:
        other = price_bar("2026-01-02", 200)
        conn.execute("INSERT INTO price_history VALUES(?,?,?,?,?,?,?)",
                     (other["date"], "BBBB", 200, 200, 200, 200, 9999))
        before = financial_snapshot(conn)
        conn.execute("CREATE TRIGGER fail_price BEFORE INSERT ON price_history "
                     "WHEN NEW.date='2026-01-05' BEGIN SELECT RAISE(ABORT,'injected price failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected price failure"):
            bf.insert_inventory(conn, "VKTR", observations(), representation=RAW_ACTUAL)
        assert conn.in_transaction
        assert financial_snapshot(conn) == before
        conn.commit()
        assert financial_snapshot(conn) == before


def test_successful_ticker_waits_for_caller_commit(tmp_path):
    path = str(tmp_path / "pending.sqlite")
    with price_db(path) as conn:
        bf.insert_inventory(conn, "VKTR", observations(), representation=RAW_ACTUAL)
        assert conn.in_transaction
        with sqlite3.connect(path) as reader:
            assert reader.execute("SELECT count(*) FROM price_history").fetchone() == (0,)
            assert reader.execute("SELECT count(*) FROM broker_flow").fetchone() == (0,)
        conn.rollback()
        assert financial_snapshot(conn) == {"price_history": [], "broker_flow": []}


def test_revision_refuses_new_session_and_null_repair_in_the_same_payload():
    with price_db() as conn:
        body = observations()
        seed(conn, body)
        conn.execute("UPDATE price_history SET open=NULL WHERE date='2026-01-05'")
        conn.commit()
        before, changes = financial_snapshot(conn), conn.total_changes
        proposal = observations(31)
        proposal["data"]["ohlc"][25]["volume"] += 1
        with pytest.raises(bf.HistoricalRevisionError):
            bf.insert_inventory(conn, "VKTR", proposal, representation=RAW_ACTUAL)
        conn.commit()
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes


def test_savepoint_release_failure_cannot_leak_financial_writes():
    class FailRelease:
        def __init__(self, conn):
            self.conn, self.failed = conn, False

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args):
            if sql == "RELEASE inventory_ticker" and not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("injected release failure")
            return self.conn.execute(sql, *args)

    with price_db() as conn:
        before = financial_snapshot(conn)
        with pytest.raises(sqlite3.OperationalError, match="injected release failure"):
            bf.insert_inventory(FailRelease(conn), "VKTR", observations(), representation=RAW_ACTUAL)
        conn.commit()
        assert financial_snapshot(conn) == before


def test_failed_null_repair_and_netval_refresh_are_rolled_back(tmp_path):
    path = str(tmp_path / "repair-failure.sqlite")
    with price_db(path) as conn:
        body = observations()
        seed(conn, body)
        conn.execute("UPDATE price_history SET open=NULL WHERE date='2026-01-05'")
        conn.execute("CREATE TRIGGER fail_refresh BEFORE UPDATE ON broker_flow "
                     "WHEN NEW.broker_code='BK' BEGIN SELECT RAISE(ABORT,'refresh failed'); END")
        conn.commit()
        before = financial_snapshot(conn)
        proposal = copy.deepcopy(body)
        proposal["data"]["nlot"] = {"AK": [150] * 30, "BK": [-100] * 30}
        with pytest.raises(sqlite3.IntegrityError, match="refresh failed"):
            bf.insert_inventory(conn, "VKTR", proposal, representation=RAW_ACTUAL)
        conn.commit()
        assert financial_snapshot(conn) == before


def test_run_refusal_is_fatal_below_failure_threshold_and_evidence_has_no_auth(tmp_path):
    from test_inventory_capture import run_backfill, Resp, ic, only_manifest
    body = observations()
    path = str(tmp_path / "neobdm.db")
    with price_db(path) as conn:
        seed(conn, body)
        before = financial_snapshot(conn)
    proposal = adjusted(body, 1.10, 8)
    proposal["authentication"] = {"password": "DO_NOT_EXPOSE", "cookie": "DO_NOT_EXPOSE"}
    script = {"VKTR": [Resp(200, body=proposal)]}
    for multiplier, ticker in enumerate(["BBBB", "CCCC", "DDDD", "EEEE"], start=2):
        other = observations(ticker=ticker)
        for bar in other["data"]["ohlc"]:
            bar["volume"] += 10000 + ord(ticker[0])
            for field in ("open", "high", "low", "close"):
                bar[field] *= multiplier
        script[ticker] = [Resp(200, body=other)]
    output, exit_message, _ = run_backfill(str(tmp_path), script, list(script))
    assert "unauthorized historical revisions" in str(exit_message)
    evidence = json.loads((tmp_path / "topup-failure.json").read_text())
    assert evidence.get("reason") == "UNAUTHORIZED_HISTORICAL_REVISION"
    assert len(evidence["refusals"]) == 8
    assert "DO_NOT_EXPOSE" not in output + (tmp_path / "topup-failure.json").read_text()
    captures = ic.read_captures(only_manifest(str(tmp_path)))
    assert captures[0]["status"] == ic.REJECTED
    assert all(c["status"] == ic.OK for c in captures[1:])
    with sqlite3.connect(path) as conn:
        for table in before:
            assert conn.execute(f"SELECT rowid,* FROM {table} WHERE ticker='VKTR' ORDER BY rowid").fetchall() == before[table]


@pytest.mark.parametrize("change", ["revision", "delete", "erase", "new", "identical", "null_repair"])
def test_precommit_delta_gate(tmp_path, change, capsys):
    path, baseline = tmp_path / "candidate.sqlite", tmp_path / "before.sqlite"
    with price_db(str(path)) as conn:
        body = observations()
        seed(conn, body)
        if change == "null_repair":
            conn.execute("UPDATE price_history SET open=NULL WHERE date='2026-01-05'")
            conn.commit()
        revision.snapshot_database(path, baseline)
        if change == "revision":
            conn.execute("UPDATE price_history SET close=close*1.10,open=open*1.10,high=high*1.10,low=low*1.10")
        elif change == "delete":
            conn.execute("DELETE FROM price_history WHERE date='2026-01-05'")
        elif change == "erase":
            conn.execute("UPDATE price_history SET volume=NULL WHERE date='2026-01-05'")
        elif change == "null_repair":
            bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        elif change == "new":
            bf.insert_inventory(conn, "VKTR", observations(31), representation=RAW_ACTUAL)
        conn.commit()
        before_check = financial_snapshot(conn)
        rejected = change in {"revision", "delete", "erase"}
        assert revision.main(["verify", "--database", str(path), "--baseline", str(baseline)]) == int(rejected)
        assert financial_snapshot(conn) == before_check
    assert not baseline.stat().st_mode & 0o222


def test_missing_invalid_or_reused_baseline_fails_closed(tmp_path):
    path = tmp_path / "candidate.sqlite"
    with price_db(str(path)) as conn:
        seed(conn, observations())
    for baseline in (tmp_path / "missing.sqlite", path):
        assert revision.main(["verify", "--database", str(path), "--baseline", str(baseline)]) == 1
    invalid = tmp_path / "invalid.sqlite"
    invalid.write_text("invalid SQLite")
    assert revision.main(["verify", "--database", str(path), "--baseline", str(invalid)]) == 1
    baseline = tmp_path / "before.sqlite"
    revision.snapshot_database(path, baseline)
    assert revision.main(["snapshot", "--database", str(path), "--baseline", str(baseline)]) == 1


def test_workflow_commit_requires_both_gates_and_successful_writer():
    workflow = Path(__file__).parent / ".github/workflows/price-history-topup.yml"
    text = workflow.read_text()
    baseline = text.index("python price_history_revision.py snapshot")
    writer = text.index("run: python backfill_inventory.py")
    gate = text.index("python price_history_revision.py verify")
    commit = text.index("- name: Commit updated neobdm.db")
    assert baseline < writer < gate < commit
    commit_step = text[commit:]
    assert "success()" in commit_step
    for step in ("topup", "contamination_gate", "revision_gate"):
        assert f"steps.{step}.outcome == 'success'" in commit_step
    assert "continue-on-error" not in text


if __name__ == "__main__":
    print(json.dumps([vktr_witness(1.10), vktr_witness(1.24)], indent=2, sort_keys=True))
