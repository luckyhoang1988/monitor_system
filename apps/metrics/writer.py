"""Ghi NormalizedData vào PostgreSQL qua Django ORM."""
import logging
from datetime import datetime
from django.utils import timezone
from apps.collectors.base import NormalizedData, InterfaceData
from apps.devices.models import Device, Interface
from .models import InterfaceStats, SystemHealth, VMStats

logger = logging.getLogger(__name__)


def save_metrics(device: Device, data: NormalizedData) -> None:
    _save_system_health(device, data)
    if data.interfaces:
        _save_interface_stats(device, data)
    if data.extra.get("vms"):
        _save_vm_stats(device, data)


def _save_system_health(device: Device, data: NormalizedData) -> None:
    # Lưu extra vendor-specific (bỏ qua key "vms" vì đã xử lý riêng)
    extra = {k: v for k, v in data.extra.items() if k != "vms"}
    SystemHealth.objects.create(
        device=device,
        timestamp=data.timestamp,
        cpu_percent=data.cpu_percent,
        mem_percent=data.mem_percent,
        uptime_secs=data.uptime_secs or None,
        extra=extra,
    )


from datetime import timedelta

def _save_interface_stats(device: Device, data: NormalizedData) -> None:
    # 1. Đồng bộ Interface list (giảm N get_or_create thành bulk)
    existing_ifaces = {i.if_index: i for i in Interface.objects.filter(device=device)}
    new_ifaces = []
    update_ifaces = []

    for iface_data in data.interfaces:
        if iface_data.if_index in existing_ifaces:
            iface = existing_ifaces[iface_data.if_index]
            if iface.name != iface_data.name:
                iface.name = iface_data.name
                update_ifaces.append(iface)
        else:
            new_ifaces.append(Interface(
                device=device,
                if_index=iface_data.if_index,
                name=iface_data.name,
                description=iface_data.description,
                is_uplink=(iface_data.name in device.uplink_ports),
            ))

    if new_ifaces:
        # bulk_create trả về objects đã có ID (nếu Postgres) hoặc không. Để an toàn, fetch lại:
        Interface.objects.bulk_create(new_ifaces)
        # Fetch lại để có PK
        existing_ifaces = {i.if_index: i for i in Interface.objects.filter(device=device)}
    
    if update_ifaces:
        Interface.objects.bulk_update(update_ifaces, ["name"])

    # 2. Fetch "previous stats" cho tất cả interface bằng 1 query (lấy trong khoảng 3 chu kỳ gần nhất)
    # Rất tối ưu, tránh N queries.
    cutoff = data.timestamp - timedelta(seconds=device.collect_interval * 3)
    recent_stats = InterfaceStats.objects.filter(
        interface__device=device,
        timestamp__gte=cutoff
    ).order_by("interface_id", "-timestamp")

    prev_stats_dict = {}
    for stat in recent_stats:
        if stat.interface_id not in prev_stats_dict:
            prev_stats_dict[stat.interface_id] = stat

    # 3. Tính toán và bulk_create
    stats_to_create = []
    for iface_data in data.interfaces:
        iface = existing_ifaces.get(iface_data.if_index)
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


def _calc_mbps(
    prev_stat: InterfaceStats | None,
    new: InterfaceData,
    new_timestamp: datetime | None,
    fallback_interval_secs: int,
) -> tuple[float, float]:
    """Tính tốc độ Mbps từ delta bytes / delta time giữa 2 poll."""
    if not prev_stat or prev_stat.in_bytes == 0:
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
        raw_delta_in = new.in_bytes - prev_stat.in_bytes
        raw_delta_out = new.out_bytes - prev_stat.out_bytes

        # If counter decreased (reboot/reset), clamp delta to 0.
        delta_in = max(0, raw_delta_in)
        delta_out = max(0, raw_delta_out)

        in_mbps  = (delta_in * 8) / (interval * 1_000_000)
        out_mbps = (delta_out * 8) / (interval * 1_000_000)
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
