"""Plain-script tests for targeted_actor_observations (py -3 test_targeted_actor_observations.py).

Offline and synthetic. Every panel is built the way production builds one:
targeted_actor_panel.collect against test_targeted_actor_panel's FakeVendor, the
/api/inventory model verified 2026-09-27, into a temporary directory. A test that
needs a panel the collector would refuse tampers with a COPY of a collected
database through its own sqlite3 connection; the reader under test never writes.
Importing test_targeted_actor_panel also blocks neobdm_scraper and Playwright, so
nothing here can log in.
"""

import copy
import glob
import hashlib
import inspect
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager

import test_targeted_actor_panel as tp          # FakeVendor and fixtures; blocks the scraper
import broker_book as bb                         # noqa: E402
import build_inventory_db as bidb                # noqa: E402
import targeted_actor_db as tdb                  # noqa: E402
import targeted_actor_observations as tao        # noqa: E402
import targeted_actor_panel as tap               # noqa: E402
import targeted_selectors as ts                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "targeted_actor_observations.py")

C, NL = tao.CALCULATED, tao.NO_LOTS
WV, WB = tao.WITHHELD_VALUE_WITHOUT_REPORTED_LOTS, tao.WITHHELD_KNOWN_BASIS_CONFLICT
KNOWN, NO_KNOWN = "KNOWN_CONFLICT", "NO_KNOWN_CONFLICT_NOT_VERIFIED"


# ── fixtures ───────────────────────────────────────────────────────────────

def tmpdir():
    # A failed assertion can leave a connection open, and Windows then cannot
    # delete the file: the cleanup error must not mask the assertion.
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def collect(tmp, markets, db="panel.db", **vendor_kw):
    """Collect every ticker of `markets` into tmp/db with the production
    collector; its result (db_path, run_id, ...)."""
    res = tp.run(tp.FakeVendor(markets, **vendor_kw), sorted(markets), tmp, db=db)
    assert res["status"] == "ok", res
    return res


def observe(db, ticker, as_of=None, include_series=True):
    conn = tao.open_readonly(db)
    try:
        return tao.observe(conn, ticker, as_of, include_series=include_series)
    finally:
        conn.close()


def refused(db, ticker, error, contains):
    """observe() must raise `error` saying `contains`; the message."""
    try:
        observe(db, ticker)
    except error as e:
        assert contains in str(e), (contains, str(e))
        return str(e)
    raise AssertionError(f"observed a panel that must fail: {contains}")


def fingerprint(path):
    """(sha256, mtime_ns) of a file and the listing of its directory."""
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return digest, os.stat(path).st_mtime_ns, sorted(os.listdir(os.path.dirname(path)))


def tampered(src, tmp, name, sql, params=(), unchecked=False):
    """A copy of the database `src` with one statement applied by the test's
    own connection (CHECK constraints ignored when `unchecked`)."""
    dst = os.path.join(tmp, name, "panel.db")
    os.makedirs(os.path.dirname(dst))
    shutil.copyfile(src, dst)
    conn = sqlite3.connect(dst)
    try:
        if unchecked:
            conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
    return dst


def run_cli(args):
    return subprocess.run([sys.executable, CLI, *args], cwd=HERE, capture_output=True,
                          text=True, timeout=300)


