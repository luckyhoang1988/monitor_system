# Project Rules — Monitor System

## Code quality
- Bắt buộc type hints cho tất cả functions và methods
- Không hard-code IP, username, password — dùng config/devices.yaml + .env
- Mọi network operation phải có timeout (mặc định 30s)
- Retry logic: 3 lần với exponential backoff cho SNMP/WMI
- Log level: DEBUG khi dev, INFO khi prod

## File naming
- Collector: `collectors/{type}/{vendor}_collector.py` (vd: `collectors/switch/cisco_collector.py`)
- Test: `tests/test_{module_name}.py`
- Config: `config/{category}.yaml`
- OID mapping: `config/oids/{vendor}_{model}.yaml`

## Security rules (QUAN TRỌNG)
- KHÔNG bao giờ commit file `.env` lên git
- KHÔNG log SNMP community string hay WinRM password
- SNMP v3 preferred over v2c khi thiết bị hỗ trợ
- WinRM: dùng HTTPS (port 5986), không dùng HTTP (5985) trên production
- Credentials trong code → BUG nghiêm trọng, fix ngay

## Testing rules
- Mỗi collector class phải có mock test (không cần thiết bị thật)
- Integration test để riêng trong `tests/integration/`, chỉ chạy khi có thiết bị
- Minimum 80% code coverage cho core modules

## Git rules
- Commit message: `feat:`, `fix:`, `docs:`, `test:`, `refactor:` prefix
- Không commit: `.env`, `*.pyc`, `config/devices.yaml` (nếu có credentials)
- Branch: `feature/`, `fix/`, `docs/`

## Review checklist trước khi implement
1. [ ] Đã kiểm tra OID/WMI class trên tài liệu vendor chưa?
2. [ ] Đã test kết nối cơ bản (ping, SNMP walk, WinRM test) chưa?
3. [ ] Credentials đã dùng .env chưa?
4. [ ] Có timeout và retry chưa?
5. [ ] Có unit test chưa?
