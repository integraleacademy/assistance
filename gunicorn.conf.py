import os
bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
worker_class = "gthread"
workers = 1  # Les écritures JSON historiques restent sérialisées dans un seul processus.
threads = int(os.getenv("GUNICORN_THREADS", "32"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "5000"))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "500"))
