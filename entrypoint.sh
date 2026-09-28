#!/bin/bash
set -e

# ⚠️ migrate/sync_beat_expires/collectstatic CHỈ chạy ở nhánh KHÔNG có command riêng
# (tức container "app" — worker/beat luôn truyền `command:` celery trong docker-compose.yml).
# Trước đây cả 3 container (app/worker/beat, cùng ENTRYPOINT này) đều chạy migrate mỗi lần
# khởi động — `deploy.sh`/`docker compose up -d --build app worker beat` start gần như đồng
# thời → 2-3 container cùng gọi `manage.py migrate` (đua tranh áp schema) + cùng
# `collectstatic --clear` (xoá rồi ghi static trong khi container khác cũng đang xoá/ghi,
# nginx đọc `static_volume` có thể trúng khoảng trống giữa lúc --clear). Nay chỉ "app" migrate
# 1 lần; worker/beat qua nhánh `exec "$@"` bên dưới, khởi động thẳng process celery của nó.
# Thứ tự an toàn (worker/beat không chạy trước khi DB đã migrate xong) đảm bảo ở tầng
# docker-compose.yml: `worker`/`beat` có `depends_on: app: condition: service_healthy`
# (app chỉ "healthy" sau khi migrate xong + gunicorn lên) — xem docker-compose.yml.
if [ "$#" -gt 0 ]; then
  echo "Starting custom command: $*"
  exec "$@"
fi

echo "Running database migrations..."
python manage.py migrate --noinput

echo "Syncing celery beat expire_seconds..."
python manage.py sync_beat_expires

echo "Collecting static files..."
python manage.py collectstatic --noinput --clear

echo "Starting Gunicorn (UvicornWorker / ASGI)..."
exec gunicorn config.asgi:application \
    -k uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-4}" \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
