"""Persist TopologyLink từ LLDP discovery."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.collectors.topology_lldp import collect_lldp_neighbors, normalize_mac
from apps.devices.topology_match import get_default_ac_device, match_lldp_to_ap

if TYPE_CHECKING:
    from apps.devices.models import Device, TopologyLink

logger = logging.getLogger(__name__)

STALE_MISS_THRESHOLD = 3


def _resolve_interface(device: Device, port_name: str):
    from apps.devices.models import Interface

    return Interface.objects.filter(device=device, name=port_name).first()


def upsert_switch_topology(device: Device, ac_device=None) -> tuple[int, int]:
    """Discovery LLDP trên 1 switch → upsert TopologyLink. Trả (links_seen, confirmed)."""
    from apps.devices.models import TopologyLink

    if ac_device is None:
        ac_device = get_default_ac_device()

    neighbors = collect_lldp_neighbors(device, ap_only=True)
    seen_ports: set[str] = set()
    confirmed = 0

    for neighbor in neighbors:
        seen_ports.add(neighbor.local_port)
        match = match_lldp_to_ap(neighbor, ac_device)
        mac = normalize_mac(match.ap_mac or neighbor.remote_mac)
        iface = _resolve_interface(device, neighbor.local_port)

        defaults = {
            "local_interface": iface,
            "remote_ap_mac": mac,
            "remote_ap_name": match.ap_name or neighbor.remote_sys_name,
            "remote_sys_name": neighbor.remote_sys_name,
            "remote_chassis_id": neighbor.remote_chassis_id,
            "remote_port_id": neighbor.remote_port_id,
            "remote_mgmt_ip": neighbor.remote_mgmt_ip or None,
            "protocol": "lldp",
            "match_method": match.match_method,
            "is_confirmed": match.is_confirmed,
            "is_stale": False,
            "miss_count": 0,
            "last_seen": timezone.now(),
        }
        link, created = TopologyLink.objects.update_or_create(
            local_device=device,
            local_port=neighbor.local_port,
            defaults=defaults,
        )
        if match.is_confirmed:
            confirmed += 1
        logger.debug(
            "Topology link %s %s (%s)",
            "created" if created else "updated",
            link,
            match.match_method,
        )

    # Port không còn thấy neighbor → tăng miss_count
    stale_qs = TopologyLink.objects.filter(
        local_device=device,
        protocol="lldp",
        match_method__in=["mac", "name", "ip", "lldp"],
    ).exclude(local_port__in=seen_ports)

    for link in stale_qs:
        link.miss_count = (link.miss_count or 0) + 1
        if link.miss_count >= STALE_MISS_THRESHOLD:
            link.is_stale = True
        link.save(update_fields=["miss_count", "is_stale"])

    logger.info(
        "Topology %s: %d link(s), %d confirmed with AC",
        device.name, len(neighbors), confirmed,
    )
    return len(neighbors), confirmed


def discover_all_switches() -> dict[str, int]:
    """Chạy discovery trên mọi switch SNMP enabled."""
    from apps.devices.models import Device

    switches = Device.objects.filter(
        device_type="switch",
        enabled=True,
        protocol="snmp",
    )
    ac = get_default_ac_device()
    total_links = 0
    total_confirmed = 0
    errors = 0

    for sw in switches:
        try:
            n, c = upsert_switch_topology(sw, ac)
            total_links += n
            total_confirmed += c
        except Exception as exc:
            errors += 1
            logger.warning("Topology discovery failed %s: %s", sw.name, exc)

    return {
        "switches": switches.count(),
        "links": total_links,
        "confirmed": total_confirmed,
        "errors": errors,
    }
