"""Alert rule engine — đánh giá ngưỡng và tạo Alert record."""
import logging
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from apps.devices.models import Device
from apps.metrics import cache as metrics_cache
from .models import AlertRule, Alert, AlertNotification

logger = logging.getLogger(__name__)

# Metric nhị phân (0/1) — không áp dụng vùng đệm hysteresis.
BINARY_METRICS = {"if_status", "device_online"}
# Metric miễn trừ hysteresis: phục hồi ngay khi điều kiện hết đúng. Ngoài metric
# nhị phân còn có wifi_ap_offline (count, ngưỡng 0 → công thức hysteresis vô nghĩa).
NO_HYSTERESIS_METRICS = BINARY_METRICS | {"wifi_ap_offline"}

CONDITION_FN = {
    "gt":  lambda v, t: v > t,
    "lt":  lambda v, t: v < t,
    "gte": lambda v, t: v >= t,
    "lte": lambda v, t: v <= t,
    "eq":  lambda v, t: v == t,
    "ne":  lambda v, t: v != t,
}

METRIC_GETTERS = {
    "cpu_percent":       lambda device, since: _latest_cpu(device, since),
    "mem_percent":       lambda device, since: _latest_mem(device, since),
    "if_status":         lambda device, since: _check_if_status(device, since),
    "uplink_in_mbps_max":  lambda device, since: _uplink_traffic_max(device, since, direction="in"),
    "uplink_out_mbps_max": lambda device, since: _uplink_traffic_max(device, since, direction="out"),
    "vm_count_running":  lambda device, since: _count_vms_running(device, since),
    "vm_repl_unhealthy": lambda device, since: _count_vms_repl_unhealthy(device, since),
    "device_online":     lambda device, since: _device_online(device),
    "wifi_client_count": lambda device, since: _wifi_client_count(device, since),
    "wifi_ap_offline":   lambda device, since: _wifi_ap_offline_count(device, since),
}

SUSTAINABLE_METRICS = {"cpu_percent", "mem_percent"}

# HyperV host performance counters (Phase 1 MVP) — map metric key → (short-key ring-buffer,
# field name SystemHealth). Không tái dùng _sustained_cpu_mem (hardcode field cpu/mem).
_HOST_PERF_FIELD_MAP = {
    "cpu_hv_percent":        ("chv", "cpu_hv_percent"),
    "mem_available_mb":      ("mav", "mem_available_mb"),
    "disk_read_iops":        ("dri", "disk_read_iops"),
    "disk_write_iops":       ("dwi", "disk_write_iops"),
    "disk_read_latency_ms":  ("drl", "disk_read_latency_ms"),
    "disk_write_latency_ms": ("dwl", "disk_write_latency_ms"),
    "net_mbps_total":        ("nmb", "net_mbps_total"),
    "disk_read_throughput_mbps":  ("drt", "disk_read_throughput_mbps"),
    "disk_write_throughput_mbps": ("dwt", "disk_write_throughput_mbps"),
    "disk_queue_length":          ("dql", "disk_queue_length"),
    "avg_io_size_kb":             ("aio", "avg_io_size_kb"),
}

for _metric in _HOST_PERF_FIELD_MAP:
    METRIC_GETTERS[_metric] = (lambda device, since, m=_metric: _latest_host_perf(device, since, m))
del _metric

# iLO Redfish RAID/disk health (độc lập WinRM, model HardwareHealth riêng — KHÔNG có nhánh
# cache-mode, model này không đi qua METRICS_WRITE_MODE, xem CLAUDE.md "Phạm vi" mục iLO).
_ILO_FIELD_MAP = {
    "raid_controller_health":    "controller_health_code",
    "raid_logical_drive_health": "logical_drive_worst_code",
    "raid_missing_disk_count":   "missing_disk_count",
    "raid_enclosure_mismatch":   "enclosure_mismatch_count",
}

for _metric in _ILO_FIELD_MAP:
    METRIC_GETTERS[_metric] = (lambda device, since, m=_metric: _latest_ilo(device, since, m))
del _metric


# ── Cache-first: khi METRICS_WRITE_MODE="cache", getters đọc từ Redis thay vì DB ──
def _use_cache() -> bool:
    return metrics_cache.is_cache_mode()


def _fresh_latest(device: Device, since) -> dict | None:
    """Snapshot mới nhất từ cache nếu còn trong cửa sổ `since`, ngược lại None.

    Giữ đúng ngữ nghĩa DB (`timestamp__gte=since`): snapshot cũ hơn `since` bị bỏ
    → tránh cảnh báo trên số liệu ôi khi thiết bị ngừng poll.
    """
    snap = metrics_cache.get_latest(device.id)
    if not snap:
        return None
    if since is not None and snap.get("ts", 0) < since.timestamp():
        return None
    return snap


def _sustained_verdict(values: list, rule: AlertRule) -> float | None:
    """Logic sustained dùng chung: điều kiện phải đúng TRÊN TOÀN cửa sổ.

    gt/gte → min(values) vượt ngưỡng; lt/lte → max(values) dưới ngưỡng; eq/ne →
    xét giá trị mới nhất. Trả latest (để dựng message) nếu sustained, else None.
    """
    if not values:
        return None
    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition
    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None
    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _sustained_cpu_mem(device: Device, rule: AlertRule, window_since) -> float | None:
    """Evaluate sustained condition for CPU/MEM over a time window.

    If rule.duration_min > 0, we require the condition to hold for the whole window.
    Returns the latest value (for messaging) if sustained, else None.
    """
    if _use_cache():
        field = "cpu" if rule.metric == "cpu_percent" else "mem"
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [s[field] for s in series if s.get(field) is not None]
        if rule.metric == "mem_percent":
            values = [v for v in values if v]  # bỏ sentinel mem==0
        return _sustained_verdict(values, rule)

    from apps.metrics.models import SystemHealth

    qs = (SystemHealth.objects
          .filter(device=device, timestamp__gte=window_since)
          .order_by("timestamp")
          .values_list(rule.metric, flat=True))
    values = list(qs)
    if rule.metric == "mem_percent":
        # Loại mẫu mem == 0 (sentinel "không đo được") trước khi đánh giá sustained —
        # tránh rule lt/lte fire giả trên thiết bị không expose mem qua SNMP.
        values = [v for v in values if v]
    if not values:
        return None

    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    # For eq/ne, fall back to latest-only.
    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _latest_host_perf(device: Device, since, metric: str) -> float | None:
    """Latest value cho 1 trong 7 HyperV host performance counter (Phase 1 MVP)."""
    short_key, field_name = _HOST_PERF_FIELD_MAP[metric]
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        val = snap.get(field_name)
        return float(val) if val is not None else None

    from apps.metrics.models import SystemHealth
    rec = (SystemHealth.objects
           .filter(device=device, timestamp__gte=since, **{f"{field_name}__isnull": False})
           .order_by("-timestamp")
           .values_list(field_name, flat=True)
           .first())
    return float(rec) if rec is not None else None


def _sustained_host_perf(device: Device, rule: AlertRule, window_since) -> float | None:
    """Sustained version cho 7 HyperV host performance counter — tái dùng _sustained_verdict."""
    short_key, field_name = _HOST_PERF_FIELD_MAP[rule.metric]
    if _use_cache():
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [float(s[short_key]) for s in series if s.get(short_key) is not None]
        return _sustained_verdict(values, rule)

    from apps.metrics.models import SystemHealth
    qs = (SystemHealth.objects
          .filter(device=device, timestamp__gte=window_since, **{f"{field_name}__isnull": False})
          .order_by("timestamp")
          .values_list(field_name, flat=True))
    values = [float(v) for v in qs]
    return _sustained_verdict(values, rule)


