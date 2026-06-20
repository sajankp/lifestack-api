import logging

from app.core.logging import HealthAccessFilter, setup_logging


def _access_record(path: str, status_code: int | str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", path, "1.1", status_code),
        exc_info=None,
    )


def test_health_access_filter_drops_successful_health_probes():
    log_filter = HealthAccessFilter()

    assert not log_filter.filter(_access_record("/health", 200))
    assert not log_filter.filter(_access_record("/health?source=docker", 200))
    assert not log_filter.filter(_access_record("/health/", 204))


def test_health_access_filter_keeps_failures_and_other_requests():
    log_filter = HealthAccessFilter()

    assert log_filter.filter(_access_record("/health", 503))
    assert log_filter.filter(_access_record("/v1/todos", 200))
    assert log_filter.filter(_access_record("/health", "not-a-status"))


def test_setup_logging_does_not_duplicate_health_filter():
    access_logger = logging.getLogger("uvicorn.access")
    original_filters = access_logger.filters.copy()
    access_logger.filters.clear()

    try:
        setup_logging()
        setup_logging()
        filters = [item for item in access_logger.filters if isinstance(item, HealthAccessFilter)]
        assert len(filters) == 1
    finally:
        access_logger.filters[:] = original_filters
