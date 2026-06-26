"""Ghép neighbor LLDP với AP trên WLAN controller (WifiApStats)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.collectors.topology_lldp import NeighborRecord, normalize_mac

if TYPE_CHECKING:
    from apps.devices.models import Device
    from apps.metrics.models import WifiApStats


@dataclass
class MatchResult:
    ap_name: str = ""
    ap_mac: str = ""
    ap_ip: str = ""
    match_method: str = "lldp"
    is_confirmed: bool = False


def _latest_ap_index(ac_device: Device) -> dict[str, WifiApStats]:
    from apps.metrics.models import WifiApStats

    latest_ts = (
        WifiApStats.objects.filter(device=ac_device)
        .order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    if not latest_ts:
        return {}

    by_mac: dict[str, WifiApStats] = {}
    by_name: dict[str, WifiApStats] = {}
    by_ip: dict[str, WifiApStats] = {}
    for ap in WifiApStats.objects.filter(device=ac_device, timestamp=latest_ts):
        mac = normalize_mac(ap.ap_mac)
        if mac:
            by_mac[mac] = ap
        name = (ap.ap_name or "").strip().casefold()
        if name:
            by_name[name] = ap
        ip = (ap.ap_ip or "").strip()
        if ip:
            by_ip[ip] = ap
    return {"mac": by_mac, "name": by_name, "ip": by_ip}


def match_lldp_to_ap(neighbor: NeighborRecord, ac_device: Device | None) -> MatchResult:
    """Ghép 1 neighbor LLDP với snapshot AP mới nhất trên AC."""
    result = MatchResult(
        ap_mac=neighbor.remote_mac,
        ap_name=neighbor.remote_sys_name,
        match_method="lldp",
        is_confirmed=False,
    )
    if not ac_device:
        return result

    index = _latest_ap_index(ac_device)
    by_mac = index.get("mac") or {}
    by_name = index.get("name") or {}
    by_ip = index.get("ip") or {}

    mac = normalize_mac(neighbor.remote_mac)
    if mac and mac in by_mac:
        ap = by_mac[mac]
        return MatchResult(
            ap_name=ap.ap_name,
            ap_mac=mac,
            ap_ip=ap.ap_ip or "",
            match_method="mac",
            is_confirmed=True,
        )

    sys_name = (neighbor.remote_sys_name or "").strip().casefold()
    if sys_name and sys_name in by_name:
        ap = by_name[sys_name]
        return MatchResult(
            ap_name=ap.ap_name,
            ap_mac=normalize_mac(ap.ap_mac),
            ap_ip=ap.ap_ip or "",
            match_method="name",
            is_confirmed=True,
        )

    mgmt_ip = (neighbor.remote_mgmt_ip or "").strip()
    if mgmt_ip and mgmt_ip in by_ip:
        ap = by_ip[mgmt_ip]
        return MatchResult(
            ap_name=ap.ap_name,
            ap_mac=normalize_mac(ap.ap_mac),
            ap_ip=ap.ap_ip or "",
            match_method="ip",
            is_confirmed=True,
        )

    return result


def get_default_ac_device() -> Device | None:
    from apps.devices.models import Device

    return (
        Device.objects.filter(device_type="wlan_controller", enabled=True)
        .order_by("name")
        .first()
    )


def load_ac_ap_snapshot(ac_device: Device | None) -> dict[str, dict]:
    """MAC chuẩn hóa → {ap_name, ap_ip, is_online, client_count}."""
    from apps.metrics.models import WifiApStats

    if not ac_device:
        return {}

    latest_ts = (
        WifiApStats.objects.filter(device=ac_device)
        .order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    if not latest_ts:
        return {}

    result: dict[str, dict] = {}
    for ap in WifiApStats.objects.filter(device=ac_device, timestamp=latest_ts):
        mac = normalize_mac(ap.ap_mac)
        if not mac:
            continue
        result[mac] = {
            "ap_name": ap.ap_name,
            "ap_ip": ap.ap_ip or "",
            "is_online": ap.is_online,
            "client_count": ap.client_count,
        }
    return result


def load_ac_ap_macs(ac_device: Device | None) -> dict[str, str]:
    """MAC → tên AP (tương thích verify command)."""
    return {mac: info["ap_name"] for mac, info in load_ac_ap_snapshot(ac_device).items()}
