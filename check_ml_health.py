"""
Does the ML stack still run, and does it still produce sane numbers?

check_signal_integrity.py guards the DATA. This guards the CODE that consumes
it, against three failure modes that have all already happened in this project
or are one dependency bump away:

  1. Silent breakage. requirements.txt pins nothing, so a pandas or xgboost
     release can change a signature under the repo and every ML script starts
     erroring - or worse, keeps running with different semantics. Importing and
     exercising each module catches that on the day it lands, not on the day
     someone next opens the notebook.

  2. Bad data reaching the model. The panel is rebuilt from a database that a
     live scraper defect is still writing contaminated rows into. Invariants on
     the built panel (no returns outside the IDX limit band, feature NaN rates,
     plausible shape) catch a panel that is technically non-empty but unusable.

  3. Metrics that cannot be true. This repo recorded Sharpe 5.36 and 6.95 from a
     formula applying sqrt(252) to per-trade returns. A number like that is not
     a discovery, it is a bug signature, and it went unchallenged for weeks.
     Stage 3 removed every Sharpe (see signal_metrics.py); what this checks now
     is the honest replacement - whether the top-decile hit rate actually beats
     the universe base rate, and whether IC is distinguishable from zero.

KNOWN-DEFECT BUDGET
-------------------
Defects that are known and scheduled are pinned rather than failed on, because a
check that is permanently red for a known condition trains everyone to skip it.
The count fails only when it GROWS. SQRT252_BUDGET reached its target of 0 when
stage 3 landed; IMPOSSIBLE_TARGET_BUDGET reached 0 when every model target was
routed through price_audit.clean_panel().

Run:  py check_ml_health.py            -> print status
      py check_ml_health.py --telegram -> also send it
      py check_ml_health.py --quick    -> skip the model fit (imports, tests, data)
      py check_ml_health.py --broker-flow-manifest PATH
                                       -> build the panel under that manifest
Exit code is non-zero when unhealthy.

CAPABILITY. A declared unsupported panel/model route must execute its exact
named guard. Health records a structured UNSUPPORTED result and produces no
analytics. Missing guards, stale certificates and other errors fail health.

BROKER FLOW. A supported build_panel() reads broker flow only through
broker_flow_canonical under an explicit manifest for the exact database
(HANDOFF Lampiran V). Without --broker-flow-manifest it refreshes the manifest
first. An unsupported panel route refuses before either manifest or database
access. A refused refresh remains a health problem.
"""

import argparse
import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback

import numpy as np
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "neobdm.db")
#: Where the refresh step writes the manifest it builds (gitignored).
BROKER_FLOW_MANIFEST_OUT = os.path.join(HERE, "backtest_out", "ml_health", "broker_flow_manifest.json")

# Modules that must import and stay importable. ddqn_entry_exit needs torch,
# which is heavy; it is checked but a missing torch downgrades to a note rather
# than failing, so this check stays runnable in a light environment.
CORE_MODULES = ["price_audit", "walk_forward_backtest", "ddqn_episode_data", "strategy_variants",
                "feature_ablation", "multiday_features", "smart_money_divergence",
                "shap_analysis", "kelly_sizing", "ara_arb_simulation",
                "horizon_scan", "evaluate_signals", "ml_v2_experiment_1",
                "ml_v2_experiment_1_robustness", "pattern_type_backtest",
                "foreign_flow_signal_backtest", "regime_gated_momentum",
                "daily_picks", "telegram_inbox",
                # BROKER_LEARNING.md: pure at import (broker_collect imports
                # neobdm_scraper/playwright only inside collect()).
                "broker_book", "broker_rules", "broker_learning", "broker_learning_db",
                "broker_dashboard", "broker_collect", "broker_learning_run",
                "inventory_capture",
                # The targeted actor panel: also pure at import (collect() imports
                # neobdm_scraper/playwright only on its live path).
                "coverage_guard", "targeted_selectors", "targeted_actor_db",
                "targeted_actor_panel"]