def _latest_ilo(device: Device, since, metric: str) -> float | None:
    """Latest value cho iLO RAID/disk health — luôn đọc DB trực tiếp (HardwareHealth không có
    nhánh cache-mode, ghi thẳng Postgres mỗi poll, xem apps/collectors/tasks.py poll_all_ilo)."""
    field_name = _ILO_FIELD_MAP[metric]
    from apps.metrics.models import HardwareHealth
    rec = (HardwareHealth.objects
           .filter(device=device, timestamp__gte=since, **{f"{field_name}__isnull": False})
           .order_by("-timestamp")
           .values_list(field_name, flat=True)
           .first())
    return float(rec) if rec is not None else None


def _sustained_ilo(device: Device, rule: AlertRule, window_since) -> float | None:
    """Sustained version cho iLO RAID/disk health — tái dùng _sustained_verdict. State phần
    cứng rời rạc (không noisy) nên seed rule dùng duration_min=0 (xem seed_alert_rules.py),
    hàm này chỉ giữ nhất quán API nếu ai đổi duration_min>0 qua UI."""
    field_name = _ILO_FIELD_MAP[rule.metric]
    from apps.metrics.models import HardwareHealth
    qs = (HardwareHealth.objects
          .filter(device=device, timestamp__gte=window_since, **{f"{field_name}__isnull": False})
          .order_by("timestamp")
          .values_list(field_name, flat=True))
    values = [float(v) for v in qs]
    return _sustained_verdict(values, rule)


def _latest_cpu(device: Device, since) -> float | None:
    if _use_cache():
        snap = _fresh_latest(device, since)
        return snap.get("cpu") if snap else None
    from apps.metrics.models import SystemHealth
    rec = SystemHealth.objects.filter(device=device, timestamp__gte=since).order_by("-timestamp").first()
    return rec.cpu_percent if rec else None


def _latest_mem(device: Device, since) -> float | None:
    if _use_cache():
        snap = _fresh_latest(device, since)
        # mem == 0 là sentinel "không đo được" → bỏ qua (xem chú thích DB path bên dưới).
        return (snap.get("mem") or None) if snap else None
    from apps.metrics.models import SystemHealth
    rec = SystemHealth.objects.filter(device=device, timestamp__gte=since).order_by("-timestamp").first()
    if rec is None:
        return None
    # mem_percent == 0 là sentinel "không đo được" (Cisco Business/SMB không expose mem
    # qua SNMP, hoặc walk rỗng) — KHÔNG phải mem thật 0% → bỏ qua, tránh rule lt fire giả.
    if not rec.mem_percent:
        return None
    return rec.mem_percent


def _check_if_status(device: Device, since) -> float | None:
    """Trả về 0 nếu có uplink nào DOWN, 1 nếu tất cả UP."""
    from apps.devices.models import Interface

    if _use_cache():
        uplink_ids = list(Interface.objects.filter(device=device, is_uplink=True).values_list("id", flat=True))
        if not uplink_ids:
            return None
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        ifs = snap.get("interfaces") or {}
        for uid in uplink_ids:
            status = (ifs.get(str(uid)) or {}).get("status")
            if status is None:
                return None
            if status != "up":
                return 0.0
        return 1.0

    from apps.metrics.models import InterfaceStats
    from django.db.models import OuterRef, Subquery

    uplinks = Interface.objects.filter(device=device, is_uplink=True)
    if not uplinks.exists():
        return None

    # Annotate latest status per uplink in one query instead of N queries
    latest_sq = (InterfaceStats.objects
                 .filter(interface=OuterRef("pk"), timestamp__gte=since)
                 .order_by("-timestamp")
                 .values("status")[:1])
    for uplink in uplinks.annotate(latest_status=Subquery(latest_sq)):
        if uplink.latest_status is None:
            return None
        if uplink.latest_status != "up":
            return 0.0
    return 1.0


def _sustained_if_status(device: Device, window_since) -> float | None:
    """Sustained version of if_status within a time window.

    Returns 0.0 if ANY uplink had a non-up status within window.
    Returns 1.0 if all uplinks stayed up within window and we have at least one sample per uplink.
    Returns None if no uplinks or not enough data.
    """
    from apps.devices.models import Interface

    from django.conf import settings as _settings
    _min_grace = getattr(_settings, "ALERT_GRACE_PERIOD_SECS", 120)
    grace_secs = max(_min_grace, int(getattr(device, "collect_interval", 300)) * 2)
    min_ts = timezone.now() - timedelta(seconds=grace_secs)

    if _use_cache():
        uplink_ids = list(Interface.objects.filter(device=device, is_uplink=True).values_list("id", flat=True))
        if not uplink_ids:
            return None
        for uid in uplink_ids:
            series = metrics_cache.get_if_series(uid, window_since)
            if not series:
                return None  # thiếu dữ liệu → chưa kết luận
            if any(s.get("status") != "up" for s in series):
                return 0.0
            latest = series[-1]
            if metrics_cache.epoch_to_dt(latest.get("ts", 0)) < min_ts:
                return None  # mẫu mới nhất quá cũ (poll kẹt)
        return 1.0

    from apps.metrics.models import InterfaceStats
    from django.db.models import Exists, OuterRef, Subquery

    uplinks_qs = Interface.objects.filter(device=device, is_uplink=True)
    if not uplinks_qs.exists():
        return None

    # Annotate each uplink with: has non-up in window, latest timestamp, latest status
    nonup_in_window = InterfaceStats.objects.filter(
        interface=OuterRef("pk"), timestamp__gte=window_since
    ).exclude(status="up")
    latest_ts_sq = (InterfaceStats.objects
                    .filter(interface=OuterRef("pk"))
                    .order_by("-timestamp")
                    .values("timestamp")[:1])
    latest_status_sq = (InterfaceStats.objects
                        .filter(interface=OuterRef("pk"))
                        .order_by("-timestamp")
                        .values("status")[:1])

    uplinks = list(uplinks_qs.annotate(
        has_nonup=Exists(nonup_in_window),
        latest_ts=Subquery(latest_ts_sq),
        latest_status=Subquery(latest_status_sq),
    ))

    # If ANY uplink goes non-up within the window -> down (0).
    if any(u.has_nonup for u in uplinks):
        return 0.0

    # Require at least one recent sample per uplink (avoid false "up" when polling is stuck).
    for uplink in uplinks:
        if uplink.latest_ts is None:
            return None
        if uplink.latest_ts < min_ts:
            return None
        if uplink.latest_status != "up":
            return 0.0
    return 1.0


def _uplink_traffic_max(device: Device, since, direction: str) -> float | None:
    """Return max IN/OUT Mbps among uplink interfaces since time."""
    from apps.devices.models import Interface

    uplink_ids = list(Interface.objects.filter(device=device, is_uplink=True).values_list("pk", flat=True))
    if not uplink_ids:
        return None

    field = "in_mbps" if direction == "in" else "out_mbps"

    if _use_cache():
        peak = None
        for uid in uplink_ids:
            for s in metrics_cache.get_if_series(uid, since):
                v = s.get(field)
                if v is not None:
                    peak = float(v) if peak is None else max(peak, float(v))
        return peak

    from apps.metrics.models import InterfaceStats
    qs = (InterfaceStats.objects
          .filter(interface_id__in=uplink_ids, timestamp__gte=since)
          .order_by(f"-{field}")
          .values_list(field, flat=True))
    val = qs.first()
    return float(val) if val is not None else None


