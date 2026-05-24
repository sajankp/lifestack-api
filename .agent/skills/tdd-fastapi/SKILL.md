---
name: tdd-fastapi
description: Use for FastAPI feature work that should follow a strict Red-Green-Refactor cycle with pytest, including writing failing tests first, implementing minimal code to pass, and validating full-suite stability.
---

# TDD for FastAPI/pytest (Lifestack)

This skill covers the **TDD process**. For project-specific patterns, fixtures, and code examples, see [`docs/PATTERNS.md`](../../docs/PATTERNS.md) — specifically the **Testing Pattern** section.

## The TDD Cycle

```
┌─────────────────────────────────────────────────────────┐
│  1. RED:      Write a failing test first                │
│  2. GREEN:    Write minimal code to make it pass        │
│  3. REFACTOR: Clean up while keeping tests green        │
└─────────────────────────────────────────────────────────┘
```

---

## Running Tests

```bash
# All tests with coverage (required before PR)
uv run pytest --cov=app --cov-report=term-missing -q

# Single file
uv run pytest app/tests/test_security_hardening.py -v

# Single test
uv run pytest app/tests/test_security_hardening.py::test_x_request_id_header -v
```

---

## Key Pitfalls

| Pitfall | Fix |
|---------|-----|
| Test passes without implementation | Make assertion more specific |
| `engine` points to remote DB in tests | Use `postgres.engine` (module ref), not bare `engine` import |
| Async test not collected | Ensure `@pytest.mark.anyio` is present |
| Rate limit errors in tests | `client` fixture disables limiter automatically |
| CSRF rejection in tests | `client` fixture sets `http://test` as trusted origin automatically |

---

## Testing Checklist

- [ ] Test fails without implementation (Red verified)
- [ ] Test passes with implementation (Green verified)
- [ ] Edge cases covered (nulls, empty, invalid input)
- [ ] Error responses validated (status code + RFC 7807 body)
- [ ] Full suite clean: `uv run pytest --cov=app -q` passes with no regressions

---

*Adapted for Lifestack: 2026-05-16*
