"""Ghép neighbor LLDP với AP trên WLAN controller (snapshot AP của AC)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.collectors.topology_lldp import NeighborRecord, normalize_mac

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    ap_name: str = ""
    ap_mac: str = ""
    ap_ip: str = ""
    match_method: str = "lldp"
    is_confirmed: bool = False


def _int_or_zero(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def load_ac_ap_rows(ac_device: Device | None) -> list[dict]:
    """Danh sách AP HIỆN TẠI của AC — nguồn DUY NHẤT cho mọi chỗ của topology (đồ thị, ghép LLDP,
    discovery, API chọn AP, lệnh chẩn đoán).

    Mỗi phần tử: {ap_name, ap_mac (đã chuẩn hoá, "" nếu không đọc được), ap_ip, is_online,
    client_count, run_state}.

    Cache-mode (`METRICS_WRITE_MODE=cache`) KHÔNG còn ghi `WifiApStats` xuống DB — snapshot mới nhất
    nằm ở Redis (`m:latest:<id>["wifi_aps"]`). Đọc thẳng bảng DB ở chế độ này trả snapshot đóng băng
    từ ngày bật cache-mode (prod 2026-10-21: lần ghi cuối 2026-07-02, DB 22 AP trong khi AC thật 23)
    nên AP mới không bao giờ được map/hiện, AP offline vẫn hiện online. Redis chưa có snapshot
    (key hết TTL / Redis lỗi) thì rơi về bảng DB cho khỏi mất hẳn dữ liệu ghép — có log cảnh báo vì
    DB lúc đó có thể cũ.
    """
    if not ac_device:
        return []

    from apps.metrics import cache as metrics_cache

    if metrics_cache.is_cache_mode():
        snap = metrics_cache.get_latest(ac_device.pk)
        if snap:
            return [
                {
                    "ap_name": a.get("name") or "",
                    "ap_mac": normalize_mac(a.get("mac")),
                    "ap_ip": a.get("ip") or "",
                    "is_online": bool(a.get("is_online")),
                    "client_count": _int_or_zero(a.get("client_count")),
                    "run_state": a.get("run_state") or "",
                }
                for a in (snap.get("wifi_aps") or [])
            ]
        logger.warning(
            "Topology: cache-mode nhưng Redis chưa có snapshot AP của %s — dùng WifiApStats DB "
            "(có thể cũ)", ac_device.name,
        )

    from apps.metrics.models import WifiApStats

    latest_ts = (
        WifiApStats.objects.filter(device=ac_device)
        .order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    if not latest_ts:
        return []
    return [
        {
            "ap_name": ap.ap_name or "",
            "ap_mac": normalize_mac(ap.ap_mac),
            "ap_ip": ap.ap_ip or "",
            "is_online": bool(ap.is_online),
            "client_count": ap.client_count or 0,
            "run_state": ap.run_state or "",
        }
        for ap in WifiApStats.objects.filter(device=ac_device, timestamp=latest_ts)
    ]


def _latest_ap_index(ac_device: Device) -> dict[str, dict[str, dict]]:
    rows = load_ac_ap_rows(ac_device)
    if not rows:
        return {}
    by_mac: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    by_ip: dict[str, dict] = {}
    for ap in rows:
        if ap["ap_mac"]:
            by_mac[ap["ap_mac"]] = ap
        name = ap["ap_name"].strip().casefold()
        if name:
            by_name[name] = ap
        ip = ap["ap_ip"].strip()
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
            ap_name=ap["ap_name"],
            ap_mac=mac,
            ap_ip=ap["ap_ip"],
            match_method="mac",
            is_confirmed=True,
        )

    sys_name = (neighbor.remote_sys_name or "").strip().casefold()
    if sys_name and sys_name in by_name:
        ap = by_name[sys_name]
        return MatchResult(
            ap_name=ap["ap_name"],
            ap_mac=ap["ap_mac"],
            ap_ip=ap["ap_ip"],
            match_method="name",
            is_confirmed=True,
        )

    mgmt_ip = (neighbor.remote_mgmt_ip or "").strip()
    if mgmt_ip and mgmt_ip in by_ip:
        ap = by_ip[mgmt_ip]
        return MatchResult(
            ap_name=ap["ap_name"],
            ap_mac=ap["ap_mac"],
            ap_ip=ap["ap_ip"],
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
    return {
        ap["ap_mac"]: {
            "ap_name": ap["ap_name"],
            "ap_ip": ap["ap_ip"],
            "is_online": ap["is_online"],
            "client_count": ap["client_count"],
        }
        for ap in load_ac_ap_rows(ac_device)
        if ap["ap_mac"]
    }


def load_ac_ap_macs(ac_device: Device | None) -> dict[str, str]:
    """MAC → tên AP (tương thích verify command)."""
    return {mac: info["ap_name"] for mac, info in load_ac_ap_snapshot(ac_device).items()}
