---
name: pr-review-workflow
description: Use when processing open GitHub pull requests end-to-end, including review triage, spec-consistency checks, fix validation, thread replies and resolution, and re-review requests.
---

# PR Review Workflow Agent

Use this agent when you need to process open pull requests end-to-end.

## Applicability

- Backend-focused in `lifestack-api`.
- Use the frontend copy in `lifestack-web` for UI-only PRs.

## Quick Commands

```bash
gh pr list --state open
gh pr view <number> --json reviews,comments,statusCheckRollup,mergeStateStatus,url
bash .agent/scripts/resolve-review-threads.sh --mode outdated --dry-run
bash .agent/scripts/resolve-review-threads.sh --mode outdated
```

## Scope

- List and prioritize open PRs
- Fetch AI/human feedback and triage comments critically
- Apply fixes with tests
- Reply and resolve review threads
- Request re-review and prepare merge readiness

## Core Rules

- Never commit, merge, or close a PR without explicit user approval.
- Run local tests before push.
- Validate review suggestions against approved spec and project patterns.
- Do not run `--mode all` until dry-run + manual validation confirms non-outdated threads are addressed.
- Request `/gemini review` only after tests pass.

## Review Triage Matrix

- `Accept`: bug/risk fix, spec-consistent, and project-pattern consistent.
- `Reject`: speculative preference, style-only churn, or spec conflict.
- `Discuss`: architecture-impacting, API contract-changing, or cross-module tradeoff.

## Steps

1. List PRs: `gh pr list --state open`
2. Verify AI review exists (Gemini or required reviewer)
3. Fetch comments/reviews and classify: accept, reject, or discuss
4. Apply fixes and run tests
5. Reply to each thread with what changed
6. Resolve threads (prefer outdated first)
7. Request re-review (`/gemini review`)
8. Re-check CI and merge readiness

## Thread Resolution Helper

Use:

```bash
bash .agent/scripts/resolve-review-threads.sh --mode outdated --dry-run
bash .agent/scripts/resolve-review-threads.sh --mode outdated
```

Use `--mode all` only after manual validation of unresolved non-outdated threads.

### Practical Notes

- Script location is in this repo: `.agent/scripts/resolve-review-threads.sh`.
- It auto-detects repo and current PR by default.
- You can still target explicitly: `--repo owner/name --pr <number>`.
- If execute permissions are missing, run with `bash` as shown above.
- Useful follow-up helper for specific threads:

```bash
bash .agent/scripts/resolve-specific-threads.sh --thread <thread_id> --dry-run
bash .agent/scripts/resolve-specific-threads.sh --thread <thread_id>
```

## Comment Templates

```text
Addressed in <commit_sha>:
- <change 1>
- <change 2>
Validation:
- <test command>
```

```text
Not applying this suggestion because it conflicts with <spec/file>. Proposed alternative: <alternative>.
```

```text
/gemini review
Follow-up pushed in <commit_sha>; all previously raised points addressed and validated locally.
```

## Troubleshooting

- Permission denied on scripts:
  - Run with `bash .agent/scripts/<script>.sh ...`
- Could not auto-detect PR:
  - Pass explicit `--repo owner/name --pr <number>`
- Protected branch push rejected:
  - Create branch + PR; do not force direct push to `main`.

## Escalation Triggers

Stop and ask user when:
- Feedback conflicts with the approved spec
- A fix requires architecture changes
- CI failures imply missing scope rather than a bug

## Reference

Primary workflow doc: `../../workflows/pr-review.md`
