#!/bin/bash
# Wrapper for launchd to run the daily FitPulse sync.
# launchd runs in a minimal environment, so we source .env explicitly.
# After a successful sync, schedules the next one-shot wake so the laptop
# wakes for both the 8:30 AM and 7:00 PM runs.
# Any arguments (e.g. --if-stale 8) are passed through to manage.py.

set -e

PROJECT_DIR="/Users/megan/peloton_dashboard"
cd "$PROJECT_DIR"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

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
    WAKE_TIME=$(date -v+1d "+%m/%d/%Y 08:29:30")
    sudo /usr/bin/pmset schedule wake "$WAKE_TIME" 2>/dev/null || true
  fi
fi

exit $SYNC_EXIT