OPTIONAL_MODULES = ["ddqn_entry_exit"]

# Panel shape. Wide bands - this catches "the panel collapsed", not drift.
MIN_PANEL_ROWS = 5000
MIN_PANEL_DATES = 150
MIN_PANEL_TICKERS = 30
MAX_FEATURE_NAN = 0.15

# IDX daily limits. ara_bound() in price_audit.py is price-tiered (0.20/0.25/
# 0.35); ARA_MAX pins the widest tier so a single global ceiling can be used
# here without joining back to the price that set each row's own tier.
ARA_MAX = 0.35
ARB_MIN = -0.15
LIMIT_TOLERANCE = 0.01

# walk_forward_backtest.build_panel() sources the executable contract:
# clean_panel(horizons=(1,), open_anchored=True) -> panel["target"] = fwd_oo_1
# = open(T+1) -> open(T+2). That is NOT a single-session return, so the plain
# [ARB_MIN, ARA_MAX] band is the wrong test for it. price_audit's oo_valid mask
# chains THREE ARA/ARB-bounded transitions to build fwd_oo_1:
#   entry  open(T+1)  vs close(T)
#   step   close(T+1) vs close(T)
#   exit   open(T+2)  vs close(T+1)
# A legitimate value can legally exceed the single-session band (e.g. entry
# pinned at ARB_MIN off close(T), exit pinned at ARA_MAX twice-compounded off
# close(T) via close(T+1)) — that is not contamination. The bound below is
# DERIVED by composing the same ARA_MAX/ARB_MIN primitives across
# TARGET_HORIZON + 1 chained transitions; it is not a separately chosen,
# arbitrary widened constant. See price_audit.add_forward_returns()'s oo_valid
# construction and test_pipeline.py::test_open_anchored_labels_match_hand_computed_values.
TARGET_HORIZON = 1  # must track build_panel()'s clean_panel(horizons=(1,), ...)


def _executable_target_bounds(horizon=TARGET_HORIZON):
    """Max/min feasible fwd_oo_{horizon}, derived by chaining ARA_MAX/ARB_MIN
    across `horizon + 1` transitions (entry, `horizon` closes, exit — each an
    independent extremal draw off the same close(T) origin)."""
    hi = (1 + ARA_MAX) ** (horizon + 1) / (1 + ARB_MIN) - 1
    lo = (1 + ARB_MIN) ** (horizon + 1) / (1 + ARA_MAX) - 1
    return lo, hi

# See KNOWN-DEFECT BUDGET above. Both ratchet down, never up.
#   2026-08-20  4   initial pin
#   2026-08-23  0   stage 3 landed: all four sites replaced by signal_metrics.py
SQRT252_BUDGET = 0
# Ratchet log - never raise this:
#   2026-08-20  88   initial pin
#   2026-08-21  82   an unchanged rerun of backfill_inventory.py healed 899 of
#                    1,400 contaminated rows overnight (CDIA and COIN went from
#                    231 bad rows each to 0) and broke zero new ones
#   2026-08-30   0   all model targets now source clean_panel() with gap guards
IMPOSSIBLE_TARGET_BUDGET = 0
# SHARPE_IMPLAUSIBLE is gone with stage 3: no code path emits a Sharpe to
# sanity-check any more. What replaces it is the hit-edge/IC check below.


def _load_dotenv():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# ── checks ────────────────────────────────────

