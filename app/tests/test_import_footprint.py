"""Import-footprint guard (perf/api-memory-footprint).

The production VM has 954 MB of RAM; the API's resident size decides whether the kernel
swaps it out (see docs/PRODUCTION_DEPLOYMENT.md, "The API lives in swap"). Libraries that
only serve an occasional or optional feature must be imported where they are used, not at
module top level, so an idle API does not carry them.

Runs in a fresh interpreter: ``sys.modules`` of the test process is already polluted.
"""

import importlib
import json
import os
import subprocess
import sys

# Module prefix -> why it must not be loaded by `import app.main`.
# Note: redis itself imports ``opentelemetry.sdk.metrics`` (an optional import in its
# observability package), so the whole ``opentelemetry`` package cannot be forbidden; only
# the parts this app would pull in for tracing/log export.
ON_DEMAND = {
    "openpyxl": "Excel import runs rarely",
    "pdfplumber": "CAS PDF import runs rarely",
    "pdfminer": "CAS PDF import runs rarely",
    "pywebpush": "only needed when a push is actually sent",
    "opentelemetry.exporter": "OTLP export is inert unless OTEL_EXPORTER_OTLP_ENDPOINT is set",
    "opentelemetry.instrumentation": "instrumentors are inert unless OTEL_EXPORTER_OTLP_ENDPOINT is set",
    "opentelemetry.sdk.trace": "tracing is inert unless OTEL_EXPORTER_OTLP_ENDPOINT is set",
    "opentelemetry.sdk._logs": "log export is inert unless OTEL_EXPORTER_OTLP_ENDPOINT is set",
}

PROBE = """
import json, sys
import app.main
print(json.dumps(sorted(sys.modules)))
"""


def _loaded_after_importing_main() -> set[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=True,
    ).stdout
    # structlog may print to stdout before our JSON line; take the last line.
    return set(json.loads(out.strip().splitlines()[-1]))


def test_importing_main_does_not_load_on_demand_libraries():
    loaded = _loaded_after_importing_main()
    offenders = {
        prefix: why
        for prefix, why in ON_DEMAND.items()
        if any(m == prefix or m.startswith(prefix + ".") for m in loaded)
    }
    assert not offenders, f"imported eagerly, move the import to the point of use: {offenders}"


def test_on_demand_libraries_still_import_when_used():
    # Guards against "fixing" the footprint by deleting a dependency from pyproject.
    for prefix in ON_DEMAND:
        importlib.import_module(prefix)
