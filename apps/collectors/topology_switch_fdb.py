"""Phát hiện kết nối switch↔switch (cây nhiều tầng) từ bảng FDB.

Nguyên lý (cây): mỗi switch học MAC của switch khác qua đúng 1 cổng (cổng hướng về
phía switch đó). Với cặp X, Y — gọi seen(X,p) = tập switch X "thấy" (có MAC thuộc
switch đó) trên cổng p:

    X nối trực tiếp Y  ⟺  X thấy Y trên pX, Y thấy X trên pY, và
                          seen(X,pX) ∩ seen(Y,pY) = ∅.

Nếu có switch Z xen giữa X–Y thì Z xuất hiện trên CẢ pX lẫn pY → giao khác rỗng →
loại. Test này tự loại cổng tới end-host (không chứa MAC switch) nên không cần phân
biệt trunk/access trước.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from apps.collectors.snmp_client import (
    create_snmp_session,
    resolve_snmp_backend,
    snmp_walk_pairs,
)
from apps.collectors.topology_fdb import collect_switch_mac_table
from apps.collectors.topology_lldp import normalize_mac

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

# BRIDGE-MIB dot1dBaseBridgeAddress (scalar) — MAC gốc của bridge
OID_DOT1D_BASE_BRIDGE_ADDRESS = "1.3.6.1.2.1.17.1.1"
# IF-MIB ifPhysAddress — MAC từng interface
OID_IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"


def _snmp_kwargs(device: Device) -> dict:
    version = {"v1": 1, "v2c": 2}.get(device.snmp_version, 2)
    return {
        "hostname": device.ip_address,
        "version": version,
        "community": device.snmp_community,
        "timeout": 10,
        "retries": 2,
    }


def collect_switch_self_macs(device: Device) -> set[str]:
    """Tập MAC thuộc về chính switch (bridge base + mọi ifPhysAddress)."""
    backend = resolve_snmp_backend()
    session = create_snmp_session(_snmp_kwargs(device), backend=backend)

    macs: set[str] = set()
    for _oid, val in snmp_walk_pairs(session, OID_DOT1D_BASE_BRIDGE_ADDRESS):
        mac = normalize_mac(val)
        if mac:
            macs.add(mac)
    for _oid, val in snmp_walk_pairs(session, OID_IF_PHYS_ADDRESS):
        mac = normalize_mac(val)
        # bỏ MAC rỗng/00:00:.. (interface ảo không có MAC vật lý)
        if mac and mac != "00:00:00:00:00:00":
            macs.add(mac)
    return macs


def build_switch_mac_registry(switches: list[Device]) -> dict[str, int]:
    """Gộp MAC → device_id cho toàn fleet (gọi 1 lần mỗi vòng discovery)."""
    registry: dict[str, int] = {}
    for sw in switches:
        try:
            self_macs = collect_switch_self_macs(sw)
        except Exception as exc:  # noqa: BLE001 — SNMP lỗi không được fail cả run
            logger.warning("Topology switch-MAC %s: lỗi thu MAC: %s", sw.name, exc)
            continue
        for mac in self_macs:
            # MAC trùng giữa 2 switch (hiếm) → giữ chủ đầu tiên, ghi cảnh báo
            if mac in registry and registry[mac] != sw.id:
                logger.debug("MAC %s thấy ở nhiều switch (%s, %s)", mac, registry[mac], sw.id)
                continue
            registry[mac] = sw.id
        logger.info("Topology switch-MAC %s: %d MAC", sw.name, len(self_macs))
    return registry


def collect_switch_fdb_by_port(device: Device) -> dict[str, set[str]]:
    """{port_name: set(mac)} — toàn bộ MAC học được, gom theo cổng."""
    entries = collect_switch_mac_table(device, ap_macs=None, ap_only=False)
    by_port: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        by_port[e.local_port].add(e.mac)
    return dict(by_port)


def discover_switch_adjacency(
    switches: list[Device],
    registry: dict[str, int] | None = None,
) -> list[tuple[int, str, int, str]]:
    """Trả các cặp switch nối trực tiếp: (dev_a, port_a, dev_b, port_b).

    Cặp vô hướng (mỗi cặp xuất hiện 1 lần). Switch có FDB rỗng bất thường bị bỏ qua
    vòng này (preserve-on-empty) — không tạo/stale nhầm.
    """
    switches = list(switches)
    if registry is None:
        registry = build_switch_mac_registry(switches)

    # seen[sw_id][port] = set(device_id switch khác thấy trên cổng đó)
    seen: dict[int, dict[str, set[int]]] = {}
    for sw in switches:
        try:
            fdb = collect_switch_fdb_by_port(sw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Topology switch-FDB %s: lỗi walk FDB: %s", sw.name, exc)
            continue
        if not fdb:
            logger.info("Topology switch-FDB %s: FDB rỗng — bỏ qua vòng này", sw.name)
            continue
        per_port: dict[str, set[int]] = {}
        for port, macs in fdb.items():
            others = {
                owner
                for mac in macs
                if (owner := registry.get(mac)) is not None and owner != sw.id
            }
            if others:
                per_port[port] = others
        seen[sw.id] = per_port

    results: list[tuple[int, str, int, str]] = []
    present = sorted(seen.keys())
    for i, x in enumerate(present):
        for y in present[i + 1:]:
            x_ports = [p for p, others in seen[x].items() if y in others]
            y_ports = [p for p, others in seen[y].items() if x in others]
            if not x_ports or not y_ports:
                continue
            found: tuple[str, str] | None = None
            for px in x_ports:
                for py in y_ports:
                    if not (seen[x][px] & seen[y][py]):
                        found = (px, py)
                        break
                if found:
                    break
            if found:
                results.append((x, found[0], y, found[1]))

    logger.info(
        "Topology switch-FDB: %d switch có FDB, %d cặp nối trực tiếp",
        len(present), len(results),
    )
    return results
