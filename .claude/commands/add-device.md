# /add-device

Thêm cấu hình thiết bị mới vào hệ thống monitor.

## Cách dùng
```
/add-device switch "core-sw01" 192.168.1.1 cisco
/add-device hyperv "hyperv-host-01" 192.168.10.5 windows2022
```

## Hành động
1. Validate IP format
2. Kiểm tra device name chưa tồn tại trong config/devices.yaml
3. Thêm entry vào config/devices.yaml với template phù hợp
4. Tạo alert rule template cho device
5. Kiểm tra kết nối (ping test)
6. Báo cáo kết quả
