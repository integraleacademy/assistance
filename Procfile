web: gunicorn crm_app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads ${GUNICORN_THREADS:-16} --timeout 120 --max-requests 0
