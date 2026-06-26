"""Persist TopologyLink từ LLDP discovery hoặc bảng MAC (FDB)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.collectors.topology_fdb import collect_switch_mac_table
from apps.collectors.topology_lldp import collect_lldp_neighbors, normalize_mac
from apps.devices.topology_match import (
    get_default_ac_device,
    load_ac_ap_snapshot,
    match_lldp_to_ap,
)

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

STALE_MISS_THRESHOLD = 3


def _resolve_interface(device: Device, port_name: str):
    from apps.devices.models import Interface

    return Interface.objects.filter(device=device, name=port_name).first()


def _upsert_link(
    device: Device,
    local_port: str,
    *,
    mac: str,
    ap_name: str,
    protocol: str,
    match_method: str,
    is_confirmed: bool,
    remote_sys_name: str = "",
    remote_chassis_id: str = "",
    remote_port_id: str = "",
) -> bool:
    from apps.devices.models import TopologyLink

    iface = _resolve_interface(device, local_port)
    TopologyLink.objects.update_or_create(
        local_device=device,
        local_port=local_port,
        defaults={
            "local_interface": iface,
            "remote_ap_mac": mac,
            "remote_ap_name": ap_name,
            "remote_sys_name": remote_sys_name,
            "remote_chassis_id": remote_chassis_id,
            "remote_port_id": remote_port_id,
            "protocol": protocol,
            "match_method": match_method,
            "is_confirmed": is_confirmed,
            "is_stale": False,
            "miss_count": 0,
            "last_seen": timezone.now(),
        },
    )
    return is_confirmed


def upsert_switch_topology(device: Device, ac_device=None) -> tuple[int, int]:
    """Discovery LLDP (ưu tiên) hoặc FDB → upsert TopologyLink."""
    if ac_device is None:
        ac_device = get_default_ac_device()

    ap_snapshot = load_ac_ap_snapshot(ac_device)
    ap_macs = set(ap_snapshot.keys())

    neighbors = collect_lldp_neighbors(device, ap_only=True)
    seen_ports: set[str] = set()
    confirmed = 0
    protocol_used = "lldp"

    if neighbors:
        for neighbor in neighbors:
            seen_ports.add(neighbor.local_port)
            match = match_lldp_to_ap(neighbor, ac_device)
            mac = normalize_mac(match.ap_mac or neighbor.remote_mac)
            ok = _upsert_link(
                device,
                neighbor.local_port,
                mac=mac,
                ap_name=match.ap_name or neighbor.remote_sys_name,
                protocol="lldp",
                match_method=match.match_method,
                is_confirmed=match.is_confirmed,
                remote_sys_name=neighbor.remote_sys_name,
                remote_chassis_id=neighbor.remote_chassis_id,
                remote_port_id=neighbor.remote_port_id,
            )
            if ok:
                confirmed += 1
    elif ap_macs:
        # Fallback: MAC học trên port khớp AP trên AC
        protocol_used = "fdb"
        fdb_entries = collect_switch_mac_table(device, ap_macs=ap_macs, ap_only=True)
        for entry in fdb_entries:
            seen_ports.add(entry.local_port)
            info = ap_snapshot.get(entry.mac, {})
            ok = _upsert_link(
                device,
                entry.local_port,
                mac=entry.mac,
                ap_name=info.get("ap_name", ""),
                protocol="fdb",
                match_method="mac",
                is_confirmed=True,
            )
            if ok:
                confirmed += 1
        logger.info(
            "Topology %s: LLDP rỗng — dùng FDB, %d AP MAC match",
            device.name, len(fdb_entries),
        )

    from apps.devices.models import TopologyLink

    stale_qs = TopologyLink.objects.filter(
        local_device=device,
        protocol=protocol_used,
    ).exclude(local_port__in=seen_ports).exclude(match_method="manual")

    for link in stale_qs:
        link.miss_count = (link.miss_count or 0) + 1
        if link.miss_count >= STALE_MISS_THRESHOLD:
            link.is_stale = True
        link.save(update_fields=["miss_count", "is_stale"])

    link_count = len(neighbors) if neighbors else len(seen_ports)
    logger.info(
        "Topology %s: %d link(s) via %s, %d confirmed with AC",
        device.name, link_count, protocol_used, confirmed,
    )
    return link_count, confirmed


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
