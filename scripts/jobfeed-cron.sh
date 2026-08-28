#!/usr/bin/env bash
# One jobfeed cycle, for cron. Install with:
#   crontab -e
#   7 * * * * /home/user/Test-claude/scripts/jobfeed-cron.sh >> /var/log/jobfeed.log 2>&1
#
# Runs at :07 rather than :00 for no reason except that nothing else does.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && { set -a; . ./.env; set +a; }

# Never two at once: they would race on the same database.
exec 9>"${TMPDIR:-/tmp}/jobfeed.lock"
flock -n 9 || { echo "$(date -Is) already running, skipping"; exit 0; }

echo "=== $(date -Is)"
python3 -m jobfeed.cli run --retire
python3 -m jobfeed.cli export
