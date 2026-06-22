"""Ghi NormalizedData vào PostgreSQL qua Django ORM."""
import logging
from datetime import datetime
from django.conf import settings
from django.utils import timezone
from apps.collectors.base import NormalizedData, InterfaceData
from apps.devices.models import Device, Interface
from .models import InterfaceStats, SystemHealth, VMStats, WifiApStats, WifiClientStats

logger = logging.getLogger(__name__)


TRUNK_NAME_PREFIXES = (
    "po",              # Cisco Port-channel short name
    "port-channel",
    "eth-trunk",
    "bridge-aggregation",
    "bond",
    "lag",
    "ae",              # Juniper aggregated ethernet style
    "trunk",
    "xgigabit",        # Huawei 10G
    "xge",
    "10ge",
    "25ge",
    "40ge",
    "100ge",
)
TRUNK_DESC_KEYWORDS = (
    "trunk",
    "uplink",
    "up-link",
    "backbone",
    "inter-switch",
    "to-core",
    "to core",
)


_EXTRA_SKIP_KEYS = ("vms", "wifi_aps", "wifi_clients")


def save_metrics(device: Device, data: NormalizedData) -> None:
    _save_system_health(device, data)
    if data.interfaces:
        _save_interface_stats(device, data)
    if data.extra.get("vms"):
        _save_vm_stats(device, data)
    if data.extra.get("wifi_aps"):
        _save_wifi_ap_stats(device, data)
    if data.extra.get("wifi_clients"):
        _save_wifi_client_stats(device, data)


def _save_system_health(device: Device, data: NormalizedData) -> None:
    # Lưu extra vendor-specific (bỏ các key list lớn đã xử lý riêng).
    extra = {k: v for k, v in data.extra.items() if k not in _EXTRA_SKIP_KEYS}
    SystemHealth.objects.create(
        device=device,
        timestamp=data.timestamp,
        cpu_percent=data.cpu_percent,
        mem_percent=data.mem_percent,
        uptime_secs=data.uptime_secs or None,
        extra=extra,
    )


from datetime import timedelta


