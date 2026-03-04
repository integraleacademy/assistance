import os

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
worker_class = "gthread"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
