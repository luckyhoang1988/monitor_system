"""Management command — manually poll one device for debugging."""
import json
import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Manually poll a device and print collected metrics (for debugging/testing)"

    def add_arguments(self, parser):
        parser.add_argument(
            "device",
            help="Device name or numeric ID",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="output_json",
            help="Print collected data as JSON",
        )
        parser.add_argument(
            "--save",
            action="store_true",
            help="Persist metrics to database after polling",
        )

    def handle(self, *args, **options):
        from apps.devices.models import Device
        from apps.collectors.factory import CollectorFactory
        from apps.metrics.writer import save_metrics

        device_arg: str = options["device"]

        try:
            if device_arg.isdigit():
                device = Device.objects.get(pk=int(device_arg))
            else:
                device = Device.objects.get(name=device_arg)
        except Device.DoesNotExist:
            raise CommandError(f"Device '{device_arg}' not found.")

        self.stdout.write(f"Polling {device.name} ({device.ip_address}) ...")

        try:
            collector = CollectorFactory.create(device)
            data = collector.collect()
        except Exception as exc:
            raise CommandError(f"Collection failed: {exc}") from exc

        if options["output_json"]:
            output = {
                "device_name": data.device_name,
                "ip_address":  data.ip_address,
                "os_family":   data.os_family,
                "cpu_percent": data.cpu_percent,
                "mem_percent": data.mem_percent,
                "uptime_secs": data.uptime_secs,
                "timestamp":   data.timestamp.isoformat(),
                "interfaces":  len(data.interfaces),
                "extra_keys":  list(data.extra.keys()),
            }
            self.stdout.write(json.dumps(output, indent=2))
        else:
            self.stdout.write(self.style.SUCCESS(f"  OS Family  : {data.os_family}"))
            self.stdout.write(f"  CPU        : {data.cpu_percent:.1f}%")
            self.stdout.write(f"  Memory     : {data.mem_percent:.1f}%")
            self.stdout.write(f"  Uptime     : {data.uptime_secs}s")
            self.stdout.write(f"  Interfaces : {len(data.interfaces)}")
            vms = data.extra.get("vms", [])
            if vms:
                self.stdout.write(f"  VMs        : {len(vms)}")
                for vm in vms:
                    self.stdout.write(
                        f"    - {vm.get('name', '?')}  "
                        f"state={vm.get('state', '?')}  "
                        f"cpu={vm.get('cpu_percent', 0)}%  "
                        f"repl={vm.get('repl_health', '?')}"
                    )

        if options["save"]:
            save_metrics(device, data)
            device.last_seen = timezone.now()
            device.os_family = data.os_family
            device.save(update_fields=["last_seen", "os_family"])
            self.stdout.write(self.style.SUCCESS("Metrics saved to database."))
