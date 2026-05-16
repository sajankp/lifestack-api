---
description: Spec-driven development workflow with mandatory TDD for new features and architectural changes
---

# Spec-Driven Development Workflow

Follow this for any new feature or architectural change.

```
Spec (approved) → Tests (failing) → Implement → Verify → PR
```

---

## Does this need the full workflow?

Trigger the full workflow if **any** are true:
- Estimated time > 2 hours
- Touching > 3 files
- Affects auth, DB schema, or API contracts
- Can't explain the change in one sentence

**Skip if:** bug fix (single file, clear cause), docs, formatting, CI config, dependency bumps.

> If you're asking "should this have a spec?" — it probably should.

---

## Phase 1: Specification

1. Create `docs/specs/NNN-descriptive-title.md` (use the template in `docs/specs/README.md`)
2. **STOP** — request user review before writing any code
3. If a non-obvious architectural choice was made, create `docs/adr/NNN-title.md` too
4. **STOP** — request ADR review if applicable

Only proceed when the spec is explicitly approved.

---

## Phase 2: Test-First (Red)

1. Create a feature branch: `git checkout -b feat/descriptive-name`
2. Update spec status: `Planned` → `Approved`
3. Write failing tests based on the spec's test strategy
4. Run tests — they **must fail** (proves they test something real)
5. **STOP** — request review of test cases before implementing

```bash
uv run pytest app/tests/... -v  # Expected: FAILED
```

---

## Phase 3: Implementation (Green)

1. Write the minimum code to make the tests pass
2. Follow the spec exactly — no unrequested features
3. If you hit a spec gap or ambiguity: **STOP and discuss** — don't deviate silently

```bash
uv run pytest app/tests/... -v  # Expected: PASSED
```

---

## Phase 4: Verify & PR

1. Run the full suite with coverage — must pass cleanly:

```bash
uv run pytest --cov=app --cov-report=term-missing -q
```

2. Update spec status: `Approved` → `Implemented`
3. Push branch and open PR
4. Follow `/pr-review` workflow

---

## Commit Convention

| Prefix | When |
|--------|------|
| `docs:` | Spec / ADR / docs only |
| `test:` | Failing tests (Phase 2 commit) |
| `feat:` | Implementation |
| `refactor:` | Cleanup with no behavior change |
| `fix:` | Bug fix |
| `chore:` | CI, deps, config |

Commit approval and verification rules are in `AGENTS.md` and global user protocols.

---

*Adapted for Lifestack: 2026-05-16*
