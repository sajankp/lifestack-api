"""Email delivery via Resend (spec-081).

``send_email`` mirrors ``push.py::send_web_push``'s shape — a plain,
module-level, patchable function, not a class — implemented with the
existing ``httpx`` dependency (no new backend package).

The function is ``async`` and accepts an optional pre-created
``httpx.AsyncClient`` so that callers processing multiple deliveries in a
tight loop (e.g. ``deliver_pending_email_notifications``) can share a single
TCP connection instead of spawning a new OS thread per call.
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


async def send_email(
    to: str,
    subject: str,
    html: str,
    client: httpx.AsyncClient | None = None,
) -> EmailResult:
    if not settings.EMAIL_ENABLED or not settings.RESEND_API_KEY or not settings.EMAIL_FROM_ADDRESS:
        return EmailResult(success=False, skipped=True)

    payload = {
        "from": settings.EMAIL_FROM_ADDRESS,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    headers = {"Authorization": f"Bearer {settings.RESEND_API_KEY}"}

    try:
        if client is None:
            async with httpx.AsyncClient() as local_client:
                response = await local_client.post(
                    RESEND_ENDPOINT,
                    headers=headers,
                    json=payload,
                    timeout=10,
                )
        else:
            response = await client.post(
                RESEND_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=10,
            )
        response.raise_for_status()
        return EmailResult(success=True)
    except httpx.HTTPError as exc:
        return EmailResult(success=False, error_detail=str(exc))
