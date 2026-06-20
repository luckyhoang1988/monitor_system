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
