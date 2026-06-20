"""Adapter cho Huawei VRP — mem_percent đã là %, không cần tính."""
from datetime import datetime, timezone
from apps.collectors.base import BaseAdapter, NormalizedData


class HuaweiVRPAdapter(BaseAdapter):
    def normalize(self, raw: dict, device_name: str, ip_address: str) -> NormalizedData:
        return NormalizedData(
            device_name=device_name,
            ip_address=ip_address,
            timestamp=datetime.now(tz=timezone.utc),
            os_family="huawei_vrp",
            cpu_percent=float(raw.get("cpu_percent", 0)),
            mem_percent=float(raw.get("mem_percent", 0)),  # đã là % từ SNMP
            uptime_secs=int(raw.get("uptime_secs", 0)),
            interfaces=raw.get("interfaces", []),
        )
