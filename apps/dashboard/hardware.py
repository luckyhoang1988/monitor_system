"""Latest iLO snapshots and their presentation, independent of host connectivity."""

from django.conf import settings
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from apps.metrics.models import HardwareHealth

HEALTH_FIELDS = {
    "controller_health_code": "RAID Controller",
    "logical_drive_worst_code": "Logical Drives",
    "battery_health_code": "Battery",
    "processor_health_code": "Processors",
    "memory_health_code": "Memory",
    "fan_worst_code": "Fans",
    "temperature_worst_code": "Temperatures",
    "power_supply_worst_code": "Power Supplies",
    "bios_hardware_health_code": "BIOS/Hardware",
    "network_health_code": "Network",
}


def latest_hardware(devices):
    """Fetch one snapshot per configured host in a single query."""
    ids = [d.pk for d in devices if d.ilo_ip_address]
    if not ids:
        return {}
    latest = HardwareHealth.objects.filter(device_id=OuterRef("device_id")).order_by("-timestamp", "-pk")
    return {h.device_id: h for h in HardwareHealth.objects.filter(
        device_id__in=ids, pk=Subquery(latest.values("pk")[:1])
    ).defer("raw")}


def hardware_summary(device, health):
    label, color, problems = "Chưa cấu hình", "secondary", []
    stale = False
    if device.ilo_ip_address:
        label = "Chưa có dữ liệu"
        if health:
            codes = [getattr(health, field) for field in HEALTH_FIELDS]
            for field, name in HEALTH_FIELDS.items():
                code = getattr(health, field)
                if code in (1, 2):
                    problems.append(f"{name}: {'Critical' if code == 2 else 'Warning'}")
            degraded = False
            for field, name in (("power_redundancy_ok", "Power Redundancy"),
                                ("fan_redundancy_ok", "Fan Redundancy")):
                if getattr(health, field) is False:
                    degraded = True
                    problems.append(f"{name}: DEGRADED")
            for field, name in (("missing_disk_count", "Missing disks"),
                                ("enclosure_mismatch_count", "Enclosure mismatch")):
                if (getattr(health, field) or 0) > 0:
                    degraded = True
                    problems.append(f"{name}: {getattr(health, field)}")
            if 2 in codes or degraded:
                label, color = "Critical", "danger"
            elif 1 in codes:
                label, color = "Warning", "warning"
            elif (any(code == 0 for code in codes)
                  or health.power_redundancy_ok is True or health.fan_redundancy_ok is True):
                label, color = "OK", "success"
            else:
                label = "Không rõ"
            stale = (timezone.now() - health.timestamp).total_seconds() > 2 * settings.POLL_ILO_INTERVAL_SECS
            if stale and color == "success":
                color = "secondary"
    # Poll iLO gần nhất thất bại (401/timeout/...) — dữ liệu cũ KHÔNG còn đáng tin để báo "OK".
    # Có sự cố thật (warning/danger) trong dữ liệu cũ thì giữ nguyên mức đó, chỉ gắn thêm lỗi.
    error = device.ilo_last_error if device.ilo_ip_address else ""
    if error and color in ("success", "secondary"):
        label, color = "Mất kết nối", "warning"
    return {"label": label, "color": color, "problems": problems, "stale": stale,
            "error": error, "timestamp": health.timestamp if health else None}


def attach_hardware_summaries(devices):
    snapshots = latest_hardware(devices)
    for device in devices:
        device.ilo_summary = hardware_summary(device, snapshots.get(device.pk))
