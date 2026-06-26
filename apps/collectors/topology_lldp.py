"""LLDP neighbor discovery từ switch — dùng cho topology AP ↔ Switch."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from apps.collectors.snmp_client import (
    create_snmp_session,
    resolve_snmp_backend,
    snmp_walk_pairs,
)
from apps.collectors.switch_snmp import SwitchSNMPCollector

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

OID_DIR = Path(__file__).resolve().parent.parent.parent / "oids"

# Tên neighbor có vẻ là AP (factory naming convention).
DEFAULT_AP_NAME_PATTERN = re.compile(
    r"(^AP[-_/]|^BPVN-|^X\d[-_/]|[-_]AP\d|/AP)",
    re.IGNORECASE,
)

_decode_mac_value = SwitchSNMPCollector._decode_mac_value
_decode_inet_ipv4 = SwitchSNMPCollector._decode_inet_ipv4


@dataclass
class NeighborRecord:
    local_port: str
    local_port_num: int
    remote_sys_name: str = ""
    remote_chassis_id: str = ""
    remote_port_id: str = ""
    remote_mac: str = ""
    remote_mgmt_ip: str = ""
    is_ap_candidate: bool = False
    protocol: str = "lldp"


def normalize_mac(raw: str | None) -> str:
    """Chuẩn hóa MAC về dạng aa:bb:cc:dd:ee:ff (lowercase)."""
    if not raw:
        return ""
    decoded = _decode_mac_value(str(raw).strip())
    if decoded and ":" in decoded:
        parts = decoded.split(":")
        if len(parts) == 6 and all(len(p) == 2 for p in parts):
            return ":".join(p.lower() for p in parts)
    # OctetString 6 byte nhị phân
    try:
        b = str(raw).encode("latin-1", "ignore")
        if len(b) == 6:
            return ":".join(f"{x:02x}" for x in b)
    except (UnicodeEncodeError, AttributeError):
        pass
    hex_only = re.sub(r"[^0-9a-fA-F]", "", str(raw))
    if len(hex_only) == 12:
        return ":".join(hex_only[i:i + 2].lower() for i in range(0, 12, 2))
    return ""


def _index_after(full_oid: str, prefix: str) -> str:
    p = prefix.rstrip(".")
    if not full_oid.startswith(p + "."):
        return ""
    return full_oid[len(p) + 1:]


def _walk_column(session, oid: str | None) -> dict[str, str]:
    if not oid:
        return {}
    result: dict[str, str] = {}
    for full_oid, value in snmp_walk_pairs(session, oid):
        idx = _index_after(full_oid, oid)
        if idx:
            result[idx] = value
    return result


def _parse_rem_index(index_suffix: str) -> tuple[int, int] | None:
    """Index lldpRemTable: timeMark.localPortNum.remIndex."""
    parts = index_suffix.split(".")
    if len(parts) < 3:
        return None
    try:
        time_mark = int(parts[0])
        local_port = int(parts[1])
    except ValueError:
        return None
    return time_mark, local_port


def _group_rem_rows(columns: dict[str, dict[str, str]]) -> dict[int, dict[str, str]]:
    """Gom neighbor theo localPortNum; ưu tiên timeMark lớn nhất."""
    grouped: dict[int, tuple[int, dict[str, str]]] = {}
    all_indexes = set()
    for col_values in columns.values():
        all_indexes.update(col_values.keys())

    col_names = list(columns.keys())
    for idx in all_indexes:
        parsed = _parse_rem_index(idx)
        if not parsed:
            continue
        time_mark, local_port = parsed
        row = {name: columns[name].get(idx, "") for name in col_names}
        prev = grouped.get(local_port)
        if prev is None or time_mark >= prev[0]:
            grouped[local_port] = (time_mark, row)
    return {port: data for port, (_, data) in grouped.items()}


def _chassis_to_mac(raw: str) -> str:
    mac = normalize_mac(raw)
    if mac:
        return mac
    return ""


def is_ap_neighbor(
    sys_name: str,
    chassis_mac: str,
    ap_pattern: re.Pattern | None = None,
    ap_macs: set[str] | None = None,
) -> bool:
    """Phân loại neighbor là AP — theo MAC trên AC hoặc pattern tên AP."""
    mac = normalize_mac(chassis_mac) if chassis_mac else ""
    if ap_macs and mac and mac in ap_macs:
        return True
    pattern = ap_pattern or DEFAULT_AP_NAME_PATTERN
    name = (sys_name or "").strip()
    return bool(name and pattern.search(name))


def _load_lldp_oids(os_family: str = "huawei_vrp") -> dict:
    path = OID_DIR / f"{os_family}.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        profile = yaml.safe_load(fh) or {}
    return profile.get("lldp") or {}


def _snmp_kwargs(device: Device) -> dict:
    version = {"v1": 1, "v2c": 2}.get(device.snmp_version, 2)
    return {
        "hostname": device.ip_address,
        "version": version,
        "community": device.snmp_community,
        "timeout": 10,
        "retries": 2,
    }


def _resolve_local_port_name(
    local_port_num: int,
    loc_port_map: dict[int, str],
    base_to_ifidx: dict[int, int],
    if_names: dict[str, str],
) -> str:
    if local_port_num in loc_port_map:
        name = loc_port_map[local_port_num].strip()
        if name:
            return name
    if str(local_port_num) in if_names:
        return if_names[str(local_port_num)]
    ifidx = base_to_ifidx.get(local_port_num)
    if ifidx is not None and str(ifidx) in if_names:
        return if_names[str(ifidx)]
    return f"port-{local_port_num}"


def collect_lldp_neighbors(
    device: Device,
    *,
    ap_only: bool = True,
    ap_pattern: re.Pattern | None = None,
    ap_macs: set[str] | None = None,
) -> list[NeighborRecord]:
    """Walk LLDP trên 1 switch, trả danh sách neighbor (mặc định chỉ AP)."""
    os_family = device.os_family or "huawei_vrp"
    oids = _load_lldp_oids(os_family)
    if not oids.get("rem_sys_name"):
        logger.warning("Topology %s: thiếu OID LLDP trong profile %s", device.name, os_family)
        return []

    backend = resolve_snmp_backend()
    session = create_snmp_session(_snmp_kwargs(device), backend=backend)

    if_names = {
        oid.split(".")[-1]: val
        for oid, val in snmp_walk_pairs(session, oids.get("if_descr", "1.3.6.1.2.1.2.2.1.2"))
    }

    base_to_ifidx: dict[int, int] = {}
    bp_oid = oids.get("dot1d_baseport_ifindex", "1.3.6.1.2.1.17.1.4.1.2")
    for oid, val in snmp_walk_pairs(session, bp_oid):
        try:
            base_to_ifidx[int(oid.split(".")[-1])] = int(val)
        except (ValueError, TypeError):
            continue

    loc_nums = _walk_column(session, oids.get("loc_port_num"))
    loc_ids = _walk_column(session, oids.get("loc_port_id"))
    loc_port_map: dict[int, str] = {}
    for idx, num_raw in loc_nums.items():
        try:
            port_num = int(num_raw)
            loc_port_map[port_num] = (loc_ids.get(idx) or "").strip()
        except ValueError:
            continue

    rem_columns = {
        "sys_name": _walk_column(session, oids.get("rem_sys_name")),
        "chassis_id": _walk_column(session, oids.get("rem_chassis_id")),
        "port_id": _walk_column(session, oids.get("rem_port_id")),
    }
    grouped = _group_rem_rows(rem_columns)

    neighbors: list[NeighborRecord] = []
    for local_port_num, row in sorted(grouped.items()):
        sys_name = (row.get("sys_name") or "").strip()
        chassis_raw = row.get("chassis_id") or ""
        chassis_mac = _chassis_to_mac(chassis_raw)
        port_id = (row.get("port_id") or "").strip()
        if is_ap_neighbor(sys_name, chassis_mac, ap_pattern, ap_macs):
            is_ap = True
        else:
            is_ap = False
            if ap_only:
                continue
        local_port = _resolve_local_port_name(
            local_port_num, loc_port_map, base_to_ifidx, if_names,
        )
        neighbors.append(NeighborRecord(
            local_port=local_port,
            local_port_num=local_port_num,
            remote_sys_name=sys_name,
            remote_chassis_id=str(chassis_raw).strip(),
            remote_port_id=port_id,
            remote_mac=chassis_mac,
            is_ap_candidate=is_ap,
        ))

    logger.info(
        "Topology LLDP %s: %d AP neighbor(s) from %d rem row(s)",
        device.name, len(neighbors), len(grouped),
    )
    return neighbors
