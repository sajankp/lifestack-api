---
name: development-workflow
description: Use when delivering a new feature or architectural change that needs spec-first execution, explicit approval gates, test-first implementation, full verification, and PR handoff.
---

# Development Workflow Agent

Use this agent for feature work and architectural changes.

## Applicability

- Backend-focused in `lifestack-api`.
- For frontend-heavy work, use the frontend copy in `lifestack-web`.

## Quick Commands

```bash
git checkout -b feat/<name>
uv run pytest app/tests/... -v
uv run pytest --cov=app --cov-report=term-missing -q
gh pr create --base main --head <branch>
```

## 1-Minute Checklist

- Confirm approved spec exists before coding.
- Create branch and write/update tests first.
- Implement minimal scope per spec.
- Run full validation commands for this repo.
- Open PR and hand off with a clear validation summary.

## Scope

- Spec-first delivery
- Test-first implementation (Red-Green-Refactor)
- Verification and PR preparation

## Core Flow

Spec -> Tests (failing) -> Implement -> Verify -> PR

## Phase Guardrails

- Do not start implementation before spec approval.
- Do not skip the failing-test proof in Red phase.
- Do not request PR review before verification passes.
- Escalate when spec intent and implementation reality diverge.

## Phases

1. Specification
- Create `docs/specs/NNN-*.md`
- Request explicit approval before coding
- Add ADR if architectural choice is non-obvious

2. Test-First (Red)
- Create feature branch
- Mark spec `Approved`
- Write failing tests
- Confirm failure before implementation

3. Implementation (Green)
- Implement minimum required behavior
- Follow approved spec exactly
- Pause for user decision if spec ambiguity appears

4. Verify and PR
- Run full test suite with coverage
- Mark spec `Implemented`
- Open PR and hand off to PR review workflow
- For review-thread cleanup during handoff:
  - `bash .agent/scripts/resolve-review-threads.sh --mode outdated --dry-run`
  - `bash .agent/scripts/resolve-review-threads.sh --mode outdated`

## Commit Prefixes

- `docs:` specs/ADR/docs
- `test:` failing or updated tests
- `feat:` feature implementation
- `refactor:` structure-only cleanup
- `fix:` bug fixes
- `chore:` tooling/config/CI/deps

## Handoff Template

```text
Implemented per spec <spec-id>.
Validation:
- <test command 1>
- <test command 2>
PR ready for /pr-review workflow.
```

## Troubleshooting

- Protected main blocks push:
  - Push feature branch and open PR.
- Local env mismatch:
  - Use project runner (`uv`) and pinned version requirements.
- Pre-commit failure:
  - Fix hooks, recommit, and re-verify `git status`.

## Reference

Primary workflow doc: `../../workflows/development-workflow.md`
