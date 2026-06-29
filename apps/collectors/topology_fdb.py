"""Thu thập MAC từ bảng FDB (forwarding) trên switch — fallback khi LLDP rỗng.

Đối chiếu MAC học được trên từng port với danh sách AP từ AC (WifiApStats).
Lọc bỏ cổng trunk/uplink để tránh gán nhầm AP lên cổng uplink.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.collectors.snmp_client import (
    create_snmp_session,
    resolve_snmp_backend,
    snmp_walk_pairs,
)
from apps.collectors.topology_lldp import normalize_mac

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

# Q-BRIDGE FDB (VLAN-aware) — Huawei VRP dùng được
OID_DOT1Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"
# BRIDGE-MIB FDB (fallback)
OID_DOT1D_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
OID_DOT1D_BASEPORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"

# Cổng có >= N MAC AP → coi là trunk flooding, bỏ qua
AP_MAC_FLOOD_THRESHOLD = 3
# Cổng có >= N MAC TỔNG (mọi loại) → uplink/trunk gom nhiều thiết bị, không phải port AP.
# Bắt uplink chỉ mang 1 MAC-AP nhưng hàng trăm MAC khác (AP_MAC_FLOOD_THRESHOLD bỏ sót):
# vd switch nối tầng học 1 AP downstream qua uplink → MAC-AP=1 < 3 nhưng tổng MAC ~hàng trăm.
FDB_UPLINK_TOTAL_MAC_THRESHOLD = 25

TRUNK_NAME_PREFIXES = (
    "po", "port-channel", "eth-trunk", "bridge-aggregation",
    "bond", "lag", "ae", "trunk",
)
TRUNK_NAME_PATTERN = re.compile(
    r"^(eth-trunk|port-channel|po\d|xge|xgigabit|10ge|25ge|40ge|100ge)",
    re.IGNORECASE,
)


@dataclass
class FdbMacEntry:
    local_port: str
    local_port_num: int
    mac: str
    vlan_id: int | None = None
    ap_name: str = ""
    ap_ip: str = ""
    is_ap_match: bool = False
    excluded_uplink: bool = False


@dataclass
class PortMeta:
    port_mode: str = ""
    is_uplink: bool = False
    manual_uplink: bool = False
    name_trunk: bool = False
    ap_mac_count: int = 0
    total_mac_count: int = 0


def _normalize_port_name(name: str) -> str:
    return (name or "").strip().casefold()


def _is_trunk_port_name(port_name: str) -> bool:
    n = _normalize_port_name(port_name)
    if any(n.startswith(p) for p in TRUNK_NAME_PREFIXES):
        return True
    return bool(TRUNK_NAME_PATTERN.match(n))


def _load_port_meta(
    device: Device,
    ap_entries: list[FdbMacEntry],
    port_total_counts: dict[str, int] | None = None,
) -> dict[str, PortMeta]:
    from apps.devices.models import Interface

    port_total_counts = port_total_counts or {}
    port_ap_counts: dict[str, int] = defaultdict(int)
    for e in ap_entries:
        if e.is_ap_match:
            port_ap_counts[e.local_port] += 1

    manual_ports = {_normalize_port_name(p) for p in (device.uplink_ports or [])}
    meta: dict[str, PortMeta] = {}

    for iface in Interface.objects.filter(device=device):
        meta[iface.name] = PortMeta(
            port_mode=iface.port_mode or "",
            is_uplink=bool(iface.is_uplink),
            manual_uplink=_normalize_port_name(iface.name) in manual_ports,
            name_trunk=_is_trunk_port_name(iface.name),
            ap_mac_count=port_ap_counts.get(iface.name, 0),
            total_mac_count=port_total_counts.get(iface.name, 0),
        )

    for e in ap_entries:
        if e.local_port not in meta:
            meta[e.local_port] = PortMeta(
                name_trunk=_is_trunk_port_name(e.local_port),
                manual_uplink=_normalize_port_name(e.local_port) in manual_ports,
                ap_mac_count=port_ap_counts.get(e.local_port, 0),
                total_mac_count=port_total_counts.get(e.local_port, 0),
            )
        else:
            meta[e.local_port].ap_mac_count = port_ap_counts.get(e.local_port, 0)
            meta[e.local_port].total_mac_count = port_total_counts.get(e.local_port, 0)

    return meta


def is_uplink_port(port_name: str, port_meta: dict[str, PortMeta]) -> bool:
    """True nếu cổng là trunk/uplink — không dùng để map AP."""
    m = port_meta.get(port_name)
    if not m:
        return _is_trunk_port_name(port_name)
    if m.port_mode in ("trunk", "hybrid"):
        return True
    if m.is_uplink or m.manual_uplink or m.name_trunk:
        return True
    if m.ap_mac_count >= AP_MAC_FLOOD_THRESHOLD:
        return True
    if m.total_mac_count >= FDB_UPLINK_TOTAL_MAC_THRESHOLD:
        return True
    return False


def _pick_best_port(
    candidates: list[FdbMacEntry],
    port_meta: dict[str, PortMeta],
) -> FdbMacEntry:
    """Chọn 1 port access tốt nhất khi cùng MAC xuất hiện nhiều cổng."""

    def sort_key(e: FdbMacEntry) -> tuple:
        m = port_meta.get(e.local_port, PortMeta())
        is_access = 0 if m.port_mode == "access" else 1
        is_uplink = 1 if is_uplink_port(e.local_port, port_meta) else 0
        # Port ít MAC tổng hơn = sát thiết bị (access) hơn port gom nhiều MAC (uplink).
        return (is_uplink, is_access, m.total_mac_count, m.ap_mac_count, e.local_port)

    return min(candidates, key=sort_key)


def filter_fdb_ap_entries(
    device: Device,
    entries: list[FdbMacEntry],
    *,
    port_total_counts: dict[str, int] | None = None,
    exclude_uplink: bool = True,
) -> list[FdbMacEntry]:
    """Lọc cổng uplink/trunk; mỗi MAC AP chỉ giữ 1 port access tốt nhất."""
    ap_entries = [e for e in entries if e.is_ap_match]
    if not ap_entries:
        # Không MAC nào khớp AP → KHÔNG có AP link. Trả [] (KHÔNG phải `entries`):
        # trả toàn bộ MAC sẽ khiến caller tạo 1 link AP giả/cổng (update_or_create
        # keyed theo (device, port) → "last MAC wins") cho mọi switch không có AP
        # thật qua FDB (cisco_business không LLDP, hoặc walk ra partial table).
        return []

    port_meta = _load_port_meta(device, ap_entries, port_total_counts)
    excluded_ports = {
        p for p in port_meta if exclude_uplink and is_uplink_port(p, port_meta)
    }

    if excluded_ports:
        logger.info(
            "Topology FDB %s: loại %d cổng uplink/trunk (%s)",
            device.name,
            len(excluded_ports),
            ", ".join(sorted(excluded_ports)[:6])
            + ("..." if len(excluded_ports) > 6 else ""),
        )

    by_mac: dict[str, list[FdbMacEntry]] = defaultdict(list)
    for e in ap_entries:
        e.excluded_uplink = e.local_port in excluded_ports
        if exclude_uplink and e.excluded_uplink:
            continue
        by_mac[e.mac].append(e)

    result: list[FdbMacEntry] = []
    for mac, candidates in by_mac.items():
        if len(candidates) == 1:
            result.append(candidates[0])
        else:
            result.append(_pick_best_port(candidates, port_meta))

    return result


def collect_fdb_ap_mappings(device: Device, ap_macs: set[str]) -> list[FdbMacEntry]:
    """Walk FDB + lọc uplink + dedupe MAC — dùng cho topology discovery.

    Walk TOÀN BỘ MAC (không chỉ MAC-AP) để đếm tổng MAC/port → nhận diện uplink
    gom nhiều thiết bị (xem FDB_UPLINK_TOTAL_MAC_THRESHOLD).
    """
    raw = collect_switch_mac_table(device, ap_macs=ap_macs, ap_only=False)
    port_total_counts: dict[str, int] = defaultdict(int)
    for e in raw:
        port_total_counts[e.local_port] += 1
    return filter_fdb_ap_entries(
        device, raw, port_total_counts=port_total_counts, exclude_uplink=True,
    )


def _snmp_kwargs(device: Device) -> dict:
    version = {"v1": 1, "v2c": 2}.get(device.snmp_version, 2)
    return {
        "hostname": device.ip_address,
        "version": version,
        "community": device.snmp_community,
        "timeout": 15,
        "retries": 2,
    }


def _index_after(full_oid: str, prefix: str) -> str:
    p = prefix.rstrip(".")
    if not full_oid.startswith(p + "."):
        return ""
    return full_oid[len(p) + 1:]


def _mac_from_fdb_index(index_suffix: str) -> tuple[int | None, str]:
    """Parse index FDB: vlan.mac(6 octets) hoặc mac(6 octets)."""
    parts = index_suffix.split(".")
    try:
        if len(parts) == 7:
            vlan = int(parts[0])
            octets = [int(p) for p in parts[1:7]]
        elif len(parts) == 6:
            vlan = None
            octets = [int(p) for p in parts]
        else:
            return None, ""
        if any(o < 0 or o > 255 for o in octets):
            return None, ""
        mac = ":".join(f"{o:02x}" for o in octets)
        return vlan, mac
    except ValueError:
        return None, ""


def _resolve_port_name(
    base_port: int,
    base_to_ifidx: dict[int, int],
    if_names: dict[str, str],
) -> str:
    ifidx = base_to_ifidx.get(base_port)
    if ifidx is not None and str(ifidx) in if_names:
        return if_names[str(ifidx)]
    if str(base_port) in if_names:
        return if_names[str(base_port)]
    return f"port-{base_port}"


def _walk_fdb_port_map(session) -> dict[tuple[int | None, str], int]:
    """Trả {(vlan, mac): dot1dBasePort}."""
    result: dict[tuple[int | None, str], int] = {}
    for oid_prefix in (OID_DOT1Q_FDB_PORT, OID_DOT1D_FDB_PORT):
        rows = snmp_walk_pairs(session, oid_prefix)
        if not rows:
            continue
        for full_oid, val in rows:
            idx = _index_after(full_oid, oid_prefix)
            vlan, mac = _mac_from_fdb_index(idx)
            if not mac:
                continue
            try:
                base_port = int(val)
            except (ValueError, TypeError):
                continue
            key = (vlan, mac)
            # Ưu tiên entry có vlan khi trùng MAC
            if key not in result or vlan is not None:
                result[key] = base_port
        if result:
            break
    return result


def collect_switch_mac_table(
    device: Device,
    *,
    ap_macs: set[str] | None = None,
    ap_only: bool = False,
) -> list[FdbMacEntry]:
    """Walk FDB switch. Nếu ap_macs cho trước, chỉ trả MAC khớp AP (hoặc ap_only=True)."""
    backend = resolve_snmp_backend()
    session = create_snmp_session(_snmp_kwargs(device), backend=backend)

    if_names = {
        oid.split(".")[-1]: val
        for oid, val in snmp_walk_pairs(session, OID_IF_DESCR)
    }
    base_to_ifidx: dict[int, int] = {}
    for oid, val in snmp_walk_pairs(session, OID_DOT1D_BASEPORT_IFINDEX):
        try:
            base_to_ifidx[int(oid.split(".")[-1])] = int(val)
        except (ValueError, TypeError):
            continue

    fdb_map = _walk_fdb_port_map(session)
    entries: list[FdbMacEntry] = []
    seen_mac_port: set[tuple[str, str]] = set()

    for (vlan, mac), base_port in sorted(fdb_map.items(), key=lambda x: (x[0][1], x[1])):
        is_ap = ap_macs is not None and mac in ap_macs
        if ap_only and ap_macs is not None and not is_ap:
            continue
        port_name = _resolve_port_name(base_port, base_to_ifidx, if_names)
        key = (mac, port_name)
        if key in seen_mac_port:
            continue
        seen_mac_port.add(key)
        entries.append(FdbMacEntry(
            local_port=port_name,
            local_port_num=base_port,
            mac=mac,
            vlan_id=vlan,
            is_ap_match=is_ap,
        ))

    logger.info(
        "Topology FDB %s: %d MAC row(s), %d AP match(es)",
        device.name,
        len(fdb_map),
        sum(1 for e in entries if e.is_ap_match),
    )
    return entries
