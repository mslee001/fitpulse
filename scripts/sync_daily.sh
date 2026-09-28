#!/bin/bash
# Wrapper for launchd to run the daily FitPulse sync.
# launchd runs in a minimal environment, but we deliberately do NOT `source .env`
# here: bash parses it as shell, so any value containing a space or other shell
# metacharacter (e.g. GOOGLE_HEALTH_WEBHOOK_SECRET) is executed as a command,
# failing with "command not found" (exit 127). Django's settings.py calls
# python-dotenv's load_dotenv(), which reads .env correctly from the project
# root regardless of the calling environment.
# After a successful sync, schedules the next one-shot wake so the laptop
# wakes for both the 8:30 AM and 7:00 PM runs.
# Any arguments (e.g. --if-stale 8) are passed through to manage.py.

set -e

PROJECT_DIR="/Users/megan/peloton_dashboard"
cd "$PROJECT_DIR"

venv/bin/python3 manage.py sync_daily "$@"
SYNC_EXIT=$?

if [ $SYNC_EXIT -eq 0 ]; then
  HOUR=$(date +%H)
  if [ "$HOUR" -lt 12 ]; then
    # Morning run — schedule evening wake for today at 18:55 (5 min before 7 PM job)
    WAKE_TIME=$(date -v+0d "+%m/%d/%Y 18:59:30")
    sudo /usr/bin/pmset schedule wake "$WAKE_TIME" 2>/dev/null || true
  else
    # Evening run — schedule morning wake for tomorrow at 08:25 (5 min before 8:30 AM job)
    WAKE_TIME=$(date -v+1d "+%m/%d/%Y 07:59:30")
    sudo /usr/bin/pmset schedule wake "$WAKE_TIME" 2>/dev/null || true
  fi
fi

exit $SYNC_EXIT
