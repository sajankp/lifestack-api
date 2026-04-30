# Feature Spec: API Versioning and RFC 7807 Error Responses
**Status:** Approved
**Spec ID:** 001

## 1. Overview
As per the architecture defined in `ARCHITECTURE.md`, Lifestack API needs to implement API versioning (`/v1/`) and RFC 7807 problem details for all error responses from day one. This spec outlines the implementation of these two core API behaviors.

## 2. Requirements

### 2.1 API Versioning
- All API routes must be prefixed with `/v1`.
- The versioning should be applied at the FastAPI application or router inclusion level, not hardcoded into every individual endpoint path.
- Health endpoint policy for Stage 1 is explicit:
  - `/health` is the canonical liveness endpoint for infrastructure probes.
  - `/v1/health` is not required in this slice.
  - Business APIs remain versioned under `/v1`.

### 2.2 RFC 7807 Error Responses
- All error responses (4xx, 5xx) must follow the RFC 7807 standard JSON format:
  ```json
  {
      "type": "https://lifestack.app/errors/<error-type>",
      "title": "Human readable title",
      "status": 400,
      "detail": "Detailed error message",
      "instance": "/v1/path/that/caused/error"
  }
  ```
- FastAPIs default `RequestValidationError` (422) must be overridden to return an RFC 7807 compliant response.
- Custom application exceptions defined in `app/core/exceptions.py` must be updated to throw or format as RFC 7807.
- A global exception handler must be registered in `app/main.py` to catch all unhandled exceptions (500) and format them as RFC 7807, without leaking sensitive traceback data.
- RFC 7807 responses must use `Content-Type: application/problem+json`.
- Validation error responses may include an extension field `errors` containing field-level issues. This extension is part of the public contract for 422 responses in Stage 1.

### 2.3 Error Type Catalog (Stage 1 Baseline)
- Type URI pattern: `https://lifestack.app/errors/<error-type>`.
- Minimum baseline types:
  - `validation-error` (422)
  - `not-found` (404)
  - `conflict` (409)
  - `unauthorized` (401)
  - `forbidden` (403)
  - `internal-server-error` (500)

Module-specific conflict types (for example `category-in-use`) may extend this catalog while keeping the same URI pattern.

### 2.4 List API Contract (Shared Stage 1 Defaults)
All new list endpoints in later specs should follow this baseline unless a spec overrides it:
- Query params:
  - `limit` (default `50`, max `200`)
  - `offset` (default `0`)
  - `sort` (module-defined fields; defaults to newest-first by `created_at desc` where available)
- Response may be either plain array (existing endpoints) or an envelope shape introduced by a module spec, but modules must document the chosen shape explicitly.
- For backward compatibility, existing list endpoints may adopt this contract incrementally.

## 3. Implementation Details

1. **Versioning**:
   - In `app/main.py`, update `app.include_router(..., prefix="/v1")` for the auth and todo routers.
   - Update tests in `app/tests/e2e/test_system.py` and other test files to use the `/v1` prefix where applicable.

2. **RFC 7807**:
   - Create a base `APIError` exception class in `app/core/exceptions.py`.
   - Define subclasses for common HTTP error categories (e.g., `NotFoundError`, `UnauthorizedError`, `ConflictError`).
   - Add global exception handlers in `app/main.py` using `@app.exception_handler(HTTPException)`, `@app.exception_handler(RequestValidationError)`, and `@app.exception_handler(Exception)`.

## 4. Testing Plan
- Execute existing unit and E2E tests (`pytest app/tests/`) and confirm they pass with the new routing paths.
- Add specific test cases to verify the structure of 404, 422, and 500 responses matches the RFC 7807 JSON schema.
