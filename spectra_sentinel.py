"""Read-only fleet health Sentinel for the DailyScraper GitHub Actions collectors.

Records whether every declared GitHub workflow runs when expected, whether the
authoritative price_history is fresh, and which external feeds cannot be
certified. Never restarts, re-enables, scrapes, modifies source, or trades.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone, date
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib import request, parse, error
from zoneinfo import ZoneInfo

from ticker_registry import load_registry
from sentinel_evidence import check_price_evidence, check_receipt

WIB = ZoneInfo("Asia/Jakarta")

# filename: (role, expected_state, allowed_age_hours)
# Paused broker-learning is an intentional project policy, NOT a healthy collector.
POLICY = {
    "daily-scrape.yml": ("capture", "scheduled", 48),
    "price-history-topup.yml": ("capture", "scheduled", 48),
    "ownership-capture.yml": ("capture", "scheduled", 48),
    "capture-health.yml": ("health", "scheduled", 48),
    "signal-integrity.yml": ("health", "scheduled", 48),
    "ml-health.yml": ("model_health", "scheduled", 48),
    "telegram-inbox.yml": ("notifications", "scheduled", 16),
    "arb-veto.yml": ("auxiliary_capture", "scheduled", 216),
    "signal-eval.yml": ("signal_health", "scheduled", 216),
    "broker-learning-daily.yml": ("research", "paused", None),
    "broker-learning-weekly.yml": ("research", "paused", None),
    "ml-daily-report.yml": ("manual", "manual_only", None),
}
EXTERNAL = {
    "stockbit_broker_distribution": "WAITING_AUTHORIZED_CAPTURE_METHOD",
    "bandarmolony_done_detail": "WAITING_AUTHORIZED_SOURCE",
    "jason_windows_collector": "NOT_OBSERVABLE_FROM_GITHUB",
}

class SentinelError(Exception):
    pass

def github_get(url: str):
    if not url.startswith("https://api.github.com/repos/"):
        raise SentinelError("INVALID_GITHUB_URL")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Spectra-Sentinel/1",
    }
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with request.urlopen(request.Request(url, headers=headers), timeout=20) as response:
            return json.load(response)
    except (error.URLError, TimeoutError, ValueError):
        # Deliberately do NOT echo URLs, headers, token or API exception contents.
        raise SentinelError("GITHUB_API_READ_FAILED") from None

def timestamp(s: str) -> datetime:
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise SentinelError("UNZONED_ACTION_TIMESTAMP")
    return dt

def check_price_history(db_path: Path, today: date, registry=None) -> dict:
    return check_price_evidence(db_path, today, registry=registry)

def check_workflow(get, base, name, policy, meta, now):
    role, expected, max_age = policy
    result = dict(name=name, role=role, expected=expected, state=meta.get("state") if meta else "MISSING",
                  severity="CRITICAL", status="UNKNOWN", latest_run=None, last_conclusion=None, run_url=None)
    if not meta:
        result["status"] = "WORKFLOW_NOT_REGISTERED"
        return result
    if expected == "paused":
        state = meta.get("state")
        result.update(severity="INFO" if state == "disabled_manually" else "WARNING",
                      status="INTENTIONALLY_PAUSED" if state == "disabled_manually" else "UNEXPECTED_PAUSED_WORKFLOW_STATE")
        return result
    if expected == "manual_only":
        result.update(severity="INFO", status="MANUAL_ONLY_NO_SCHEDULE_SLA")
        return result
    if meta.get("state") != "active":
        result["status"] = "REQUIRED_WORKFLOW_DISABLED"
        return result
    try:
        data = get(f"{base}/actions/workflows/{int(meta['id'])}/runs?per_page=40")
        all_runs = data.get("workflow_runs", [])
        runs = [r for r in all_runs if r.get("event") == "schedule"]
        if not isinstance(runs, list):
            raise SentinelError("INVALID_RUNS_SCHEMA")
    except (SentinelError, ValueError, KeyError, TypeError):
        result["status"] = "RUN_HISTORY_UNAVAILABLE"
        return result
    if not runs:
        result["status"] = "NEVER_RAN_ON_CRON"
        return result
    run = runs[0]
    result["latest_run"] = run.get("created_at")
    result["last_conclusion"] = run.get("conclusion")
    result["run_url"] = run.get("html_url")
    try:
        age = (now - timestamp(run["created_at"])).total_seconds() / 3600
    except (SentinelError, ValueError, KeyError):
        result["status"] = "INVALID_RUN_TIMESTAMP"
        return result
    result.update(age_hours=round(age, 1), max_age_hours=max_age)
    if run.get("status") == "completed" and run.get("conclusion") != "success":
        result["status"] = "LATEST_SCHEDULED_RUN_FAILED"
    elif age < -2:
        result["status"] = "FUTURE_RUN_TIMESTAMP"
    elif age > max_age:
        result["status"] = "STALE_SCHEDULED_RUN"
    elif run.get("status") != "completed":
        result.update(severity="WARNING", status="SCHEDULED_RUN_IN_PROGRESS")
    else:
        result.update(severity="OK", status="RECENT_SCHEDULED_SUCCESS")
    return result

def audit(get, repo: str, now: datetime, db_path: Path, *, registry=None,
          receipts_dir=None, artifact_root=None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise SentinelError("INVALID_REPOSITORY")
    base = "https://api.github.com/repos/" + repo
    listing = get(base + "/actions/workflows?per_page=100")
    entries = listing.get("workflows", [])
    if not isinstance(entries, list) or not entries:
        raise SentinelError("WORKFLOW_LIST_UNAVAILABLE")
    workflows = {str(w.get("path", "")).split("/")[-1]: w for w in entries}
    rows = [check_workflow(get, base, name, policy, workflows.get(name), now)
            for name, policy in POLICY.items()]
    for name, meta in workflows.items():
        if name not in POLICY and name != "spectra-sentinel.yml" and name != "update-graph":
            rows.append(dict(name=name, expected="unclassified", state=meta.get("state"),
                             severity="WARNING", status="UNCLASSIFIED_WORKFLOW"))
    registry = registry or load_registry()
    today = now.astimezone(WIB).date()
    data = check_price_history(db_path, today, registry=registry)
    source_checks = [data]
    if receipts_dir is not None:
        from idx_calendar import latest_idx_session_before
        expected = latest_idx_session_before(today)
        for name in ("broker-subset-capture-receipt.json", "ownership-capture-receipt.json",
                     "company-news-receipt.json", "idx-disclosures-receipt.json"):
            source_checks.append(check_receipt(Path(receipts_dir) / name, registry,
                                               expected, artifact_root=artifact_root))
    critical = [r["name"] + ": " + r["status"] for r in rows if r["severity"] == "CRITICAL"]
    warnings = [r["name"] + ": " + r["status"] for r in rows if r["severity"] == "WARNING"]
    for item in source_checks:
        if item["severity"] == "CRITICAL":
            critical.append(item["name"] + ": " + item["status"])
    # Unobservable or acquisition-blocked required streams must never imply a
    # healthy complete fleet, even when the monitored Actions jobs are green.
    warnings.extend(name + ": " + status for name, status in EXTERNAL.items())
    return {
        "schema": "SPECTRA_GITHUB_SENTINEL_V2",
        "repo": repo,
        "checked_at_wib": now.astimezone(WIB).isoformat(),
        "overall": "RED" if critical else "DEGRADED" if warnings else "GREEN_MONITORED_ONLY",
        "critical": critical, "warnings": warnings,
        "workflows": rows, "source_checks": source_checks, "external_not_certified": EXTERNAL,
        "expected_registry": registry.metadata(),
        "full_fleet_certification": "NOT_CERTIFIED" if critical or warnings else "MONITORED_SCOPE_ONLY",
        "policy_note": ("BROKER_LEARNING intentionally disabled by project HANDOFF.md; "
                        "Github cron status != authorized full-universe source access; "
                        "Stockbit images and local Windows task are NOT covered."),
    }

def markdown(r):
    out = ["# SPECTRA Sentinel", "", "**Overall: " + r["overall"] + "**",
           "Checked WIB: " + r["checked_at_wib"], "",
           "| Workflow | Expected | State | Last Cron | Result | Status |",
           "|---|---|---|---|---|---|"]
    for x in r["workflows"]:
        out.append("| " + " | ".join((x["name"], x["expected"], str(x["state"]),
                    str(x.get("latest_run") or "—")[:19], x["severity"], x["status"])) + " |")
    out += ["", "## Dataset", ""]
    for x in r["source_checks"]:
        out.append(f"- **{x['name']}** {x['status']}; last={x.get('asof')} (official session and fixed-universe evidence)")
    out += ["", "## Known outside-GitHub / unavailable feeds"]
    out += ["- " + name + ": " + status for name, status in r["external_not_certified"].items()]
    out += ["", "## Critical findings"]
    out += ["- " + x for x in r["critical"]] or ["- None for monitored sources"]
    out += ["", "Read-only checks. No automatic re-enable, paid vendor calls, database write, Git commit or trade."]
    return "\n".join(out) + "\n"

def telegram_red(r):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if r["overall"] != "RED" or not token or not chat:
        return False
    message = "SPECTRA SENTINEL RED\n" + "\n".join(r["critical"][:9])
    message += "\nhttps://github.com/" + r["repo"] + "/actions"
    body = parse.urlencode({"chat_id": chat, "text": message[:3500]}).encode()
    try:
        req = request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=body)
        with request.urlopen(req, timeout=10) as res:
            return res.status == 200
    except (error.URLError, TimeoutError):
        return False

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "0xFridayy/DailyScraper"))
    p.add_argument("--db", type=Path, default=Path("neobdm.db"))
    p.add_argument("--notify-red", action="store_true")
    p.add_argument("--registry", type=Path, default=Path(__file__).with_name("all_tickers.json"))
    p.add_argument("--receipts-dir", type=Path, default=Path("collector_receipts"))
    p.add_argument("--artifact-root", type=Path)

    args = p.parse_args()
    now = datetime.now(timezone.utc)
    try:
        r = audit(github_get, args.repo, now, args.db,
                  registry=load_registry(args.registry), receipts_dir=args.receipts_dir,
                  artifact_root=args.artifact_root)
    except (SentinelError, OSError, ValueError, RuntimeError):
        r = {"schema": "SPECTRA_GITHUB_SENTINEL_V2", "repo": args.repo,
             "checked_at_wib": now.astimezone(WIB).isoformat(), "overall": "RED",
             "critical": ["SENTINEL_SELF_AUDIT_FAILED"], "warnings": [],
             "workflows": [], "source_checks": [],
             "external_not_certified": EXTERNAL}
    Path("sentinel_status.json").write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")
    report = markdown(r)
    Path("sentinel_summary.md").write_text(report, encoding="utf-8")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report)
    if args.notify_red or os.getenv("SENTINEL_NOTIFY_RED") == "true":
        sent = telegram_red(r)
        print("Telegram alert sent" if sent else "Telegram alert unavailable or not required")
    print(json.dumps({"overall": r["overall"], "critical": r["critical"],
                      "warnings": r["warnings"]}))
    return 1 if r["overall"] == "RED" else 0

if __name__ == "__main__":
    sys.exit(main())
