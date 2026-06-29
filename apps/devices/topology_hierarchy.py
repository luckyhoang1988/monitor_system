"""Xác định core switch và cây kết nối switch → switch."""
from __future__ import annotations

from collections import deque
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


def bfs_depths(adjacency: dict[int, set[int]], root_id: int | None) -> dict[int, int]:
    """Độ sâu BFS từ root trên đồ thị kề vô hướng (root=0). Node không tới = vắng mặt."""
    depths: dict[int, int] = {}
    if root_id is None:
        return depths
    depths[root_id] = 0
    q: deque[int] = deque([root_id])
    while q:
        cur = q.popleft()
        for nb in sorted(adjacency.get(cur, set())):
            if nb not in depths:
                depths[nb] = depths[cur] + 1
                q.append(nb)
    return depths


def build_switch_uplink_edges(
    *,
    switch_filter: int | None = None,
) -> tuple[list[dict], dict[int, int]]:
    """Trả (edges, depth_map).

    edges: {source_id, target_id, label, ...} dạng sw-<pk>, orient parent→child theo
    BFS từ core; mỗi cặp switch xuất hiện 1 lần (dedup vô hướng).
    depth_map: device_id → độ sâu BFS (core=0) để xếp tầng.

    Ưu tiên link_kind=switch trong DB; fallback star core → access switch khi DB rỗng.
    """
    from apps.devices.models import TopologyLink

    sw_links = TopologyLink.objects.filter(
        link_kind="switch",
        is_stale=False,
        remote_device__isnull=False,
    ).select_related("local_device", "remote_device")

    # Gom adjacency vô hướng + nhớ cổng/label theo từng đầu link.
    adjacency: dict[int, set[int]] = {}
    # port_label[(a, b)] = local_port của link có local_device=a, remote_device=b
    port_label: dict[tuple[int, int], str] = {}
    confirmed_pair: dict[frozenset[int], bool] = {}

    for link in sw_links:
        a = link.local_device_id
        b = link.remote_device_id
        if b is None or a == b:
            continue
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
        port_label[(a, b)] = link.local_port or ""
        key = frozenset((a, b))
        confirmed_pair[key] = confirmed_pair.get(key, False) or link.is_confirmed

    core = find_core_device()
    core_id = core.id if core else None

    if not adjacency:
        # Fallback star: core → mọi switch access (suy từ AP links ngoài hàm này).
        # Trả rỗng ở đây; caller dựng star riêng khi cần (giữ tương thích cũ).
        return _fallback_star(core_id, switch_filter)

    depth_map = bfs_depths(adjacency, core_id)

    def depth_of(dev_id: int) -> int:
        # Node chưa tới được từ core → đẩy xuống cuối (đứng sau mọi node có depth).
        return depth_map.get(dev_id, 10_000)

    edges: list[dict] = []
    seen_pairs: set[frozenset[int]] = set()

    for a, neighbors in adjacency.items():
        for b in neighbors:
            key = frozenset((a, b))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)

            # Parent = node nông hơn (gần core); hòa thì id nhỏ làm parent.
            da, db = depth_of(a), depth_of(b)
            if (da, a) <= (db, b):
                parent, child = a, b
            else:
                parent, child = b, a

            if switch_filter and switch_filter not in (parent, child):
                continue

            # Nhãn = cổng downlink của parent nếu có, không thì cổng đầu kia.
            label = port_label.get((parent, child)) or port_label.get((child, parent)) or ""
            edges.append({
                "source_id": f"sw-{parent}",
                "target_id": f"sw-{child}",
                "label": label,
                "confirmed": confirmed_pair.get(key, False),
                "trunk": True,
            })

    return edges, depth_map


def _fallback_star(
    core_id: int | None,
    switch_filter: int | None,
) -> tuple[list[dict], dict[int, int]]:
    """Star core → mọi switch access (khi DB chưa có link_kind=switch)."""
    from apps.devices.models import Device, TopologyLink

    if not core_id:
        return [], {}

    access_ids = set(
        TopologyLink.objects.filter(link_kind="ap", is_stale=False)
        .values_list("local_device_id", flat=True)
        .distinct()
    )
    targets = {switch_filter} if switch_filter else access_ids
    edges: list[dict] = []
    depth_map: dict[int, int] = {core_id: 0}
    for sw_id in sorted(targets):
        if sw_id == core_id:
            continue
        depth_map[sw_id] = 1
        edges.append({
            "source_id": f"sw-{core_id}",
            "target_id": f"sw-{sw_id}",
            "label": "",
            "confirmed": False,
            "inferred": True,
        })
    return edges, depth_map
