"""Alert rule engine — đánh giá ngưỡng và tạo Alert record."""
import logging
from datetime import timedelta
from django.conf import settings
from django.db import transaction
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
    "fw_session_count":    lambda device, since: _fw_session_count(device, since),
    "vm_count_running":  lambda device, since: _count_vms_running(device, since),
    "vm_repl_unhealthy": lambda device, since: _count_vms_repl_unhealthy(device, since),
    "device_online":     lambda device, since: _device_online(device),
    "wifi_client_count": lambda device, since: _wifi_client_count(device, since),
    "wifi_ap_offline":   lambda device, since: _wifi_ap_offline_count(device, since),
}

SUSTAINABLE_METRICS = {"cpu_percent", "mem_percent"}


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


def _fw_session_count(device: Device, since) -> float | None:
    """Latest firewall session count from SystemHealth.extra.session_count."""
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        sc = (snap.get("extra") or {}).get("session_count")
        try:
            return float(sc) if sc is not None else None
        except (TypeError, ValueError):
            return None
    from apps.metrics.models import SystemHealth
    rec = (SystemHealth.objects
           .filter(device=device, timestamp__gte=since, extra__session_count__isnull=False)
           .order_by("-timestamp")
           .values_list("extra__session_count", flat=True)
           .first())
    if rec is None:
        return None
    try:
        return float(rec)
    except (TypeError, ValueError):
        return None


def _sustained_fw_session_count(device: Device, rule: AlertRule, window_since) -> float | None:
    if _use_cache():
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [float(s["sc"]) for s in series if s.get("sc") is not None]
        return _sustained_verdict(values, rule)

    from apps.metrics.models import SystemHealth

    qs = (SystemHealth.objects
          .filter(device=device, timestamp__gte=window_since, extra__session_count__isnull=False)
          .order_by("timestamp")
          .values_list("extra__session_count", flat=True))
    values = [float(v) for v in qs if v is not None]
    return _sustained_verdict(values, rule)


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
            elif rule.metric == "fw_session_count":
                value = _sustained_fw_session_count(device, rule, window_since)
            elif rule.metric in ("vm_count_running", "vm_repl_unhealthy"):
                value = _sustained_vm_metric(device, rule, window_since)
            elif rule.metric == "device_online":
                value = _sustained_device_online(device, window_since)
            elif rule.metric == "wifi_client_count":
                value = _sustained_wifi_client_count(device, rule, window_since)
            elif rule.metric == "wifi_ap_offline":
                value = _sustained_wifi_ap_offline_count(device, rule, window_since)
            else:
                value = getter(device, since)
        else:
            value = getter(device, since)

        if value is None:
            continue

        has_active = Alert.objects.filter(device=device, rule=rule, is_active=True).exists()
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
    from apps.metrics.models import SystemHealth
    try:
        ts = metrics_cache.epoch_to_dt(snap["ts"])
        if SystemHealth.objects.filter(device=device, timestamp=ts).exists():
            return
        SystemHealth.objects.create(
            device=device,
            timestamp=ts,
            cpu_percent=snap.get("cpu") or 0,
            mem_percent=snap.get("mem") or 0,
            uptime_secs=snap.get("uptime"),
            extra=snap.get("extra") or {},
        )
    except Exception as exc:
        logger.warning("persist incident snapshot (dev=%s) failed: %s", device.name, exc)


def _fire_alert(device: Device, rule: AlertRule, value: float) -> None:
    def _fmt_metric(metric: str, v: float) -> str:
        if metric in ("cpu_percent", "mem_percent"):
            return f"{v:.1f}%"
        if metric in ("uplink_in_mbps_max", "uplink_out_mbps_max"):
            return f"{v:.3f} Mbps"
        if metric == "fw_session_count":
            return f"{v:.0f}"
        if metric in ("vm_count_running", "vm_repl_unhealthy", "wifi_client_count"):
            return f"{v:.0f}"
        if metric == "wifi_ap_offline":
            return f"{v:.0f} AP"
        if metric == "if_status":
            return "DOWN" if v == 0 else "UP"
        if metric == "device_online":
            return "OFFLINE" if v == 0 else "ONLINE"
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
    # Cache-mode: lưu bằng chứng CPU/mem vào Postgres cho sự cố này.
    _persist_incident_snapshot(device)
    if _is_flapping(device, rule):
        logger.warning("ALERT flapping — bỏ qua notification: %s", alert.message)
    else:
        _send_notifications(alert, rule.channels)
    logger.warning("ALERT fired: %s", alert.message)


def _resolve_alert(device: Device, rule: AlertRule) -> None:
    alerts_to_resolve = list(Alert.objects.filter(device=device, rule=rule, is_active=True))
    if not alerts_to_resolve:
        return
    resolved_at = timezone.now()
    for alert in alerts_to_resolve:
        # Gán resolved_at lên object TRƯỚC khi gửi để tin RECOVERED có ngày giờ
        # (trước đây gửi trước update → alert.resolved_at=None → hiện "N/A").
        alert.resolved_at = resolved_at
        # Chỉ gửi RECOVERED nếu fire của alert này ĐÃ TỪNG gửi thành công. Nếu fire
        # bị flapping-suppress (không có notification "sent") thì im lặng resolve →
        # tránh dội ✅ RECOVERED cho cảnh báo giả/flapping mà người dùng chưa từng thấy.
        if AlertNotification.objects.filter(alert=alert, status="sent").exists():
            _send_recovery_notifications(alert, rule.channels)
    Alert.objects.filter(pk__in=[a.pk for a in alerts_to_resolve]).update(
        is_active=False,
        resolved_at=resolved_at,
    )
    logger.info("ALERT resolved: %s — %s", device.name, rule.name)


def _send_recovery_notifications(alert: Alert, channels: list[str]) -> None:
    for channel in channels:
        try:
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
            AlertNotification.objects.create(alert=alert, channel=channel, status="sent")
        except Exception as exc:
            AlertNotification.objects.create(alert=alert, channel=channel,
                                              status="failed", error=str(exc))
            logger.error("Recovery notification failed [%s]: %s", channel, exc)


def _send_notifications(alert: Alert, channels: list[str]) -> None:
    for channel in channels:
        try:
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
            AlertNotification.objects.create(alert=alert, channel=channel, status="sent")
        except Exception as exc:
            AlertNotification.objects.create(alert=alert, channel=channel,
                                              status="failed", error=str(exc))
            logger.error("Notification failed [%s]: %s", channel, exc)