def check_imports(problems, notes, stats):
    """Import every module; fall back to a compile check where importing needs
    something this check has no business requiring.

    Only the optional Torch dependency permits a compile-only result. A core
    module's import/runtime error is a health failure.
    """
    ok, compiled = 0, []
    for mod in CORE_MODULES + OPTIONAL_MODULES:
        try:
            __import__(mod)
            ok += 1
        except ModuleNotFoundError as e:
            if mod not in OPTIONAL_MODULES or e.name != "torch":
                problems.append(f"{mod} fails to import: {type(e).__name__}: {e}")
                continue
            path = os.path.join(HERE, f"{mod}.py")
            try:
                # builtin compile(), not py_compile: syntax-checks the source
                # without writing a .pyc anywhere.
                compile(open(path, encoding="utf-8").read(), path, "exec")
                compiled.append(f"{mod} ({type(e).__name__})")
            except (OSError, SyntaxError) as ce:
                problems.append(f"{mod} does not even compile: {ce}")
        except Exception as e:
            problems.append(f"{mod} fails to import: {type(e).__name__}: {e}")

    stats["modules_ok"] = ok
    stats["modules_compiled_only"] = len(compiled)
    if compiled:
        notes.append(f"{len(compiled)} module(s) compile-checked only, not imported "
                     f"(missing optional Torch dependency): "
                     f"{', '.join(compiled)}")


def check_unit_tests(problems, stats):
    """test_pipeline.py holds the leakage and gap-guard invariants. If those
    regress, every downstream number is void, so this runs first among the
    behavioural checks.

    test_experiment_1f_phase2.py holds the #1F candidate data contract: strict
    source domain, detection-is-not-authorisation, regime segmentation and exact
    factor arithmetic. It lives in its own file so a candidate rule can never be
    satisfied by relaxing a production test, and it runs here so the separation
    does not become an excuse for it to stop running.

    test_inventory_evidence.py guards finite-anchor flow, coverage, revision and
    availability semantics. It runs in a separate process, including in --quick.

    test_bandarmolony_trade_capture.py checks offline trade normalization, private
    output, immutable captures, and verification. It also runs in --quick.

    PASS, FAIL, SKIP and UNAVAILABLE stay separate. A check that returned early
    because an optional artifact is absent ("N skipped: name: why") is
    UNAVAILABLE, never a pass. Every listed suite is mandatory: one that ran
    and passed nothing is a mandatory UNAVAILABLE and blocks any PASS claim.
    """
    passed, skipped, unavailable, mandatory, results = 0, 0, [], [], []
    for name in ("test_pipeline.py", "test_experiment_1f_phase2.py", "test_daily_picks.py",
                 "test_broker_book.py", "test_broker_rules.py", "test_broker_learning.py",
                 "test_broker_dashboard.py", "test_broker_collect.py",
                 "test_broker_learning_run.py", "test_inventory_capture.py",
                 "test_targeted_actor_panel.py", "test_arb_veto.py",
                 "test_targeted_actor_observations.py", "test_inventory_evidence.py",
                 "test_bandarmolony_trade_capture.py",
                 "test_morning.py", "test_ml_health.py"):
        r = subprocess.run([sys.executable, os.path.join(HERE, name)],
                           capture_output=True, text=True, cwd=HERE, timeout=900)
        unittest_summary = re.search(r"^Ran (\d+) tests? in ", r.stderr, flags=re.M)
        script_summary = re.search(r"^All (\d+) tests? (?:passed|OK)\b\.?(?: \((\d+) skipped: (.*)\))?$",
                                   r.stdout, flags=re.M)
        executed, gated, suite_skips = 0, [], 0
        if r.returncode == 0:
            if unittest_summary:
                found = re.search(r"\bskipped=(\d+)", r.stderr)
                suite_skips = int(found.group(1)) if found else 0
                executed = int(unittest_summary.group(1)) - suite_skips
            elif script_summary:
                gated = [f"{name}: {item}" for item in (script_summary.group(3) or "").split("; ") if item]
                executed = int(script_summary.group(1)) - len(gated)
            else:
                executed = r.stdout.count(" passed") + r.stdout.count("  ok ")
            status = "PASS" if executed > 0 else "UNAVAILABLE"
            if status == "UNAVAILABLE":
                mandatory.append(f"{name}: no test executed")
        else:
            status = "FAIL"
            tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
            problems.append(f"{name} FAILED — " + " | ".join(tail))
        passed += executed if status == "PASS" else 0
        skipped += suite_skips
        unavailable.extend(gated)
        results.append({"suite": name, "status": status, "passed": executed if status == "PASS" else 0,
                        "skipped": suite_skips, "unavailable": len(gated)})
    stats.update(tests_passed=passed, tests_skipped=skipped, tests_unavailable=unavailable,
                 suite_results=results)
    stats["mandatory_unavailable"] = stats.get("mandatory_unavailable", []) + mandatory


