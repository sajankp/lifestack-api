"""VM memory gauges read from /proc at scrape time (spec-112).

Spec-110 forbids adding an exporter to the 1 GB production VM. `/proc/meminfo` and
`/proc/pressure/memory` are system-wide files that a container can read, so the API can report the
host's own memory pressure with no extra process. That is what tests the "slow queries are swap
thrash" hypothesis: PSI `some avg10` is the percentage of the last 10 s that tasks stalled on memory.

Every read is best-effort: a missing or unreadable file (macOS dev box, PSI disabled in the kernel)
omits the metric instead of failing the whole `/metrics` scrape.
"""

from pathlib import Path

from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily

_KB = 1024
_registered = False


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _meminfo(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[name.strip()] = int(fields[0]) * _KB  # /proc reports kB
    return values


def _pressure_some_avg10(text: str) -> float | None:
    for line in text.splitlines():
        if line.startswith("some "):
            for token in line.split():
                if token.startswith("avg10="):
                    try:
                        return float(token.removeprefix("avg10="))
                    except ValueError:
                        return None
    return None


class HostMetricsCollector:
    def __init__(self, proc_root: Path | str = "/proc") -> None:
        self._proc = Path(proc_root)

    def describe(self):
        return []  # collected lazily; keeps registration free of any /proc read

    def collect(self):
        meminfo_text = _read(self._proc / "meminfo")
        if meminfo_text:
            mem = _meminfo(meminfo_text)
            if "MemAvailable" in mem:
                yield _gauge(
                    "lifestack_host_memory_available_bytes",
                    "Host MemAvailable (kernel estimate of memory usable without swapping)",
                    mem["MemAvailable"],
                )
            if "SwapTotal" in mem and "SwapFree" in mem:
                yield _gauge(
                    "lifestack_host_swap_used_bytes",
                    "Host swap in use",
                    mem["SwapTotal"] - mem["SwapFree"],
                )

        pressure_text = _read(self._proc / "pressure" / "memory")
        if pressure_text:
            avg10 = _pressure_some_avg10(pressure_text)
            if avg10 is not None:
                yield _gauge(
                    "lifestack_host_memory_pressure_some_avg10",
                    "Host memory PSI: % of the last 10 s some task stalled on memory",
                    avg10,
                )


def _gauge(name: str, doc: str, value: float) -> GaugeMetricFamily:
    family = GaugeMetricFamily(name, doc)
    family.add_metric([], value)
    return family


def register_host_metrics(proc_root: Path | str = "/proc") -> None:
    """Register once per process (a second `create_app()` must not raise on duplicates)."""
    global _registered
    if _registered:
        return
    REGISTRY.register(HostMetricsCollector(proc_root))
    _registered = True
