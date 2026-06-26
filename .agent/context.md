# Lifestack API — Agent Context

> This file is the canonical source of project conventions for AI coding agents.
> It is tool-agnostic. Entry-point files for specific tools (CLAUDE.md, AGENTS.md, etc.) reference or include this content.

## Project Overview

- Python 3.14, FastAPI, SQLAlchemy (async), PostgreSQL
- Architecture: Router → Service → Repository layered pattern
- Multi-tenant: all data scoped by `workspace_id` via composite foreign keys
- Auth: JWT in HttpOnly cookies, Argon2id hashing, refresh token rotation, CSRF double-submit
- Package manager: `uv`
- Test runner: `pytest` with `anyio`

## Quick Commands

- Run dev server: `uv run fastapi dev`
- Run all tests: `uv run pytest --cov=app --cov-report=term-missing -q`
- Run single test file: `uv run pytest app/tests/<name>.py -v`
- Run single test: `uv run pytest app/tests/<name>.py::test_function_name -v`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`
- Create feature branch: `git checkout -b feat/<name>`
- Open PR: `gh pr create --base main --head <branch>`

## Commit Prefixes

Use these prefixes for all commits:

- `docs:` — specs, ADRs, documentation
- `test:` — new or updated tests
- `feat:` — feature implementation
- `refactor:` — structure-only cleanup (no behavior change)
- `fix:` — bug fixes
- `chore:` — tooling, config, CI, dependencies

## Development Workflow

Follow this sequence for all feature work and architectural changes:

1. **Specification** — Create `docs/specs/NNN-*.md`. Get explicit user approval before coding. Add an ADR if the architectural choice is non-obvious.
2. **Test-First (Red)** — Create feature branch. Write failing tests FIRST. Confirm they fail before writing any implementation.
3. **Implementation (Green)** — Write minimal code to make tests pass. Follow the approved spec exactly. Pause and ask if spec is ambiguous.
4. **Verify** — Run full test suite with coverage. Mark spec as `Implemented`.
5. **PR** — Open pull request. Hand off with a validation summary.

### Phase guardrails

- Do NOT start implementation before spec approval.
- Do NOT skip the failing-test proof in the Red phase.
- Do NOT request PR review before verification passes.
- Escalate when spec intent and implementation diverge.

## PR Review Process

When processing pull requests:

1. List open PRs: `gh pr list --state open`
2. Fetch review comments: `bash .agent/scripts/fetch-review-comments.sh`
3. Triage each comment using the matrix below
4. Apply fixes and run tests locally
5. Reply to each thread explaining what changed
6. Resolve threads: `bash .agent/scripts/resolve-review-threads.sh --mode outdated --dry-run` then `bash .agent/scripts/resolve-review-threads.sh --mode outdated`
7. Request re-review

### Triage matrix

- **Accept**: bug fix, risk fix, spec-consistent, project-pattern consistent
- **Reject**: speculative preference, style-only churn, spec conflict
- **Discuss**: architecture-impacting, API contract change, cross-module tradeoff

### Comment templates

When replying to review threads:

```text
Addressed in <commit_sha>:
- <change 1>
- <change 2>
Validation:
- <test command>
```

When rejecting a suggestion:

```text
Not applying this suggestion because it conflicts with <spec/file>. Proposed alternative: <alternative>.
```

### Thread resolution scripts

```bash
# Resolve outdated threads (safe, do dry-run first):
bash .agent/scripts/resolve-review-threads.sh --mode outdated --dry-run
bash .agent/scripts/resolve-review-threads.sh --mode outdated

# Resolve a specific thread:
bash .agent/scripts/resolve-specific-threads.sh --thread <thread_id> --dry-run
bash .agent/scripts/resolve-specific-threads.sh --thread <thread_id>
```

## TDD Pitfalls (FastAPI + pytest specific)

| Pitfall | Fix |
|---------|-----|
| Test passes without implementation | Make assertion more specific |
| `engine` points to remote DB in tests | Use `postgres.engine` (module ref), not bare `engine` import |
| Async test not collected | Ensure `@pytest.mark.anyio` is present |
| Rate limit errors in tests | `client` fixture disables limiter automatically |
| CSRF rejection in tests | `client` fixture sets `http://test` as trusted origin automatically |

## Testing checklist

Before marking any implementation as done:

- [ ] Test fails without implementation (Red verified)
- [ ] Test passes with implementation (Green verified)
- [ ] Edge cases covered (nulls, empty, invalid input)
- [ ] Error responses validated (status code + RFC 7807 body)
- [ ] Full suite clean: `uv run pytest --cov=app -q` passes with no regressions

## Escalation triggers

Stop and ask the user when:

- Review feedback conflicts with the approved spec
- A fix requires architecture changes beyond the spec scope
- CI failures imply missing scope rather than a bug

## Handoff template

When handing off completed work:

```text
Implemented per spec <spec-id>.
Validation:
- uv run pytest --cov=app --cov-report=term-missing -q
- uv run ruff check .
PR ready for review.
```

## Troubleshooting

- Protected main blocks push → Push feature branch and open PR instead
- Local env mismatch → Use `uv` and pinned version requirements
- Pre-commit failure → Fix hooks, recommit, re-verify with `git status`
