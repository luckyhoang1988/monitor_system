---
name: "source-command-add-device"
description: "Migrated source command `add-device`"
---

# source-command-add-device

Use this skill when the user asks to run the migrated source command `add-device`.

## Command Template

# /add-device

Thêm thiết bị mới vào hệ thống monitor. Thiết bị là DỮ LIỆU trong DB — không có file cấu hình YAML.

## Cách dùng
```
/add-device switch "core-sw01" 192.168.1.1 cisco
/add-device hyperv "hyperv-host-01" 192.168.10.5 microsoft
```

## Hành động
1. Validate IP; kiểm tra `Device.name` chưa tồn tại (unique) và IP chưa có trong DB.
2. Chọn đúng enum của model: `device_type` ∈ switch|router|firewall|nas|hyperv|wlan_controller|ap; `vendor` ∈ cisco|huawei|synology|microsoft; `protocol` ∈ snmp|ssh|winrm|ping. Thiết bị mạng/NAS poll bằng `snmp` (Huawei USG phải SNMP, không SSH); HyperV dùng `winrm`.
3. Tạo qua UI `/devices/add/` (hoặc quét CIDR rồi import ở `/devices/discovery/`). Credential (community/mật khẩu) nhập tại form — `EncryptedCharField` tự mã hoá; KHÔNG ghi vào file, log hay commit.
4. `os_family` tự detect ở lần poll đầu (sysObjectID + sysDescr) — không nhập tay.
5. Kiểm tra kết nối: nút Test (`/devices/<id>/test/`) hoặc `docker compose exec -T worker python manage.py poll_device <tên|id> [--json]` (SNMP ACL của thiết bị thường chỉ cho monitorsrv nên không chạy được từ máy dev).
6. Rule cảnh báo áp theo `device_type` (rule `all` hoặc theo loại), không tạo rule riêng từng thiết bị; xem/tuỳ chỉnh ở `/alerts/rules/`, seed mặc định bằng `manage.py seed_alert_rules`.
7. Báo cáo kết quả (không in credential).
