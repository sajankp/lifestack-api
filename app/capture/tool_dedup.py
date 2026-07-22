"""spec-090: tool-call idempotency guard for resumed voice-capture sessions.

Production incident (2026-07-22 addendum): when a dropped WebSocket reconnects
with `?resume=<handle>`, Gemini restores the conversation and can re-emit
function calls whose side effects already committed but whose toolResponse
never reached it — measured at ≥15% of all write executions, with replays up
to +37 minutes and drifted args. The guard suppresses exactly that class:
write calls arriving on a **resumed** connection **before any user input**,
matching a recent successful execution.

The ledger is deliberately in-process (a module-level singleton): a resumed
session lands on the same single-container deployment, and a cross-process
store is explicitly out of scope until the deployment is horizontal
(spec-090 Out of scope).
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.config import settings

# Mutating capture tools — the only ones the guard records or suppresses.
# Read tools (get_*/list_*) always execute.
WRITE_TOOLS = frozenset({
    "create_todo_task",
    "create_recurring_todo",
    "log_spending_transaction",
    "log_weight",
    "log_medication_event",
    "update_todo",
    "delete_todo",
})

# Per-tool fuzzy key fields — the args that survived replay drift in the
# measured incidents (descriptions were reworded, categories flipped,
# account_name appeared; amount and date held). Tools not listed key on their
# full canonical args, i.e. exact-match only.
_FUZZY_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "log_spending_transaction": ("amount", "occurred_at"),
}


def _normalize_amount(value) -> str:
    """Decimal-normalize so "90" and "90.00" collide."""
    try:
        return str(Decimal(str(value)).normalize())
    except InvalidOperation:
        return str(value)


def _normalize_occurred_at(value, user_timezone: str, now: float) -> str:
    """A missing occurred_at means "today in the user's timezone" (the tool's
    own default) — an incident replay spelled today out explicitly while the
    original omitted it, and both must land on the same key."""
    if value:
        return str(value)
    try:
        tz = ZoneInfo(user_timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.fromtimestamp(now, tz).date().isoformat()


def _dedup_key(
    workspace_id: int,
    user_id: int,
    tool: str,
    args: dict,
    user_timezone: str,
    now: float,
) -> tuple:
    fuzzy_fields = _FUZZY_KEY_FIELDS.get(tool)
    if fuzzy_fields is None:
        canonical = tuple(sorted((str(k), str(v)) for k, v in args.items()))
        return (workspace_id, user_id, tool, canonical)
    parts = []
    for name in fuzzy_fields:
        if name == "amount":
            parts.append(_normalize_amount(args.get("amount")))
        elif name == "occurred_at":
            parts.append(_normalize_occurred_at(args.get("occurred_at"), user_timezone, now))
        else:
            parts.append(str(args.get(name)))
    return (workspace_id, user_id, tool, tuple(parts))


@dataclass
class _Execution:
    at: float
    result: dict
    suppression_used: bool = False


class CaptureToolDedupLedger:
    """Recent successful write-tool executions, keyed fuzzily, with each
    execution able to absorb exactly one suppressed replay (multiplicity:
    N legit identical originals absorb at most N replayed duplicates)."""

    def __init__(self, window_seconds: int):
        self.window_seconds = window_seconds
        self._entries: dict[tuple, list[_Execution]] = {}

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        for key in list(self._entries):
            alive = [e for e in self._entries[key] if e.at >= cutoff]
            if alive:
                self._entries[key] = alive
            else:
                del self._entries[key]

    def record(
        self,
        *,
        workspace_id: int,
        user_id: int,
        tool: str,
        args: dict,
        result: dict,
        user_timezone: str,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        self._prune(now)
        key = _dedup_key(workspace_id, user_id, tool, args, user_timezone, now)
        self._entries.setdefault(key, []).append(_Execution(at=now, result=dict(result)))

    def check_replay(
        self,
        *,
        workspace_id: int,
        user_id: int,
        tool: str,
        args: dict,
        user_timezone: str,
        now: float | None = None,
    ) -> dict | None:
        """Return (and consume) the original result of a matching recent
        execution, or None when the call is not a replay of one."""
        now = time.time() if now is None else now
        self._prune(now)
        key = _dedup_key(workspace_id, user_id, tool, args, user_timezone, now)
        for execution in self._entries.get(key, []):
            if not execution.suppression_used:
                execution.suppression_used = True
                return dict(execution.result)
        return None

    def size(self) -> int:
        return len(self._entries)


@dataclass
class SessionDedupContext:
    """Per-WebSocket-connection guard state threaded into the toolCall path.

    Suppression applies only while ``replay_suspect`` — a resumed connection
    on which the user has not yet spoken or typed. The moment real client
    input arrives, identical calls are intentional (observed: two same-price
    fares in one utterance) and must execute."""

    ledger: CaptureToolDedupLedger
    resumed: bool
    user_input_seen: bool = field(default=False)

    @property
    def replay_suspect(self) -> bool:
        return self.resumed and not self.user_input_seen


_global_ledger: CaptureToolDedupLedger | None = None


def get_global_ledger() -> CaptureToolDedupLedger:
    """Process-wide ledger so a replay landing on a NEW WebSocket session (the
    observed failure mode — every incident session was a fresh connection)
    still sees the prior connection's executions."""
    global _global_ledger
    if _global_ledger is None:
        _global_ledger = CaptureToolDedupLedger(
            window_seconds=settings.CAPTURE_TOOL_DEDUP_WINDOW_SECONDS
        )
    return _global_ledger
