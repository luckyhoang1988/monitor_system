"""Build JSON topology graph cho Cytoscape.js."""
from __future__ import annotations

from collections import defaultdict

from django.urls import reverse
from django.utils import timezone

from apps.collectors.topology_lldp import normalize_mac
from apps.devices.models import Device, TopologyLink
from apps.metrics.models import WifiApStats

ORPHAN_GROUP_ID = "group-orphan"


def _ap_node_id(mac: str, name: str = "") -> str:
    mac_norm = normalize_mac(mac)
    if mac_norm:
        return "ap-" + mac_norm.replace(":", "")
    safe = "".join(c if c.isalnum() else "-" for c in (name or "unknown"))[:40]
    return f"ap-name-{safe}"


def _switch_node_id(device_id: int) -> str:
    return f"sw-{device_id}"


def _short_label(name: str, max_len: int = 22) -> str:
    n = (name or "").strip()
    return n if len(n) <= max_len else n[: max_len - 1] + "…"


def _latest_ap_snapshot(ac: Device) -> dict[str, dict]:
    """MAC normalized → {ap_name, ap_ip, is_online, client_count, run_state}."""
    latest_ts = (
        WifiApStats.objects.filter(device=ac)
        .order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    if not latest_ts:
        return {}

    result: dict[str, dict] = {}
    for ap in WifiApStats.objects.filter(device=ac, timestamp=latest_ts):
        mac = normalize_mac(ap.ap_mac)
        entry = {
            "ap_name": ap.ap_name,
            "ap_mac": mac or ap.ap_mac,
            "ap_ip": ap.ap_ip or "",
            "is_online": ap.is_online,
            "client_count": ap.client_count,
            "run_state": ap.run_state,
        }
        if mac:
            result[mac] = entry
        name_key = (ap.ap_name or "").strip().casefold()
        if name_key:
            result.setdefault(f"name:{name_key}", entry)
    return result


def build_topology_graph(
    ac: Device | None = None,
    switch_filter: int | None = None,
) -> dict:
    """Trả {nodes, edges, meta} — compound parent=switch, không dùng edge (tránh chồng chéo)."""
    if ac is None:
        ac = (
            Device.objects.filter(device_type="wlan_controller", enabled=True)
            .order_by("name")
            .first()
        )

    ap_by_mac = _latest_ap_snapshot(ac) if ac else {}
    all_aps_on_ac: list[dict] = []
    if ac:
        latest_ts = (
            WifiApStats.objects.filter(device=ac)
            .order_by("-timestamp")
            .values_list("timestamp", flat=True)
            .first()
        )
        if latest_ts:
            for ap in WifiApStats.objects.filter(device=ac, timestamp=latest_ts):
                all_aps_on_ac.append({
                    "name": ap.ap_name,
                    "mac": normalize_mac(ap.ap_mac),
                    "ip": ap.ap_ip or "",
                    "online": ap.is_online,
                    "client_count": ap.client_count,
                })

    links_qs = TopologyLink.objects.filter(is_stale=False).select_related(
        "local_device", "local_interface",
    )
    if switch_filter:
        links_qs = links_qs.filter(local_device_id=switch_filter)

    links = list(links_qs.order_by("local_device__name", "local_port"))
    switch_ids: set[int] = set()
    if switch_filter:
        switch_ids.add(switch_filter)
    else:
        switch_ids.update(link.local_device_id for link in links)

    ap_per_switch: dict[int, int] = defaultdict(int)
    for link in links:
        ap_per_switch[link.local_device_id] += 1

    nodes: list[dict] = []
    mapped_macs: set[str] = set()

    for sw_id in sorted(switch_ids):
        try:
            sw = Device.objects.get(pk=sw_id)
        except Device.DoesNotExist:
            continue
        sw_nid = _switch_node_id(sw.id)
        ap_n = ap_per_switch.get(sw_id, 0)
        label = sw.name if ap_n == 0 else f"{sw.name} ({ap_n} AP)"
        nodes.append({
            "data": {
                "id": sw_nid,
                "label": label,
                "type": "switch",
                "ip": sw.ip_address,
                "online": sw.is_online,
                "location": sw.location or "",
                "detail_url": reverse("dashboard:switch_detail", args=[sw.id]),
                "ap_count": ap_n,
            },
        })

    for link in links:
        sw = link.local_device
        sw_nid = _switch_node_id(sw.id)
        mac = normalize_mac(link.remote_ap_mac)
        ap_info = ap_by_mac.get(mac, {}) if mac else {}
        if not ap_info and link.remote_ap_name:
            ap_info = ap_by_mac.get(f"name:{link.remote_ap_name.strip().casefold()}", {})

        ap_name = ap_info.get("ap_name") or link.remote_ap_name or link.remote_sys_name or mac or "AP"
        ap_ip = ap_info.get("ap_ip") or (link.remote_mgmt_ip or "")
        is_online = ap_info.get("is_online", True)
        client_count = ap_info.get("client_count", 0)
        ap_nid = _ap_node_id(mac, ap_name)
        port = link.local_port or ""

        if mac:
            mapped_macs.add(mac)

        nodes.append({
            "data": {
                "id": ap_nid,
                "label": _short_label(ap_name),
                "full_label": ap_name,
                "type": "ap",
                "mac": mac or link.remote_ap_mac,
                "ip": ap_ip,
                "online": is_online,
                "client_count": client_count,
                "confirmed": link.is_confirmed,
                "switch_name": sw.name,
                "switch_port": port,
                "parent": sw_nid,
            },
        })

    orphan_aps: list[dict] = []
    for ap in all_aps_on_ac:
        if ap["mac"] and ap["mac"] in mapped_macs:
            continue
        if not ap["mac"] and ap["name"]:
            continue
        orphan_aps.append(ap)

    if orphan_aps:
        nodes.append({
            "data": {
                "id": ORPHAN_GROUP_ID,
                "label": f"Chưa map ({len(orphan_aps)} AP)",
                "type": "orphan-group",
            },
        })
        for ap in orphan_aps:
            ap_nid = _ap_node_id(ap["mac"], ap["name"])
            nodes.append({
                "data": {
                    "id": ap_nid,
                    "label": _short_label(ap["name"]),
                    "full_label": ap["name"],
                    "type": "ap",
                    "mac": ap["mac"],
                    "ip": ap["ip"],
                    "online": ap["online"],
                    "client_count": ap["client_count"],
                    "confirmed": False,
                    "orphan": True,
                    "switch_name": "",
                    "switch_port": "",
                    "parent": ORPHAN_GROUP_ID,
                },
            })

    ap_offline = sum(1 for ap in all_aps_on_ac if not ap["online"])

    ac_list = list(
        Device.objects.filter(device_type="wlan_controller", enabled=True)
        .order_by("name")
        .values("id", "name")
    )

    switches_with_ap = len([sid for sid in switch_ids if ap_per_switch.get(sid, 0) > 0])

    return {
        "nodes": nodes,
        "edges": [],
        "meta": {
            "ac_id": ac.id if ac else None,
            "ac_name": ac.name if ac else "",
            "ac_list": ac_list,
            "ap_total": len(all_aps_on_ac),
            "ap_mapped": len(mapped_macs),
            "ap_unmapped": len(orphan_aps),
            "ap_offline": ap_offline,
            "switch_count": switches_with_ap,
            "switch_filter": switch_filter,
            "layout": "compound",
            "generated_at": timezone.now().isoformat(),
        },
    }
