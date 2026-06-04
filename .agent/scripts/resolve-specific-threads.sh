#!/usr/bin/env bash
set -euo pipefail

# Resolve specific GitHub PR review threads by thread ID.
# Optional: post a reply before resolving.
#
# Usage:
#   .agent/scripts/resolve-specific-threads.sh --thread PRRT_xxx
#   .agent/scripts/resolve-specific-threads.sh --thread PRRT_xxx --reply "Addressed in commit abc123"
#   .agent/scripts/resolve-specific-threads.sh --threads-file /tmp/threads.txt --reply-file /tmp/reply.md
#   .agent/scripts/resolve-specific-threads.sh --thread PRRT_xxx --dry-run

THREAD_IDS=()
THREADS_FILE=""
REPLY_BODY=""
REPLY_FILE=""
DRY_RUN="false"

usage() {
  cat <<USAGE
Usage: $0 [--thread <thread_id>]... [--threads-file <path>] [--reply <text> | --reply-file <path>] [--dry-run]

Options:
  --thread        Thread ID (repeatable), e.g. PRRT_kw...
  --threads-file  File with one thread ID per line
  --reply         Reply body to post before resolving
  --reply-file    Read reply body from file
  --dry-run       Print planned actions only
  -h, --help      Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --thread)
      THREAD_IDS+=("${2:-}")
      shift 2
      ;;
    --threads-file)
      THREADS_FILE="${2:-}"
      shift 2
      ;;
    --reply)
      REPLY_BODY="${2:-}"
      shift 2
      ;;
    --reply-file)
      REPLY_FILE="${2:-}"
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

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: gh CLI is required." >&2
  exit 1
fi

if [[ -n "$REPLY_BODY" && -n "$REPLY_FILE" ]]; then
  echo "Error: use only one of --reply or --reply-file." >&2
  exit 1
fi

if [[ -n "$THREADS_FILE" ]]; then
  if [[ ! -f "$THREADS_FILE" ]]; then
    echo "Error: threads file not found: $THREADS_FILE" >&2
    exit 1
  fi
  while IFS= read -r line; do
    line="$(echo "$line" | xargs)"
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    THREAD_IDS+=("$line")
  done < "$THREADS_FILE"
fi

if [[ -n "$REPLY_FILE" ]]; then
  if [[ ! -f "$REPLY_FILE" ]]; then
    echo "Error: reply file not found: $REPLY_FILE" >&2
    exit 1
  fi
  REPLY_BODY="$(cat "$REPLY_FILE")"
fi

if [[ ${#THREAD_IDS[@]} -eq 0 ]]; then
  echo "Error: provide at least one --thread or --threads-file." >&2
  usage
  exit 1
fi

read -r -d '' QUERY_THREAD <<'GRAPHQL' || true
query($id: ID!) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      id
      isResolved
      isOutdated
    }
  }
}
GRAPHQL

read -r -d '' MUTATION_REPLY <<'GRAPHQL' || true
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id url }
  }
}
GRAPHQL

read -r -d '' MUTATION_RESOLVE <<'GRAPHQL' || true
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
GRAPHQL

processed=0
resolved=0
skipped=0

for tid in "${THREAD_IDS[@]}"; do
  processed=$((processed + 1))

  state_line="$({ gh api graphql -f query="$QUERY_THREAD" -f id="$tid" --jq '.data.node | [.id, .isResolved, .isOutdated] | @tsv' || true; } )"
  if [[ -z "$state_line" ]]; then
    echo "skip: $tid (not found or inaccessible)"
    skipped=$((skipped + 1))
    continue
  fi

  IFS=$'\t' read -r _id is_resolved is_outdated <<< "$state_line"

  if [[ "$is_resolved" == "true" ]]; then
    echo "skip: $tid (already resolved)"
    skipped=$((skipped + 1))
    continue
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] target: $tid (outdated=$is_outdated)"
    [[ -n "$REPLY_BODY" ]] && echo "[dry-run] would reply: yes"
    echo "[dry-run] would resolve: yes"
    continue
  fi

  if [[ -n "$REPLY_BODY" ]]; then
    gh api graphql -f query="$MUTATION_REPLY" -f threadId="$tid" -f body="$REPLY_BODY" >/dev/null
    echo "replied: $tid"
  fi

  gh api graphql -f query="$MUTATION_RESOLVE" -f threadId="$tid" >/dev/null
  echo "resolved: $tid"
  resolved=$((resolved + 1))
done

echo "processed: $processed"
echo "resolved: $resolved"
echo "skipped: $skipped"
