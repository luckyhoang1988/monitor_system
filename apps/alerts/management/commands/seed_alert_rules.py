"""
Seed default alert rules.

Usage:
    python manage.py seed_alert_rules
    python manage.py seed_alert_rules --channels email telegram
    python manage.py seed_alert_rules --overwrite
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
    {
        "name": "Firewall Sessions High",
        "device_type": "firewall", "metric": "fw_session_count",
        "condition": "gt", "threshold": 200000.0,
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

    def handle(self, *args, **options):
        channels  = options["channels"]
        overwrite = options["overwrite"]
        created = updated = skipped = 0

        for rule_data in DEFAULT_RULES:
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
