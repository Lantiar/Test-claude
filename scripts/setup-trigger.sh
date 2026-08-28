#!/usr/bin/env bash
# Point a real scheduler at the jobfeed workflow.
#
# GitHub's own cron is best-effort: it skipped every slot for the first hour
# after this workflow was added, and a scheduler that silently does not fire is
# the worst kind to have in front of a source that expires. So an external
# scheduler calls workflow_dispatch instead, and GitHub's schedule stays on
# underneath as a fallback. The workflow refuses to do the work twice in quick
# succession, so both firing costs nothing.
#
# Needs two secrets, neither of which goes anywhere near the repo:
#   GH_PAT      a fine-grained personal access token for Lantiar/Test-claude
#               with Actions: read and write. github.com/settings/tokens?type=beta
#   QSTASH_TOKEN  from console.upstash.com -> QStash
#
# Usage:
#   GH_PAT=... QSTASH_TOKEN=... bash scripts/setup-trigger.sh
#   GH_PAT=... QSTASH_TOKEN=... bash scripts/setup-trigger.sh --list
#   QSTASH_TOKEN=... bash scripts/setup-trigger.sh --delete <schedule-id>
set -euo pipefail

REPO="${JOBFEED_REPO:-Lantiar/Test-claude}"
BRANCH="${JOBFEED_BRANCH:-claude/plan-reasoning-verification-7i3zz7}"
CRON="${JOBFEED_CRON:-*/30 * * * *}"
TARGET="https://api.github.com/repos/${REPO}/actions/workflows/jobfeed.yml/dispatches"

: "${QSTASH_TOKEN:?set QSTASH_TOKEN (console.upstash.com -> QStash)}"

if [ "${1:-}" = "--list" ]; then
  curl -sS -H "Authorization: Bearer $QSTASH_TOKEN" \
    https://qstash.upstash.io/v2/schedules | python3 -m json.tool
  exit 0
fi

if [ "${1:-}" = "--delete" ]; then
  curl -sS -X DELETE -H "Authorization: Bearer $QSTASH_TOKEN" \
    "https://qstash.upstash.io/v2/schedules/${2:?which schedule id?}"
  echo "deleted ${2}"
  exit 0
fi

: "${GH_PAT:?set GH_PAT (a fine-grained token with Actions: read and write)}"

# Check the token actually works before handing it to a scheduler that will use
# it unattended every half hour. A 404 here is usually a token without Actions
# write, not a missing workflow.
echo "checking the token can dispatch the workflow..."
code=$(curl -sS -o /tmp/dispatch.out -w "%{http_code}" -X POST "$TARGET" \
  -H "Authorization: Bearer $GH_PAT" \
  -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  -d "{\"ref\":\"${BRANCH}\"}")
if [ "$code" != "204" ]; then
  echo "GitHub said $code:" >&2; cat /tmp/dispatch.out >&2
  echo "  204 is success. 404 usually means the token lacks Actions: write," >&2
  echo "  or the branch name is wrong." >&2
  exit 1
fi
echo "  ok, a run should be starting now"

echo "creating the QStash schedule ($CRON)..."
curl -sS -X POST "https://qstash.upstash.io/v2/schedules/${TARGET}" \
  -H "Authorization: Bearer $QSTASH_TOKEN" \
  -H "Upstash-Cron: ${CRON}" \
  -H "Upstash-Method: POST" \
  -H "Upstash-Forward-Authorization: Bearer ${GH_PAT}" \
  -H "Upstash-Forward-Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  -d "{\"ref\":\"${BRANCH}\"}" | python3 -m json.tool
echo
echo "done. --list shows it; the workflow's own schedule stays on as a fallback."
