from app.core.middleware import _format_size_limit


def test_format_size_limit_uses_human_readable_units():
    assert _format_size_limit(512) == "512B"
    assert _format_size_limit(1536) == "1.5KB"
    assert _format_size_limit(10 * 1024 * 1024) == "10MB"
