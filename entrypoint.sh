#!/bin/bash
set -e

echo "Running database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput --clear

# Nếu service truyền command (vd: celery worker/beat), chạy command đó.
if [ "$#" -gt 0 ]; then
  echo "Starting custom command: $*"
  exec "$@"
fi

echo "Starting Gunicorn (UvicornWorker / ASGI)..."
exec gunicorn config.asgi:application \
    -k uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-4}" \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
