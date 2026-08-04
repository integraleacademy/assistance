import os
bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
worker_class = "gthread"
workers = 1  # Socket.IO: conserver un processus sans sticky sessions/coordination complète.
threads = int(os.getenv("GUNICORN_THREADS", "16"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
max_requests = 0
