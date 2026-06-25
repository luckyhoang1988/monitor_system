# Monitor System — CLAUDE.md

## Mục tiêu
Giám sát hạ tầng mạng + ảo hoá: **Switch** Cisco (IOS/IOS-XE) & Huawei (VRP — S5700/S6700/S9300); **HyperV** (VM health, host resources, replication, snapshot).

## Tech Stack
Django 5.x + Bootstrap 5 · PostgreSQL (prod) / SQLite (dev) · Celery + Redis + django-celery-beat · Netmiko (cisco_ios/huawei_vrp) · pysnmp + easysnmp · pywinrm + PowerShell · Chart.js (AJAX) · **Realtime: SSE (async Django view) qua ASGI/uvicorn + Redis pub/sub** · Alert: Email SMTP + Telegram.

## Luồng dữ liệu
```
Celery Beat (120s) → CollectorFactory → SNMP/SSH/WinRM
  → Adapter (normalize theo vendor) → MetricWriter → DB
  → evaluate_alert_rules → Email/Telegram
  → Django Views + Chart.js → Dashboard
  → publish_device_event → Redis pub/sub → SSE async view → EventSource (cập nhật realtime, không reload)
```

## Cấu trúc Apps
```
apps/
├── devices/      # Device, Interface CRUD + test connection
├── collectors/   # SNMP/SSH/WinRM collector + adapter Cisco/Huawei + tasks
├── metrics/      # InterfaceStats, SystemHealth, VMStats + writer + Chart.js API
├── alerts/       # AlertRule CRUD + engine + dedup + Email/Telegram
├── dashboard/    # index, switch/hyperv/wlan/firewall_detail
├── accounts/     # RBAC 2 cấp (Admin/Review) + UI quản lý user (không có model)
└── realtime/     # SSE push: publisher (Redis pub/sub) + async stream view (không có model)
```

## Nguyên tắc & convention
- `vendor` (cisco/huawei) trong Device; `os_family` tự detect khi poll đầu qua sysObjectID + sysDescr.
- OID profiles: `oids/{cisco_ios,cisco_iosxe,huawei_vrp}.yaml`. Interface metrics dùng MIB-II chuẩn, không phụ thuộc vendor/model.
- Adapter pattern: `collect_raw()` → `normalize()` → `MetricWriter.save_metrics()`.
- Timestamps UTC (`USE_TZ=True`, display `Asia/Ho_Chi_Minh`). Credentials trong `Device.ssh_password`/`snmp_community`.
- **Không hard-code** IP/password/community. Type hints bắt buộc cho collector/adapter.
- Log: `logger.info("Device %s: CPU %.1f%%", device.name, value)`.
- ⚠️ **Số Django nhúng vào JS phải `{{ x|unlocalize }}`** (`{% load l10n %}`). Locale `vi` đổi dấu thập phân thành **phẩy** → `var x = 1782380079,836022;` là **SyntaxError làm chết CẢ `<script>` inline** (nút, poller, SSE, reload đều ngừng → dashboard treo). Test phía server KHÔNG bắt được — chỉ trình duyệt parse JS. Đã áp dụng cho `poll_fresh`, `device.pk`.

## OID đã xác minh runtime (fleet thật 16 thiết bị, 2026-06)
> Ghi lại để không lặp lỗi gán nhầm OID.

**Huawei VRP / YunShan** — `hwEntityResourceTable` `1.3.6.1.4.1.2011.5.25.31.1.1.1.1.X`:
- `.5` = **hwEntityCpuUsage** (CPU% ✅) · `.6` = CpuUsageThreshold (NGƯỠNG 90/95, ❌ không phải CPU) · `.7` = **hwEntityMemUsage** (Mem% ✅).
- ⚠️ **Từng gán nhầm CPU→`.6`** (mọi switch báo CPU 90-95% giả) và Mem→`.5`. Đã fix.
- Scalar `.0` thường trống → walk table, lấy entity "MPU Board"/mainboard (giá trị > 0).
- Dùng chung cho VRP V5 (S5735 V200R021), YunShan (CloudEngine S5735-L-V2 V600R023/024), **và firewall USG6525E** (VRP V600R007C20SPC600, entity MPU 67108873) — collector `huawei_vrp` chạy nguyên.
- ⚠️ **USG từ chối PTY** (chỉ exec-channel) → netmiko `huawei_vrp` fail "Channel closed" → firewall phải poll **SNMP** (hoặc exec-channel paramiko gửi `system-view\n…\nquit` trong 1 phiên).

**Cisco**:
- IOS classic (C2960X): CPU `1.3.6.1.4.1.9.2.1.58.0` (OLD-CISCO-CPU 5min), Mem pool `.1`.
- Business/SMB (Catalyst 1200/1300, CBS250/350): CPU `rlCpuUtil 1.3.6.1.4.1.9.6.1.101.1.9.0`. **Mem KHÔNG expose SNMP → mem=0** (giới hạn HW, không phải bug).
- IOS-XE — ⚠️ **CHƯA kiểm chứng** (không có thiết bị): CPU/mem hard-code index `.1`; cần walk/verify khi có thiết bị thật (index khác trên stack/multi-RP).

