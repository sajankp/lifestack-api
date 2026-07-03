"""Web Push delivery via pywebpush (spec-052).

``send_web_push`` is synchronous (pywebpush makes a blocking HTTP call);
async callers (``push_delivery_job``) wrap it in ``asyncio.to_thread``. Kept
as a plain module-level function — not a class — so it's patchable in tests
the same way ``app.investing.service._fetch_stock_price`` is.
"""

import json
from dataclasses import dataclass

from pywebpush import WebPushException, webpush

from app.config import settings


@dataclass
class PushResult:
    success: bool
    # True on a permanent push-service rejection (404/410) — the standard
    # Web Push contract for "this subscription no longer exists."
    gone: bool = False
    error_detail: str | None = None


def send_web_push(endpoint: str, p256dh: str, auth: str, payload: dict) -> PushResult:
    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.VAPID_SUBJECT},
        )
        return PushResult(success=True)
    except WebPushException as exc:
        status_code = getattr(exc.response, "status_code", None)
        gone = status_code in (404, 410)
        return PushResult(success=False, gone=gone, error_detail=str(exc))
