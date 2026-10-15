# /design-collector

Thiết kế collector cho một loại thiết bị mới. CHỈ làm khi có thiết bị thật để verify (xem CLAUDE.md "Nguyên tắc làm việc") — thiết kế trên giấy cho hãng chưa có thiết bị từng gây silent-fail.

## Cách dùng
```
/design-collector switch cisco catalyst9300
/design-collector hyperv windows-server-2022
```

## Prompt template
Dựa trên thông tin thiết bị được cung cấp, hãy:
1. Liệt kê các metrics cần thu thập (đối chiếu `NormalizedData` ở `apps/collectors/base.py`)
2. Chọn giao thức (SNMP / SSH / WinRM / Redfish) và nêu cách verify trên thiết bị thật
3. Thiết kế theo adapter pattern hiện có: `BaseCollector.collect_raw()` → `BaseAdapter.normalize()` → `NormalizedData`
4. Viết skeleton có type hints và xử lý lỗi (walk lỗi ≠ bảng rỗng; không nuốt `SoftTimeLimitExceeded`)
5. Đề xuất OID/command list cụ thể, đánh dấu rõ cái đã verify và cái chưa
6. Tạo test không cần thiết bị thật (session SNMP/WinRM giả)

## Đầu ra (khớp cấu trúc repo)
- `apps/collectors/<collector>.py` + đăng ký chọn collector ở `apps/collectors/factory.py`
- `apps/collectors/adapters/<os_family>.py` + registry trong `apps/collectors/adapters/__init__.py`
- `oids/<os_family>.yaml`
- `tests/collectors/test_<module>.py`