def _expects_refusal(consumer):
    """The ledger declares capability; only an executed guard proves refusal."""
    from price_contract import CONTRACT_VERSION
    with open(os.path.join(HERE, "corporate_action_consumer_routes.json"), encoding="utf-8") as f:
        ledger = json.load(f)
    if ledger.get("contract_version") != CONTRACT_VERSION or ledger.get("mode") != "EXPLICIT_REFUSAL":
        raise ValueError("Invalid corporate-action consumer capability ledger")
    module, name = consumer.rsplit(".", 1)
    return name in ledger.get("routes", {}).get(module, [])


def _check_refusal(consumer, invoke, problems, stats):
    """Exercise a declared guard without source inputs or persistent output.

    A bare UnsupportedPriceContract or a refusal from another consumer is a
    failure. It can represent stale inputs or an unexpected downstream error.
    """
    from price_contract import CONTRACT_VERSION, UnsupportedPriceContract
    try:
        invoke()
    except UnsupportedPriceContract as exc:
        if (exc.consumer == consumer and exc.status == "UNSUPPORTED"
                and exc.contract_version == CONTRACT_VERSION):
            stats.setdefault("unsupported_routes", []).append(exc.as_dict())
        else:
            problems.append(f"{consumer} raised an unexpected contract refusal: {exc}")
    except Exception as exc:
        problems.append(f"{consumer} guard failed: {type(exc).__name__}: {exc}")
    else:
        problems.append(f"{consumer} returned output despite its declared unsupported contract")


def refresh_broker_flow_manifest(problems, stats, out=BROKER_FLOW_MANIFEST_OUT):
    """Step 1 of 2: the evidence manifest for neobdm.db, by the refresh tool
    (HANDOFF Lampiran U). Returns its path, or None after recording why not."""
    import broker_flow_manifest_refresh as bfmr

    try:
        manifest, _ = bfmr.refresh(DB_PATH, out)
    except Exception as e:
        problems.append(f"broker_flow manifest refresh refused: {type(e).__name__}: {e}")
        traceback.print_exc()
        return None
    stats["broker_flow_refreshed"] = True
    return out


