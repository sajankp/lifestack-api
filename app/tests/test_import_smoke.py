"""Import smoke tests — catch module-level errors (NameError, missing imports, etc.)
that endpoint tests may bypass because they don't force full app evaluation at import time.
"""
import pytest


def test_app_imports_cleanly():
    """Importing app.main must succeed without NameError or ImportError."""
    from app.main import app, create_app

    assert app is not None
    # Creating a fresh app instance also works (used in production-disabled docs test)
    fresh = create_app()
    assert fresh is not None


def test_all_router_modules_import_cleanly():
    """Importing every router module must not raise NameError/ImportError at module level."""
    from app.investing import router as investing_router
    from app.finance import router as finance_router
    from app.capture import router as capture_router
    from app.auth import router as auth_router
    from app.exports import router as exports_router
    from app.imports import router as imports_router
    from app.platform import router as platform_router
    from app.notifications import router as notifications_router
    from app.health import router as health_router

    # Just verify they're module objects with expected attributes
    for mod in [
        investing_router,
        finance_router,
        capture_router,
        auth_router,
        exports_router,
        imports_router,
        platform_router,
        notifications_router,
        health_router,
    ]:
        assert hasattr(mod, "router"), f"{mod} missing 'router' attribute"


def test_investing_router_portfolio_report_types_resolve():
    """Specifically verify the portfolio report helper uses PortfolioReportItem, not PortfolioReport."""
    from app.investing.router import _portfolio_report_item
    from app.investing.schemas import PortfolioReportItem

    # The function signature should reference PortfolioReportItem
    import inspect

    sig = inspect.signature(_portfolio_report_item)
    report_param = sig.parameters["report"]
    assert report_param.annotation is PortfolioReportItem, (
        f"_portfolio_report_item 'report' param annotation is {report_param.annotation}, "
        f"expected PortfolioReportItem"
    )


def test_investing_router_return_type_annotation_resolves():
    """Verify dict[str, Any] return type resolves (Any must be imported)."""
    from app.investing.router import _portfolio_report_item
    import inspect

    sig = inspect.signature(_portfolio_report_item)
    return_annotation = sig.return_annotation
    # Should be dict[str, Any] — the Any should resolve without NameError
    assert return_annotation is not inspect.Signature.empty


def test_no_undefined_names_in_router_type_hints():
    """Force evaluation of all type hints in investing router to catch undefined names.

    This uses typing.get_type_hints() which evaluates annotations at runtime
    (respecting from __future__ import annotations), catching NameError.
    """
    import typing
    from app.investing import router as investing_router

    # This will raise NameError if any type annotation references an undefined name
    type_hints = typing.get_type_hints(investing_router._portfolio_report_item)
    assert "report" in type_hints
    assert "include_payload" in type_hints
    assert "return" in type_hints


def test_all_schemas_exported_and_resolve():
    """Import all schemas and verify they're valid pydantic models."""
    import uuid
    from app.investing import schemas

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
    from app.main import create_app

    app = create_app()
    # If this fails, it's a runtime import error (e.g., circular import, missing dependency)
    assert app is not None
    # Verify app has routes registered (they live under _IncludedRouter)
    assert len(app.routes) > 0
    # Health endpoint should be reachable via test client (tested in test_main.py)
    # This test just ensures the app factory runs without import/runtime errors


if __name__ == "__main__":
    pytest.main([__file__, "-v"])