"""Ghép neighbor LLDP với switch khác trong fleet."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.collectors.topology_lldp import NeighborRecord
    from apps.devices.models import Device

# Tên switch trong fleet: X1_SW1_..., CORE, ...
SWITCH_NAME_PATTERN = re.compile(
    r"(^CORE|SW\d|_SW|SWITCH|Huawei|S57|S67)",
    re.IGNORECASE,
)


def build_switch_device_index() -> tuple[dict[str, Device], dict[str, Device]]:
    from apps.devices.models import Device

    by_name: dict[str, Device] = {}
    by_ip: dict[str, Device] = {}
    for d in Device.objects.filter(enabled=True, device_type="switch"):
        name_key = d.name.strip().casefold()
        by_name[name_key] = d
        by_ip[str(d.ip_address)] = d
        # alias: hostname trước dấu chấm / khoảng trắng
        short = name_key.split(".")[0].split()[0]
        by_name.setdefault(short, d)
    return by_name, by_ip


def match_neighbor_to_switch(
    neighbor: NeighborRecord,
    by_name: dict[str, Device],
    by_ip: dict[str, Device],
    local_device: Device,
) -> Device | None:
    """Tìm switch đích từ sysName / IP neighbor LLDP."""
    sys_name = (neighbor.remote_sys_name or "").strip()
    if sys_name:
        key = sys_name.casefold()
        if key in by_name:
            candidate = by_name[key]
            if candidate.pk != local_device.pk:
                return candidate
        short = key.split(".")[0]
        if short in by_name:
            candidate = by_name[short]
            if candidate.pk != local_device.pk:
                return candidate

    mgmt = (neighbor.remote_mgmt_ip or "").strip()
    if mgmt and mgmt in by_ip:
        candidate = by_ip[mgmt]
        if candidate.pk != local_device.pk:
            return candidate

    return None


def looks_like_switch_name(sys_name: str) -> bool:
    return bool(sys_name and SWITCH_NAME_PATTERN.search(sys_name))
