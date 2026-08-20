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

  3. Metrics that cannot be true. This repo has recorded Sharpe 5.36 and 6.95
     from a formula applying sqrt(252) to per-trade returns. A number like that
     is not a discovery, it is a bug signature, and it went unchallenged for
     weeks. Anything implausible is surfaced here rather than celebrated.

KNOWN-DEFECT BUDGET
-------------------
HANDOFF.md stage 3 has not been done yet, so the sqrt(252) defect is still
present in four files. Failing the build on it would leave this check
permanently red, which trains everyone to ignore it. Instead the count is
pinned: the existing four are reported as outstanding, and the check fails only
if a FIFTH appears. Lower SQRT252_BUDGET as stage 3 removes them - it is
supposed to ratchet down to zero.

Run:  py check_ml_health.py            -> print status
      py check_ml_health.py --telegram -> also send it
      py check_ml_health.py --quick    -> skip the model fit (imports + data only)
Exit code is non-zero when unhealthy.
"""

import ast
import os
import sqlite3
import subprocess
import sys
import traceback

import numpy as np
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "neobdm.db")

# Modules that must import and stay importable. ddqn_entry_exit needs torch,
# which is heavy; it is checked but a missing torch downgrades to a note rather
# than failing, so this check stays runnable in a light environment.
CORE_MODULES = ["price_audit", "walk_forward_backtest", "strategy_variants",
                "feature_ablation", "multiday_features", "smart_money_divergence",
                "shap_analysis", "kelly_sizing", "ara_arb_simulation",
                "horizon_scan", "evaluate_signals"]
OPTIONAL_MODULES = ["ddqn_entry_exit"]

# Panel shape. Wide bands - this catches "the panel collapsed", not drift.
MIN_PANEL_ROWS = 5000
MIN_PANEL_DATES = 150
MIN_PANEL_TICKERS = 30
MAX_FEATURE_NAN = 0.15

# IDX daily limits. A target outside this band is arithmetically impossible on a
# real listing, so its presence means bad prices reached the panel.
ARA_MAX = 0.35
ARB_MIN = -0.15
LIMIT_TOLERANCE = 0.01

# See KNOWN-DEFECT BUDGET above. Both ratchet down, never up.
SQRT252_BUDGET = 4
# build_panel() still reads price_history directly, so contaminated rows reach
# the panel and 88 targets land outside the IDX limit band. Swapping it to
# price_audit.clean_panel() (HANDOFF.md stage 1) takes this to 0. Pinned rather
# than merely reported so that contamination getting WORSE still fails the
# build, instead of hiding inside a number that was already red.
IMPOSSIBLE_TARGET_BUDGET = 88
SHARPE_IMPLAUSIBLE = 4.0


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

    Some modules cannot be imported in a bare ML environment through no fault of
    their own: smart_money_divergence pulls broker constants from
    neobdm_scraper, which imports playwright AND calls _require_env() for the
    NeoBDM login at module scope, and ddqn_entry_exit needs torch. Rather than
    hand this check the scraper's credentials, those degrade to a syntax/compile
    check, which still catches the breakage this exists to find.
    """
    ok, compiled = 0, []
    for mod in CORE_MODULES + OPTIONAL_MODULES:
        try:
            __import__(mod)
            ok += 1
        except (ImportError, RuntimeError) as e:
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
                     f"(missing optional dependency or scraper credentials): "
                     f"{', '.join(compiled)}")


def check_unit_tests(problems, stats):
    """test_pipeline.py holds the leakage and gap-guard invariants. If those
    regress, every downstream number is void, so this runs first among the
    behavioural checks."""
    r = subprocess.run([sys.executable, os.path.join(HERE, "test_pipeline.py")],
                       capture_output=True, text=True, cwd=HERE, timeout=900)
    passed = r.stdout.count(" passed")
    stats["tests_passed"] = passed
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
        problems.append("test_pipeline.py FAILED — " + " | ".join(tail))


