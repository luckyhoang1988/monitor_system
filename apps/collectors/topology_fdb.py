"""Thu thập MAC từ bảng FDB (forwarding) trên switch — fallback khi LLDP rỗng.

Đối chiếu MAC học được trên từng port với danh sách AP từ AC (WifiApStats).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from apps.collectors.snmp_client import (
    create_snmp_session,
    resolve_snmp_backend,
    snmp_walk_pairs,
)
from apps.collectors.topology_lldp import normalize_mac

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

OID_DIR = Path(__file__).resolve().parent.parent.parent / "oids"

# Q-BRIDGE FDB (VLAN-aware) — Huawei VRP dùng được
OID_DOT1Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"
# BRIDGE-MIB FDB (fallback)
OID_DOT1D_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
OID_DOT1D_BASEPORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"


@dataclass
class FdbMacEntry:
    local_port: str
    local_port_num: int
    mac: str
    vlan_id: int | None = None
    ap_name: str = ""
    ap_ip: str = ""
    is_ap_match: bool = False


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