def _sustained_uplink_traffic_max(device: Device, rule: AlertRule, window_since) -> float | None:
    """Sustained version of uplink traffic max.

    We compute 'max uplink Mbps' per poll-snapshot timestamp, then require the condition
    to hold for all snapshots in the window.
    """
    from apps.devices.models import Interface

    uplink_ids = list(Interface.objects.filter(device=device, is_uplink=True).values_list("pk", flat=True))
    if not uplink_ids:
        return None

    field = "in_mbps" if rule.metric == "uplink_in_mbps_max" else "out_mbps"

    if _use_cache():
        per_ts: dict = {}
        for uid in uplink_ids:
            for s in metrics_cache.get_if_series(uid, window_since):
                ts = s.get("ts")
                per_ts[ts] = max(per_ts.get(ts, 0.0), float(s.get(field) or 0.0))
        values = [per_ts[ts] for ts in sorted(per_ts)]
        return _sustained_verdict(values, rule)

    from django.db.models import Max
    from apps.metrics.models import InterfaceStats
    # 1 query: gom max(field) theo từng snapshot timestamp (thay vòng lặp N query).
    rows = (InterfaceStats.objects
            .filter(interface_id__in=uplink_ids, timestamp__gte=window_since)
            .values("timestamp")
            .annotate(m=Max(field))
            .order_by("timestamp"))
    values: list[float] = [float(r["m"] or 0.0) for r in rows]
    if not values:
        return None

    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _sustained_vm_metric(device: Device, rule: AlertRule, window_since) -> float | None:
    """Evaluate sustained VM metrics across snapshots in window.

    VMStats are stored per VM with the same poll timestamp. We compute the metric per timestamp snapshot,
    then require the condition to hold for ALL snapshots in the window.
    Returns latest snapshot value (for messaging) if sustained, else None.
    """
    if _use_cache():
        field = "vmr" if rule.metric == "vm_count_running" else "vmu"
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [float(s[field]) for s in series if field in s]
        return _sustained_verdict(values, rule)

    from django.db.models import Count
    from apps.metrics.models import VMStats

    # timestamps present in window (snapshots)
    timestamps = list(
        VMStats.objects.filter(device=device, timestamp__gte=window_since)
        .order_by("timestamp")
        .values_list("timestamp", flat=True)
        .distinct()
    )
    if not timestamps:
        return None

    # 1 query gom nhóm theo timestamp; snapshot không khớp điều kiện → count = 0.
    if rule.metric == "vm_count_running":
        rows = (VMStats.objects
                .filter(device=device, timestamp__gte=window_since, state="Running")
                .values("timestamp").annotate(c=Count("id")))
    elif rule.metric == "vm_repl_unhealthy":
        _HEALTHY = {"Normal", "NotConfigured"}
        rows = (VMStats.objects
                .filter(device=device, timestamp__gte=window_since)
                .exclude(repl_health__in=_HEALTHY)
                .values("timestamp").annotate(c=Count("id")))
    else:
        return None

    counts = {r["timestamp"]: r["c"] for r in rows}
    values: list[float] = [float(counts.get(ts, 0)) for ts in timestamps]
    if not values:
        return None

    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _count_vms_running(device: Device, since) -> float | None:
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        vms = snap.get("vms") or []
        if not vms:  # không có VM ghi nhận → None (đồng nhất DB path)
            return None
        return float(sum(1 for v in vms if v.get("state") == "Running"))
    from apps.metrics.models import VMStats
    latest = (VMStats.objects.filter(device=device, timestamp__gte=since)
              .order_by("-timestamp").values("timestamp").first())
    if not latest:
        return None
    return float(VMStats.objects.filter(
        device=device, timestamp=latest["timestamp"], state="Running"
    ).count())


def _count_vms_repl_unhealthy(device: Device, since) -> float | None:
    _HEALTHY = {"Normal", "NotConfigured"}
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        vms = snap.get("vms") or []
        if not vms:
            return None
        return float(sum(1 for v in vms if (v.get("repl_health") or "") not in _HEALTHY))
    from apps.metrics.models import VMStats
    latest = (VMStats.objects.filter(device=device, timestamp__gte=since)
              .order_by("-timestamp").values("timestamp").first())
    if not latest:
        return None
    return float(VMStats.objects.filter(
        device=device, timestamp=latest["timestamp"]
    ).exclude(repl_health__in=_HEALTHY).count())


def _device_online(device: Device) -> float:
    """1.0 nếu thiết bị online, 0.0 nếu offline — DÙNG CHO CẢNH BÁO.

    Dựa trên `is_online_for_alert` (mốc `last_ok_seen` + grace), KHÔNG dùng `is_online`
    (mốc `last_seen` bị xoá mỗi lần poll trượt). Nhờ đó 1 vòng poll lỗi tạm không bắn
    cảnh báo offline giả; chỉ báo khi mất tín hiệu thật vượt grace.
    """
    return 1.0 if device.is_online_for_alert else 0.0


def _sustained_device_online(device: Device, window_since) -> float | None:
    """Yêu cầu trạng thái offline duy trì trong cửa sổ duration_min.

    Dùng `last_ok_seen` (không bị xoá khi poll trượt) làm mốc, dự phòng `created_at`.
    - Còn trong grace → coi online (1.0).
    - Offline và mốc OK gần nhất đã cũ hơn cửa sổ → xác nhận offline (0.0).
    - Mới rớt, chưa đủ cửa sổ → None (bỏ qua, chờ thêm).
    """
    if device.is_online_for_alert:
        return 1.0
    ref = device.last_ok_seen or device.created_at
    if ref and ref < window_since:
        return 0.0
    return None


def _wifi_client_count_at_ts(device: Device, ts) -> float:
    """Tổng client tại 1 snapshot — fallback sum AP khi không có bảng STA."""
    from django.db.models import Sum
    from apps.metrics.models import WifiClientStats, WifiApStats

    cl_count = WifiClientStats.objects.filter(device=device, timestamp=ts).count()
    if cl_count > 0:
        return float(cl_count)
    total = (
        WifiApStats.objects.filter(device=device, timestamp=ts)
        .aggregate(s=Sum("client_count"))["s"]
    )
    return float(total or 0)


def _wifi_client_count(device: Device, since) -> float | None:
    """Tổng số client WiFi ở snapshot mới nhất của WLAN controller."""
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        clients = snap.get("wifi_clients")
        aps = snap.get("wifi_aps")
        if not clients and not aps:
            return None
        if clients:
            return float(len(clients))
        return float(sum(int(a.get("client_count") or 0) for a in (aps or [])))

    from apps.metrics.models import WifiClientStats, WifiApStats

    latest_ts = (WifiClientStats.objects
                 .filter(device=device, timestamp__gte=since)
                 .order_by("-timestamp")
                 .values_list("timestamp", flat=True)
                 .first())
    if latest_ts is None:
        latest_ts = (WifiApStats.objects
                     .filter(device=device, timestamp__gte=since)
                     .order_by("-timestamp")
                     .values_list("timestamp", flat=True)
                     .first())
    if latest_ts is None:
        return None
    return _wifi_client_count_at_ts(device, latest_ts)


def _sustained_wifi_client_count(device: Device, rule: AlertRule, window_since) -> float | None:
    if _use_cache():
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [float(s["wc"]) for s in series if "wc" in s]
        return _sustained_verdict(values, rule)

    from apps.metrics.models import WifiApStats

    timestamps = list(
        WifiApStats.objects.filter(device=device, timestamp__gte=window_since)
        .order_by("timestamp")
        .values_list("timestamp", flat=True)
        .distinct()
    )
    if not timestamps:
        return None

    values = [_wifi_client_count_at_ts(device, ts) for ts in timestamps]
    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _wifi_ap_offline_at_ts(device: Device, ts) -> float:
    from apps.metrics.models import WifiApStats
    return float(WifiApStats.objects.filter(
        device=device, timestamp=ts, is_online=False,
    ).count())


