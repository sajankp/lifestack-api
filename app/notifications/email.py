"""Email delivery via Resend (spec-081).

``send_email`` mirrors ``push.py::send_web_push``'s shape — a plain,
module-level, patchable function, not a class — implemented with the
existing ``httpx`` dependency (no new backend package).
"""

from dataclasses import dataclass

import httpx

from app.config import settings

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass
class EmailResult:
    success: bool
    skipped: bool = False
    error_detail: str | None = None


def send_email(to: str, subject: str, html: str) -> EmailResult:
    if not settings.EMAIL_ENABLED or not settings.RESEND_API_KEY or not settings.EMAIL_FROM_ADDRESS:
        return EmailResult(success=False, skipped=True)

    try:
        response = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={
                "from": settings.EMAIL_FROM_ADDRESS,
                "to": [to],
                "subject": subject,
                "html": html,
            },
            timeout=10,
        )
        response.raise_for_status()
        return EmailResult(success=True)
    except httpx.HTTPError as exc:
        return EmailResult(success=False, error_detail=str(exc))
