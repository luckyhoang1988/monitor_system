# /design-collector

Thiết kế collector module cho một loại thiết bị cụ thể.

## Cách dùng
```
/design-collector switch cisco catalyst9300
/design-collector hyperv windows-server-2022
```

## Prompt template
Dựa trên thông tin thiết bị được cung cấp, hãy:
1. Liệt kê các metrics quan trọng cần thu thập
2. Xác định giao thức tốt nhất (SNMP / SSH / WMI / API)
3. Thiết kế class collector với interface chuẩn
4. Viết code skeleton với type hints và error handling
5. Đề xuất OID list hoặc WMI class list cụ thể
6. Tạo test case cơ bản

Đầu ra phải bao gồm file: `collectors/{device_type}/{vendor}_collector.py`