def _sustained_wifi_ap_offline_count(device: Device, rule: AlertRule, window_since) -> float | None:
    if _use_cache():
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [float(s["wao"]) for s in series if "wao" in s]
        return _sustained_verdict(values, rule)

    from apps.metrics.models import WifiApStats

    timestamps = list(
        WifiApStats.objects.filter(device=device, timestamp__gte=window_since)
        .order_by("timestamp")
        .values_list("timestamp", flat=True)
        .distinct()
    )
    if not timestamps:
        return None

    values = [_wifi_ap_offline_at_ts(device, ts) for ts in timestamps]
    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _wifi_ap_offline_count(device: Device, since) -> float | None:
    """Số AP offline ở snapshot WifiApStats mới nhất của WLAN controller.

    Lọc theo `since` để khi AC mất kết nối (không có snapshot mới) → trả None
    (bỏ qua), tránh báo AP offline giả khi chính AC đang down (AC down đã có
    rule device_online riêng).
    """
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        aps = snap.get("wifi_aps")
        if not aps:  # AC không có AP ghi nhận → None (đồng nhất DB path)
            return None
        return float(sum(1 for a in aps if not a.get("is_online")))

    from apps.metrics.models import WifiApStats
    latest_ts = (WifiApStats.objects
                 .filter(device=device, timestamp__gte=since)
                 .order_by("-timestamp")
                 .values_list("timestamp", flat=True).first())
    if latest_ts is None:
        return None
    return float(WifiApStats.objects.filter(
        device=device, timestamp=latest_ts, is_online=False).count())


def _wifi_offline_ap_names(device: Device) -> list[str]:
    """Tên các AP đang offline ở snapshot WifiApStats mới nhất (cho message cảnh báo)."""
    if _use_cache():
        snap = metrics_cache.get_latest(device.id)
        if not snap:
            return []
        return sorted(
            str(a.get("name") or "")
            for a in (snap.get("wifi_aps") or [])
            if not a.get("is_online")
        )

    from apps.metrics.models import WifiApStats
    latest_ts = (WifiApStats.objects
                 .filter(device=device)
                 .order_by("-timestamp")
                 .values_list("timestamp", flat=True).first())
    if latest_ts is None:
        return []
    return list(WifiApStats.objects
                .filter(device=device, timestamp=latest_ts, is_online=False)
                .order_by("ap_name")
                .values_list("ap_name", flat=True))


def _recovered(rule: AlertRule, value: float) -> bool:
    """True nếu value đã ra khỏi vùng đệm hysteresis (đủ điều kiện phục hồi).

    - Metric nhị phân (if_status) và eq/ne: không có vùng đệm → phục hồi ngay.
    - gt/gte: phục hồi khi value < threshold * (1 - pct).
    - lt/lte: phục hồi khi value > threshold * (1 + pct).
    """
    if rule.metric in NO_HYSTERESIS_METRICS or rule.condition in ("eq", "ne"):
        return True
    pct = float(getattr(settings, "ALERT_HYSTERESIS_PCT", 0.1) or 0)
    t = float(rule.threshold)
    if rule.condition in ("gt", "gte"):
        return value < t * (1 - pct)
    if rule.condition in ("lt", "lte"):
        return value > t * (1 + pct)
    return True


def _decide_transition(rule: AlertRule, value: float, has_active: bool) -> str:
    """Quyết định hành động: 'fire' | 'resolve' | 'hold' | 'none'."""
    cond_fn = CONDITION_FN.get(rule.condition)
    if cond_fn and cond_fn(value, rule.threshold):
        return "fire"
    if not has_active:
        return "none"
    return "resolve" if _recovered(rule, value) else "hold"


def _is_flapping(device: Device, rule: AlertRule) -> bool:
    """True nếu (device, rule) fire quá nhiều lần trong cửa sổ → nên chặn notification spam."""
    window = int(getattr(settings, "ALERT_FLAP_WINDOW_MIN", 30))
    threshold = int(getattr(settings, "ALERT_FLAP_THRESHOLD", 4))
    if threshold <= 0:
        return False
    flap_since = timezone.now() - timedelta(minutes=window)
    recent_fires = Alert.objects.filter(
        device=device, rule=rule, triggered_at__gte=flap_since
    ).count()
    return recent_fires >= threshold


def check_device_alerts(device: Device, since) -> None:
    rules = AlertRule.objects.filter(enabled=True).filter(
        device_type__in=[device.device_type, "all"]
    )
    for rule in rules:
        getter = METRIC_GETTERS.get(rule.metric)
        if not getter:
            continue

        has_active = Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

        # duration_min: if set, require condition to be sustained for the whole window.
        if rule.duration_min and rule.duration_min > 0:
            window_since = timezone.now() - timedelta(minutes=int(rule.duration_min))
            if rule.metric in SUSTAINABLE_METRICS:
                value = _sustained_cpu_mem(device, rule, window_since)
            elif rule.metric == "if_status":
                # if_status semantics: 1 if all uplinks up, 0 if any down
                value = _sustained_if_status(device, window_since)
            elif rule.metric in ("uplink_in_mbps_max", "uplink_out_mbps_max"):
                value = _sustained_uplink_traffic_max(device, rule, window_since)
            elif rule.metric in ("vm_count_running", "vm_repl_unhealthy"):
                value = _sustained_vm_metric(device, rule, window_since)
            elif rule.metric == "device_online":
                value = _sustained_device_online(device, window_since)
            elif rule.metric == "wifi_client_count":
                value = _sustained_wifi_client_count(device, rule, window_since)
            elif rule.metric == "wifi_ap_offline":
                value = _sustained_wifi_ap_offline_count(device, rule, window_since)
            elif rule.metric in _HOST_PERF_FIELD_MAP:
                value = _sustained_host_perf(device, rule, window_since)
            elif rule.metric in _ILO_FIELD_MAP:
                value = _sustained_ilo(device, rule, window_since)
            else:
                value = getter(device, since)

            # ⚠️ 2026-09-28: các hàm `_sustained_*` dùng `_sustained_verdict` (mọi metric TRỪ
            # device_online/if_status — 2 metric đó tự trả 0.0/1.0 rõ ràng) trả về `None` bất
            # cứ khi nào điều kiện KHÔNG còn đúng suốt window — nhưng đó CHÍNH LÀ lúc metric đã
            # hồi phục. Trước đây `value is None` → `continue` thẳng, không bao giờ tới nhánh
            # "resolve" bên dưới → alert đã fire (duration_min>0) KHÔNG BAO GIỜ tự resolve được,
            # kẹt `is_active=True` vĩnh viễn dù metric đã về ngưỡng bình thường từ lâu (verify
            # runtime prod: 11/13 alert active thuộc nhóm rule này đã hồi phục thật từ nhiều
            # ngày/tháng trước nhưng chưa từng resolve — xem memory `alert-sustained-never-resolve.md`).
            # Fix: khi sustained=None mà đang có alert active, fallback đọc giá trị TỨC THỜI
            # (không sustain, đúng hàm `getter` dùng cho nhánh duration_min=0) để xét resolve qua
            # hysteresis — resolve KHÔNG cần sustain (giống cách device_online/if_status đã làm
            # đúng từ đầu: chỉ sustain lúc FIRE để lọc nhiễu, resolve thì tức thời ngay khi có bằng
            # chứng hồi phục mới nhất). Không đổi bán kính "fire" (vẫn phải sustain đủ window).
            if value is None:
                if has_active:
                    instant = getter(device, since)
                    if instant is not None and _decide_transition(rule, instant, has_active) == "resolve":
                        _resolve_alert(device, rule)
                continue
        else:
            value = getter(device, since)

        if value is None:
            continue

        action = _decide_transition(rule, value, has_active)
        if action == "fire":
            _fire_alert(device, rule, value)
        elif action == "resolve":
            _resolve_alert(device, rule)
        # "hold" (trong vùng đệm hysteresis) / "none" (không có alert active): không làm gì


