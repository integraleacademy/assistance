"""Small Linux memory samples, including periods with no HTTP requests."""
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import threading
import time


def memory_sample():
    sample = {"pid": os.getpid()}
    try:
        pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
        sample["rss_mib"] = round(pages * os.sysconf("SC_PAGE_SIZE") / 2**20, 1)
    except (OSError, ValueError, IndexError):
        pass
    for name, paths in {
        "container_mib": ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        "limit_mib": ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    }.items():
        for path in paths:
            try:
                value = int(Path(path).read_text(encoding="ascii").strip())
                if 0 < value < 2**60:
                    sample[name] = round(value / 2**20, 1)
                break
            except (OSError, ValueError):
                continue
    return sample


def log_memory(phase, logger=None, **values):
    """Diagnostics must never interrupt a request or a background sync."""
    try:
        values = {**memory_sample(), **values}
        (logger or logging.getLogger("gunicorn.error")).info(
            "crm_memory phase=%s %s", phase,
            " ".join(f"{key}={value}" for key, value in values.items()),
        )
    except Exception:
        pass


@contextmanager
def memory_phase(name):
    started = time.monotonic()
    log_memory(name + "_start")
    try:
        yield
    finally:
        log_memory(name + "_end", duration_ms=round((time.monotonic() - started) * 1000))


def start_memory_monitor(logger, interval=60):
    stop = threading.Event()

    def run():
        while not stop.is_set():
            log_memory("periodic", logger)
            if stop.wait(interval):
                return

    threading.Thread(target=run, name="crm-memory-monitor", daemon=True).start()
    return stop
