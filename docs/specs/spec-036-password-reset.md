# Feature Spec 036: Password Reset Functionality

**Status:** Proposed
**Spec ID:** 036

---

## 1. Overview
Currently, users of Lifestack cannot reset their password if they forget it. This specification defines a secure, production-ready password reset flow spanning the FastAPI backend and React frontend.

In the absence of a configured SMTP email delivery service, the system will securely log the password reset link to standard output (container logs) for development and mock testing. In a real production deployment, this would trigger email delivery.

---

## 2. API & Data Model Changes

### 2.1 Database Schema: PasswordResetToken
We will introduce a new model to store password reset tokens:

```python
class PasswordResetToken(SQLModel, table=True):
    __tablename__ = "password_reset_tokens"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(index=True, unique=True, max_length=64)  # SHA-256 hash of the token
    expires_at: datetime = Field(sa_type=sa.DateTime(timezone=True), index=True)
    used_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
```

### 2.2 Backend Endpoints

#### 1. Request Password Reset
* **Path:** `/v1/auth/forgot-password`
* **Method:** `POST`
* **Rate Limit:** `RATE_LIMIT_AUTH` (10/minute)
* **Request Body:**
  ```json
  {
    "email": "user@example.com"
  }
  ```
* **Response Body (200 OK):**
  ```json
  {
    "message": "If the email is registered, a password reset link has been sent."
  }
  ```
* **Behavior:**
  * Find the user by email.
  * If found, generate a cryptographically secure random token (`secrets.token_urlsafe(32)`).
  * Store the SHA-256 hash of the token, `user_id`, and `expires_at` (now + 15 minutes) in `password_reset_tokens`.
  * Log the reset URL to standard output/logs: `https://www.apis.sajankp.com/reset-password?token=<token>`.
  * **Critical:** Always return a generic success message and `200 OK` regardless of whether the email exists, preventing email/user enumeration.

#### 2. Perform Password Reset
* **Path:** `/v1/auth/reset-password`
* **Method:** `POST`
* **Rate Limit:** `RATE_LIMIT_AUTH` (10/minute)
* **Request Body:**
  ```json
  {
    "token": "plain-text-token",
    "new_password": "NewSecurePassword123!"
  }
  ```
* **Response Body (200 OK):**
  ```json
  {
    "message": "Password has been reset successfully."
  }
  ```
* **Behavior:**
  * Hash the input token using SHA-256.
  * Retrieve the matching record from `password_reset_tokens`.
  * Validate that the token is found, has not expired (`expires_at > now`), and has not been used (`used_at is None`).
  * If validation fails, return `400 Bad Request` with message `"Invalid or expired password reset token."`
  * Update the user's `hashed_password` using `hash_password(new_password)`.
  * Set `used_at = datetime.now(UTC)` on the token.
  * **Critical:** Revoke all active sessions (`AuthSession`s) for that user to terminate any existing sessions across devices.

---

## 3. UI/UX Changes

### 3.1 Forgot Password Page (`/forgot-password`)
* **Route:** Public, unauthenticated.
* **Layout:** Premium dark-themed form matching the LoginPage design.
* **Fields:** Email address input.
* **Flow:** On submission, call `/v1/auth/forgot-password` and show a nice success message notifying the user to check their email (or check container logs in this sandbox).

### 3.2 Reset Password Page (`/reset-password`)
* **Route:** Public, unauthenticated.
* **Layout:** Premium dark-themed form matching the LoginPage design.
* **Fields:** New Password, Confirm New Password.
* **Flow:**
  * Extract the `token` from query parameters (`?token=...`). If missing, show an error.
  * On submission, call `/v1/auth/reset-password` and redirect to `/login` with a success message state.

### 3.3 Login Page Link
* Add a "Forgot Password?" link below the password input in `LoginPage.tsx` navigating to `/forgot-password`.

---

## 4. Acceptance Criteria
* Requesting a password reset does not leak user existence (generic response).
* Hashed tokens are stored securely in the database.
* Re-using a token or using an expired token fails.
* Successful reset revokes all active `AuthSession` records.
* Complete UI pages exist for both requesting a reset and performing a reset.