def _persist_incident_snapshot(device: Device) -> None:
    """Cache-mode: ghi 1 SystemHealth từ snapshot Redis làm bằng chứng lúc alert fire.

    Nhờ đó sự cố có điểm dữ liệu CPU/mem trong Postgres (chart/điều tra sau này) dù
    metrics thường xuyên không còn ghi raw. Bỏ qua nếu đã có row cùng timestamp.
    """
    if not _use_cache():
        return
    snap = metrics_cache.get_latest(device.id)
    if not snap or snap.get("ts") is None:
        return
    from apps.metrics.models import SystemHealth, VolumeStats
    try:
        ts = metrics_cache.epoch_to_dt(snap["ts"])
        if not SystemHealth.objects.filter(device=device, timestamp=ts).exists():
            SystemHealth.objects.create(
                device=device,
                timestamp=ts,
                cpu_percent=snap.get("cpu") or 0,
                mem_percent=snap.get("mem") or 0,
                uptime_secs=snap.get("uptime"),
                extra=snap.get("extra") or {},
                cpu_hv_percent=snap.get("cpu_hv_percent"),
                mem_available_mb=snap.get("mem_available_mb"),
                disk_read_iops=snap.get("disk_read_iops"),
                disk_write_iops=snap.get("disk_write_iops"),
                disk_read_latency_ms=snap.get("disk_read_latency_ms"),
                disk_write_latency_ms=snap.get("disk_write_latency_ms"),
                net_mbps_total=snap.get("net_mbps_total"),
                disk_read_throughput_mbps=snap.get("disk_read_throughput_mbps"),
                disk_write_throughput_mbps=snap.get("disk_write_throughput_mbps"),
                disk_queue_length=snap.get("disk_queue_length"),
                avg_io_size_kb=snap.get("avg_io_size_kb"),
            )
        # Per-volume evidence (nếu host có volumes) — bằng chứng cho "VM nào bị ảnh hưởng"
        # tại đúng thời điểm alert fire. Bỏ qua nếu đã có row cùng timestamp.
        volumes = snap.get("volumes") or []
        if volumes and not VolumeStats.objects.filter(device=device, timestamp=ts).exists():
            VolumeStats.objects.bulk_create([
                VolumeStats(
                    device=device,
                    timestamp=ts,
                    volume_name=str(vol.get("name") or "")[:100],
                    read_iops=vol.get("read_iops"),
                    write_iops=vol.get("write_iops"),
                    read_mbps=vol.get("read_mbps"),
                    write_mbps=vol.get("write_mbps"),
                    read_latency_ms=vol.get("read_latency_ms"),
                    write_latency_ms=vol.get("write_latency_ms"),
                    queue_length=vol.get("queue_length"),
                    avg_io_size_kb=vol.get("avg_io_size_kb"),
                    current_queue_length=vol.get("current_queue_length"),
                    transfers_per_sec=vol.get("transfers_per_sec"),
                    split_io_per_sec=vol.get("split_io_per_sec"),
                    idle_time_percent=vol.get("idle_time_percent"),
                    vm_names=vol.get("vm_names") or [],
                )
                for vol in volumes
            ])
    except Exception as exc:
        logger.warning("persist incident snapshot (dev=%s) failed: %s", device.name, exc)


def _fire_alert(device: Device, rule: AlertRule, value: float) -> None:
    def _fmt_metric(metric: str, v: float) -> str:
        if metric in ("cpu_percent", "mem_percent"):
            return f"{v:.1f}%"
        if metric in ("uplink_in_mbps_max", "uplink_out_mbps_max"):
            return f"{v:.3f} Mbps"
        if metric in ("vm_count_running", "vm_repl_unhealthy", "wifi_client_count"):
            return f"{v:.0f}"
        if metric == "wifi_ap_offline":
            return f"{v:.0f} AP"
        if metric == "if_status":
            return "DOWN" if v == 0 else "UP"
        if metric == "device_online":
            return "OFFLINE" if v == 0 else "ONLINE"
        if metric == "cpu_hv_percent":
            return f"{v:.1f}%"
        if metric == "mem_available_mb":
            return f"{v:.0f} MB"
        if metric in ("disk_read_iops", "disk_write_iops"):
            return f"{v:.0f} IOPS"
        if metric in ("disk_read_latency_ms", "disk_write_latency_ms"):
            return f"{v:.1f} ms"
        if metric == "net_mbps_total":
            return f"{v:.1f} Mbps"
        if metric in ("disk_read_throughput_mbps", "disk_write_throughput_mbps"):
            return f"{v:.1f} MB/s"
        if metric == "disk_queue_length":
            return f"{v:.2f}"
        if metric == "avg_io_size_kb":
            return f"{v:.1f} KB"
        return f"{v:.2f}"

    metric_value_str = _fmt_metric(rule.metric, float(value))
    threshold_str = _fmt_metric(rule.metric, float(rule.threshold))

    if rule.metric == "wifi_ap_offline":
        names = _wifi_offline_ap_names(device)
        suffix = f" ({', '.join(names)})" if names else ""
        message = f"{device.name}: {metric_value_str} offline{suffix}"
    else:
        message = (f"{device.name}: {rule.metric} = {metric_value_str} "
                   f"(ngưỡng {rule.condition} {threshold_str})")

    # ⚠️ Transactional-outbox-lite (2026-09-28, theo báo cáo review — cùng root cause với
    # _resolve_alert bên dưới nên sửa đối xứng cả 2): ghi Alert + "ý định gửi" (AlertNotification
    # status="pending") CÙNG 1 transaction. Nếu worker chết NGAY SAU khi commit (trước khi kịp
    # gọi _dispatch_notifications thật bên dưới) thì DB vẫn có bằng chứng "còn nợ gửi" (row
    # pending) để task retry_pending_alert_notifications (định kỳ) nhặt lại — trước đây không
    # có bằng chứng nào, và vòng eval kế tiếp coi alert đã is_active=True là "đã xử lý xong" nên
    # không bao giờ tự gửi lại → mất trắng thông báo. is_flapping tính TRONG transaction (cùng
    # connection nên vẫn thấy `alert` vừa create dù chưa commit — read-committed) để quyết định
    # có tạo pending row hay không (giữ nguyên hành vi cũ: flapping thì không notification nào).
    with transaction.atomic():
        Device.objects.select_for_update().filter(pk=device.pk).first()
        if Alert.objects.filter(device=device, rule=rule, is_active=True).exists():
            return
        alert = Alert.objects.create(
            device=device,
            rule=rule,
            severity=rule.severity,
            message=message,
            metric_value=float(value),
            is_active=True,
        )
        is_flapping = _is_flapping(device, rule)
        if not is_flapping:
            AlertNotification.objects.bulk_create([
                AlertNotification(alert=alert, channel=ch, kind="fire", status="pending")
                for ch in rule.channels
            ])
    # Cache-mode: lưu bằng chứng CPU/mem vào Postgres cho sự cố này.
    _persist_incident_snapshot(device)
    if is_flapping:
        logger.warning("ALERT flapping — bỏ qua notification: %s", alert.message)
    else:
        _dispatch_notifications(alert, rule.channels, kind="fire")
    logger.warning("ALERT fired: %s", alert.message)


