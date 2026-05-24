---
name: development-workflow
description: Use when delivering a new feature or architectural change that needs spec-first execution, explicit approval gates, test-first implementation, full verification, and PR handoff.
---

# Development Workflow Agent

Use this agent for feature work and architectural changes.

## Scope

- Spec-first delivery
- Test-first implementation (Red-Green-Refactor)
- Verification and PR preparation

## Core Flow

Spec -> Tests (failing) -> Implement -> Verify -> PR

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

## Commit Prefixes

- `docs:` specs/ADR/docs
- `test:` failing or updated tests
- `feat:` feature implementation
- `refactor:` structure-only cleanup
- `fix:` bug fixes
- `chore:` tooling/config/CI/deps

## Reference

Primary workflow doc: `../../workflows/development-workflow.md`
