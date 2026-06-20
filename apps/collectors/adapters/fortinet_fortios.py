"""Adapter cho Fortinet FortiOS — parse raw SNMP/SSH data sang NormalizedData."""
from datetime import datetime, timezone
from apps.collectors.base import BaseAdapter, NormalizedData


class FortinetFortiOSAdapter(BaseAdapter):
    def normalize(self, raw: dict, device_name: str, ip_address: str) -> NormalizedData:
        return NormalizedData(
            device_name=device_name,
            ip_address=ip_address,
            timestamp=datetime.now(tz=timezone.utc),
            os_family="fortinet_fortios",
            cpu_percent=float(raw.get("cpu_percent", 0)),
            mem_percent=float(raw.get("mem_percent", 0)),
            uptime_secs=int(raw.get("uptime_secs", 0)),
            interfaces=raw.get("interfaces", []),
            extra=raw.get("extra", {}),  # chứa session_count nếu có
        )