def check_panel(problems, notes, stats, broker_flow_manifest=None):
    """Build the real training panel and assert it is usable."""
    from walk_forward_backtest import build_panel, FEATURES

    # Do this before refreshing a manifest or opening the database. The
    # unavailable consumer must prove that it refuses without reading inputs.
    consumer = "walk_forward_backtest.build_panel"
    if _expects_refusal(consumer):
        _check_refusal(consumer, lambda: build_panel(None, broker_flow_db_path=None,
                       broker_flow_manifest_path=None), problems, stats)
        return None

    if broker_flow_manifest is None:
        broker_flow_manifest = refresh_broker_flow_manifest(problems, stats)
        if broker_flow_manifest is None:
            return None
    conn = None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        panel = build_panel(conn, broker_flow_db_path=DB_PATH,
                            broker_flow_manifest_path=broker_flow_manifest)
    except Exception as e:
        problems.append(f"build_panel() raised {type(e).__name__}: {e}")
        traceback.print_exc()
        return None
    finally:
        if conn is not None:
            conn.close()

    bf = panel.attrs["broker_flow"]
    stats["broker_flow"] = (f"db {bf['db_sha256'][:12]} manifest {bf['manifest_sha256'][:12]} "
                            f"({bf['manifest_snapshot']}"
                            f"{', refreshed this run' if stats.get('broker_flow_refreshed') else ''})"
                            f", quarantined {bf['quarantined_sessions']}")
    stats["panel"] = f"{len(panel)} rows / {panel['date'].nunique()}d / {panel['ticker'].nunique()}t"
    if len(panel) < MIN_PANEL_ROWS:
        problems.append(f"panel collapsed to {len(panel)} rows (expected >{MIN_PANEL_ROWS})")
    if panel["date"].nunique() < MIN_PANEL_DATES:
        problems.append(f"panel has only {panel['date'].nunique()} dates")
    if panel["ticker"].nunique() < MIN_PANEL_TICKERS:
        problems.append(f"panel has only {panel['ticker'].nunique()} tickers")

    t = panel["target"].dropna()
    if t.empty:
        problems.append("panel target is entirely NaN")
        return panel

    lo, hi = _executable_target_bounds()
    impossible = ((t > hi + LIMIT_TOLERANCE) | (t < lo - LIMIT_TOLERANCE)).sum()
    stats["impossible_targets"] = int(impossible)
    stats["target_kurtosis"] = round(float(t.kurt()), 1)
    if impossible > IMPOSSIBLE_TARGET_BUDGET:
        worst = t.abs().nlargest(3).tolist()
        problems.append(
            f"{impossible} target(s) outside the fwd_oo_{TARGET_HORIZON} feasible "
            f"band [{lo:+.1%}, {hi:+.1%}] (derived by chaining ARA/ARB across "
            f"{TARGET_HORIZON + 1} transitions), budget is "
            f"{IMPOSSIBLE_TARGET_BUDGET} (worst "
            f"{', '.join(f'{v*100:+.0f}%' for v in worst)}) — contamination is "
            f"GROWING. The scraper defect is writing new bad rows; see "
            f"HANDOFF.md stage 2.")
    elif impossible:
        notes.append(f"{impossible} target(s) outside the pinned limit budget")

    # Base rate belongs next to any hit_rate that gets quoted. Recorded here so
    # a model that merely reproduces it cannot look like a finding.
    stats["base_rate"] = round(float((t > 0).mean()), 3)

    nan_hot = [f"{f} {panel[f].isna().mean():.0%}" for f in FEATURES
               if panel[f].isna().mean() > MAX_FEATURE_NAN]
    if nan_hot:
        problems.append(f"feature(s) mostly missing: {', '.join(nan_hot)}")
    return panel


def check_model_runs(panel, problems, notes, stats):
    """One real walk-forward cycle end to end: does it fit, and are the outputs
    finite? Cheaper than the full backtest, catches the same breakage."""
    from price_contract import refuse_unmigrated
    refuse_unmigrated("check_ml_health.check_model_runs")
    from walk_forward_backtest import run_walk_forward

    if panel is None or panel.empty:
        notes.append("model smoke test skipped — no panel")
        return
    train_min, test_window = 30, 6
    dates = sorted(panel["date"].unique())[-60:]
    if len(dates) < train_min + test_window:
        notes.append(f"model smoke test skipped — only {len(dates)} dates, need "
                     f"{train_min + test_window}")
        return

    slice_ = panel[panel["date"].isin(dates)]
    try:
        cycles, pooled, _ = run_walk_forward(slice_, train_min=train_min,
                                             test_window=test_window)
    except Exception as e:
        problems.append(f"run_walk_forward() raised {type(e).__name__}: {e}")
        traceback.print_exc()
        return

    # cycles is a DataFrame, so test length rather than truthiness.
    if len(cycles) == 0:
        problems.append(f"run_walk_forward() produced no cycles on a {len(dates)}-day slice")
        return

    stats["cycles"] = len(cycles)

    def _r(key, nd=3):
        v = pooled.get(key)
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), nd)

    stats["pooled_ic"] = _r("ic")
    stats["daily_ic"] = _r("daily_ic")
    stats["pooled_hit"] = _r("top_hit")
    stats["pooled_hit_edge"] = _r("top_hit_edge")
    stats["pooled_edge"] = _r("edge", 4)

    # The number that actually answers "is the signal any good". A top-decile
    # hit rate means nothing next to a base rate it matches - this repo reported
    # 42.8% for months while the base rate was also 42.8%.
    if stats["pooled_hit_edge"] is not None and abs(stats["pooled_hit_edge"]) < 0.005:
        notes.append(
            f"top-decile hit {stats['pooled_hit']:.1%} matches the universe base "
            f"rate {pooled.get('base_rate', float('nan')):.1%} to within 0.5pp — "
            f"the model is adding no directional information.")
    if stats["pooled_ic"] is not None and abs(stats["pooled_ic"]) < 0.02:
        notes.append(f"IC {stats['pooled_ic']:+.3f} is indistinguishable from zero.")


