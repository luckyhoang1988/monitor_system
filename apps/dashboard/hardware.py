"""Latest iLO snapshots and their presentation, independent of host connectivity."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Max, Q
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


REDUNDANCY_FIELDS = (("power_redundancy_ok", "Power Redundancy"), ("fan_redundancy_ok", "Fan Redundancy"))
COUNT_FIELDS = (("missing_disk_count", "Missing disks"), ("enclosure_mismatch_count", "Enclosure mismatch"))
FIELD_LABELS = {**HEALTH_FIELDS, **dict(REDUNDANCY_FIELDS), **dict(COUNT_FIELDS)}
# Poll một phần lưu field None cho nhóm không đọc được; trong cửa sổ này mục đó dùng lại giá trị
# đọc được gần nhất (giống alert engine chọn "latest non-null") thay vì coi là đã hồi phục.
CARRY_WINDOW = timedelta(hours=24)


def latest_hardware(devices):
    """Snapshot mới nhất mỗi host, đã gộp giá trị đọc được gần nhất cho field bị thiếu.

    Nguồn DUY NHẤT cho dashboard lẫn trang chi tiết (đừng đọc HardwareHealth mới nhất trực tiếp).
    Field None ở snapshot mới nhất (poll một phần) được xử lý theo bằng chứng trong lịch sử:
    - từng đọc được trong CARRY_WINDOW → mang giá trị cũ (không lưu DB), `snapshot.carried[field]`
      = thời điểm của giá trị đó;
    - từng đọc được nhưng đã quá CARRY_WINDOW → KHÔNG xác nhận khoẻ: `snapshot.expired[field]`
      = thời điểm đọc được lần cuối, hardware_summary báo "Chưa xác định";
    - chưa từng đọc được (vd BIOS/Network trên iLO4) → bỏ qua, không phải thiếu dữ liệu.
    Truy vấn: 1 query cửa sổ CARRY_WINDOW (+ 1 query rẻ cho mỗi host không có snapshot nào trong
    cửa sổ) + 1 query bằng chứng cũ (chỉ khi còn field None chưa giải quyết).
    """
    ids = list(dict.fromkeys(d.pk for d in devices if d.ilo_ip_address))
    if not ids:
        return {}
    since = timezone.now() - CARRY_WINDOW
    # Chỉ lọc theo cửa sổ thời gian (dùng index device_id+timestamp). Bản cũ gộp thêm
    # `OR pk = (subquery snapshot mới nhất của host)` vào cùng WHERE → Postgres chạy lại subquery
    # cho MỌI dòng nằm NGOÀI cửa sổ (đo 2026-10-01: 25.920 dòng → 484 ms, 103.680 dòng → 2,1 s,
    # tăng tuyến tính theo lịch sử, trong khi query cửa sổ riêng ~17 ms không đổi).
    rows = (HardwareHealth.objects
            .filter(device_id__in=ids, timestamp__gte=since)
            .defer("raw")
            .order_by("device_id", "-timestamp", "-pk"))
    history: dict[int, list] = {}
    for row in rows:
        history.setdefault(row.device_id, []).append(row)
    # Host có snapshot trong cửa sổ thì snapshot mới nhất của nó chắc chắn nằm trong cửa sổ. Host
    # không có (iLO ngừng poll > CARRY_WINDOW, hoặc chưa từng có dữ liệu) → lấy riêng snapshot mới
    # nhất, mỗi host 1 query đọc ngược index (device_id, timestamp DESC) rồi dừng ở dòng đầu.
    for device_id in ids:
        if device_id not in history:
            latest = (HardwareHealth.objects.filter(device_id=device_id).defer("raw")
                      .order_by("-timestamp", "-pk").first())
            if latest is not None:
                history[device_id] = [latest]
    snapshots, unresolved = {}, {}
    for device_id, snaps in history.items():
        head = snaps[0]
        head.carried, head.expired = {}, {}
        for field in FIELD_LABELS:
            if getattr(head, field) is not None:
                continue
            for older in snaps[1:]:
                if getattr(older, field) is not None:
                    setattr(head, field, getattr(older, field))
                    head.carried[field] = older.timestamp
                    break
            else:
                unresolved.setdefault(device_id, []).append(field)
        snapshots[device_id] = head
    if unresolved:
        fields = sorted({f for fs in unresolved.values() for f in fs})
        last_known = (HardwareHealth.objects.filter(device_id__in=list(unresolved))
                      .values("device_id")
                      .annotate(**{f"last_{f}": Max("timestamp", filter=Q(**{f"{f}__isnull": False}))
                                   for f in fields}))
        for rec in last_known:
            for field in unresolved[rec["device_id"]]:
                if rec[f"last_{field}"] is not None:
                    snapshots[rec["device_id"]].expired[field] = rec[f"last_{field}"]
    return snapshots


def hardware_summary(device, health):
    label, color, problems = "Chưa cấu hình", "secondary", []
    stale = False
    carried, expired = {}, {}
    if device.ilo_ip_address:
        label = "Chưa có dữ liệu"
        if health:
            codes = [getattr(health, field) for field in HEALTH_FIELDS]
            for field, name in HEALTH_FIELDS.items():
                code = getattr(health, field)
                if code in (1, 2):
                    problems.append(f"{name}: {'Critical' if code == 2 else 'Warning'}")
            degraded = False
            for field, name in REDUNDANCY_FIELDS:
                if getattr(health, field) is False:
                    degraded = True
                    problems.append(f"{name}: DEGRADED")
            for field, name in COUNT_FIELDS:
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
            carried = {FIELD_LABELS[f]: ts for f, ts in getattr(health, "carried", {}).items()}
            expired = {FIELD_LABELS[f]: ts for f, ts in getattr(health, "expired", {}).items()}
            if expired and color in ("success", "secondary"):
                # Từng đọc được nhưng quá cửa sổ giữ → không có bằng chứng khoẻ, KHÔNG báo OK.
                label, color = "Chưa xác định", "warning"
            stale = (timezone.now() - health.timestamp).total_seconds() > 2 * settings.POLL_ILO_INTERVAL_SECS
            if stale and color == "success":
                color = "secondary"
    # Poll iLO gần nhất thất bại (401/timeout/...) — dữ liệu cũ KHÔNG còn đáng tin để báo "OK".
    # Có sự cố thật (warning/danger) trong dữ liệu cũ thì giữ nguyên mức đó, chỉ gắn thêm lỗi.
    error = device.ilo_last_error if device.ilo_ip_address else ""
    if error and color in ("success", "secondary"):
        label, color = "Mất kết nối", "warning"
    return {"label": label, "color": color, "problems": problems, "stale": stale,
            "carried": carried, "expired": expired, "error": error, "timestamp": health.timestamp if health else None}


def attach_hardware_summaries(devices):
    snapshots = latest_hardware(devices)
    for device in devices:
        device.ilo_summary = hardware_summary(device, snapshots.get(device.pk))
