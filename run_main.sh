#!/bin/bash
# Cron entry point for the video auto-poster (Stage 1; in auto_publish mode it
# also publishes). Keep this thin — all logic is in main.py.
#
# Installed in crontab as e.g.:
#   */10 * * * * "/.../video-autoposter/run_main.sh"
#
# NOTE (macOS): cron must have Full Disk Access to read this iCloud folder.
# System Settings → Privacy & Security → Full Disk Access → enable /usr/sbin/cron.

set -euo pipefail

PROJECT_DIR="/Users/thecharlestonpropertycompany/Library/Mobile Documents/com~apple~CloudDocs/Claude/CrappyRv/video-autoposter"
cd "$PROJECT_DIR" || exit 1
mkdir -p logs

# Timestamp each cron invocation in the cron log, then run one pass.
echo "===== cron run $(date '+%Y-%m-%d %H:%M:%S') =====" >> logs/cron.log
exec ./.venv/bin/python main.py >> logs/cron.log 2>&1