def _sqrt252_sites():
    """Real annualisation CALLS, found via the AST.

    A plain text search does not work here: this repo discusses the defect in
    prose extensively, so docstrings in walk_forward_backtest.py, horizon_scan.py
    and this file all mention it. Parsing means only executable code counts, and
    the description of a bug never registers as the bug.

    Matches any sqrt(...) whose argument contains the literal 252 anywhere in
    its expression tree -- so sqrt(252), sqrt(252 / hold_days) and
    sqrt(n * 252) all count. Rescaling the constant repairs only the
    dimensional half of HANDOFF.md TEMUAN 2 and leaves the independence half
    untouched, so a "repaired" site is still a site. Kept narrow: only a
    sqrt() argument is examined, and only the constant 252, so sqrt(5) /
    sqrt(10) normalisations and a bare 252 elsewhere never register.

    Must stay in step with test_pipeline.py::_sqrt_252_call_sites.
    """
    listed = subprocess.run(
        ["git", "ls-files", "--", "*.py"], cwd=HERE,
        capture_output=True, text=True, check=False,
    )
    files = listed.stdout.splitlines() if listed.returncode == 0 else [
        fn for fn in os.listdir(HERE) if fn.endswith(".py")
    ]
    hits = []
    for fn in sorted(files):
        if fn.replace("\\", "/") == os.path.basename(__file__):
            continue
        try:
            tree = ast.parse(open(os.path.join(HERE, fn), encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) != 1:
                continue
            fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                     else getattr(node.func, "id", None))
            if fname != "sqrt":
                continue
            for sub in ast.walk(node.args[0]):
                if (isinstance(sub, ast.Constant) and not isinstance(sub.value, bool)
                        and sub.value == 252):
                    hits.append(f"{fn}:{node.lineno}")
                    break
    return hits


def check_known_defects(problems, notes, stats):
    """Pinned counts for defects that are known and scheduled. Fails on a NEW
    occurrence, not on the existing backlog."""
    hits = _sqrt252_sites()
    stats["sqrt252"] = len(hits)
    if len(hits) > SQRT252_BUDGET:
        problems.append(
            f"sqrt(252)-on-per-trade-returns appears {len(hits)} times, budget is "
            f"{SQRT252_BUDGET} — a new one was added at {', '.join(hits[SQRT252_BUDGET:])}")
    elif hits:
        notes.append(f"{len(hits)} known sqrt(252) site(s) outstanding "
                     f"({', '.join(hits)}) — HANDOFF.md stage 3")


# ── reporting ─────────────────────────────────

def check(quick=False, broker_flow_manifest=None):
    problems, notes, stats = [], [], {}
    check_imports(problems, notes, stats)
    check_known_defects(problems, notes, stats)
    check_unit_tests(problems, stats)
    panel = check_panel(problems, notes, stats, broker_flow_manifest)
    if quick:
        notes.append("--quick: model smoke test skipped")
    else:
        consumer = "check_ml_health.check_model_runs"
        if _expects_refusal(consumer):
            _check_refusal(consumer, lambda: check_model_runs(panel, problems, notes, stats),
                           problems, stats)
        else:
            try:
                check_model_runs(panel, problems, notes, stats)
            except Exception as exc:
                problems.append(f"{consumer} failed: {type(exc).__name__}: {exc}")
    return problems, notes, stats


