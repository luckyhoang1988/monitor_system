"""Xác định core switch và cây kết nối switch → switch."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.devices.models import Device


def find_core_device() -> Device | None:
    """Tìm switch core — ưu tiên tên chứa CORE."""
    from apps.devices.models import Device

    qs = Device.objects.filter(device_type="switch", enabled=True)
    core = qs.filter(name__icontains="CORE").order_by("name").first()
    if core:
        return core
    return qs.filter(name__iregex=r"(?i)core").order_by("name").first()


def build_switch_uplink_edges(
    access_switch_ids: set[int],
    *,
    switch_filter: int | None = None,
) -> list[dict]:
    """Trả danh sách edge {source_id, target_id, label} dạng sw-<pk>.

    Ưu tiên link_kind=switch trong DB; fallback star từ core → access switch.
    """
    from apps.devices.models import Device, TopologyLink

    edges: list[dict] = []
    seen: set[tuple[int, int]] = set()

    sw_links = TopologyLink.objects.filter(
        link_kind="switch",
        is_stale=False,
        remote_device__isnull=False,
    ).select_related("local_device", "remote_device")

    if switch_filter:
        sw_links = sw_links.filter(
            local_device_id=switch_filter,
        ) | sw_links.filter(remote_device_id=switch_filter)

    for link in sw_links:
        src_id = link.local_device_id
        dst_id = link.remote_device_id
        if switch_filter and switch_filter not in (src_id, dst_id):
            continue
        if dst_id not in access_switch_ids and src_id not in access_switch_ids:
            continue
        key = (src_id, dst_id)
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "source_id": f"sw-{src_id}",
            "target_id": f"sw-{dst_id}",
            "label": link.local_port or "",
            "confirmed": link.is_confirmed,
        })

    if edges:
        return edges

    core = find_core_device()
    if not core:
        return []

    targets = access_switch_ids
    if switch_filter:
        targets = {switch_filter}

    for sw_id in sorted(targets):
        if sw_id == core.id:
            continue
        edges.append({
            "source_id": f"sw-{core.id}",
            "target_id": f"sw-{sw_id}",
            "label": "",
            "confirmed": False,
            "inferred": True,
        })
    return edges
