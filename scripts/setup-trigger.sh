#!/usr/bin/env bash
# Point a real scheduler at the jobfeed workflow.
#
# GitHub's own cron is best-effort: it skipped every slot for hours after this
# workflow was added, and a scheduler that silently does not fire is the worst
# kind in front of a source that expires. So cron-job.org calls
# workflow_dispatch on a real schedule, and GitHub's cron stays on underneath
# as a fallback. The workflow refuses to do the work twice in quick succession,
# so both firing costs nothing.
#
# cron-job.org's free tier runs down to every minute with no job limit, which
# is far more than this needs, and it has no other product attached to it.
#
# Two secrets, neither of which goes near the repository:
#   GH_PAT       fine-grained token for this repo, Actions: read and write
#                github.com/settings/tokens?type=beta
#   CRONJOB_KEY  cron-job.org -> Settings -> API
#
# Usage:
#   GH_PAT=... CRONJOB_KEY=... bash scripts/setup-trigger.sh
#   CRONJOB_KEY=... bash scripts/setup-trigger.sh --list
#   CRONJOB_KEY=... bash scripts/setup-trigger.sh --delete <job-id>
set -euo pipefail

REPO="${JOBFEED_REPO:-Lantiar/Test-claude}"
BRANCH="${JOBFEED_BRANCH:-claude/plan-reasoning-verification-7i3zz7}"
MINUTES="${JOBFEED_MINUTES:-0,30}"          # which minutes of each hour
API="https://api.cron-job.org"
TARGET="https://api.github.com/repos/${REPO}/actions/workflows/jobfeed.yml/dispatches"

: "${CRONJOB_KEY:?set CRONJOB_KEY (cron-job.org -> Settings -> API)}"
auth=(-H "Authorization: Bearer ${CRONJOB_KEY}" -H "Content-Type: application/json")

if [ "${1:-}" = "--list" ]; then
  curl -sS "${auth[@]}" "$API/jobs" | python3 -m json.tool
  exit 0
fi
if [ "${1:-}" = "--delete" ]; then
  curl -sS -X DELETE "${auth[@]}" "$API/jobs/${2:?which job id?}"
  echo "deleted ${2}"; exit 0
fi

: "${GH_PAT:?set GH_PAT (fine-grained token with Actions: read and write)}"

# Prove the token works before handing it to something that will use it
# unattended every half hour. A 404 here is almost always a token without
# Actions: write rather than a missing workflow -- GitHub returns 404 rather
# than 403 for permissions it will not discuss.
echo "checking the token can dispatch the workflow..."
code=$(curl -sS -o /tmp/dispatch.out -w "%{http_code}" -X POST "$TARGET" \
  -H "Authorization: Bearer $GH_PAT" \
  -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  -d "{\"ref\":\"${BRANCH}\"}")
if [ "$code" != "204" ]; then
  echo "GitHub said $code:" >&2; cat /tmp/dispatch.out >&2; echo >&2
  echo "  204 means success. 404 usually means the token lacks Actions: write," >&2
  echo "  or has not been granted access to ${REPO}, or the branch is wrong." >&2
  exit 1
fi
echo "  ok -- a run should be starting now"

mins=$(python3 -c "import sys;print(','.join(str(int(m)) for m in sys.argv[1].split(',')))" "$MINUTES")
echo "creating the cron-job.org schedule (minutes ${mins} of every hour, UTC)..."
body=$(python3 - "$TARGET" "$GH_PAT" "$BRANCH" "$mins" <<'PY'
import json, sys
target, pat, branch, mins = sys.argv[1:5]
print(json.dumps({"job": {
    "url": target,
    "enabled": True,
    "saveResponses": True,     # so a failure at 3am can be read afterwards
    "title": "jobfeed (every 30 min)",
    "requestMethod": 1,        # POST
    "extendedData": {
        "headers": {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        "body": json.dumps({"ref": branch}),
    },
    "schedule": {
        "timezone": "UTC",
        "expiresAt": 0,
        "hours": [-1], "mdays": [-1], "months": [-1], "wdays": [-1],
        "minutes": [int(m) for m in mins.split(",")],
    },
}}))
PY
)
curl -sS -X PUT "${auth[@]}" -d "$body" "$API/jobs" | python3 -m json.tool
echo
echo "done. --list shows it. GitHub's own cron stays on as a fallback;"
echo "the workflow refuses to poll twice within JOBFEED_MIN_MINUTES."
