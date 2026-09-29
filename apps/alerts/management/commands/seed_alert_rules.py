"""
Seed default alert rules.

Usage:
    python manage.py seed_alert_rules
    python manage.py seed_alert_rules --channels email telegram
    python manage.py seed_alert_rules --overwrite
    python manage.py seed_alert_rules --metric-prefix ilo_ raid_
"""
from django.core.management.base import BaseCommand
from apps.alerts.models import AlertRule

DEFAULT_RULES = [
    # Switch rules
    {
        "name": "Switch CPU Critical",
        "device_type": "switch", "metric": "cpu_percent",
        "condition": "gt", "threshold": 90.0,
        "severity": "CRITICAL", "duration_min": 5,
    },
    {
        "name": "Switch CPU Warning",
        "device_type": "switch", "metric": "cpu_percent",
        "condition": "gt", "threshold": 75.0,
        "severity": "WARNING", "duration_min": 10,
    },
    {
        "name": "Switch RAM Critical",
        "device_type": "switch", "metric": "mem_percent",
        "condition": "gt", "threshold": 90.0,
        "severity": "CRITICAL", "duration_min": 5,
    },
    {
        "name": "Switch Uplink Down",
        "device_type": "switch", "metric": "if_status",
        "condition": "eq", "threshold": 0.0,
        "severity": "CRITICAL", "duration_min": 0,
    },
    {
        "name": "Switch Uplink IN High (Mbps)",
        "device_type": "switch", "metric": "uplink_in_mbps_max",
        "condition": "gt", "threshold": 800.0,
        "severity": "WARNING", "duration_min": 10,
    },
    {
        "name": "Switch Uplink OUT High (Mbps)",
        "device_type": "switch", "metric": "uplink_out_mbps_max",
        "condition": "gt", "threshold": 800.0,
        "severity": "WARNING", "duration_min": 10,
    },
    # HyperV rules
    {
        "name": "HyperV CPU Critical",
        "device_type": "hyperv", "metric": "cpu_percent",
        "condition": "gt", "threshold": 90.0,
        "severity": "CRITICAL", "duration_min": 5,
    },
    {
        "name": "HyperV RAM Critical",
        "device_type": "hyperv", "metric": "mem_percent",
        "condition": "gt", "threshold": 90.0,
        "severity": "CRITICAL", "duration_min": 5,
    },
    {
        "name": "HyperV RAM Warning",
        "device_type": "hyperv", "metric": "mem_percent",
        "condition": "gt", "threshold": 80.0,
        "severity": "WARNING", "duration_min": 10,
    },
    {
        "name": "HyperV VM Replication Unhealthy",
        "device_type": "hyperv", "metric": "vm_repl_unhealthy",
        "condition": "gt", "threshold": 0.0,
        "severity": "WARNING", "duration_min": 0,
    },
    # HyperV RAID/disk health (iLO Redfish, độc lập WinRM — xem apps/collectors/ilo_redfish.py).
    # duration_min=0 cho cả 3: state phần cứng rời rạc, cần báo NGAY không chờ sustain (bài học
    # thật: RAID5 Hyperv-02 mất 2/4 disk vượt khả năng chịu lỗi).
    {
        "name": "HyperV RAID Controller Critical",
        "device_type": "hyperv", "metric": "raid_controller_health",
        "condition": "gte", "threshold": 2.0,
        "severity": "CRITICAL", "duration_min": 0,
    },
    {
        "name": "HyperV RAID Logical Drive Warning+",
        "device_type": "hyperv", "metric": "raid_logical_drive_health",
        "condition": "gte", "threshold": 1.0,
        "severity": "WARNING", "duration_min": 0,
    },
    {
        "name": "HyperV RAID Missing Disk",
        "device_type": "hyperv", "metric": "raid_missing_disk_count",
        "condition": "gte", "threshold": 1.0,
        "severity": "CRITICAL", "duration_min": 0,
    },
    # HyperV hardware health mở rộng ngoài RAID (iLO Redfish, 2026-09-29 — Battery/Processor/Memory/
    # Fan/Temperature/PowerSupply/Redundancy, xem apps/collectors/ilo_redfish.py). duration_min=0
    # như 3 rule RAID ở trên (state phần cứng rời rạc). Tên "Warning+" cho ngưỡng gte 1.0 (bắt cả
    # Warning/Critical) khớp precedent "RAID Logical Drive Warning+" — không đặt tên "Critical" cho
    # rule severity WARNING. PSU dùng ngưỡng Critical-only (gte 2.0) vì dữ liệu thật chỉ thấy
    # OK/Critical (không có Warning cho PSU trên HPE). Không có rule cho ams_device_discovery
    # (thông tin "có/không cài Agentless Management Service", không phải lỗi phần cứng).
    {
        "name": "HyperV Battery Warning+",
        "device_type": "hyperv", "metric": "ilo_battery_health",
        "condition": "gte", "threshold": 1.0,
        "severity": "WARNING", "duration_min": 0,
    },
    {
        "name": "HyperV Processor Warning+",
        "device_type": "hyperv", "metric": "ilo_processor_health",
        "condition": "gte", "threshold": 1.0,
        "severity": "WARNING", "duration_min": 0,
    },
    {
        "name": "HyperV Memory Warning+",
        "device_type": "hyperv", "metric": "ilo_memory_health",
        "condition": "gte", "threshold": 1.0,
        "severity": "WARNING", "duration_min": 0,
    },
    {
        "name": "HyperV Fan Warning+",
        "device_type": "hyperv", "metric": "ilo_fan_health",
        "condition": "gte", "threshold": 1.0,
        "severity": "WARNING", "duration_min": 0,
    },
    {
        "name": "HyperV Temperature Warning+",
        "device_type": "hyperv", "metric": "ilo_temperature_health",
        "condition": "gte", "threshold": 1.0,
        "severity": "WARNING", "duration_min": 0,
    },
    {
        "name": "HyperV Power Supply Critical",
        "device_type": "hyperv", "metric": "ilo_power_supply_health",
        "condition": "gte", "threshold": 2.0,
        "severity": "CRITICAL", "duration_min": 0,
    },
    {
        "name": "HyperV Power Not Redundant",
        "device_type": "hyperv", "metric": "ilo_power_redundancy",
        "condition": "eq", "threshold": 0.0,
        "severity": "CRITICAL", "duration_min": 0,
    },
    # 3 rule bonus vòng 2 (cùng ngày) — CHỈ có dữ liệu trên host iLO5 (Oem.Hpe.AggregateHealthStatus,
    # phát hiện qua Hyperv-01 thật ra là DL380 Gen10/iLO5), None vĩnh viễn trên host iLO4
    # (Hyperv-02/Hyprver03) nên rule không bao giờ fire cho 2 host đó — không phải bug, chỉ là
    # không có nguồn dữ liệu tương đương trên iLO4.
    {
        "name": "HyperV BIOS/Hardware Health Warning+",
        "device_type": "hyperv", "metric": "ilo_bios_hardware_health",
        "condition": "gte", "threshold": 1.0,
        "severity": "WARNING", "duration_min": 0,
    },
    {
        "name": "HyperV Network Health Warning+",
        "device_type": "hyperv", "metric": "ilo_network_health",
        "condition": "gte", "threshold": 1.0,
        "severity": "WARNING", "duration_min": 0,
    },
    {
        "name": "HyperV Fan Not Redundant",
        "device_type": "hyperv", "metric": "ilo_fan_redundancy",
        "condition": "eq", "threshold": 0.0,
        "severity": "CRITICAL", "duration_min": 0,
    },
    # Wireless rules
    {
        "name": "AP Offline",
        "device_type": "ap", "metric": "device_online",
        "condition": "eq", "threshold": 0.0,
        "severity": "CRITICAL", "duration_min": 5,
    },
    {
        "name": "WLAN Controller Offline",
        "device_type": "wlan_controller", "metric": "device_online",
        "condition": "eq", "threshold": 0.0,
        "severity": "CRITICAL", "duration_min": 5,
    },
    {
        "name": "AP offline (dưới WLAN AC)",
        "device_type": "wlan_controller", "metric": "wifi_ap_offline",
        "condition": "gt", "threshold": 0.0,
        "severity": "WARNING", "duration_min": 0,
    },
]


