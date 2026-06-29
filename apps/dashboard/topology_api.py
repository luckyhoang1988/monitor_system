"""Build JSON topology graph cho Cytoscape.js — phân tầng Core → Switch → AP."""
from __future__ import annotations

from collections import defaultdict

from django.urls import reverse
from django.utils import timezone

from apps.collectors.topology_lldp import normalize_mac
from apps.devices.models import Device, TopologyLink
from apps.devices.topology_hierarchy import build_switch_uplink_edges, find_core_device
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


def _ap_identity(link: TopologyLink, ap_by_mac: dict[str, dict]) -> tuple[str, dict, str, str]:
    """(mac, ap_info, ap_name, ap_node_id) cho 1 AP link — dùng chung pre-pass & vẽ node."""
    mac = normalize_mac(link.remote_ap_mac)
    ap_info = ap_by_mac.get(mac, {}) if mac else {}
    if not ap_info and link.remote_ap_name:
        ap_info = ap_by_mac.get(f"name:{link.remote_ap_name.strip().casefold()}", {})
    ap_name = ap_info.get("ap_name") or link.remote_ap_name or link.remote_sys_name or mac or "AP"
    return mac, ap_info, ap_name, _ap_node_id(mac, ap_name)


def _short_label(name: str, max_len: int = 22) -> str:
    n = (name or "").strip()
    return n if len(n) <= max_len else n[: max_len - 1] + "…"