def check_panel(problems, notes, stats):
    """Build the real training panel and assert it is usable."""
    from walk_forward_backtest import build_panel, FEATURES

    conn = sqlite3.connect(DB_PATH)
    try:
        panel = build_panel(conn)
    except Exception as e:
        problems.append(f"build_panel() raised {type(e).__name__}: {e}")
        traceback.print_exc()
        return None
    finally:
        conn.close()

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

    impossible = ((t > ARA_MAX + LIMIT_TOLERANCE) | (t < ARB_MIN - LIMIT_TOLERANCE)).sum()
    stats["impossible_targets"] = int(impossible)
    stats["target_kurtosis"] = round(float(t.kurt()), 1)
    if impossible > IMPOSSIBLE_TARGET_BUDGET:
        worst = t.abs().nlargest(3).tolist()
        problems.append(
            f"{impossible} target(s) outside the IDX limit band, budget is "
            f"{IMPOSSIBLE_TARGET_BUDGET} (worst "
            f"{', '.join(f'{v*100:+.0f}%' for v in worst)}) — contamination is "
            f"GROWING. The scraper defect is writing new bad rows; see "
            f"HANDOFF.md stage 2.")
    elif impossible:
        notes.append(
            f"{impossible} target(s) outside the IDX limit band (within the "
            f"pinned budget of {IMPOSSIBLE_TARGET_BUDGET}) — build_panel() still "
            f"reads price_history directly. Swap it to price_audit.clean_panel() "
            f"and this goes to 0; HANDOFF.md stage 1.")

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

    sharpe = pooled.get("sharpe")
    stats["cycles"] = len(cycles)
    stats["pooled_sharpe"] = None if sharpe is None or np.isnan(sharpe) else round(float(sharpe), 2)
    stats["pooled_hit"] = (None if pooled.get("hit_rate") is None
                           or np.isnan(pooled["hit_rate"]) else round(float(pooled["hit_rate"]), 3))

    if sharpe is not None and not np.isnan(sharpe) and abs(sharpe) > SHARPE_IMPLAUSIBLE:
        notes.append(
            f"pooled Sharpe {sharpe:.2f} is outside anything a real strategy "
            f"sustains (>{SHARPE_IMPLAUSIBLE}). With no transaction costs and "
            f"sqrt(252) applied to per-trade returns, read it as a metric bug, "
            f"not an edge — HANDOFF.md stage 3.")

    if stats["pooled_hit"] is not None and stats.get("base_rate") is not None:
        edge = stats["pooled_hit"] - stats["base_rate"]
        stats["hit_vs_base"] = round(edge, 3)
        if abs(edge) < 0.005:
            notes.append(
                f"hit_rate {stats['pooled_hit']:.1%} matches the universe base "
                f"rate {stats['base_rate']:.1%} to within 0.5pp — the model is "
                f"adding no directional information.")


def _sqrt252_sites():
    """Real sqrt(252) CALLS, found via the AST.

    A plain text search does not work here: this repo discusses the defect in
    prose extensively, so docstrings in walk_forward_backtest.py, horizon_scan.py
    and this file all mention it. Parsing means only executable code counts, and
    the description of a bug never registers as the bug.
    """
    hits = []
    for fn in sorted(os.listdir(HERE)):
        if not fn.endswith(".py") or fn == os.path.basename(__file__):
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
            arg = node.args[0]
            if fname == "sqrt" and isinstance(arg, ast.Constant) and arg.value == 252:
                hits.append(f"{fn}:{node.lineno}")
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

def check(quick=False):
    problems, notes, stats = [], [], {}
    check_imports(problems, notes, stats)
    check_known_defects(problems, notes, stats)
    check_unit_tests(problems, stats)
    panel = check_panel(problems, notes, stats)
    if quick:
        notes.append("--quick: model smoke test skipped")
    else:
        check_model_runs(panel, problems, notes, stats)
    return problems, notes, stats


def format_report(problems, notes, stats):
    head = "🔴 ML HEALTH FAILED" if problems else "🟢 ML health OK"
    lines = [head]

    bits = []
    if "modules_ok" in stats:
        bits.append(f"{stats['modules_ok']} modules import")
    if "tests_passed" in stats:
        bits.append(f"{stats['tests_passed']} tests pass")
    if "panel" in stats:
        bits.append(f"panel {stats['panel']}")
    if bits:
        lines.append(" | ".join(bits))

    m = []
    if stats.get("pooled_sharpe") is not None:
        m.append(f"Sharpe {stats['pooled_sharpe']}")
    if stats.get("pooled_hit") is not None:
        m.append(f"hit {stats['pooled_hit']:.1%}")
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


def main():
    _load_dotenv()
    problems, notes, stats = check(quick="--quick" in sys.argv)
    report = format_report(problems, notes, stats)
    print(report)
    if "--telegram" in sys.argv:
        send_telegram(report)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
