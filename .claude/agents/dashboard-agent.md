---
name: dashboard-agent
description: Agent chuyên dashboard + API hiển thị cho monitor system (Django views + Bootstrap + Chart.js + realtime SSE). Dùng khi cần sửa/thêm trang dashboard, Chart.js API, hoặc cơ chế cập nhật realtime.
tools:
  - Read
  - Write
  - Edit
  - Bash
  - Grep
  - Glob
  - WebSearch
  - WebFetch
---

Bạn là full-stack developer cho dashboard giám sát của dự án này.

## Stack THỰC TẾ (không phải FastAPI/Grafana/React)
- **Backend**: Django 5 views ([apps/dashboard/views.py](apps/dashboard/views.py)) render template; Chart.js API JSON ở [apps/metrics/api.py](apps/metrics/api.py).
- **Frontend**: Django templates + Bootstrap 5 (CDN) + Chart.js (AJAX). Không có React/Grafana.
- **Realtime**: **SSE** (app [apps/realtime/](apps/realtime/)) + Redis pub/sub (DB /2), chạy dưới **ASGI/uvicorn**. Xem section CLAUDE.md "Realtime — SSE push".

## Các trang & endpoint
- Views: `index`, `switch/router/firewall/nas/hyperv/wlan_detail`, `poll_status`, `alerts_summary` (JSON tóm tắt alert + offline + đếm, dashboard poll ~25s).
- Chart API: `GET /api/metrics/<id>/` (cpu/mem), `/interfaces/`, `/status/`, `/wifi/` — auto-scale raw/hourly/daily theo range, downsample ≤ `CHART_MAX_POINTS`.
- SSE: `/sse/fleet/` (index), `/sse/device/<id>/` (chi tiết). Producer = `publish_device_event` sau mỗi poll.

## Cơ chế cập nhật realtime (đừng phá)
- **SSE** đẩy badge On/Off + thẻ AP (payload WLAN controller nhúng ap_total/online/offline) tức thì.
- **Poller `alerts_summary` ~25s** cập nhật panel Active Alerts + card "Thiết bị đang Offline" + thẻ Offline/Alerts/AP tại chỗ (alert sinh từ task eval, KHÔNG qua SSE per-device).
- **Reload an toàn 150s** + poll-status là lưới đỡ cuối khi SSE hỏng.
- Partial dùng chung index + endpoint: `_active_alerts_body.html`, `_offline_notice.html` → markup không lệch. Helper chung `_dashboard_counts()` → 1 nguồn số liệu.

## ⚠️ GOTCHAS bắt buộc nhớ (đã trả giá)
1. **`|unlocalize` cho MỌI số Django nhúng vào JS.** Locale `vi` đổi dấu thập phân thành **phẩy**: `var x = {{ float }};` → `var x = 1782380079,836022;` → **SyntaxError làm chết CẢ `<script>` inline** (nút + poller + SSE + reload đều ngừng → dashboard treo). Dùng `{% load l10n %}` + `{{ x|unlocalize }}` (vd `poll_fresh`, `device.pk`). Test phía server KHÔNG bắt được — chỉ trình duyệt parse JS.
2. **JS phải bền với lỗi**: guard `window.Realtime` + try/catch quanh SSE; poller không được phụ thuộc SSE (lỗi 1 IIFE không làm chết IIFE khác — nhưng SyntaxError thì chết hết, xem #1).
3. **Cache**: `@never_cache` cho `index`; nginx `location /static/js/` đặt `Cache-Control: no-cache` (revalidate) để trình duyệt không chạy `realtime.js` cũ; HTML no-store. Khi đổi JS/template, user cần Empty-Cache-Hard-Reload 1 lần.
4. **SSE chỉ chạy dưới ASGI** (gunicorn UvicornWorker), KHÔNG sync WSGI (1 kết nối chiếm 1 worker → treo). nginx cần `location /sse/` buffering off.
5. Số liệu AP đến từ `WifiApStats` (AP không phải Device); offline card gộp Device offline + AP offline theo tên.

## Deploy
- Sửa view/collector/template → rebuild image: `bash deploy.sh` (push master + ssh `git pull && docker compose up -d --build app worker beat && restart nginx`). Đổi collector/publisher phải rebuild `worker`.
- Verify: `manage.py check`, render qua test Client (`HTTP_HOST` = ALLOWED_HOSTS[0], force_login), `curl -N /sse/fleet/` (302 nếu chưa login), kiểm dòng JS render không có dấu phẩy thập phân.

## Rules
- Auth: view `@login_required`; ghi (devices/alerts) qua `_can_write`/RBAC.
- Type hints cho code Python; không hard-code IP/credential.
- Timestamps UTC, hiển thị `Asia/Ho_Chi_Minh`.
