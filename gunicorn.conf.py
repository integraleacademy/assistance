import os
import threading
import time

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
worker_class = "gthread"
workers = 1  # Les écritures JSON historiques restent sérialisées dans un seul processus.
# Un pic de requêtes ne doit pas créer 32 copies simultanées du JSON métier.
threads = int(os.getenv("GUNICORN_THREADS", "8"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "120"))
# Protection complémentaire, sans arrêt brutal ni nouveau processus concurrent.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "1500"))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "150"))

_memory_log_lock = threading.Lock()
_next_memory_log = 0.0


def post_request(worker, req, environ, resp):
    """Sample current process RSS, not a lifetime peak, at most once/minute."""
    global _next_memory_log
    if time.monotonic() < _next_memory_log:
        return
    if not _memory_log_lock.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if now < _next_memory_log:
            return
        _next_memory_log = now + 60
        try:
            with open("/proc/self/statm", encoding="ascii") as source:
                resident_pages = int(source.read().split()[1])
            rss_mib = resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
            worker.log.info("crm_memory rss_mib=%.1f requests=%s pid=%s",
                            rss_mib, getattr(worker, "nr", 0), os.getpid())
        except (OSError, ValueError, IndexError):
            # Non-Linux/test environments must never fail a request for metrics.
            pass
    finally:
        _memory_log_lock.release()
