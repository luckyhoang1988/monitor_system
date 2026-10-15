# /research-oids

Tìm kiếm và tổng hợp OID SNMP cho một vendor/model thiết bị cụ thể.

## Cách dùng
```
/research-oids cisco catalyst9300
/research-oids huawei s5735
/research-oids synology ds920
```

## Hành động
1. Tìm kiếm MIB files và OID documentation cho vendor/model
2. Liệt kê OID quan trọng: CPU, Memory, Interface, Error counters
3. Tạo/cập nhật `oids/{os_family}.yaml` (vd `oids/huawei_vrp.yaml`) với OID mapping — file chỉ là dữ liệu, collector `apps/collectors/switch_snmp.py` + adapter phải đọc đúng khoá đó
4. Ghi chú SNMP version support (v1/v2c/v3); v3 chỉ chạy khi có backend easysnmp, prod đang dùng pysnmp v1/v2c
5. Verify bằng walk trên worker (`docker compose exec -T worker python manage.py shell` hoặc lệnh `verify_*_oids`) — SNMP ACL của thiết bị thường chỉ cho monitorsrv

## Xác minh runtime cột bảng (BẮT BUỘC khi không có MIB)
Số cột `.x` trong bảng SNMP khác nhau theo firmware → **không tin số đếm/giá trị
nếu chưa đối chiếu với mốc thật**. Quy trình đã dùng cho Huawei AC6508 (2026-06):

1. **Walk subtree, gom theo cột**: dùng `python manage.py verify_wlan_oids <id> --parent <oid>`
   (hoặc `snmp_walk_pairs` qua container) để liệt kê các cột và số dòng.
2. **Lấy MỐC SỰ THẬT** từ thiết bị: Web UI / CLI (`display station all-number`,
   `display ap all`…). Vd Web UI báo Total user = 586 (2.4G 75 + 5G 511).
3. **Tìm cột khớp mốc**: lọc giá trị/“tổng cột” bằng đúng con số mốc. Vd tổng
   cột `.44` = 588 ≈ 586 → đó là số client/AP hiện tại.
4. **Phân biệt cột tĩnh vs động**: poll 2 lần cách ~75s.
   - Bất biến → ngưỡng/cấu hình (vd max-sta `.17`=512, `.33/.34`).
   - Chỉ tăng đơn điệu → counter tích lũy (bytes/packets).
   - Dao động LÊN/XUỐNG → giá trị “hiện tại” (số user, RSSI…).
   - ⚠️ Có thể NHIỀU cột cùng dao động (vd `.41` lẫn `.44`) → phải dùng mốc
     (bước 3) để chọn đúng. `.41` từng bị gán nhầm vì cũng dao động.
5. Ghi rõ trong YAML: cột đúng + lý do + cảnh báo cột dễ nhầm.

## Giới hạn cần lưu
- Một số AC không expose bảng STA chi tiết qua SNMP (hwWlanStaInfoTable rỗng) →
  chỉ lấy được SỐ LƯỢNG client/AP, không liệt kê từng client.

## Output format
```yaml
# oids/cisco_ios.yaml (cấu trúc thật — mở file để lấy đủ khoá)
os_family: cisco_ios
snmp_versions: [v2c, v3]
cpu:
  cpu_5min: "1.3.6.1.4.1.9.2.1.58.0"      # OLD-CISCO-CPU-MIB
memory:
  mem_processor_used: "1.3.6.1.4.1.9.9.48.1.1.1.5.1"   # CISCO-MEMORY-POOL-MIB
  mem_processor_free: "1.3.6.1.4.1.9.9.48.1.1.1.6.1"
vlan:
  vm_vlan: "1.3.6.1.4.1.9.9.68.1.2.2.1.2"        # CISCO-VLAN-MEMBERSHIP-MIB
```