def _resolve_alert(device: Device, rule: AlertRule) -> None:
    # ⚠️ Alert được eval từ CẢ inline sau mỗi poll (_poll_device_once) LẪN periodic
    # evaluate_alert_rules (safety net) — 2 đường có thể chạy gần như đồng thời trên các
    # worker Celery khác nhau (--concurrency=4). Bản cũ đọc alerts_to_resolve (không lock)
    # → gửi RECOVERED → mới update is_active=False: 2 lời gọi trùng thời điểm cùng đọc thấy
    # is_active=True, cùng gửi Telegram/email RECOVERED trùng lặp. Fix: "claim" atomically
    # (lock Device + update is_active=False TRƯỚC, cùng pattern select_for_update đã dùng ở
    # _fire_alert) rồi MỚI gửi notification ngoài transaction — lệnh gọi thứ 2 tới sau sẽ
    # thấy is_active đã False (đã commit) → alerts_to_resolve rỗng → tự return, không gửi lại.
    with transaction.atomic():
        Device.objects.select_for_update().filter(pk=device.pk).first()
        alerts_to_resolve = list(
            Alert.objects.select_for_update().filter(device=device, rule=rule, is_active=True)
        )
        if not alerts_to_resolve:
            return
        resolved_at = timezone.now()
        Alert.objects.filter(pk__in=[a.pk for a in alerts_to_resolve]).update(
            is_active=False,
            resolved_at=resolved_at,
        )
        # Transactional-outbox-lite — cùng transaction với claim is_active=False ở trên (xem
        # comment đầy đủ ở _fire_alert). Chỉ tạo pending cho (alert, channel) nào ĐÃ có fire
        # notification "sent" — giữ nguyên logic chống dội RECOVERED cho alert bị flapping-
        # suppress lúc fire.
        # ⚠️ 2026-09-29 (review vòng 2): quyết định recovery PHẢI theo TỪNG (alert, channel), KHÔNG
        # theo alert — bản trước chỉ cần 1 channel đã "sent" là tạo recovery cho MỌI channel của
        # rule, kể cả channel khác còn chưa gửi fire (vd SMTP treo) → channel đó nhận RECOVERED
        # TRƯỚC khi từng nhận ALERT. `sent_fire_channels` là set (alert_id, channel).
        sent_fire_channels = set(
            AlertNotification.objects.filter(
                alert__in=alerts_to_resolve, kind="fire", status="sent"
            ).values_list("alert_id", "channel")
        )
        pending_rows = [
            AlertNotification(alert=alert, channel=ch, kind="recovery", status="pending")
            for alert in alerts_to_resolve
            for ch in rule.channels
            if (alert.pk, ch) in sent_fire_channels
        ]
        if pending_rows:
            AlertNotification.objects.bulk_create(pending_rows)

    for alert in alerts_to_resolve:
        # Gán resolved_at lên object TRƯỚC khi gửi để tin RECOVERED có ngày giờ
        # (trước đây gửi trước update → alert.resolved_at=None → hiện "N/A").
        alert.resolved_at = resolved_at
        channels_to_notify = [ch for ch in rule.channels if (alert.pk, ch) in sent_fire_channels]
        if channels_to_notify:
            _dispatch_notifications(alert, channels_to_notify, kind="recovery")
    logger.info("ALERT resolved: %s — %s", device.name, rule.name)


def _queue_late_recovery_if_resolved(alert_id: int, channel: str) -> None:
    """Gọi ngay sau khi 1 fire notification của `channel` vừa chuyển "sent" thật — LUÔN từ bên
    trong `_finalize_fire_sent` (transaction đã khoá Device), KHÔNG gọi trực tiếp nơi khác trừ
    nhánh fallback "chưa từng có outbox row" trong `_dispatch_notifications` (gọi ngoài luồng
    chuẩn, vd test) — xem 2 hàm đó.

    ⚠️ 2026-09-29 (theo báo cáo review): `_resolve_alert` chỉ queue recovery cho (alert, channel)
    có fire "sent" TẠI THỜI ĐIỂM resolve chạy — nếu fire còn "pending"/"processing" lúc đó (worker
    chết giữa lúc _fire_alert commit outbox và lúc kịp dispatch), resolve bỏ qua VĨNH VIỄN (quyết
    định chỉ đưa ra 1 lần, không có gì kích hoạt lại). Sau đó `retry_pending_alert_notifications`
    vẫn gửi fire trễ đó thành công → người nhận thấy "sự cố mới" dù nó đã hồi phục từ trước, và
    KHÔNG BAO GIỜ nhận được RECOVERED theo sau. Fix: coi "recovery còn nợ" là điều kiện được
    re-check tại CẢ 2 nơi có thể làm nó đúng — _resolve_alert (khi resolve chạy sau) VÀ đây (khi
    fire cuối cùng cũng gửi xong sau khi resolve đã chạy trước) — bên nào xảy ra SAU sẽ là bên
    phát hiện + tạo pending recovery.

    ⚠️ 2026-09-29 (review vòng 2 — 2 khoảng hở còn sót ở bản đầu):
    (a) Bản đầu gọi hàm này bằng 1 lệnh DB RIÊNG, sau khi đã COMMIT "sent" ở 1 lệnh khác — worker
        chết đúng giữa 2 lệnh làm mất VĨNH VIỄN cơ hội tạo recovery (fire đã "sent" — trạng thái
        cuối, không còn gì kích hoạt lại được). Fix: `_finalize_fire_sent` gộp finalize "sent" +
        gọi hàm này vào CHUNG 1 `transaction.atomic()` — crash giữa chừng thì TOÀN BỘ rollback
        (row vẫn "processing" như cũ), sweep sau reclaim và làm lại nguyên vẹn cả 2 bước.
    (b) Đọc `is_active=False` ở đây và đọc `sent_fire_channels` trong `_resolve_alert` là 2 lệnh
        SELECT độc lập không cùng khoá gì — fire chuyển "sent" đúng lúc nằm GIỮA lúc _resolve_alert
        đọc xong danh sách "đã gửi" và lúc nó COMMIT is_active=False: cả 2 bên đều đọc phải giá
        trị "cũ" của phía kia → KHÔNG bên nào tạo recovery. Fix: `_finalize_fire_sent` khoá
        `Device` (`select_for_update()`) giống hệt `_fire_alert`/`_resolve_alert` TRƯỚC KHI đọc
        `is_active` ở đây — 2 giao dịch cạnh tranh cùng device luôn serialize (Postgres, KHÔNG
        phải SQLite — SQLite bỏ qua select_for_update, xem test), không còn khoảng hở giữa đọc và
        commit của bên kia.

    An toàn không cần lock RIÊNG bên trong hàm này (lock đã có ở caller `_finalize_fire_sent`): 1
    fire-notification row chỉ chuyển "sent" ĐÚNG 1 LẦN (claim token chặn double-send cùng row)
    nên helper này chỉ có thể tự kích hoạt đúng 1 lần cho mỗi (alert, channel) — vẫn giữ check
    `.exists()` để an toàn kép, tránh tạo trùng nếu `_resolve_alert` đã lỡ tạo sẵn.
    """
    try:
        alert = Alert.objects.select_related("rule").get(pk=alert_id, is_active=False)
    except Alert.DoesNotExist:
        return  # Alert chưa resolve (hoặc đã bị xoá) — chưa có gì "nợ", nhánh resolve lo sau.
    if AlertNotification.objects.filter(alert=alert, channel=channel, kind="recovery").exists():
        return
    AlertNotification.objects.create(alert=alert, channel=channel, kind="recovery", status="pending")
    logger.info(
        "Recovery queued trễ (fire [%s] gửi xong SAU khi alert đã resolve): %s — %s",
        channel, alert.device.name, alert.rule.name,
    )


