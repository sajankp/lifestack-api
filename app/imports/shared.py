"""Small helpers and types shared by every per-module import validator/committer.

`ImportService` (`app/imports/service.py`) owns the shared harness — file
reading, header matching, chunked commit iteration, audit logging, batch
status transitions — and dispatches to per-module functions living in
`app/imports/<module>_import.py`. Those per-module files import from here
instead of from `service.py`, so there is no import cycle: `service.py`
imports the per-module files, and the per-module files only import this
leaf module (plus models/repositories, never the service).
"""

from collections.abc import Callable
from decimal import Decimal

# Signature of the per-row `add_error(field, code, msg, value=None)` closure
# built inside `ImportService.validate_batch_file`'s row loop.
AddErrorFn = Callable[..., None]

# `(instrument_symbol, as_of_date_str) -> weight` — returned by the
# investing-constituents row validator and accumulated across rows of the
# batch so the total per (symbol, date) group can be checked against the
# 0.99-1.01 tolerance after the loop. Every other module's row validator
# always returns `None` here.
WeightEntry = tuple[tuple[str, str], Decimal]


def enum_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def norm(value: str | None) -> str:
    return (value or "").strip()
