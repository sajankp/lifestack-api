"""Per-request database accounting (spec-112).

`StructlogMiddleware` opens a profile for every HTTP request; the SQLAlchemy cursor listeners
(`app.core.database.slow_query`) add each statement to it. At the end of the request the totals
become two histograms by route template, which is what separates "slow because of the database"
from "slow for another reason", and "one slow query" from "an N+1 loop of fast ones".

The profile is a mutable object held in a `ContextVar`: the value set before `call_next` is copied
into the endpoint's task, and the object itself is shared, so the middleware reads what the
endpoint wrote.
"""

from collections.abc import MutableMapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass
class RequestDbProfile:
    scope: MutableMapping[str, Any]
    queries: int = 0
    seconds: float = 0.0


_current: ContextVar[RequestDbProfile | None] = ContextVar(
    "lifestack_request_db_profile", default=None
)


def begin_request_profile(scope: MutableMapping[str, Any]) -> tuple[RequestDbProfile, Token]:
    profile = RequestDbProfile(scope=scope)
    return profile, _current.set(profile)


def end_request_profile(token: Token) -> None:
    _current.reset(token)


def current_request_profile() -> RequestDbProfile | None:
    return _current.get()


def record_query(seconds: float) -> RequestDbProfile | None:
    """Add one finished statement to the active request's profile (if any) and return it."""
    profile = _current.get()
    if profile is not None:
        profile.queries += 1
        profile.seconds += seconds
    return profile
