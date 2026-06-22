# Monitor System — CLAUDE.md

## Mục tiêu
Giám sát hạ tầng mạng + ảo hoá doanh nghiệp:
- **Switch**: Cisco (IOS / IOS-XE), Huawei (VRP — S5700/S6700/S9300)
- **HyperV**: VM health, host resources, replication, snapshot

## Tech Stack
| Tầng | Công nghệ |
|---|---|
| Web | Django 5.x + Bootstrap 5 |
| DB | PostgreSQL (prod) / SQLite (dev) |
| Task | Celery + Redis + django-celery-beat |
| SSH | Netmiko (cisco_ios, huawei_vrp) |
| SNMP | pysnmp + easysnmp |
| HyperV | pywinrm + PowerShell |
| Charts | Chart.js (AJAX) |
| Alert | Email SMTP + Telegram Bot |

## Luồng dữ liệu
```
Celery Beat (5 phút)
  → CollectorFactory → SNMP / SSH / WinRM
  → Adapter (normalize theo vendor)
  → MetricWriter → PostgreSQL
  → evaluate_alert_rules → Email / Telegram
  → Django Views + Chart.js → Dashboard
```

## Cấu trúc Apps
```
apps/
├── devices/      # Device, Interface CRUD + test connection
├── collectors/   # SNMP/SSH/WinRM collector + adapter Cisco/Huawei + tasks
├── metrics/      # InterfaceStats, SystemHealth, VMStats + writer + Chart.js API
├── alerts/       # AlertRule CRUD + engine + dedup + Email/Telegram
└── dashboard/    # Dashboard index, switch_detail, hyperv_detail
```

## Nguyên tắc thiết kế
- `vendor` (cisco / huawei) trong Device — không cần biết model cụ thể
- `os_family` tự detect khi poll đầu tiên qua sysObjectID + sysDescr
- OID profiles: `oids/cisco_ios.yaml`, `oids/cisco_iosxe.yaml`, `oids/huawei_vrp.yaml`
- Interface metrics dùng standard MIB-II — không phụ thuộc vendor/model
- Adapter pattern: `collect_raw()` → `normalize()` → `MetricWriter.save_metrics()`

## Conventions
- Timestamps: UTC (`USE_TZ = True`, display `Asia/Ho_Chi_Minh`)
- Credentials: lưu trong `Device.ssh_password` / `Device.snmp_community`
- **Không hard-code** IP, password, community string
- Type hints bắt buộc cho collector/adapter
- Log: `logger.info("Device %s: CPU %.1f%%", device.name, value)`

## OID đã xác minh runtime (theo từng OS-family)
> Đã quét fleet thật 16 thiết bị (2026-06). Ghi lại để không lặp lỗi gán nhầm OID.

### Huawei VRP / YunShan OS — `hwEntityResourceTable` (1.3.6.1.4.1.2011.5.25.31.1.1.1.1.X)
| Cột | OID đầy đủ | Ý nghĩa | Dùng |
|---|---|---|---|
| `.5` | `...1.1.1.1.5` | **hwEntityCpuUsage** (CPU % thật) | ✅ CPU |
| `.6` | `...1.1.1.1.6` | hwEntityCpuUsageThreshold (NGƯỠNG, mặc định 90/95) | ❌ KHÔNG phải CPU |
| `.7` | `...1.1.1.1.7` | **hwEntityMemUsage** (Memory % thật) | ✅ Memory |
- **CẢNH BÁO**: từng gán nhầm CPU→`.6` (ngưỡng) → mọi switch báo CPU 90-95% giả; Mem→`.5` (CPU). Đã fix.
- Scalar `.0` thường trống → collector walk table, lấy entity "MPU Board"/mainboard (giá trị > 0).
- VRP V5 (S5735 V200R021) và YunShan OS (CloudEngine S5735-L-V2 V600R023/024) **dùng chung cấu trúc OID này**.
- **Firewall USG6525E (USG6500E, VRP V600R007C20SPC600)** — ĐÃ xác minh 2026-06: **dùng chung `hwEntityResourceTable` .5/.7** y hệt switch (entity MPU index 67108873 → CPU/Mem khớp `display cpu-usage`/`display memory-usage`). Collector `huawei_vrp` chạy nguyên không cần OID riêng.
  - Lưu ý SSH: USG **chỉ cấp exec-channel, từ chối PTY** → netmiko `huawei_vrp` (invoke_shell) fail "Channel closed". Với firewall này phải poll bằng SNMP (hoặc exec-channel paramiko 1 lệnh/phiên, gửi `system-view\n…\nquit` trong 1 lần).

