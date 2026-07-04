"""Per-session guardrails for the voice-capture WebSocket bridge.

Extracted verbatim from ``agent.py`` (D3) — enforces frame/session byte caps,
a text-length cap, and a wall-clock session limit. Behavior unchanged.
"""

import time
from dataclasses import dataclass, field

from app.config import settings

CAPTURE_POLICY_VIOLATION_CLOSE_CODE = 4008


class CaptureSessionLimitExceededError(Exception):
    def __init__(
        self,
        detail: str,
        *,
        close_code: int = CAPTURE_POLICY_VIOLATION_CLOSE_CODE,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.close_code = close_code


@dataclass
class CaptureSessionLimiter:
    max_frame_bytes: int
    max_session_bytes: int
    max_session_seconds: float
    max_text_chars: int
    started_at: float = field(default_factory=time.monotonic)
    total_client_bytes: int = 0

    @classmethod
    def from_settings(cls) -> "CaptureSessionLimiter":
        return cls(
            max_frame_bytes=settings.CAPTURE_MAX_WS_FRAME_BYTES,
            max_session_bytes=settings.CAPTURE_MAX_SESSION_BYTES,
            max_session_seconds=settings.CAPTURE_MAX_SESSION_SECONDS,
            max_text_chars=settings.CAPTURE_MAX_TEXT_CHARS,
        )

    def check_elapsed(self) -> None:
        if time.monotonic() - self.started_at > self.max_session_seconds:
            raise CaptureSessionLimitExceededError("Voice session time limit reached.")

    def validate_client_message(self, message: dict) -> None:
        self.check_elapsed()

        audio_bytes = message.get("bytes")
        if audio_bytes is not None:
            frame_size = len(audio_bytes)
            if frame_size > self.max_frame_bytes:
                raise CaptureSessionLimitExceededError("Voice audio frame is too large.")

            self.total_client_bytes += frame_size
            if self.total_client_bytes > self.max_session_bytes:
                raise CaptureSessionLimitExceededError("Voice session audio limit reached.")

        text = message.get("text")
        if text is not None and len(text) > self.max_text_chars:
            raise CaptureSessionLimitExceededError("Voice text message is too large.")
