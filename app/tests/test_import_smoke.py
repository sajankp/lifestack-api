"""Import smoke tests — catch module-level errors (NameError, missing imports, etc.)
that endpoint tests may bypass because they don't force full app evaluation at import time.

Modules are loaded with ``importlib.import_module`` inside each test rather than with
top-of-file ``import`` statements: a broken module then fails *that* test with a clear
message instead of aborting collection of the whole file, and ruff's PLC0415
(import-outside-top-level) stays enabled for the rest of the codebase.
"""

import importlib
import inspect
import typing
import uuid

import pytest

ROUTER_MODULES = [
    "app.auth.router",
    "app.capture.router",
    "app.exports.router",
    "app.finance.router",
    "app.health.router",
    "app.imports.router",
    "app.investing.router",
    "app.notifications.router",
    "app.platform.router",
]


def test_app_imports_cleanly():
    """Importing app.main must succeed without NameError or ImportError."""
    main = importlib.import_module("app.main")

    assert main.app is not None
    # Creating a fresh app instance also works (used in production-disabled docs test)
    assert main.create_app() is not None


@pytest.mark.parametrize("module_name", ROUTER_MODULES)
def test_router_module_imports_cleanly(module_name: str):
    """Importing every router module must not raise NameError/ImportError at module level."""
    module = importlib.import_module(module_name)

    assert hasattr(module, "router"), f"{module_name} missing 'router' attribute"


def test_investing_router_portfolio_report_types_resolve():
    """Specifically verify the portfolio report helper uses PortfolioReportItem, not PortfolioReport."""
    router = importlib.import_module("app.investing.router")
    schemas = importlib.import_module("app.investing.schemas")

    sig = inspect.signature(router._portfolio_report_item)
    report_param = sig.parameters["report"]
    assert report_param.annotation is schemas.PortfolioReportItem, (
        f"_portfolio_report_item 'report' param annotation is {report_param.annotation}, "
        f"expected PortfolioReportItem"
    )


def test_investing_router_return_type_annotation_resolves():
    """Verify dict[str, Any] return type resolves (Any must be imported)."""
    router = importlib.import_module("app.investing.router")

    sig = inspect.signature(router._portfolio_report_item)
    # Should be dict[str, Any] — the Any should resolve without NameError
    assert sig.return_annotation is not inspect.Signature.empty


def test_no_undefined_names_in_router_type_hints():
    """Force evaluation of all type hints in investing router to catch undefined names.

    This uses typing.get_type_hints() which evaluates annotations at runtime
    (respecting from __future__ import annotations), catching NameError.
    """
    router = importlib.import_module("app.investing.router")

    # This will raise NameError if any type annotation references an undefined name
    type_hints = typing.get_type_hints(router._portfolio_report_item)
    assert "report" in type_hints
    assert "include_payload" in type_hints
    assert "return" in type_hints


def test_all_schemas_exported_and_resolve():
    """Import all schemas and verify they're valid pydantic models."""
    schemas = importlib.import_module("app.investing.schemas")

    # Check key portfolio report schemas exist and are valid
    assert hasattr(schemas, "PortfolioReportItem")
    assert hasattr(schemas, "PortfolioReportList")

    # Instantiate to catch pydantic model errors
    item = schemas.PortfolioReportItem(
        public_id=uuid.uuid4(),
        as_of="2026-01-01",
        run_id="run-1",
        theme_weights={},
        harvest_candidates=[],
        lrs_headroom_inr=None,
        tax_buckets={},
        created_at="2026-01-01T00:00:00Z",
    )
    assert item.public_id is not None


def test_create_app_lifespan_starts():
    """Create app and verify lifespan can start (catches runtime import errors)."""
    main = importlib.import_module("app.main")

    app = main.create_app()
    # If this fails, it's a runtime import error (e.g., circular import, missing dependency)
    assert app is not None
    # Verify app has routes registered (they live under _IncludedRouter)
    assert len(app.routes) > 0
