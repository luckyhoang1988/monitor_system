"""Persist TopologyLink từ LLDP discovery hoặc bảng MAC (FDB)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.collectors.topology_fdb import collect_fdb_ap_mappings
from apps.collectors.topology_lldp import collect_lldp_neighbors, normalize_mac
from apps.devices.topology_match import (
    get_default_ac_device,
    load_ac_ap_snapshot,
    match_lldp_to_ap,
)
from apps.devices.topology_switch_match import (
    build_switch_device_index,
    match_neighbor_to_switch,
)

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

STALE_MISS_THRESHOLD = 3


def _resolve_interface(device: Device, port_name: str):
    from apps.devices.models import Interface

    return Interface.objects.filter(device=device, name=port_name).first()


def _upsert_ap_link(
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
            "link_kind": "ap",
            "local_interface": iface,
            "remote_device": None,
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


def _upsert_switch_link(
    device: Device,
    local_port: str,
    remote: Device,
    *,
    remote_sys_name: str = "",
    remote_port_id: str = "",
    is_confirmed: bool = True,
    protocol: str = "lldp",
    match_method: str | None = None,
) -> None:
    from apps.devices.models import TopologyLink

    iface = _resolve_interface(device, local_port)
    if match_method is None:
        match_method = "name" if is_confirmed else "lldp"
    TopologyLink.objects.update_or_create(
        local_device=device,
        local_port=local_port,
        defaults={
            "link_kind": "switch",
            "local_interface": iface,
            "remote_device": remote,
            "remote_ap_mac": "",
            "remote_ap_name": remote.name,
            "remote_sys_name": remote_sys_name,
            "remote_port_id": remote_port_id,
            "protocol": protocol,
            "match_method": match_method,
            "is_confirmed": is_confirmed,
            "is_stale": False,
            "miss_count": 0,
            "last_seen": timezone.now(),
        },
    )


def discover_switch_links_fdb(switches: list[Device]) -> int:
    """Phát hiện cạnh switch↔switch qua FDB → upsert TopologyLink(link_kind=switch).

    Orient parent (gần core)→child bằng BFS để admin/đồ thị đọc tự nhiên; graph builder
    vẫn tự reorient theo BFS nên hướng lưu không bắt buộc.
    """
    from apps.collectors.topology_switch_fdb import (
        build_switch_mac_registry,
        discover_switch_adjacency,
    )
    from apps.devices.models import TopologyLink
    from apps.devices.topology_hierarchy import bfs_depths, find_core_device

    switches = list(switches)
    by_id = {sw.id: sw for sw in switches}
    registry = build_switch_mac_registry(switches)
    pairs = discover_switch_adjacency(switches, registry)

    # BFS từ core để chọn parent (đầu nông hơn).
    adjacency: dict[int, set[int]] = {}
    for a, _pa, b, _pb in pairs:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    core = find_core_device()
    depths = bfs_depths(adjacency, core.id if core else None)

    def depth_of(dev_id: int) -> int:
        return depths.get(dev_id, 10_000)

    seen_keys: set[tuple[int, str]] = set()
    count = 0
    for a, port_a, b, port_b in pairs:
        if depth_of(a) <= depth_of(b):
            parent, parent_port, child = a, port_a, b
        else:
            parent, parent_port, child = b, port_b, a
        parent_dev = by_id.get(parent)
        child_dev = by_id.get(child)
        if not parent_dev or not child_dev:
            continue
        _upsert_switch_link(
            parent_dev,
            parent_port,
            child_dev,
            remote_sys_name=child_dev.name,
            is_confirmed=True,
            protocol="fdb",
            match_method="mac",
        )
        seen_keys.add((parent, parent_port))
        count += 1

    # Stale các link FDB không còn thấy (giữ manual).
    stale_qs = TopologyLink.objects.filter(
        link_kind="switch",
        protocol="fdb",
    ).exclude(match_method="manual")
    for link in stale_qs:
        if (link.local_device_id, link.local_port) in seen_keys:
            continue
        link.miss_count = (link.miss_count or 0) + 1
        if link.miss_count >= STALE_MISS_THRESHOLD:
            link.is_stale = True
        link.save(update_fields=["miss_count", "is_stale"])

    logger.info("Topology switch-FDB: %d cạnh switch↔switch", count)
    return count


def discover_switch_uplinks(
    device: Device,
    device_index: tuple[dict, dict],
) -> int:
    """LLDP neighbor là switch khác → TopologyLink link_kind=switch."""
    by_name, by_ip = device_index
    neighbors = collect_lldp_neighbors(device, ap_only=False, ap_macs=set())
    seen_ports: set[str] = set()
    count = 0

    for neighbor in neighbors:
        if neighbor.is_ap_candidate:
            continue
        remote = match_neighbor_to_switch(neighbor, by_name, by_ip, device)
        if not remote:
            continue
        seen_ports.add(neighbor.local_port)
        _upsert_switch_link(
            device,
            neighbor.local_port,
            remote,
            remote_sys_name=neighbor.remote_sys_name,
            remote_port_id=neighbor.remote_port_id,
        )
        count += 1

    from apps.devices.models import TopologyLink

    stale_qs = TopologyLink.objects.filter(
        local_device=device,
        link_kind="switch",
        protocol="lldp",
    ).exclude(local_port__in=seen_ports).exclude(match_method="manual")

    for link in stale_qs:
        link.miss_count = (link.miss_count or 0) + 1
        if link.miss_count >= STALE_MISS_THRESHOLD:
            link.is_stale = True
        link.save(update_fields=["miss_count", "is_stale"])

    if count:
        logger.info("Topology %s: %d switch uplink(s) via LLDP", device.name, count)
    return count


def upsert_switch_topology(device: Device, ac_device=None) -> tuple[int, int]:
    """Discovery LLDP (ưu tiên) hoặc FDB → upsert TopologyLink AP."""
    if ac_device is None:
        ac_device = get_default_ac_device()

    ap_snapshot = load_ac_ap_snapshot(ac_device)
    ap_macs = set(ap_snapshot.keys())

    neighbors = collect_lldp_neighbors(device, ap_only=True, ap_macs=ap_macs)
    seen_ports: set[str] = set()
    confirmed = 0
    protocol_used = "lldp"

    if neighbors:
        for neighbor in neighbors:
            seen_ports.add(neighbor.local_port)
            match = match_lldp_to_ap(neighbor, ac_device)
            mac = normalize_mac(match.ap_mac or neighbor.remote_mac)
            ok = _upsert_ap_link(
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
        protocol_used = "fdb"
        fdb_entries = collect_fdb_ap_mappings(device, ap_macs)
        for entry in fdb_entries:
            seen_ports.add(entry.local_port)
            info = ap_snapshot.get(entry.mac, {})
            ok = _upsert_ap_link(
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
        link_kind="ap",
        protocol=protocol_used,
    ).exclude(local_port__in=seen_ports).exclude(match_method="manual")

    for link in stale_qs:
        link.miss_count = (link.miss_count or 0) + 1
        if link.miss_count >= STALE_MISS_THRESHOLD:
            link.is_stale = True
        link.save(update_fields=["miss_count", "is_stale"])

    link_count = len(neighbors) if neighbors else len(seen_ports)
    logger.info(
        "Topology %s: %d AP link(s) via %s, %d confirmed with AC",
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
    device_index = build_switch_device_index()
    total_links = 0
    total_confirmed = 0
    switch_links = 0
    errors = 0

    switch_list = list(switches)
    for sw in switch_list:
        try:
            n, c = upsert_switch_topology(sw, ac)
            total_links += n
            total_confirmed += c
            switch_links += discover_switch_uplinks(sw, device_index)
        except Exception as exc:
            errors += 1
            logger.warning("Topology discovery failed %s: %s", sw.name, exc)

    # Phát hiện cạnh switch↔switch qua FDB (1 lần/run — cần MAC toàn fleet).
    try:
        switch_links += discover_switch_links_fdb(switch_list)
    except Exception as exc:
        errors += 1
        logger.warning("Topology switch-FDB discovery failed: %s", exc)

    return {
        "switches": switches.count(),
        "links": total_links,
        "confirmed": total_confirmed,
        "switch_links": switch_links,
        "errors": errors,
    }