**Interface (mọi vendor)** — MIB-II, dùng 64-bit HC counters `ifHCInOctets/Out` = `.31.1.1.1.6/.10`.

**Access VLAN / PVID per port** (`Interface.access_vlan`, collector `_collect_access_vlans`, OID trong `oids/*.yaml` `vlan:`):
- **Cisco** (ios/iosxe/business): `vmVlan` CISCO-VLAN-MEMBERSHIP-MIB `1.3.6.1.4.1.9.9.68.1.2.2.1.2`, **index = ifIndex trực tiếp**. Chỉ access port có entry → trunk/uplink trống (đúng ý, UI hiện badge "Trunk").
- **Huawei + fallback chuẩn**: `dot1qPvid` Q-BRIDGE-MIB `1.3.6.1.2.1.17.7.1.4.5.1.1` **index = dot1dBasePort** → phải map qua `dot1dBasePortIfIndex` `1.3.6.1.2.1.17.1.4.1.2`. Cisco Business cũng dùng đường này (vmVlan rỗng).
- Chỉ lấy **access VLAN (1 số/port)**, KHÔNG lấy allowed-list trên trunk (phạm vi cố ý). UI: cột VLAN ở `switch_detail`.
- ⚠️ **OID CHƯA verify runtime trên fleet thật** (mới deploy 2026-06-23) — cần snmpwalk đối chiếu `show interface switchport`/`display port vlan`. Nếu Huawei/Business trống bất thường → mở SNMP view nhánh `1.3.6.1.2.1.17` (Q-BRIDGE).

**Huawei WLAN/AC — AC6508** (`device_type=wlan_controller`, HUAWEI-WLAN MIB `…2011.6.139`, OID đầy đủ trong `oids/huawei_vrp.yaml` `wlan:`):
- Bảng AP `hwWlanApInfoTable` `…6.139.13.3.3.1.X` (index=MAC AP): `.4` name · `.5` group · `.6` run_state (`8`=online) · `.44` = **client đang kết nối/AP** (cả 2 band, ✅).
- ⚠️ **client/AP đúng là `.44`, KHÔNG phải `.41`** (`.41`≈số khác; `.17/.33/.34` bất biến = config). Cách dò: poll 2 lần lọc cột dao động + đối chiếu Total Web UI (`/research-oids`).
- Bảng STA chi tiết **KHÔNG expose** SNMP → chỉ lấy được **số lượng** client/AP, không liệt kê từng client/MAC. Lệch nhẹ vs Web UI từng thời điểm là bình thường.
- Tool dò: `python manage.py verify_wlan_oids <device_id> --parent <oid>`.

## RBAC — 2 cấp (app `apps.accounts`, không có model riêng)
- **Admin** = group `Network Admins` (hoặc superuser): full + quản lý user. **Review** = `Read-Only Operators`: chỉ xem, write → 403.
- Nguồn sự thật: [apps/accounts/roles.py](apps/accounts/roles.py) (`is_admin/get_role/set_role`) — dùng chung với `_can_write` (devices/alerts) và `IsAdminOrReadOnly` (DRF).
- UI: `/users/` (admin-only), đổi mật khẩu `/users/password/`. Group tạo sẵn ở migration `devices/0007_create_rbac_groups`.

## Realtime — SSE push (app `apps.realtime`, không có model)
> UI cập nhật tại chỗ thay vì full page reload. Producer = Celery worker, consumer = web ASGI; bridge **bắt buộc qua Redis pub/sub** (2 process không chung bộ nhớ).