### Cisco IOS classic (C2960X...) — OK
- CPU: OLD-CISCO-CPU-MIB `1.3.6.1.4.1.9.2.1.58.0` (5min). Mem: CISCO-MEMORY-POOL-MIB pool `.1`.

### Cisco Business / SMB (Catalyst 1200/1300, CBS250/350)
- CPU: `rlCpuUtil` `1.3.6.1.4.1.9.6.1.101.1.9.0`. **Memory KHÔNG expose qua SNMP → mem=0** (giới hạn phần cứng, không phải bug).

### Cisco IOS-XE — ⚠️ CHƯA kiểm chứng runtime (không có thiết bị trong fleet)
- CPU/mem hard-code index `.1` (`...109.1.1.1.1.5.1`, pool `.5.1/.6.1`). Cần walk/verify khi có thiết bị IOS-XE thật (index có thể khác trên stack/multi-RP).

### Interface (mọi vendor) — MIB-II standard, OK
- Dùng 64-bit HC counters (`ifHCInOctets/Out` = `.31.1.1.1.6/.10`). Fleet hiện tại đều hỗ trợ HC.

## Chạy dev
```bash
cp .env.example .env          # điền giá trị
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver

# Terminal riêng
celery -A config worker -l info
celery -A config beat -l info
```

## Trạng thái các Phase
| Phase | Nội dung | Trạng thái |
|---|---|---|
| 1 | Project setup, models, collector skeleton, OID profiles | ✅ |
| 2 | SNMP/SSH collector hoàn chỉnh + unit tests | ✅ |
| 3 | Celery automation + HyperV WinRM | ✅ |
| 4 | Django dashboard + Chart.js + login/logout | ✅ |
| 5 | Alert engine + Email + Telegram + Rule CRUD UI | ✅ |
| 6 | Docker Compose + Production deploy | ⏳ chưa làm |

### Phase 6 — việc cần làm
- `Dockerfile` cho Django app (gunicorn + whitenoise)
- `docker-compose.yml`: app + postgres + redis + celery worker + celery beat
- `nginx/nginx.conf`: reverse proxy + static files
- `.env.production`: template đầy đủ cho prod
- `entrypoint.sh`: migrate + collectstatic + start gunicorn

## File quan trọng
| File | Mô tả |
|---|---|
| [apps/collectors/base.py](apps/collectors/base.py) | BaseCollector, BaseAdapter, NormalizedData |
| [apps/collectors/switch_snmp.py](apps/collectors/switch_snmp.py) | SNMP collector + auto-detect os_family |
| [apps/collectors/switch_ssh.py](apps/collectors/switch_ssh.py) | SSH collector (Netmiko) |
| [apps/collectors/factory.py](apps/collectors/factory.py) | CollectorFactory |
| [apps/metrics/writer.py](apps/metrics/writer.py) | Ghi metrics vào DB, tính delta Mbps |
| [apps/alerts/engine.py](apps/alerts/engine.py) | Alert rule evaluation + deduplication |
| [oids/](oids/) | OID profiles YAML per vendor |
| [config/settings/production.py](config/settings/production.py) | Production settings |
| [requirements/prod.txt](requirements/prod.txt) | Production dependencies |
