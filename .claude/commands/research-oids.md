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
