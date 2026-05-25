---
name: pr-review-workflow
description: Use when processing open GitHub pull requests end-to-end, including review triage, spec-consistency checks, fix validation, thread replies and resolution, and re-review requests.
---

# PR Review Workflow Agent

Use this agent when you need to process open pull requests end-to-end.

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
bash .agent/scripts/resolve-review-threads.sh --repo <owner>/<repo> --pr <number> --mode outdated --dry-run
bash .agent/scripts/resolve-review-threads.sh --repo <owner>/<repo> --pr <number> --mode outdated
```

Use `--mode all` only after manual validation of unresolved non-outdated threads.

### Practical Notes

- Script location is in this repo: `.agent/scripts/resolve-review-threads.sh`.
- It can target any repo by `--repo owner/name` (for example: `sajankp/lifestack-web`).
- If execute permissions are missing, run with `bash` as shown above.
- Useful follow-up helper for specific threads:

```bash
bash .agent/scripts/resolve-specific-threads.sh --repo <owner>/<repo> --thread-id <thread_id>
```

## Escalation Triggers

Stop and ask user when:
- Feedback conflicts with the approved spec
- A fix requires architecture changes
- CI failures imply missing scope rather than a bug

## Reference

Primary workflow doc: `../../workflows/pr-review.md`
