"""Adapter cho Cisco IOS-XE — format giống IOS, một số field khác."""
from datetime import datetime, timezone
from apps.collectors.base import BaseAdapter, NormalizedData


class CiscoIOSXEAdapter(BaseAdapter):
    def normalize(self, raw: dict, device_name: str, ip_address: str) -> NormalizedData:
        return NormalizedData(
            device_name=device_name,
            ip_address=ip_address,
            timestamp=datetime.now(tz=timezone.utc),
            os_family="cisco_iosxe",
            cpu_percent=float(raw.get("cpu_percent", 0)),
            mem_percent=float(raw.get("mem_percent", 0)),
            uptime_secs=int(raw.get("uptime_secs", 0)),
            interfaces=raw.get("interfaces", []),
        )