@contextmanager
def basis(tmp, regimes, newline="\n"):
    """BASIS_FILE pointed at a temporary reference holding `regimes`."""
    path = os.path.join(tmp, "basis", "observed_basis_factor.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        json.dump({"note": "test reference", "regimes": regimes}, fh, indent=1)
    old, tao.BASIS_FILE = tao.BASIS_FILE, path
    try:
        yield path
    finally:
        tao.BASIS_FILE = old


def pad_with_zp(ticker, kept, env):
    """ZP comes back from request A with explicit all-zero series: observed,
    and a member of nothing (as in test_targeted_actor_panel)."""
    if "TOP_5_NB_LOT_C5" in kept:
        for f in tp.FIELDS:
            env["data"][f]["ZP"] = [0] * tp.N_SESSIONS
            env["data"][f] = dict(sorted(env["data"][f].items()))
    return env


def earlier(market, k):
    """The market as it stood k sessions earlier."""
    m = copy.deepcopy(market)
    m["dates"], m["ohlc"] = m["dates"][:-k], m["ohlc"][:-k]
    for s in m["brokers"].values():
        for f in s:
            s[f] = s[f][:-k]
    return m


# PRCE: 60 sessions, so C5 = 55..59, C20 = 40..59, C50 = 10..59, ALL = 0..59.
# (session, blot, bval, slot, sval) per broker; every other session is zero,
# and nlot = blot - slot, nval = bval - sval exactly.
P_N = 60
P_DATES = tp.weekdays("2025-10-06", P_N)
P_TRADES = {
    "AK": [(30, 5, 250_000, 0, 0), (55, 10, 1_000_000, 0, 0), (56, 30, 6_000_000, 0, 0),
           (57, 0, 0, 20, 6_000_000)],
    "BK": [(45, 0, 50_000, 0, 0), (58, 10, 1_100_000, 0, 0)],      # buy value, no buy lots, at 45
    "CC": [(20, 0, 0, 0, 7_000), (59, 0, 0, 5, 500_000)],          # sell value, no sell lots, at 20
    "DR": [(12, 9, 900_000, 0, 0), (50, 2, 300_000, 0, 0)],
}


def trade_market(trades, n=P_N):
    dates = tp.weekdays("2025-10-06", n)
    brokers = {}
    for code, rows in trades.items():
        s = {f: [0] * n for f in tp.FIELDS}
        for i, blot, bval, slot, sval in rows:
            s["blot"][i], s["bval"][i], s["slot"][i], s["sval"][i] = blot, bval, slot, sval
            s["nlot"][i], s["nval"][i] = blot - slot, bval - sval
        brokers[code] = s
    return {"dates": dates, "ohlc": tp.ohlc_rows(dates, 700.0), "brokers": brokers}


# broker -> horizon -> (buy, sell), each (status, implied price, value without reported lots),
# with no known basis conflict. Hand-computed from P_TRADES.
PRICES = {
    "AK": {"C5": ((C, 7_000_000 / 4_000, 0.0), (C, 6_000_000 / 2_000, 0.0)),
           "C20": ((C, 7_000_000 / 4_000, 0.0), (C, 6_000_000 / 2_000, 0.0)),
           "C50": ((C, 7_250_000 / 4_500, 0.0), (C, 6_000_000 / 2_000, 0.0)),
           "ALL": ((C, 7_250_000 / 4_500, 0.0), (C, 6_000_000 / 2_000, 0.0))},
    "BK": {"C5": ((C, 1_100_000 / 1_000, 0.0), (NL, None, 0.0)),
           "C20": ((WV, None, 50_000.0), (NL, None, 0.0)),
           "C50": ((WV, None, 50_000.0), (NL, None, 0.0)),
           "ALL": ((WV, None, 50_000.0), (NL, None, 0.0))},
    "CC": {"C5": ((NL, None, 0.0), (C, 500_000 / 500, 0.0)),
           "C20": ((NL, None, 0.0), (C, 500_000 / 500, 0.0)),
           "C50": ((NL, None, 0.0), (WV, None, 7_000.0)),
           "ALL": ((NL, None, 0.0), (WV, None, 7_000.0))},
    "DR": {"C5": ((NL, None, 0.0), (NL, None, 0.0)),
           "C20": ((C, 300_000 / 200, 0.0), (NL, None, 0.0)),
           "C50": ((C, 1_200_000 / 1_100, 0.0), (NL, None, 0.0)),
           "ALL": ((C, 1_200_000 / 1_100, 0.0), (NL, None, 0.0))},
}


def check_prices(doc, table):
    for b, by_horizon in table.items():
        for h, (buy, sell) in by_horizon.items():
            window = doc["brokers"][b]["windows"][h]
            for side, (status, price, unpriced) in (("gross_buy", buy), ("gross_sell", sell)):
                assert window[side] == {"status": status, tao.PRICE: price, tao.UNPRICED: unpriced}, (
                    b, h, side, window[side])


# ── 1. identity ────────────────────────────────────────────────────────────

def test_identity_is_ticker_and_discovery_as_of_and_run_id_is_provenance():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_bad_arguments_are_refused_before_any_query():
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
        conn = tao.open_readonly(db)
        try:
            calls = ([lambda t=t: tao.observe(conn, t) for t in ("aaaa", "AAAA ", "AAAAA", "AAA", 1234, None)]
                     + [lambda d=d: tao.observe(conn, "AAAA", d)
                        for d in ("2026-3-17", "20260317", "2026-02-30", 20260317, "")]
                     + [lambda: tao.observe(conn, "AAAA", include_series="yes"),
                        lambda: tao.list_snapshots(conn, "aaaa")])
            for call in calls:
                try:
                    call()
                except ValueError:
                    continue
                raise AssertionError("a bad argument was accepted")
        finally:
            conn.close()
    print("  ok test_bad_arguments_are_refused_before_any_query")


# ── 2. membership by horizon ───────────────────────────────────────────────

def test_membership_by_horizon_follows_the_stored_rows():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_boundary_ties_and_short_history_are_undetermined():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


# ── 3. implied price ───────────────────────────────────────────────────────

def test_implied_price_is_a_ratio_of_sums_over_reported_lots():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_a_known_basis_conflict_withholds_only_what_nothing_else_explains():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


# ── 4. integrity ───────────────────────────────────────────────────────────

def test_gross_data_the_collector_accepts_can_still_fail_the_reader():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


# (what the reader must say, SQL, params, CHECK constraints ignored)
TAMPER = (
    ("!= blot - slot", "UPDATE observed_series SET nlot = nlot + 1 WHERE broker = 'AK' "
                       "AND session_date = ?", (P_DATES[55],), False),
    ("|nval - (bval - sval)| exceeds", "UPDATE observed_series SET nval = nval + 0.75 "
                                       "WHERE broker = 'AK' AND session_date = ?", (P_DATES[55],), False),
    ("one for one", "DELETE FROM observed_series WHERE broker = 'AK' AND session_date = ?",
     (P_DATES[0],), False),
    ("disagrees with its values", "UPDATE observed_series SET coverage = 'OBSERVED_NONZERO' "
                                  "WHERE broker = 'AK' AND session_date = ?", (P_DATES[0],), False),
    ("stored membership rows disagree", "DELETE FROM selection_membership WHERE horizon = 'C5' "
                                        "AND metric = 'NB_LOT' AND broker = 'BK'", (), False),
    ("rank 2 but 0 stored member(s)", "UPDATE selection_membership SET rank = 2 WHERE horizon = 'C5' "
                                      "AND metric = 'NB_LOT' AND broker = 'AK'", (), False),
    ("tied 1 but 1 stored member(s)", "UPDATE selection_membership SET tied = 1 WHERE horizon = 'C5' "
                                      "AND metric = 'NB_LOT' AND broker = 'AK'", (), False),
    ("does not have the NB sign", "UPDATE selection_membership SET window_value = -window_value "
                                  "WHERE horizon = 'C20' AND metric = 'NB_LOT'", (), False),
    ("union_capture_ids", "UPDATE selection_membership SET union_capture_ids = '[\"x\", \"y\"]' "
                          "WHERE horizon = 'C5' AND metric = 'NB_LOT' AND broker = 'AK'", (), False),
    ("provenance 'VENDOR_RANK'", "UPDATE selection_membership SET provenance = 'VENDOR_RANK' "
                                 "WHERE horizon = 'C5' AND metric = 'NB_LOT' AND broker = 'AK'", (), True),
    ("window fields", "UPDATE selection_membership SET window_first_session = ? WHERE horizon = 'C5' "
                      "AND metric = 'NB_LOT' AND broker = 'AK'", (P_DATES[0],), False),
    ("not exactly the 16", "DELETE FROM selection_status WHERE horizon = 'C5' AND metric = 'NS_VAL'",
     (), False),
    ("but has membership rows", "UPDATE selection_status SET status = 'UNRESOLVED_BOUNDARY_TIE', "
                                "member_count = 0, boundary_tie_value = 10, "
                                "boundary_tie_brokers = '[\"AK\", \"BK\"]' "
                                "WHERE horizon = 'C5' AND metric = 'NB_LOT'", (), False),
    ("expanded_brokers disagree", "UPDATE observed_brokers SET in_request_a = 0 WHERE broker = 'AK'",
     (), False),
    ("unexplained_brokers ['AK'] disagree", "UPDATE panel_captures SET unexplained_brokers = '[\"AK\"]' "
                                            "WHERE request_group = 'A'", (), False),
    ("selector_plan 'targeted_actor_v2'", "UPDATE panel_snapshots SET selector_plan = 'targeted_actor_v2'",
     (), False),
    ("horizons and tokens", "UPDATE panel_captures SET selector_tokens = '[]' WHERE request_group = 'B'",
     (), False),
    ("session_index", "UPDATE panel_sessions SET session_index = session_index + 100 "
                      "WHERE session_date = ?", (P_DATES[3],), False),
)


def test_the_stored_structure_is_checked_and_never_rederived():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


# ── 5. read-only ───────────────────────────────────────────────────────────

def test_the_reader_never_writes_and_never_creates():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


# ── 6. determinism ─────────────────────────────────────────────────────────

def test_the_document_is_canonical_and_free_of_paths_machines_and_time():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


# ── 7. the v1 output schema ────────────────────────────────────────────────

_CAPTURE = ("request_group", "horizons", "selector_tokens", "capture_id", "manifest_run_id",
            "attempt", "query_sha256", "http_status", "response_bytes", "response_sha256",
            "response_text_sha256", "captured_at", "vendor_success", "vendor_meta_brokers",
            "expanded_brokers", "unexplained_brokers", "source_status")
_SELECTOR = ("selector_token", "horizon", "metric", "direction", "unit", "request_group",
             "selector_capture_id", "status", "sessions_required", "sessions_available",
             "window_first_session", "window_last_session", "qualifying_count", "member_count",
             "boundary_tie_value", "boundary_tie_brokers", "members")
_REASON = ("selector_token", "horizon", "metric", "direction", "unit", "rank", "tied",
           "window_value", "window_first_session", "window_last_session", "window_sessions",
           "request_group", "selector_capture_id", "union_capture_ids", "provenance")
_SIX = ("blot", "bval", "slot", "sval", "nlot", "nval")
_B = "brokers.<broker>"
# Every key path of a v1 document, written out here so that a schema change
# has to change this test as well as the module.
ALLOWED_PATHS = frozenset(
    ["schema", "schema_version", "contract", "snapshot", "snapshot.ticker", "snapshot.discovery_as_of",
     "source", "source.captures", "basis_reference", "basis_reference.source_id",
     "basis_reference.canonical_json_sha256", "basis_reference.known_conflict_intervals",
     "basis_reference.known_basis_conflict_by_horizon",
     "basis_reference.known_basis_conflict_by_horizon.<horizon>", "series_included", "axis",
     "axis.sessions", "axis.ohlc", "horizons", "horizons.<horizon>", "selectors",
     "observed_brokers", "selected_brokers", "unexplained_brokers", "brokers", _B]
    + [f"contract.{k}" for k in (
        "coverage_scope", "full_universe", "absent_from_observed", "unobserved_is_zero",
        "selected_means", "unexplained_means", "rank_provenance", "qualifying_count_scope",
        "selector_resolution_scope", "horizon_order", "metric_order", "horizons_nested",
        "horizon_anchor", "shares_per_lot", "value_unit", "price_unit", "price_method",
        "basis_absence_means")]
    + [f"source.{k}" for k in (
        "product", "panel_schema_version", "collection_mode", "selector_plan", "investor_type",
        "requested_start_date", "requested_end_date", "run_id", "content_sha256", "recorded_utc")]
    + [f"source.captures[].{k}" for k in _CAPTURE]
    + [f"axis.ohlc.{k}" for k in ("open", "high", "low", "close", "volume", "volume_sma20")]
    + [f"horizons.<horizon>.{k}" for k in ("required_sessions", "available_sessions",
                                           "window_first_session", "window_last_session",
                                           "history_status")]
    + [f"selectors[].{k}" for k in _SELECTOR]
    + [f"selectors[].members[].{k}" for k in ("rank", "broker", "window_value", "tied")]
    + [f"{_B}.{k}" for k in ("returned_by", "nonzero_sessions", "selection_reasons",
                             "horizons_selected", "directions_selected", "units_selected",
                             "membership_by_horizon", "resolved_member_horizons", "windows", "series")]
    + [f"{_B}.selection_reasons[].{k}" for k in _REASON]
    + [f"{_B}.membership_by_horizon.<metric>", f"{_B}.membership_by_horizon.<metric>.<horizon>"]
    + [f"{_B}.membership_by_horizon.<metric>.<horizon>.{k}"
       for k in ("state", "selector_status", "boundary_tie_candidate")]
    + [f"{_B}.resolved_member_horizons.<metric>", f"{_B}.windows.<horizon>",
       f"{_B}.windows.<horizon>.sums"]
    + [f"{_B}.windows.<horizon>.sums.{k}" for k in _SIX]
    + [f"{_B}.windows.<horizon>.{side}" for side in ("gross_buy", "gross_sell")]
    + [f"{_B}.windows.<horizon>.{side}.{k}" for side in ("gross_buy", "gross_sell")
       for k in ("status", "implied_price_from_reported_lots_rp_per_share",
                 "value_without_reported_lots_rp")]
    + [f"{_B}.series.{k}" for k in _SIX + ("coverage",)])

# Interpretive fields that must not exist anywhere in the document.
FORBIDDEN_KEYS = ("owner", "bandar", "retail_class", "smart_money", "signal", "recommendation",
                  "entry", "exit", "market_concentration", "market_share")
HORIZON_MAPS = ("horizons", "windows", "known_basis_conflict_by_horizon")
METRIC_MAPS = ("membership_by_horizon", "resolved_member_horizons")


def key_paths(value, path="", out=None):
    """Every key path of a document, with <broker>, <horizon> and <metric>
    standing for those map keys (each checked to be one)."""
    out = set() if out is None else out
    if isinstance(value, dict):
        last = path.rsplit(".", 1)[-1]
        for key, sub in value.items():
            if path == "brokers":
                assert re.fullmatch(r"[A-Z]{2}", key), key
                seg = "<broker>"
            elif last in HORIZON_MAPS or last == "<metric>":
                assert key in ts.HORIZONS, (path, key)
                seg = "<horizon>"
            elif last in METRIC_MAPS:
                assert key in ts.METRICS, (path, key)
                seg = "<metric>"
            else:
                seg = key
            child = f"{path}.{seg}" if path else seg
            out.add(child)
            key_paths(sub, child, out)
    elif isinstance(value, list):
        for item in value:
            key_paths(item, path + "[]", out)
    return out


def all_keys(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        out.update(value)
        for sub in value.values():
            all_keys(sub, out)
    elif isinstance(value, list):
        for sub in value:
            all_keys(sub, out)
    return out


def test_the_output_schema_is_exactly_the_v1_allowlist():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


# ── 8. the basis reference and shared constants ────────────────────────────

def test_the_basis_reference_follows_broker_book_and_fails_closed():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


# ── 9. hygiene and the CLI ─────────────────────────────────────────────────

def test_imports_are_light_and_the_collector_is_never_loaded():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_the_cli_prints_json_and_fails_without_output():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


# ── 10. source states: only a quiescent source is read ─────────────────────
#
# v1 reads only a quiescent source (no -wal, -shm or -journal beside it: rollback
# journal, or WAL fully checkpointed), always opened with immutable=1; every other
# state is refused before SQLite opens the file. The WAL fixtures are real WAL
# databases: another process commits the newest snapshot through the WAL, then closes
# (checkpointed), exits without closing (the frames stay in -wal beside -shm) or stays
# open. The expected document is the same snapshot read from a plain rollback-journal
# copy, so "correct" means byte-identical to that.

WAL_WRITER = r"""
import os, sqlite3, sys
db, src, finish = sys.argv[1], sys.argv[2], sys.argv[3]
c = sqlite3.connect(db, isolation_level=None)
assert c.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("ATTACH DATABASE ? AS src", (src,))
c.execute("BEGIN")
for table in ("panel_runs", "panel_snapshots", "panel_captures", "panel_sessions",
              "observed_brokers", "observed_series", "selection_status", "selection_membership"):
    c.execute(f"INSERT INTO main.{table} SELECT * FROM src.{table}")
c.execute("COMMIT")
c.execute("DETACH DATABASE src")
if finish == "close":
    c.close()
    sys.exit(0)
if finish == "hold":
    print("ready", flush=True)
    sys.stdin.readline()
os._exit(0)          # never closed: the committed frames stay in -wal, beside -shm
"""

SQL_WRITER = r"""
import os, sqlite3, sys
db, finish, script = sys.argv[1], sys.argv[2], sys.argv[3]
c = sqlite3.connect(db, isolation_level=None)
c.executescript(script)
if finish == "close":
    c.close()
    sys.exit(0)
os._exit(0)
"""


def wal_panel(tmp, finish):
    """(db, expected latest document, holder process or None): a panel whose
    newest snapshot another process committed through WAL, then `finish`ed:
    close, crash or hold (the caller releases a holder)."""
    market = tp.handbuilt_market()
    source, newer = os.path.join(tmp, "source"), os.path.join(tmp, "newer")
    os.makedirs(source)
    os.makedirs(newer)
    db = collect(source, {"AAAA": earlier(market, 1)})["db_path"]
    newer_db = collect(newer, {"AAAA": market})["db_path"]
    expected = tao.observation_json(observe(newer_db, "AAAA"))
    args = [sys.executable, "-c", WAL_WRITER, db, newer_db, finish]
    if finish == "hold":
        holder = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert holder.stdout.readline().strip() == "ready"
        return db, expected, holder
    subprocess.run(args, check=True, timeout=120)
    return db, expected, None


def release(holder):
    holder.stdin.write("\n")
    holder.stdin.flush()
    holder.wait(timeout=60)


def sql_writer(db, finish, script):
    subprocess.run([sys.executable, "-c", SQL_WRITER, db, finish, script], check=True, timeout=120)


def source_files(db):
    """The database directory's file set, and each file's size and sha256."""
    d = os.path.dirname(db)
    out = {}
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                out[name] = (os.path.getsize(path), hashlib.sha256(fh.read()).hexdigest())
        else:
            out[name] = "directory"
    return out


def header_format(db):
    with open(db, "rb") as fh:
        header = fh.read(100)
    return header[18], header[19]            # (1, 1) rollback journal, (2, 2) WAL


def main_file_only(db):
    """The snapshots in the main database file alone, the WAL ignored: the
    test's own immutable read, which creates and changes nothing."""
    conn = sqlite3.connect(pathlib.Path(db).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT discovery_as_of FROM panel_snapshots ORDER BY 1")]
    finally:
        conn.close()


@contextmanager
def recorded_opens():
    """Every database URI sqlite3.connect is given while the block runs."""
    seen, real = [], sqlite3.connect

    def spy(database, *args, **kwargs):
        seen.append(database)
        return real(database, *args, **kwargs)
    sqlite3.connect = spy
    try:
        yield seen
    finally:
        sqlite3.connect = real


def refused_state(db, contains):
    """observe() must raise ReadOnlySourceStateError saying `contains` before
    SQLite opens the file, and leave every file of the directory as it was."""
    before = source_files(db)
    with recorded_opens() as opens:
        try:
            observe(db, "AAAA")
        except tao.ReadOnlySourceStateError as e:
            assert contains in str(e), (contains, str(e))
        else:
            raise AssertionError(f"read an unsupported source state ({contains}); files now "
                                 f"{sorted(source_files(db))}")
    assert opens == [], opens                                        # refused before SQLite opened it
    assert source_files(db) == before, (sorted(before), sorted(source_files(db)))


def read_supported(db, as_of=None):
    """(list_snapshots, the canonical document, the URIs SQLite was given, the
    directory while the connection was open) for a supported source."""
    with recorded_opens() as opens:
        conn = tao.open_readonly(db)
    try:
        listed = [r["discovery_as_of"] for r in tao.list_snapshots(conn)]
        text = tao.observation_json(tao.observe(conn, "AAAA", as_of))
        during = source_files(db)
    finally:
        conn.close()
    return listed, text, opens, during


def test_a_stable_rollback_panel_is_read_immutable_without_sidecars():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_a_checkpointed_wal_panel_is_read_without_creating_sidecars():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_a_wal_with_shm_is_refused_before_sqlite_opens_it():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_a_wal_without_its_shm_is_refused_not_repaired():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_other_unsupported_source_states_are_refused_without_writing():
    with tmpdir() as tmp:
        plain = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]

        def case(name, sidecars=(), wal=False):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            db = os.path.join(d, "panel.db")
            shutil.copyfile(plain, db)
            if wal:
                sql_writer(db, "close", "PRAGMA journal_mode=WAL;")
                assert header_format(db) == (2, 2) and not os.path.exists(db + "-wal")
            for suffix in sidecars:
                with open(db + suffix, "wb") as fh:
                    fh.write(b"\0" * 4096)
            return db
        refused_state(case("rollback_with_wal", ["-wal"]), "-wal present beside the database")
        refused_state(case("rollback_with_shm", ["-shm"]), "-shm present beside the database")
        refused_state(case("wal_shm_only", ["-shm"], wal=True), "-shm present beside the database")
        refused_state(case("wal_with_journal", ["-journal"], wal=True), "-journal present beside the database")
        refused_state(case("rollback_with_journal", ["-journal"]), "-journal present beside the database")
        # a writer that died mid-transaction leaves a hot rollback journal
        hot = case("hot_journal")
        sql_writer(hot, "crash", "PRAGMA cache_size=1; BEGIN; CREATE TABLE spill (x); "
                                 "INSERT INTO spill WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL "
                                 "SELECT i + 1 FROM r WHERE i < 20000) SELECT i FROM r;")
        assert os.path.exists(hot + "-journal") and header_format(hot) == (1, 1)
        refused_state(hot, "-journal present beside the database")
        # an unknown header format
        odd = case("unknown_format")
        with open(odd, "r+b") as fh:
            fh.seek(18)
            fh.write(bytes([3, 3]))
        refused_state(odd, "unknown file format")
    print("  ok test_other_unsupported_source_states_are_refused_without_writing")


def test_a_same_process_writer_keeps_its_sidecars_and_the_reader_refuses():
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
        writer = sqlite3.connect(db, isolation_level=None)
        try:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE panel_runs SET note = 'held by a writer in this process'")
            assert {"panel.db-wal", "panel.db-shm"} <= set(source_files(db))
            refused_state(db, "-wal and -shm present beside the database")   # -shm untouched too
        finally:
            writer.close()
    print("  ok test_a_same_process_writer_keeps_its_sidecars_and_the_reader_refuses")


def test_a_writer_between_inspection_and_open_is_refused_without_sidecar_writes():
    """Codex's H1: the source is inspected as a quiescent rollback-journal file,
    then a writer switches it to WAL before SQLite opens it. The reader must
    neither create nor change a sidecar, and must refuse rather than return data."""
    scripts = {"crash": "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; "
                        "UPDATE panel_runs SET note = 'written between inspection and open';",
               "close": "PRAGMA journal_mode=WAL; "
                        "UPDATE panel_runs SET note = 'written between inspection and open';"}
    for finish, script in scripts.items():
        with tmpdir() as tmp:
            db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
            real, calls, left = tao._source_state, [], {}

            def paused(path):
                state = real(path)
                if not calls:                       # after the first inspection, before the open
                    calls.append(state)
                    sql_writer(db, finish, script)
                    left.update(source_files(db))
                return state
            tao._source_state = paused
            try:
                with recorded_opens() as opens:
                    try:
                        conn = tao.open_readonly(db)        # the check right after the open refuses
                    except tao.ReadOnlySourceStateError as e:
                        assert "changed since it was opened" in str(e), (finish, e)
                    else:
                        conn.close()
                        raise AssertionError(f"{finish}: open_readonly returned a connection to a "
                                             "source that changed between inspection and open")
            finally:
                tao._source_state = real
            after = source_files(db)
        assert calls == [tao.ROLLBACK_JOURNAL], (finish, calls)
        if finish == "crash":
            assert {"panel.db-wal", "panel.db-shm"} <= set(left), (finish, sorted(left))
        assert after == left, (finish, "sidecars created or changed by the reader:",
                               sorted(n for n in set(after) | set(left) if after.get(n) != left.get(n)))
        assert opens and all(u.endswith("?mode=ro&immutable=1") for u in opens), (finish, opens)
    print("  ok test_a_writer_between_inspection_and_open_is_refused_without_sidecar_writes")


def test_a_source_changed_during_a_public_read_is_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_a_source_that_changes_after_it_was_opened_is_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_only_connections_from_open_readonly_are_read():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


def test_a_read_only_directory_changes_nothing():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("targeted_actor_observations.observe")


ALL = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]


def _guarded():
    """What no test may change: the repository's panel database, its manifest
    and the basis reference."""
    state = {"basis_file": tao.BASIS_FILE,
             "manifest": sorted(glob.glob(os.path.join(HERE, tp.ic.MANIFEST_DIR, "*")))}
    for path in (tdb.DB_PATH, tao.BASIS_FILE):
        state[path] = fingerprint(path)[:2] if os.path.exists(path) else None
    return state


def main():
    before = _guarded()
    print(f"targeted actor observations: {len(ALL)} tests\n")
    for fn in ALL:
        fn()
    assert _guarded() == before, "a test changed the repository's panel database, manifest or basis reference"
    print(f"\nAll {len(ALL)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