def _finalize_fire_sent(alert: Alert, channel: str, token: str) -> bool:
    """Chuyển 1 AlertNotification(kind="fire") sang "sent" + (nếu alert đã resolve) queue
    recovery pending cho ĐÚNG channel đó — trong CÙNG 1 transaction, khoá `Device` giống hệt
    `_fire_alert`/`_resolve_alert`. Xem 2 bẫy (a)/(b) ở docstring `_queue_late_recovery_if_resolved`
    — hàm này là chỗ sửa cho cả 2.

    Trả về True nếu claim_token khớp (finalize thành công); False nếu đã bị reclaim (1 tiến trình
    khác đã/đang xử lý row này — caller KHÔNG được tạo row mới thay thế, không ghi đè).
    """
    with transaction.atomic():
        Device.objects.select_for_update().filter(pk=alert.device_id).first()
        updated = AlertNotification.objects.filter(
            alert=alert, channel=channel, kind="fire", status="processing", claim_token=token,
        ).update(status="sent", updated_at=timezone.now())
        if not updated:
            return False
        _queue_late_recovery_if_resolved(alert.pk, channel)
    return True


def _send_channel_message(kind: str, channel: str, alert: Alert) -> None:
    """Gọi thẳng hàm gửi thật theo (kind, channel) — không tự ghi AlertNotification, raise nếu
    lỗi (caller _dispatch_notifications bắt exception để cập nhật status)."""
    if kind == "recovery":
        if channel == "email":
            from .channels.email_channel import send_email_recovery
            send_email_recovery(alert)
        elif channel == "telegram":
            from .channels.telegram import send_telegram_recovery
            send_telegram_recovery(alert)
        elif channel == "slack":
            from .channels.webhook import send_slack_recovery
            send_slack_recovery(alert)
        elif channel == "teams":
            from .channels.webhook import send_teams_recovery
            send_teams_recovery(alert)
    else:
        if channel == "email":
            from .channels.email_channel import send_email_alert
            send_email_alert(alert)
        elif channel == "telegram":
            from .channels.telegram import send_telegram_alert
            send_telegram_alert(alert)
        elif channel == "slack":
            from .channels.webhook import send_slack_alert
            send_slack_alert(alert)
        elif channel == "teams":
            from .channels.webhook import send_teams_alert
            send_teams_alert(alert)


def _dispatch_notifications(alert: Alert, channels: list[str], kind: str) -> None:
    """Gửi notification thật cho từng channel.

    ⚠️ 2026-09-28 (theo báo cáo review — "2 retry task có thể gửi trùng"): TRƯỚC khi gửi phải
    claim nguyên tử row AlertNotification(status="pending") đã ghi sẵn trong transaction lúc
    fire/resolve (transactional-outbox-lite) sang "processing" bằng UPDATE có điều kiện
    (`.filter(status="pending").update(...)` — chỉ 1 caller nhận được số dòng >0, caller khác
    tới sau nhận 0 và tự bỏ qua). Không chỉ 2 lần gọi retry_pending_alert_notifications() chồng
    nhau mới cần claim — chính lệnh gọi GỐC này (từ _fire_alert/_resolve_alert) cũng có thể bị
    sweep định kỳ tranh mất cùng 1 row nếu gửi CHẬM hơn grace_secs (90s mặc định của sweep).
    Không thấy row pending nào (gọi ngoài luồng chuẩn, vd test gọi thẳng hàm) → vẫn gửi + tự tạo
    row mới, giữ hàm hoạt động độc lập.

    ⚠️ 2026-09-29 (theo báo cáo review tiếp theo — "gửi trùng khi SMTP treo quá
    stale_processing_secs"): claim ở trên chỉ chặn 2 lệnh UPDATE THEO ĐÚNG NGHĨA ĐEN chạy đồng
    thời trên CÙNG row (compare-and-swap DB). Nó KHÔNG chặn được trường hợp: lệnh gọi NÀY claim
    xong rồi treo thật sự lâu (channel `email` dùng Django `send_mail`, trước đây KHÔNG set
    `EMAIL_TIMEOUT` nên smtplib có thể treo vô thời hạn) > `stale_processing_secs` (300s), trong
    lúc đó sweep coi row "kẹt" và RECLAIM (ghi `claim_token` mới) rồi tự gửi+finalize xong TRƯỚC —
    khi lệnh gọi NÀY cuối cùng cũng gửi xong (dù thành công hay lỗi), nó không biết mình đã mất
    quyền sở hữu row, dễ ghi đè `status` mà sweep vừa set. Fix 2 lớp: (1) `EMAIL_TIMEOUT` (settings,
    config/settings/base.py) bound thời gian gửi email dưới `stale_processing_secs` thật sự — chặn
    gốc rễ; (2) `claim_token` ngẫu nhiên ghi lúc claim, finalize PHẢI match đúng token đó — nếu ai
    đó đã reclaim (token đổi), UPDATE finalize match 0 dòng → tự bỏ qua, KHÔNG tạo row mới đè
    (khác best-effort cũ luôn tạo row mới khi update=0). Lớp (2) là phòng thủ thứ 2 cho các nguyên
    nhân trễ khác ngoài SMTP (GC pause, network buffering...) mà (1) không bound hết được.

    ⚠️ 2026-09-29 (review vòng 2): finalize riêng cho `kind=="fire"` giờ đi qua
    `_finalize_fire_sent` — gộp "chuyển sent" + "queue recovery trễ nếu alert đã resolve" vào
    CHUNG 1 transaction có khoá `Device`, đóng 2 khoảng hở (crash giữa 2 bước; race đọc/ghi
    `is_active` với `_resolve_alert`) — xem docstring đầy đủ ở `_finalize_fire_sent`/
    `_queue_late_recovery_if_resolved`. `kind=="recovery"` không cần lock/late-check (không có gì
    phụ thuộc trạng thái sau khi recovery gửi xong) nên vẫn finalize đơn giản như cũ.
    """
    for channel in channels:
        token = uuid.uuid4().hex
        claimed = AlertNotification.objects.filter(
            alert=alert, channel=channel, kind=kind, status="pending"
        ).update(status="processing", updated_at=timezone.now(), claim_token=token)
        if not claimed and AlertNotification.objects.filter(
            alert=alert, channel=channel, kind=kind
        ).exists():
            # Có row nhưng KHÔNG claim được — 1 tiến trình khác (sweep, hoặc lời gọi khác) đã
            # hoặc đang xử lý rồi. Không gửi trùng.
            continue
        try:
            _send_channel_message(kind, channel, alert)
        except Exception as exc:
            updated = AlertNotification.objects.filter(
                alert=alert, channel=channel, kind=kind, status="processing", claim_token=token,
            ).update(status="failed", error=str(exc), updated_at=timezone.now())
            if not updated and not claimed:
                AlertNotification.objects.create(
                    alert=alert, channel=channel, kind=kind, status="failed", error=str(exc)
                )
            elif not updated:
                logger.warning(
                    "%s notification [%s] bị reclaim trước khi ghi lỗi xong — bỏ qua, tiến "
                    "trình reclaim chịu trách nhiệm ghi kết quả cuối", kind, channel,
                )
            logger.error("%s notification failed [%s]: %s", kind, channel, exc)
            continue

        if kind == "fire":
            if claimed:
                if not _finalize_fire_sent(alert, channel, token):
                    logger.warning(
                        "%s notification [%s] bị reclaim trước khi gửi xong (gửi chậm hơn "
                        "stale_processing_secs) — bỏ qua cập nhật, tiến trình reclaim chịu "
                        "trách nhiệm ghi kết quả cuối, không ghi đè", kind, channel,
                    )
            else:
                # Chưa từng có outbox row (gọi ngoài luồng chuẩn, vd test) — không có gì để
                # claim/khoá theo pattern chuẩn, tạo thẳng row "sent" rồi vẫn check late-recovery.
                AlertNotification.objects.create(alert=alert, channel=channel, kind=kind, status="sent")
                _queue_late_recovery_if_resolved(alert.pk, channel)
        else:
            updated = AlertNotification.objects.filter(
                alert=alert, channel=channel, kind=kind, status="processing", claim_token=token,
            ).update(status="sent", updated_at=timezone.now())
            if not updated:
                if claimed:
                    logger.warning(
                        "%s notification [%s] bị reclaim trước khi gửi xong (gửi chậm hơn "
                        "stale_processing_secs) — bỏ qua cập nhật, tiến trình reclaim chịu "
                        "trách nhiệm ghi kết quả cuối, không ghi đè", kind, channel,
                    )
                    continue
                AlertNotification.objects.create(alert=alert, channel=channel, kind=kind, status="sent")