def format_report(problems, notes, stats):
    mandatory = stats.get("mandatory_unavailable") or []
    if problems:
        head = "🔴 ML HEALTH FAILED"
    elif mandatory:
        head = ("🟠 ML HEALTH INCOMPLETE — mandatory check(s) UNAVAILABLE; no PASS claim: "
                + "; ".join(mandatory[:4]))
    else:
        head = "🟢 ML health OK"
    lines = [head]

    bits = []
    if "modules_ok" in stats:
        bits.append(f"{stats['modules_ok']} modules import")
    if "tests_passed" in stats:
        bits.append(f"{stats['tests_passed']} tests pass")
    if stats.get("tests_skipped"):
        bits.append(f"{stats['tests_skipped']} skipped")
    if stats.get("tests_unavailable"):
        bits.append(f"{len(stats['tests_unavailable'])} optional check(s) UNAVAILABLE")
    if "panel" in stats:
        bits.append(f"panel {stats['panel']}")
    if bits:
        lines.append(" | ".join(bits))
    for refusal in stats.get("unsupported_routes", []):
        lines.append(f"Expected {refusal['status']}: {refusal['consumer']} "
                     f"({refusal['contract_version']}); no analytics produced")
    if "broker_flow" in stats:
        from walk_forward_backtest import PIT_WARNING
        lines.append(f"broker flow: {stats['broker_flow']}")
        lines.append(f"⚠️ {PIT_WARNING}")

    m = []
    if stats.get("pooled_ic") is not None:
        m.append(f"IC {stats['pooled_ic']:+.3f}")
    if stats.get("daily_ic") is not None:
        m.append(f"daily IC {stats['daily_ic']:+.3f}")
    if stats.get("pooled_hit") is not None:
        m.append(f"top-hit {stats['pooled_hit']:.1%}")
    if stats.get("pooled_hit_edge") is not None:
        m.append(f"edge {stats['pooled_hit_edge']:+.1%}")
    if stats.get("base_rate") is not None:
        m.append(f"base {stats['base_rate']:.1%}")
    if "impossible_targets" in stats:
        m.append(f"impossible targets {stats['impossible_targets']}")
    if m:
        lines.append(" | ".join(m))

    for p in problems:
        lines.append(f"❌ {p}")
    for n in notes:
        lines.append(f"⚠️ {n}")
    if problems:
        lines += ["", 'Tell Claude: "check_ml_health.py is failing with the above."']
    return "\n".join(lines)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — not sending")
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": message,
                            "disable_web_page_preview": True}, timeout=15)
    print("sent to Telegram" if r.ok else f"telegram error {r.status_code}: {r.text}")


def parse_args(argv=None):
    """Strict: unknown options and abbreviations are errors. An explicit
    --broker-flow-manifest (either form, even empty) is never treated as
    omitted; only a missing flag lets check_panel refresh one."""
    ap = argparse.ArgumentParser(description="Does the ML stack still run, and does it still "
                                             "produce sane numbers?", allow_abbrev=False)
    ap.add_argument("--quick", action="store_true", help="skip the model fit")
    ap.add_argument("--telegram", action="store_true", help="also send the report on problems")
    ap.add_argument("--broker-flow-manifest", default=None, metavar="PATH",
                    help="build the panel under this manifest; without it the check refreshes "
                         "one for neobdm.db first")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.telegram:
        _load_dotenv()
    problems, notes, stats = check(quick=args.quick,
                                   broker_flow_manifest=args.broker_flow_manifest)
    report = format_report(problems, notes, stats)
    print(report)
    # Quiet when healthy: Telegram only hears about problems or missing checks.
    incomplete = bool(stats.get("mandatory_unavailable"))
    if args.telegram and (problems or incomplete):
        send_telegram(report)
    sys.exit(1 if problems else 2 if incomplete else 0)


if __name__ == "__main__":
    main()
