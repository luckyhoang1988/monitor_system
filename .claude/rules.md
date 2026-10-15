# Project Rules — Monitor System

## Code quality
- Bắt buộc type hints cho tất cả functions và methods
- Không hard-code IP, username, password — credential thiết bị nằm trong DB (mã hoá), secret ứng dụng ở `.env`/`.env.production`
- Mọi network operation phải có timeout (mặc định 30s)
- Retry logic: 3 lần với exponential backoff cho SNMP/WMI
- Log level: DEBUG khi dev, INFO khi prod

## File naming
- Collector: `apps/collectors/{module}.py`; adapter theo OS: `apps/collectors/adapters/{os_family}.py`
- Test: `tests/{app}/test_{module_name}.py` (vd `tests/collectors/test_topology_walk_failure.py`)
- Cấu hình: `config/settings/*.py` + biến môi trường (`.env.example` liệt kê khoá)
- OID mapping: `oids/{os_family}.yaml`

## Security rules (QUAN TRỌNG)
- KHÔNG bao giờ commit file `.env` lên git
- KHÔNG log SNMP community string hay WinRM password
- SNMP v3 ưu tiên khi thiết bị VÀ backend đều hỗ trợ (hiện prod dùng pysnmp: chỉ v1/v2c; v3 cần easysnmp)
- WinRM: dùng HTTPS (port 5986), không dùng HTTP (5985) trên production
- Credentials trong code → BUG nghiêm trọng, fix ngay

## Testing rules
- Mỗi collector class phải có mock test (không cần thiết bị thật)
- Integration test để riêng trong `tests/integration/`, chỉ chạy khi có thiết bị
- Minimum 80% code coverage cho core modules

## Git rules
- Commit message: `feat:`, `fix:`, `docs:`, `test:`, `refactor:` prefix
- Không commit: `.env*` (trừ `.env.example`), `*.pyc`, bảng tính IP/mật khẩu (`*.xlsx`), cấu hình/backup thiết bị thật, dump DB/log có credential — repo GitHub là PUBLIC
- Branch: `feature/`, `fix/`, `docs/`

## Review checklist trước khi implement
1. [ ] Đã kiểm tra OID/WMI class trên tài liệu vendor chưa?
2. [ ] Đã test kết nối cơ bản (ping, SNMP walk, WinRM test) chưa?
3. [ ] Credential thiết bị đã đi qua field mã hoá (không hard-code, không log) chưa?
4. [ ] Có timeout và retry chưa?
5. [ ] Có unit test chưa?