class Command(BaseCommand):
    help = "Seed default alert rules vào database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--channels", nargs="+",
            choices=["email", "telegram"],
            default=["email"],
            help="Kênh thông báo áp dụng cho tất cả rules (default: email)",
        )
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Ghi đè rules đã tồn tại",
        )
        parser.add_argument(
            "--metric-prefix", nargs="+", default=None,
            help=(
                "Chỉ seed rule có `metric` bắt đầu bằng 1 trong các prefix này (vd: ilo_ raid_). "
                "Mặc định (không truyền): seed toàn bộ DEFAULT_RULES. Dùng khi chỉ muốn đảm bảo "
                "1 nhóm rule cụ thể tồn tại (vd chạy tự động ở entrypoint.sh) mà KHÔNG đụng tới "
                "các rule khác — kể cả rule đã bị người dùng xoá chủ động qua UI (xem CLAUDE.md "
                "mục 'Thay đổi quan trọng' 2026-09-29, review ngoài: seed toàn bộ DEFAULT_RULES "
                "vô điều kiện mỗi lần app khởi động sẽ tạo lại y hệt bất kỳ rule mặc định nào đã "
                "bị xoá có chủ ý, vì seed chỉ so khớp theo tên, không phân biệt được 'chưa từng "
                "tạo' với 'đã xoá chủ động')."
            ),
        )

    def handle(self, *args, **options):
        channels  = options["channels"]
        overwrite = options["overwrite"]
        prefixes  = options["metric_prefix"]
        rules = DEFAULT_RULES
        if prefixes:
            rules = [r for r in DEFAULT_RULES if r["metric"].startswith(tuple(prefixes))]
        created = updated = skipped = 0

        for rule_data in rules:
            rule_data["channels"] = channels
            existing = AlertRule.objects.filter(name=rule_data["name"]).first()
            if existing:
                if overwrite:
                    for k, v in rule_data.items():
                        setattr(existing, k, v)
                    existing.save()
                    updated += 1
                    self.stdout.write(f"  ~ Updated: {rule_data['name']}")
                else:
                    skipped += 1
                    self.stdout.write(f"  - Skipped (exists): {rule_data['name']}")
            else:
                AlertRule.objects.create(**rule_data)
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  + Created: {rule_data['name']}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone: {created} created, {updated} updated, {skipped} skipped."
            )
        )
