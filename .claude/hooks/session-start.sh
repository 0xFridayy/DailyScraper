#!/bin/bash
set -euo pipefail

# SessionStart hook for Claude Code on the web.
# Installs Python dependencies so the scraper, linters, and tests can run.
# Only runs in the remote (web) environment.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Install Python dependencies (playwright, requests, schedule, pytz).
# pip upgrade is best-effort; a system-managed pip may refuse to self-upgrade.
python3 -m pip install --upgrade pip || true
python3 -m pip install -r requirements.txt

# NOTE: Chromium is pre-installed in the remote environment
# (PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1),
# so we deliberately skip `playwright install`.
