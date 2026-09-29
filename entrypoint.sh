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

# ⚠️ Fix 2026-09-29 (review ngoài, 2 vòng): vòng 1 phát hiện 13 rule iLO/RAID mới (2 vòng mở
# rộng ngoài RAID) chỉ nằm trong DEFAULT_RULES (seed_alert_rules.py) — không có bước nào trong
# deploy.sh/entrypoint.sh từng tự chạy seed, nên nếu quên chạy tay `manage.py seed_alert_rules`
# sau deploy, code thu thập dữ liệu chạy đúng nhưng KHÔNG rule nào tồn tại để cảnh báo (silent,
# không lỗi gì để lộ ra). ⚠️ Vòng 2: seed CẢ 26 rule (toàn bộ DEFAULT_RULES) vô điều kiện mỗi
# lần app khởi động có tác dụng phụ SAI — `seed_alert_rules` chỉ so khớp theo TÊN, không phân
# biệt được "rule chưa từng tạo" với "rule đã bị xoá chủ động qua UI" (`rule_delete`,
# apps/alerts/views.py) — nên bất kỳ rule mặc định nào (kể cả 13 rule không liên quan iLO, vd
# "Switch CPU Critical") đã bị xoá tay sẽ tự SỐNG LẠI ở lần app khởi động kế tiếp. Fix: dùng cờ
# mới `--metric-prefix ilo_ raid_` — CHỈ seed đúng 13 rule có metric bắt đầu `ilo_`/`raid_`
# (khớp đúng phạm vi gốc của fix vòng 1), không đụng 13 rule còn lại dù chúng có bị xoá chủ ý.
# Vẫn AN TOÀN chạy mỗi lần container "app" khởi động trong phạm vi 13 rule này: mặc định (không
# `--overwrite`) command chỉ CREATE rule tên chưa tồn tại, SKIP rule đã có. Prod dùng Telegram
# (không còn Email, xem CLAUDE.md "Telegram Alerting").
echo "Seeding iLO/RAID alert rules (idempotent, chỉ 13 rule metric ilo_/raid_ — không đụng rule khác)..."
python manage.py seed_alert_rules --channels telegram --metric-prefix ilo_ raid_

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
