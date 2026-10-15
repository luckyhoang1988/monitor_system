# Monitor System — AGENTS.md

Hướng dẫn cho agent (Codex và các công cụ đọc `AGENTS.md`) làm việc trong repo này.

> **Nguồn sự thật đầy đủ là [CLAUDE.md](CLAUDE.md).** File này cố ý chỉ là bản rút gọn: trước đây nó là bản sao
> nguyên văn của CLAUDE.md nên lệch ngay khi CLAUDE.md đổi. Đọc CLAUDE.md (và `.claude/commands/deploy.md`) trước khi sửa code.

## Dự án
Giám sát hạ tầng: switch Cisco/Huawei, firewall Huawei, NAS Synology, WLAN AC, HyperV host (WinRM) + iLO (Redfish).
Django + Celery + PostgreSQL + Redis; cảnh báo Telegram/Email; dashboard cập nhật realtime bằng SSE. Chạy prod bằng
Docker Compose trên 1 host.

## Nguyên tắc bắt buộc
1. **Không suy luận linh tinh, không đoán mò.** Kết luận (OID, root cause, mapping) phải có bằng chứng; chưa chứng minh thì nói "chưa chắc" và đi verify.
2. **Đo bằng kết quả thật trước khi đổi code** (thiết bị/DB/shell thật), rồi sửa, rồi verify lại sau deploy.
3. **Đọc tài liệu hãng/OS của từng thiết bị** — Cisco IOS ≠ IOS-XE ≠ Business; Huawei VRP ≠ YunShan. Tài liệu chung mâu thuẫn thiết bị thật đã verify → tin thiết bị thật.
4. **Đổi code = đọc `/deploy` skill trước; học được gì mới thì cập nhật skill + CLAUDE.md.**

## Quy tắc an toàn
- Repo GitHub là **PUBLIC**: không commit secret, `.env*`, bảng tính IP/mật khẩu (`*.xlsx`), cấu hình hay backup thiết bị thật, output lệnh thiết bị, dump có credential. Dùng `git add <đường dẫn>`, không `git add .`.
- Không in hay log credential, token bot, community string; probe trên thiết bị thật chỉ in số đếm.
- Credential nằm trong DB (mã hoá Fernet, `ENCRYPTION_KEY` chỉ ở `.env.production`); code mới đọc credential phải gọi `ensure_credentials_readable` trước khi dùng.
- Mỗi lần commit/push/deploy cần người dùng duyệt riêng; đồng ý lần này không kéo sang lần sau.

## Lệnh thường dùng
```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python -m pytest -q                 # cần PostgreSQL + Redis local
python -m ruff check <đường dẫn>
./deploy.sh                         # push master + rebuild app/worker/beat trên monitorsrv (chỉ khi đã được duyệt)
```

## Cấu trúc
```
apps/devices/     Device, Interface, TopologyLink; CRUD, test kết nối, discovery
apps/collectors/  SNMP/SSH/WinRM/Redfish collector + adapter + task Celery
apps/metrics/     SystemHealth, InterfaceStats, VMStats...; writer, cache Redis, rollup, Chart.js API
apps/alerts/      AlertRule, Alert, AlertNotification; engine + kênh Email/Telegram/Slack/Teams
apps/dashboard/   index + trang chi tiết theo loại thiết bị
apps/accounts/    RBAC 2 cấp (Admin/Review), không có model
apps/realtime/    SSE (publisher Redis pub/sub + async view), không có model
oids/             OID profile theo os_family
tests/            pytest-django, factory-boy (tests/conftest.py)
```

## Đọc mục nào của CLAUDE.md khi làm việc gì
| Việc | Mục |
|---|---|
| Collector / OID / vendor | "OID đã xác minh runtime" |
| Online/offline, badge, cảnh báo offline | "Online/offline — poll + dashboard đếm" |
| Realtime/SSE, JS trong template | "Realtime — SSE push" và quy tắc `unlocalize` (số Django nhúng vào JS) ở "Nguyên tắc & convention" |
| Metrics ghi Redis thay vì DB | "Cache-first metrics" |
| HyperV, WinRM, giới hạn dòng lệnh | "HyperV Host Performance Counters" |
| iLO / RAID / phần cứng | "iLO Redfish" |
| Celery Beat, `expire_seconds` | "Celery Beat" |
| Lịch sử thay đổi, bẫy đã dính, việc còn mở | "Thay đổi quan trọng" |
