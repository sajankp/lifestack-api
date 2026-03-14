# Feature Spec: API Versioning and RFC 7807 Error Responses
**Status:** Approved
**Spec ID:** 001

## 1. Overview
As per the architecture defined in `ARCHITECTURE.md`, Lifestack API needs to implement API versioning (`/v1/`) and RFC 7807 problem details for all error responses from day one. This spec outlines the implementation of these two core API behaviors.

## 2. Requirements

### 2.1 API Versioning
- All API routes must be prefixed with `/v1`.
- The versioning should be applied at the FastAPI application or router inclusion level, not hardcoded into every individual endpoint path.
- Existing endpoints (`/health`, `/auth/*`, `/todo/*`) should be moved under the `/v1` prefix.
- The root path `/` or `/v1/health` should remain accessible for health checks. (We will keep `/health` unversioned for infrastructure probes, and prefix business routes with `/v1`).

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

## 3. Implementation Details

1. **Versioning**:
   - In `app/main.py`, update `app.include_router(..., prefix="/v1")` for the auth and todo routers.
   - Update tests in `app/tests/e2e/test_system.py` and other test files to use the `/v1` prefix where applicable.

2. **RFC 7807**:
   - Create a base `RFC7807Exception` class in `app/core/exceptions.py`.
   - Update existing custom exceptions (e.g., `NotFoundError`, `UnauthorizedError`) to inherit from it or map to it.
   - Add global exception handlers in `app/main.py` using `@app.exception_handler(HTTPException)`, `@app.exception_handler(RequestValidationError)`, and `@app.exception_handler(Exception)`.

## 4. Testing Plan
- Execute existing unit and E2E tests (`pytest app/tests/`) and confirm they pass with the new routing paths.
- Add specific test cases to verify the structure of 404, 422, and 500 responses matches the RFC 7807 JSON schema.