def _normalize_text(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_trunk_interface(device: Device, iface_data: InterfaceData) -> bool:
    # Manual cấu hình luôn được ưu tiên.
    normalized_manual = {_normalize_text(p) for p in (device.uplink_ports or [])}
    normalized_name = _normalize_text(iface_data.name)
    if normalized_name in normalized_manual:
        return True

    normalized_desc = _normalize_text(iface_data.description)
    if normalized_desc and any(keyword in normalized_desc for keyword in TRUNK_DESC_KEYWORDS):
        return True

    if normalized_name.startswith(TRUNK_NAME_PREFIXES):
        return True

    # Uplink liên tầng thường là 10G+ (heuristic mềm, tránh phụ thuộc vendor).
    if float(iface_data.speed_mbps or 0) >= 10000:
        return True

    return False


def _iface_key(name: str | None) -> str:
    """Khoá định danh interface — theo TÊN (ổn định cho cả SNMP & SSH).

    SSH collector sinh if_index theo vị trí block CLI nên không ổn định giữa các poll;
    tên cổng mới là định danh bền vững. Chuẩn hoá strip + casefold để tránh lệch hoa/thường.
    """
    return (name or "").strip().casefold()


def _save_interface_stats(device: Device, data: NormalizedData) -> None:
    # 1. Đồng bộ Interface list (giảm N get_or_create thành bulk) — khớp theo TÊN.
    existing_ifaces = {_iface_key(i.name): i for i in Interface.objects.filter(device=device)}
    new_ifaces = []
    update_ifaces = []

    for iface_data in data.interfaces:
        key = _iface_key(iface_data.name)
        if key in existing_ifaces:
            iface = existing_ifaces[key]
            next_is_uplink = _is_trunk_interface(device, iface_data)
            if (
                iface.if_index != iface_data.if_index
                or iface.description != iface_data.description
                or iface.is_uplink != next_is_uplink
            ):
                iface.if_index = iface_data.if_index
                iface.description = iface_data.description
                iface.is_uplink = next_is_uplink
                update_ifaces.append(iface)
        else:
            new_ifaces.append(Interface(
                device=device,
                if_index=iface_data.if_index,
                name=iface_data.name,
                description=iface_data.description,
                is_uplink=_is_trunk_interface(device, iface_data),
            ))

    if new_ifaces:
        # bulk_create trả về objects đã có ID (nếu Postgres) hoặc không. Để an toàn, fetch lại:
        Interface.objects.bulk_create(new_ifaces)
        # Fetch lại để có PK
        existing_ifaces = {_iface_key(i.name): i for i in Interface.objects.filter(device=device)}

    if update_ifaces:
        Interface.objects.bulk_update(update_ifaces, ["if_index", "description", "is_uplink"])

    # 2. Fetch "previous stats" cho tất cả interface bằng 1 query.
    # Cửa sổ tìm prev phải đủ rộng: nhịp poll thực do Celery beat quyết định (vd 300s)
    # và có thể LỚN hơn device.collect_interval. Nếu cửa sổ < nhịp poll thật,
    # prev luôn nằm ngoài → mbps=0 giả. Dùng floor METRIC_PREV_LOOKBACK_SECS.
    lookback = max(
        device.collect_interval * 3,
        int(getattr(settings, "METRIC_PREV_LOOKBACK_SECS", 900)),
    )
    cutoff = data.timestamp - timedelta(seconds=lookback)
    recent_stats = InterfaceStats.objects.filter(
        interface__device=device,
        timestamp__lt=data.timestamp,
        timestamp__gte=cutoff,
    ).order_by("interface_id", "-timestamp")

    prev_stats_dict = {}
    for stat in recent_stats:
        if stat.interface_id not in prev_stats_dict:
            prev_stats_dict[stat.interface_id] = stat

    # 3. Tính toán và bulk_create
    stats_to_create = []
    for iface_data in data.interfaces:
        iface = existing_ifaces.get(_iface_key(iface_data.name))
        if not iface:
            continue

        prev_stat = prev_stats_dict.get(iface.id)
        in_mbps, out_mbps = _calc_mbps(prev_stat, iface_data, data.timestamp, device.collect_interval)

        stats_to_create.append(InterfaceStats(
            interface=iface,
            timestamp=data.timestamp,
            status=iface_data.status,
            in_bytes=iface_data.in_bytes,
            out_bytes=iface_data.out_bytes,
            in_errors=iface_data.in_errors,
            out_errors=iface_data.out_errors,
            in_mbps=in_mbps,
            out_mbps=out_mbps,
        ))

    if stats_to_create:
        InterfaceStats.objects.bulk_create(stats_to_create)


def _counter_delta(prev: int, new: int, max_counter: int) -> int:
    """Tính delta của counter, phân biệt wrap (tràn) vs reset (reboot).

    - Delta dương: bình thường.
    - Delta âm + prev gần trần (> 90% max): counter tràn vòng → cộng max_counter.
    - Delta âm khác: counter bị reset (thiết bị reboot) → trả 0 (bỏ mẫu, tránh spike giả).
    """
    delta = new - prev
    if delta >= 0:
        return delta
    if prev > max_counter * 0.9:
        return delta + max_counter
    return 0


def _calc_mbps(
    prev_stat: InterfaceStats | None,
    new: InterfaceData,
    new_timestamp: datetime | None,
    fallback_interval_secs: int,
) -> tuple[float, float]:
    """Tính tốc độ Mbps từ delta bytes / delta time giữa 2 poll."""
    if not prev_stat:
        return 0.0, 0.0
    try:
        # seconds between samples
        interval = float(fallback_interval_secs or 0)
        if new_timestamp and prev_stat.timestamp:
            dt = (new_timestamp - prev_stat.timestamp).total_seconds()
            if dt > 0:
                interval = float(dt)
        if not interval:
            return 0.0, 0.0

        max_counter = 2**64  # ifHCIn/OutOctets are 64-bit counters
        delta_in = _counter_delta(prev_stat.in_bytes, new.in_bytes, max_counter)
        delta_out = _counter_delta(prev_stat.out_bytes, new.out_bytes, max_counter)

        in_mbps  = (delta_in * 8) / (interval * 1_000_000)
        out_mbps = (delta_out * 8) / (interval * 1_000_000)

        # Sanity cap: nếu tốc độ tính ra vượt ~1.5× tốc độ cổng → coi là số rác
        # (counter nhảy bất thường) và bỏ mẫu thay vì lưu spike giả.
        speed = float(getattr(new, "speed_mbps", 0) or 0)
        if speed > 0:
            cap = speed * 1.5
            if in_mbps > cap or out_mbps > cap:
                logger.debug(
                    "_calc_mbps: bỏ mẫu vì vượt cap (in=%.1f out=%.1f cap=%.1f Mbps)",
                    in_mbps, out_mbps, cap,
                )
                return 0.0, 0.0

        return round(in_mbps, 3), round(out_mbps, 3)
    except Exception as exc:
        logger.debug("_calc_mbps failed: %s", exc)
        return 0.0, 0.0


def _save_vm_stats(device: Device, data: NormalizedData) -> None:
    vms_to_create = []
    for vm in data.extra.get("vms", []):
        try:
            vms_to_create.append(VMStats(
                device=device,
                timestamp=data.timestamp,
                vm_name=vm.get("name", ""),
                state=vm.get("state", ""),
                cpu_percent=float(vm.get("cpu_percent") or 0),
                mem_assigned_mb=int(vm.get("mem_mb") or 0),
                repl_health=(vm.get("repl_health") or "")[:50],
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("Device %s: skip VM %r — bad data: %s",
                           device.name, vm.get("name"), exc)
    if vms_to_create:
        VMStats.objects.bulk_create(vms_to_create)


def _save_wifi_ap_stats(device: Device, data: NormalizedData) -> None:
    aps_to_create = []
    for ap in data.extra.get("wifi_aps", []):
        try:
            aps_to_create.append(WifiApStats(
                device=device,
                timestamp=data.timestamp,
                ap_name=str(ap.get("name") or "")[:200],
                ap_mac=str(ap.get("mac") or "")[:32],
                ap_ip=str(ap.get("ip") or "")[:64],
                ap_group=str(ap.get("group") or "")[:128],
                is_online=bool(ap.get("is_online")),
                run_state=str(ap.get("run_state") or "")[:32],
                client_count=int(ap.get("client_count") or 0),
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("Device %s: skip AP %r — bad data: %s",
                           device.name, ap.get("name"), exc)
    if aps_to_create:
        WifiApStats.objects.bulk_create(aps_to_create)


def _save_wifi_client_stats(device: Device, data: NormalizedData) -> None:
    clients_to_create = []
    for c in data.extra.get("wifi_clients", []):
        try:
            rssi = c.get("rssi")
            clients_to_create.append(WifiClientStats(
                device=device,
                timestamp=data.timestamp,
                mac=str(c.get("mac") or "")[:32],
                ip=str(c.get("ip") or "")[:64],
                ssid=str(c.get("ssid") or "")[:128],
                ap_name=str(c.get("ap_name") or "")[:200],
                radio=str(c.get("radio") or "")[:32],
                rssi=int(rssi) if rssi not in (None, "") else None,
                online_secs=int(c.get("online_secs") or 0),
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("Device %s: skip WiFi client %r — bad data: %s",
                           device.name, c.get("mac"), exc)
    if clients_to_create:
        WifiClientStats.objects.bulk_create(clients_to_create)
