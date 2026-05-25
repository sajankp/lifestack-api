#!/usr/bin/env bash
set -euo pipefail

# Resolve GitHub PR review threads via GraphQL using gh CLI.
#
# Usage:
#   .agent/scripts/resolve-review-threads.sh --mode outdated
#   .agent/scripts/resolve-review-threads.sh --repo owner/name --pr 10 --mode all --dry-run
#
# Modes:
#   outdated  Resolve only outdated unresolved threads (default, safer)
#   all       Resolve all unresolved threads

REPO=""
PR_NUMBER=""
MODE="outdated"
DRY_RUN="false"

usage() {
  cat <<USAGE
Usage: $0 [--repo <owner/name>] [--pr <number>] [--mode outdated|all] [--dry-run]

Options:
  --repo       Repository in owner/name format (optional: auto-detected from git remote)
  --pr         Pull request number (optional: auto-detected from current branch PR)
  --mode       Thread selection mode: outdated|all (default: outdated)
  --dry-run    Print targeted thread IDs without resolving
  -h, --help   Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="${2:-}"
      shift 2
      ;;
    --pr)
      PR_NUMBER="${2:-}"
      shift 2
      ;;
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$MODE" != "outdated" && "$MODE" != "all" ]]; then
  echo "Error: --mode must be 'outdated' or 'all'." >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: gh CLI is required." >&2
  exit 1
fi

if [[ -z "$REPO" ]]; then
  remote_url="$(git config --get remote.origin.url 2>/dev/null || true)"
  if [[ "$remote_url" =~ github.com[:/]([^/]+)/([^/.]+)(\.git)?$ ]]; then
    REPO="${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
  fi
fi

if [[ -z "$PR_NUMBER" ]]; then
  PR_NUMBER="$(gh pr view --json number --jq '.number' 2>/dev/null || true)"
fi

if [[ -z "$REPO" || -z "$PR_NUMBER" ]]; then
  echo "Error: unable to determine --repo and/or --pr. Provide them explicitly." >&2
  usage
  exit 1
fi

OWNER="${REPO%%/*}"
NAME="${REPO##*/}"

read -r -d '' QUERY <<'GRAPHQL' || true
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
        }
      }
    }
  }
}
GRAPHQL

read -r -d '' MUTATION <<'GRAPHQL' || true
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
GRAPHQL

threads="$({ gh api graphql -f query="$QUERY" -f owner="$OWNER" -f name="$NAME" -F number="$PR_NUMBER" --jq '.data.repository.pullRequest.reviewThreads.nodes[] | [.id, .isResolved, .isOutdated] | @tsv' || true; } )"

if [[ -z "$threads" ]]; then
  echo "No review threads found for $REPO PR #$PR_NUMBER"
  exit 0
fi

count_total=0
count_target=0
resolved_now=0

while IFS=$'\t' read -r id is_resolved is_outdated; do
  [[ -z "${id:-}" ]] && continue
  count_total=$((count_total + 1))

  if [[ "$is_resolved" == "true" ]]; then
    continue
  fi

  should_resolve="false"
  if [[ "$MODE" == "all" ]]; then
    should_resolve="true"
  elif [[ "$MODE" == "outdated" && "$is_outdated" == "true" ]]; then
    should_resolve="true"
  fi

  if [[ "$should_resolve" == "true" ]]; then
    count_target=$((count_target + 1))
    if [[ "$DRY_RUN" == "true" ]]; then
      echo "[dry-run] would resolve thread: $id (outdated=$is_outdated)"
    else
      gh api graphql -f query="$MUTATION" -f threadId="$id" >/dev/null
      echo "resolved thread: $id"
      resolved_now=$((resolved_now + 1))
    fi
  fi
done <<< "$threads"

echo "review threads total: $count_total"
echo "unresolved targeted ($MODE): $count_target"
if [[ "$DRY_RUN" == "false" ]]; then
  echo "resolved now: $resolved_now"
fi
