# /research-oids

Tìm kiếm và tổng hợp OID SNMP cho một vendor/model thiết bị cụ thể.

## Cách dùng
```
/research-oids cisco catalyst9300
/research-oids hp aruba-2930f
/research-oids juniper ex4300
```

## Hành động
1. Tìm kiếm MIB files và OID documentation cho vendor/model
2. Liệt kê OID quan trọng: CPU, Memory, Interface, Error counters
3. Tạo file `config/oids/{vendor}_{model}.yaml` với OID mapping
4. Ghi chú SNMP version support (v1/v2c/v3)
5. Test OID bằng snmpwalk nếu có thiết bị thật

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
# config/oids/cisco_catalyst9300.yaml
vendor: cisco
model: catalyst9300
snmp_versions: [v2c, v3]
oids:
  cpu_5min: 1.3.6.1.4.1.9.2.1.58.0
  cpu_1min: 1.3.6.1.4.1.9.2.1.57.0
  memory_used: 1.3.6.1.4.1.9.2.1.8.0
  memory_free: 1.3.6.1.4.1.9.2.1.6.0
interfaces:
  table: 1.3.6.1.2.1.2.2
  in_octets: 1.3.6.1.2.1.2.2.1.10
  out_octets: 1.3.6.1.2.1.2.2.1.16
```