def retry_pending_alert_notifications(grace_secs: int = 90, stale_processing_secs: int = 300) -> int:
    """Quét + claim nguyên tử rồi gửi lại AlertNotification bị "kẹt" — 2 nhóm:
    (1) status="pending" cũ hơn `grace_secs` — worker chết giữa lúc commit trạng thái Alert và
        lúc kịp claim/gửi (transactional-outbox-lite, xem _fire_alert/_resolve_alert/
        _dispatch_notifications).
    (2) status="processing" cũ hơn `stale_processing_secs` — ĐÃ có 1 tiến trình claim (kể cả
        chính hàm này ở lần gọi trước) nhưng chết giữa chừng, không kịp chuyển sent/failed →
        "thu hồi" row bị bỏ rơi. `stale_processing_secs` (300s, dài hơn hẳn `grace_secs`) vì
        1 tiến trình ĐANG THẬT SỰ gửi (không chết) có thể hợp lệ mất vài chục giây (email
        không timeout — xem comment ở _dispatch_notifications) nên không được vội thu hồi.

    ⚠️ Claim bằng UPDATE có điều kiện, re-check `stale_cutoff` NGAY TRONG WHERE của chính câu
    UPDATE claim (không chỉ ở bước chọn candidate_ids) — đây là phần mấu chốt chặn "2 retry task
    chạy chồng nhau (task trước kéo dài quá chu kỳ beat 120s, nhiều beat, gọi tay...) cùng gửi
    trùng 1 row" mà review chỉ ra: candidate_ids có thể chứa cùng 1 pk ở cả 2 lần gọi, nhưng
    UPDATE claim dùng `stale_cutoff` CỐ ĐỊNH (tính 1 lần trước vòng lặp) — lệnh UPDATE nào tới
    DB trước sẽ set `updated_at=NOW()` (rất mới) và COMMIT; lệnh UPDATE tới sau (dù đã bị
    Postgres tự khoá row chờ lệnh trước commit) re-evaluate WHERE trên dữ liệu MỚI COMMIT đó —
    `updated_at` giờ không còn `< stale_cutoff` nữa (stale_cutoff là mốc cũ, "vừa claim xong"
    luôn mới hơn) → khớp 0 dòng → tự bỏ qua, không gửi trùng. Trả về số row đã claim + xử lý
    (gửi hoặc đánh failed). Gọi từ task Celery định kỳ (apps/alerts/tasks.py) — KHÔNG lock
    Device vì đây là xử lý bù trên chính bảng AlertNotification, không cạnh tranh trực tiếp với
    fire/resolve của cùng device tại đúng thời điểm này.

    ⚠️ 2026-09-29: claim ở trên chỉ chặn 2 lệnh UPDATE claim tranh nhau — KHÔNG chặn được việc
    hàm này reclaim 1 row mà chủ cũ (lệnh gọi gốc `_dispatch_notifications`, hoặc chính hàm này ở
    lần gọi trước) thực ra vẫn đang gửi hợp lệ (chỉ là chậm hơn `stale_processing_secs`, vd SMTP
    không timeout trước fix `EMAIL_TIMEOUT`) chứ chưa chết hẳn — cả 2 phía khi đó đều gọi
    `_send_channel_message` thật (gửi trùng không tránh được nếu đã xảy ra), nhưng finalize phải
    dùng `claim_token` để đảm bảo chỉ chủ MỚI NHẤT (người reclaim) được ghi kết quả cuối — chủ cũ
    finalize trễ hơn sẽ thấy `claim_token` không khớp (đã bị ghi đè lúc reclaim) → tự bỏ qua,
    không ghi đè lại kết quả đúng. Xem comment đầy đủ ở `_dispatch_notifications`.
    """
    now_ = timezone.now()
    pending_cutoff = now_ - timedelta(seconds=grace_secs)
    stale_cutoff = now_ - timedelta(seconds=stale_processing_secs)

    candidate_ids = list(
        AlertNotification.objects.filter(
            Q(status="pending", sent_at__lt=pending_cutoff)
            | Q(status="processing", updated_at__lt=stale_cutoff)
        ).values_list("pk", flat=True)
    )

    processed = 0
    for pk in candidate_ids:
        token = uuid.uuid4().hex
        claimed = AlertNotification.objects.filter(
            Q(pk=pk) & (
                Q(status="pending")
                | Q(status="processing", updated_at__lt=stale_cutoff)
            )
        ).update(status="processing", updated_at=timezone.now(), claim_token=token)
        if not claimed:
            continue  # bị 1 tiến trình khác claim mất giữa lúc chọn candidate và lúc tới lượt
        n = AlertNotification.objects.select_related("alert").get(pk=pk)
        try:
            _send_channel_message(n.kind, n.channel, n.alert)
        except Exception as exc:
            updated = AlertNotification.objects.filter(
                pk=pk, status="processing", claim_token=token
            ).update(status="failed", error=str(exc), updated_at=timezone.now())
            processed += 1
            if updated:
                logger.error("Retry %s notification failed [%s]: %s", n.kind, n.channel, exc)
            else:
                logger.warning(
                    "Retry %s notification [%s] bị reclaim trong lúc gửi (chủ mới đã ghi kết "
                    "quả) — bỏ qua ghi lỗi, không ghi đè", n.kind, n.channel,
                )
            continue

        if n.kind == "fire":
            # _finalize_fire_sent gộp "chuyển sent" + "queue recovery trễ nếu đã resolve" vào
            # chung 1 transaction có khoá Device — xem docstring đầy đủ ở đó (review vòng 2).
            finalized = _finalize_fire_sent(n.alert, n.channel, token)
            processed += 1
            if not finalized:
                logger.warning(
                    "Retry %s notification [%s] bị reclaim trước khi gửi xong — bỏ qua cập "
                    "nhật, chủ mới chịu trách nhiệm ghi kết quả cuối", n.kind, n.channel,
                )
            continue

        updated = AlertNotification.objects.filter(
            pk=pk, status="processing", claim_token=token
        ).update(status="sent", updated_at=timezone.now())
        processed += 1
        if not updated:
            logger.warning(
                "Retry %s notification [%s] bị reclaim trước khi gửi xong — bỏ qua cập nhật, "
                "chủ mới chịu trách nhiệm ghi kết quả cuối", n.kind, n.channel,
            )
    if processed:
        logger.warning("Retried %d pending/stuck alert notification(s)", processed)
    return processed