def _latest_ap_snapshot(ac: Device) -> dict[str, dict]:
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
    """Core (trên) → Switch access (giữa, khung compound) → AP (trong khung)."""
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

    ap_links_qs = TopologyLink.objects.filter(
        is_stale=False,
        link_kind="ap",
    ).select_related("local_device", "local_interface")

    if switch_filter:
        ap_links_qs = ap_links_qs.filter(local_device_id=switch_filter)

    ap_links = list(ap_links_qs.order_by("local_device__name", "local_port"))

    access_switch_ids: set[int] = {link.local_device_id for link in ap_links}
    if switch_filter:
        access_switch_ids.add(switch_filter)

    core = find_core_device()
    core_id = core.id if core else None

    # 1 AP (theo node-id/MAC) chỉ thuộc 1 switch — link đầu tiên theo thứ tự sắp xếp
    # "sở hữu" AP; link trùng từ switch khác (vd MAC flood qua uplink) bị bỏ qua.
    # Badge "(n AP)" đếm đúng số node AP thực vẽ ra → không lệch tổng vs hiển thị.
    ap_owner: dict[str, int] = {}
    ap_per_switch: dict[int, int] = defaultdict(int)
    for link in ap_links:
        _mac, _info, _name, ap_nid = _ap_identity(link, ap_by_mac)
        if ap_nid in ap_owner:
            continue
        ap_owner[ap_nid] = link.local_device_id
        ap_per_switch[link.local_device_id] += 1

    nodes: list[dict] = []
    edges: list[dict] = []
    mapped_macs: set[str] = set()
    switch_nodes_added: set[int] = set()
    ap_nodes_added: set[str] = set()

    # Cạnh + độ sâu cây switch (BFS từ core) — tính sớm để xếp tầng node.
    uplink_edges, depth_map = build_switch_uplink_edges(switch_filter=switch_filter)

    # Tập switch cần vẽ: mọi switch enabled (yêu cầu "vẽ mọi switch").
    if switch_filter:
        render_switch_ids = {switch_filter}
        for ue in uplink_edges:
            for sid in (ue["source_id"], ue["target_id"]):
                if sid.startswith("sw-"):
                    render_switch_ids.add(int(sid[3:]))
        if core_id:
            render_switch_ids.add(core_id)
    else:
        render_switch_ids = set(
            Device.objects.filter(device_type="switch", enabled=True)
            .values_list("id", flat=True)
        )
        render_switch_ids |= access_switch_ids
        if core_id:
            render_switch_ids.add(core_id)

    sw_objs = {d.id: d for d in Device.objects.filter(pk__in=render_switch_ids)}

    for sw_id in sorted(render_switch_ids):
        sw = sw_objs.get(sw_id)
        if not sw or sw_id in switch_nodes_added:
            continue
        switch_nodes_added.add(sw_id)
        is_core = sw_id == core_id
        ap_n = ap_per_switch.get(sw_id, 0)
        if is_core:
            node_type, tier, label = "core", 0, sw.name
        else:
            node_type = "switch"
            tier = depth_map.get(sw_id) or 1
            label = f"{sw.name}\n({ap_n} AP)" if ap_n else sw.name
        nodes.append({
            "data": {
                "id": _switch_node_id(sw.id),
                "label": label,
                "type": node_type,
                "ip": sw.ip_address,
                "online": sw.is_online,
                "location": sw.location or "",
                "detail_url": reverse("dashboard:switch_detail", args=[sw.id]),
                "ap_count": ap_n,
                "tier": tier,
            },
        })

    # AP trong khung switch
    for link in ap_links:
        sw = link.local_device
        if switch_filter and sw.id != switch_filter:
            continue
        if core_id and sw.id == core_id:
            continue
        sw_nid = _switch_node_id(sw.id)
        mac, ap_info, ap_name, ap_nid = _ap_identity(link, ap_by_mac)
        # AP đã thuộc switch khác (link trùng do MAC flood qua uplink) → bỏ qua,
        # không vẽ thêm node/edge để badge "(n AP)" khớp số node thực.
        if ap_owner.get(ap_nid, sw.id) != sw.id:
            continue

        ap_ip = ap_info.get("ap_ip") or (link.remote_mgmt_ip or "")
        is_online = ap_info.get("is_online", True)
        client_count = ap_info.get("client_count", 0)

        if mac:
            mapped_macs.add(mac)

        port = link.local_port or ""
        node_label = _short_label(ap_name)
        if port:
            node_label = f"{node_label}\n⇡ {port}"
        if ap_nid not in ap_nodes_added:
            ap_nodes_added.add(ap_nid)
            nodes.append({
                "data": {
                    "id": ap_nid,
                    "label": node_label,
                    "full_label": ap_name,
                    "type": "ap",
                    "mac": mac or link.remote_ap_mac,
                    "ip": ap_ip,
                    "online": is_online,
                    "client_count": client_count,
                    "confirmed": link.is_confirmed,
                    "switch_name": sw.name,
                    "switch_port": port,
                    "tier": 2,
                },
            })

        # Edge switch → AP (thay cho compound parent)
        edges.append({
            "data": {
                "id": f"e-ap-{sw.id}-{ap_nid}",
                "source": sw_nid,
                "target": ap_nid,
                "label": link.local_port or "",
                "type": "ap",
                "online": is_online,
                "inferred": not link.is_confirmed,
            },
        })

    # Edge switch → switch (core → dist → access, cây nhiều tầng) — đã tính ở trên.
    for ue in uplink_edges:
        edges.append({
            "data": {
                "id": f"e-{ue['source_id']}-{ue['target_id']}",
                "source": ue["source_id"],
                "target": ue["target_id"],
                "label": ue.get("label") or "",
                "type": "uplink",
                "trunk": ue.get("trunk", False),
                "inferred": ue.get("inferred", False),
            },
        })

    # Orphan AP
    orphan_aps: list[dict] = []
    for ap in all_aps_on_ac:
        if ap["mac"] and ap["mac"] in mapped_macs:
            continue
        if not ap["mac"] and ap["name"]:
            continue
        orphan_aps.append(ap)

    if orphan_aps and not switch_filter:
        nodes.append({
            "data": {
                "id": ORPHAN_GROUP_ID,
                "label": f"Chưa map ({len(orphan_aps)} AP)",
                "type": "orphan-group",
                "tier": 1,
            },
        })
        for ap in orphan_aps:
            ap_nid = _ap_node_id(ap["mac"], ap["name"])
            if ap_nid in ap_nodes_added:
                continue
            ap_nodes_added.add(ap_nid)
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
                    "tier": 2,
                },
            })
            edges.append({
                "data": {
                    "id": f"e-orphan-{ap_nid}",
                    "source": ORPHAN_GROUP_ID,
                    "target": ap_nid,
                    "type": "ap",
                    "inferred": True,
                },
            })

    ap_offline = sum(1 for ap in all_aps_on_ac if not ap["online"])

    ac_list = list(
        Device.objects.filter(device_type="wlan_controller", enabled=True)
        .order_by("name")
        .values("id", "name")
    )

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "ac_id": ac.id if ac else None,
            "ac_name": ac.name if ac else "",
            "ac_list": ac_list,
            "ap_total": len(all_aps_on_ac),
            "ap_mapped": len(mapped_macs),
            "ap_unmapped": len(orphan_aps),
            "ap_offline": ap_offline,
            "switch_count": len(switch_nodes_added),
            "core_id": core_id,
            "core_name": core.name if core else "",
            "switch_filter": switch_filter,
            "layout": "hierarchy",
            "generated_at": timezone.now().isoformat(),
        },
    }
