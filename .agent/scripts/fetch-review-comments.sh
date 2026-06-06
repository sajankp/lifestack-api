#!/usr/bin/env bash
set -euo pipefail

# Fetch pull request review comments and threads.
#
# Usage:
#   .agent/scripts/fetch-review-comments.sh
#   .agent/scripts/fetch-review-comments.sh --all
#   .agent/scripts/fetch-review-comments.sh --repo owner/name --pr 42

REPO=""
PR_NUMBER=""
SHOW_ALL="false"

usage() {
  cat <<USAGE
Usage: $0 [--repo <owner/name>] [--pr <number>] [--all]

Options:
  --repo       Repository in owner/name format (optional: auto-detected from git remote)
  --pr         Pull request number (optional: auto-detected from current branch PR)
  --all        Show all threads including resolved ones (default: unresolved only)
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
    --all)
      SHOW_ALL="true"
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
          path
          line
          comments(first: 50) {
            nodes {
              id
              author {
                login
              }
              body
              createdAt
            }
          }
        }
      }
    }
  }
}
GRAPHQL

# Fetch GraphQL payload
raw_json=$(gh api graphql -f query="$QUERY" -f owner="$OWNER" -f name="$NAME" -F number="$PR_NUMBER")

# Parse and display using python3
python3 -c '
import json, sys

show_all = "'"$SHOW_ALL"'" == "true"
data = json.loads(sys.argv[1])
pull_request = data.get("data", {}).get("repository", {}).get("pullRequest", {})
if not pull_request:
    print("No pull request data found.")
    sys.exit(0)

threads = pull_request.get("reviewThreads", {}).get("nodes", [])
if not threads:
    print("No review threads found.")
    sys.exit(0)

visible_threads = [t for t in threads if show_all or not t.get("isResolved")]

if not visible_threads:
    if show_all:
        print("No threads found.")
    else:
        print("All threads are resolved! Pass --all to inspect resolved threads.")
    sys.exit(0)

print(f"\n📢 Found {len(visible_threads)} review threads:")
print("=" * 80)

for i, thread in enumerate(visible_threads, 1):
    thread_id = thread.get("id")
    is_resolved = thread.get("isResolved")
    is_outdated = thread.get("isOutdated")
    path = thread.get("path")
    line = thread.get("line")

    status_parts = []
    if is_resolved:
        status_parts.append("✅ RESOLVED")
    else:
        status_parts.append("🔴 UNRESOLVED")
    if is_outdated:
        status_parts.append("⚠️ OUTDATED")

    status_str = ", ".join(status_parts)
    line_str = f"L{line}" if line else "New/No Line Info"

    print(f"\n[{i}] Thread ID: {thread_id}")
    print(f"    Status: {status_str}")
    print(f"    File:   {path}:{line_str}")
    print("    " + "-" * 76)

    comments = thread.get("comments", {}).get("nodes", [])
    for c in comments:
        author = c.get("author", {}).get("login") if c.get("author") else "ghost"
        body = c.get("body", "").strip()
        # Indent body lines
        body_indented = "\n".join(f"      {line}" for line in body.splitlines())
        print(f"    👤 @{author}:")
        print(body_indented)
        print()
    print("=" * 80)
' "$raw_json"
