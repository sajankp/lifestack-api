---
name: workflow-orchestrator
description: Use when one agent should run the full lifecycle across development-workflow and pr-review-workflow, from spec and test-first implementation through review-comment handling and merge readiness.
---

# Workflow Orchestrator

Use this skill when you want one agent to run the full lifecycle:

Spec -> Test-first implementation -> Verification -> PR review processing -> Merge readiness

## Inputs

- Approved feature scope (or request to draft spec first)
- Target repository state/branch
- Any explicit constraints from user comments/review feedback

## Sources of Truth

- `../../workflows/development-workflow.md`
- `../../workflows/pr-review.md`

## Execution Sequence

1. **Development Workflow First**
- Follow `development-workflow.md` phases in order:
  - Specification
  - Test-first (Red)
  - Implementation (Green)
  - Verify and PR
- Respect all stop points that require explicit user confirmation.

2. **PR Review Workflow Next**
- Follow `pr-review.md` to process AI/human feedback:
  - collect comments
  - evaluate against spec integrity
  - apply fixes with tests
  - reply and resolve threads
  - request re-review

3. **Thread Resolution Helpers**
- For outdated unresolved threads:
  - `.agent/scripts/resolve-review-threads.sh --repo <owner>/<repo> --pr <number> --mode outdated --dry-run`
  - `.agent/scripts/resolve-review-threads.sh --repo <owner>/<repo> --pr <number> --mode outdated`
- For manually verified specific threads:
  - `.agent/scripts/resolve-specific-threads.sh --thread <thread_id> --reply "Addressed in commit <sha>: <summary>"`

## Guardrails

- Never commit without explicit user approval where required by workflow rules.
- Never merge without explicit user approval.
- Never accept review comments that conflict with approved specs without escalation.
- Always run local tests before push.

## Deliverables

- Spec/ADR updates (if applicable)
- Test + implementation commits
- PR with resolved review threads and re-review requested
- Final merge-readiness summary

## Reference

- Development workflow: `../../workflows/development-workflow.md`
- PR review workflow: `../../workflows/pr-review.md`