- **Producer**: [_poll_device_once](apps/collectors/tasks.py) gọi `publish_device_event(device, online, data)` **sau `device.save()`** (cả nhánh success lẫn ICMP-down), **ngoài** `atomic()` của `save_metrics` (tránh phát event cho transaction rollback). Publish **nuốt mọi exception** → Redis chết chỉ mất realtime, KHÔNG fail/retry poll.
- **Kênh** ([apps/realtime/channels.py](apps/realtime/channels.py)): `events:fleet` (index) + `events:device:<id>` (chi tiết). Redis DB **/2** riêng (suy từ `REALTIME_REDIS_URL`, mặc định đổi index từ `REDIS_URL`).
- **Consumer** ([apps/realtime/views.py](apps/realtime/views.py)): 2 **async** view (`redis.asyncio`) `@login_required`, `StreamingHttpResponse` text/event-stream, heartbeat 20s, dọn subscription khi client đóng. URL `/sse/fleet/` + `/sse/device/<id>/` (ngoài `/api/` để né rate-limit).
- **Payload** (compact JSON): `{v,type,device_id,name,device_type,online,last_seen,cpu,mem,if_up,if_total,ts}` + `ap_total/ap_online/ap_offline` khi `device_type=wlan_controller` (để thẻ Access Point cập nhật ngay sau khi AC poll). KHÔNG mang mbps từng port (mbps tính ở writer, không có trong `NormalizedData`) → trang chi tiết re-fetch `/api/.../interfaces/`.
- **Frontend** ([static/js/realtime.js](static/js/realtime.js) `Realtime.connectSSE`): index cập nhật badge On/Off tại chỗ (`tr[data-device-id]`); panel Alerts/đếm qua poll `alerts_summary` ~25s. Trang chi tiết re-fetch chart khi range là 1h/6h/24h. **Fallback**: SSE hỏng 4 lần → quay về poll-status reload (index) / setInterval (chi tiết). EventSource tự reconnect.
- **Dashboard cập nhật NGOÀI SSE**: alert sinh từ task eval (không qua SSE per-device) → index poll nhẹ `dashboard:alerts_summary` (~25s) cập nhật **panel Active Alerts + card "Thiết bị đang Offline" (gồm AP offline theo tên) + thẻ đếm** tại chỗ. Endpoint + index dùng chung helper `_dashboard_counts()` ([apps/dashboard/views.py](apps/dashboard/views.py)) và partial `_active_alerts_body.html`/`_offline_notice.html` → 1 nguồn số liệu, markup không lệch. Alert eval còn chạy **inline sau mỗi poll** trong `_poll_device_once` (sát dữ liệu; beat `evaluate_alert_rules` là safety net).
- **Chống treo/hiển thị cũ**: `@never_cache` cho view `index`; nginx `location /static/js/` đặt `Cache-Control: no-cache` (revalidate — tránh trình duyệt chạy `realtime.js` bản cũ 30d); guard `window.Realtime` + try/catch quanh SSE để lỗi SSE/JS **không làm dừng script** (nếu không poller 25s ngừng → treo). Đổi JS/template → user cần **Empty-Cache-Hard-Reload 1 lần**.
- ⚠️ **Bắt buộc ASGI**: SSE dưới sync WSGI/gunicorn chiếm trọn 1 worker/kết nối → 4 dashboard là treo. [entrypoint.sh](entrypoint.sh) chạy `gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker`. [nginx.conf](nginx/nginx.conf) có `location /sse/` riêng (`proxy_buffering off`, `read_timeout 3600s`). Deploy: đổi runtime web (WSGI→ASGI) + publish trong worker → **rebuild cả `app` lẫn `worker`** + reload nginx.
- Test SSE: `uvicorn config.asgi:application` rồi `curl -N http://127.0.0.1:8000/sse/fleet/ --cookie "sessionid=<valid>"` (thấy `: connected` → `: heartbeat`); trigger poll → bắn `event: metrics`.

## Chạy dev
```bash
cp .env.example .env && python manage.py migrate && python manage.py createsuperuser && python manage.py runserver
# Terminal riêng:
celery -A config worker -l info
celery -A config beat -l info
```

## Trạng thái
Phase 1–7 **đã hoàn thành** (setup/models → collector SNMP/SSH + tests → Celery + HyperV WinRM → dashboard + Chart.js → alert Email/Telegram + Rule CRUD → Docker/prod deploy → RBAC 2 cấp).

### Production (đang chạy)
- Server `monitorsrv` = `10.0.193.234` (SSH sẵn, user `monitorsys`); app tại `/home/monitorsys/monitor_system`.
- Docker Compose: `app` (gunicorn+**UvicornWorker/ASGI** cho SSE) + `worker` + `beat` + `db` (postgres16) + `redis` + `nginx`. Code **build vào image** (`build: .`, không bind-mount).
- Deploy: commit/push → trên server `git pull && docker compose build app worker && docker compose up -d`. Collector chạy trong `worker` → đổi OID/collector phải rebuild `worker`.
- Docker Hub không vào được: tạm `docker cp` file + `docker compose restart` (recreate sẽ mất → rebuild khi registry hồi).

## File quan trọng
| File | Mô tả |
|---|---|
| [apps/collectors/base.py](apps/collectors/base.py) | BaseCollector, BaseAdapter, NormalizedData |
| [apps/collectors/switch_snmp.py](apps/collectors/switch_snmp.py) | SNMP collector + auto-detect os_family |
| [apps/collectors/switch_ssh.py](apps/collectors/switch_ssh.py) | SSH collector (Netmiko) |
| [apps/collectors/factory.py](apps/collectors/factory.py) | CollectorFactory |
| [apps/metrics/writer.py](apps/metrics/writer.py) | Ghi metrics, tính delta Mbps |
| [apps/alerts/engine.py](apps/alerts/engine.py) | Alert rule evaluation + dedup |
| [apps/realtime/publisher.py](apps/realtime/publisher.py) | publish_device_event + build_payload (Redis pub/sub, sync) |
| [apps/realtime/views.py](apps/realtime/views.py) | Async SSE stream view (redis.asyncio) |
| [static/js/realtime.js](static/js/realtime.js) | `Realtime.connectSSE` + cập nhật badge/chart, fallback polling |
| [apps/dashboard/views.py](apps/dashboard/views.py) | index + *_detail + `alerts_summary`/`poll_status` + helper `_dashboard_counts` |
| [oids/](oids/) | OID profiles YAML per vendor |
| [config/settings/production.py](config/settings/production.py) | Production settings |
| [requirements/prod.txt](requirements/prod.txt) | Production dependencies |
