#!/usr/bin/env bash
# Fire the C2C Job Agent workflow on demand via GitHub's workflow_dispatch API.
# Usage:  GH_DISPATCH_TOKEN=github_pat_xxx ./scripts/trigger.sh [--dry-run]
# The same request (URL, headers, body) is what an external scheduler such as cron-job.org sends.
set -euo pipefail

REPO="${REPO:-pagidipalli7/c2c-job-agent}"
WORKFLOW="${WORKFLOW:-job_agent.yml}"
REF="${REF:-main}"
DRY_RUN="false"
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN="true"

if [[ -z "${GH_DISPATCH_TOKEN:-}" ]]; then
  echo "GH_DISPATCH_TOKEN is not set (fine-grained PAT with Actions: Read and write on $REPO)" >&2
  exit 1
fi

code=$(curl -sS -o /tmp/dispatch_resp.txt -w "%{http_code}" -X POST \
  "https://api.github.com/repos/$REPO/actions/workflows/$WORKFLOW/dispatches" \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GH_DISPATCH_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "User-Agent: c2c-job-agent-trigger" \
  -d "{\"ref\":\"$REF\",\"inputs\":{\"dry_run\":\"$DRY_RUN\"}}")

if [[ "$code" == "204" ]]; then
  echo "dispatched $WORKFLOW on $REF (dry_run=$DRY_RUN). Watch: https://github.com/$REPO/actions"
else
  echo "dispatch failed: HTTP $code" >&2
  cat /tmp/dispatch_resp.txt >&2
  exit 1
fi
