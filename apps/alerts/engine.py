"""Alert rule engine — đánh giá ngưỡng và tạo Alert record."""
import logging
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from apps.devices.models import Device
from .models import AlertRule, Alert, AlertNotification

logger = logging.getLogger(__name__)

# Metric nhị phân (0/1) — không áp dụng vùng đệm hysteresis.
BINARY_METRICS = {"if_status", "device_online"}

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
}

SUSTAINABLE_METRICS = {"cpu_percent", "mem_percent"}


def _sustained_cpu_mem(device: Device, rule: AlertRule, window_since) -> float | None:
    """Evaluate sustained condition for CPU/MEM over a time window.

    If rule.duration_min > 0, we require the condition to hold for the whole window.
    Returns the latest value (for messaging) if sustained, else None.
    """
    from apps.metrics.models import SystemHealth

    qs = (SystemHealth.objects
          .filter(device=device, timestamp__gte=window_since)
          .order_by("timestamp")
          .values_list(rule.metric, flat=True))
    values = list(qs)
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
    from apps.metrics.models import SystemHealth
    rec = SystemHealth.objects.filter(device=device, timestamp__gte=since).order_by("-timestamp").first()
    return rec.cpu_percent if rec else None


def _latest_mem(device: Device, since) -> float | None:
    from apps.metrics.models import SystemHealth
    rec = SystemHealth.objects.filter(device=device, timestamp__gte=since).order_by("-timestamp").first()
    return rec.mem_percent if rec else None


def _check_if_status(device: Device, since) -> float | None:
    """Trả về 0 nếu có uplink nào DOWN, 1 nếu tất cả UP."""
    from apps.metrics.models import InterfaceStats
    from apps.devices.models import Interface
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
        if uplink.latest_status is not None and uplink.latest_status != "up":
            return 0.0
    return 1.0


def _sustained_if_status(device: Device, window_since) -> float | None:
    """Sustained version of if_status within a time window.

    Returns 0.0 if ANY uplink had a non-up status within window.
    Returns 1.0 if all uplinks stayed up within window and we have at least one sample per uplink.
    Returns None if no uplinks or not enough data.
    """
    from apps.metrics.models import InterfaceStats
    from apps.devices.models import Interface
    from django.db.models import Exists, OuterRef, Subquery

    uplinks_qs = Interface.objects.filter(device=device, is_uplink=True)
    if not uplinks_qs.exists():
        return None

    from django.conf import settings as _settings
    _min_grace = getattr(_settings, "ALERT_GRACE_PERIOD_SECS", 120)
    grace_secs = max(_min_grace, int(getattr(device, "collect_interval", 300)) * 2)
    min_ts = timezone.now() - timedelta(seconds=grace_secs)

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
    from apps.metrics.models import InterfaceStats
    from apps.devices.models import Interface

    uplink_ids = list(Interface.objects.filter(device=device, is_uplink=True).values_list("pk", flat=True))
    if not uplink_ids:
        return None

    field = "in_mbps" if direction == "in" else "out_mbps"
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
    from django.db.models import Max
    from apps.metrics.models import InterfaceStats
    from apps.devices.models import Interface

    uplink_ids = list(Interface.objects.filter(device=device, is_uplink=True).values_list("pk", flat=True))
    if not uplink_ids:
        return None

    field = "in_mbps" if rule.metric == "uplink_in_mbps_max" else "out_mbps"
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
    from apps.metrics.models import SystemHealth

    qs = (SystemHealth.objects
          .filter(device=device, timestamp__gte=window_since, extra__session_count__isnull=False)
          .order_by("timestamp")
          .values_list("extra__session_count", flat=True))
    values = [float(v) for v in qs if v is not None]
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
    from apps.metrics.models import VMStats
    latest = (VMStats.objects.filter(device=device, timestamp__gte=since)
              .order_by("-timestamp").values("timestamp").first())
    if not latest:
        return None
    return float(VMStats.objects.filter(
        device=device, timestamp=latest["timestamp"], state="Running"
    ).count())


def _count_vms_repl_unhealthy(device: Device, since) -> float | None:
    from apps.metrics.models import VMStats
    _HEALTHY = {"Normal", "NotConfigured"}
    latest = (VMStats.objects.filter(device=device, timestamp__gte=since)
              .order_by("-timestamp").values("timestamp").first())
    if not latest:
        return None
    return float(VMStats.objects.filter(
        device=device, timestamp=latest["timestamp"]
    ).exclude(repl_health__in=_HEALTHY).count())


def _device_online(device: Device) -> float:
    """1.0 nếu thiết bị online (theo last_seen + grace), 0.0 nếu offline.

    Dùng chung công thức grace với Device.is_online để dashboard và alert nhất quán.
    Phù hợp cho AP (protocol=ping) — phát hiện AP offline.
    """
    return 1.0 if device.is_online else 0.0


def _wifi_client_count(device: Device, since) -> float | None:
    """Tổng số client WiFi ở snapshot mới nhất của WLAN controller."""
    from apps.metrics.models import WifiClientStats
    latest_ts = (WifiClientStats.objects
                 .filter(device=device, timestamp__gte=since)
                 .order_by("-timestamp")
                 .values_list("timestamp", flat=True)
                 .first())
    if latest_ts is None:
        return None
    return float(WifiClientStats.objects.filter(device=device, timestamp=latest_ts).count())


def _recovered(rule: AlertRule, value: float) -> bool:
    """True nếu value đã ra khỏi vùng đệm hysteresis (đủ điều kiện phục hồi).

    - Metric nhị phân (if_status) và eq/ne: không có vùng đệm → phục hồi ngay.
    - gt/gte: phục hồi khi value < threshold * (1 - pct).
    - lt/lte: phục hồi khi value > threshold * (1 + pct).
    """
    if rule.metric in BINARY_METRICS or rule.condition in ("eq", "ne"):
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


def _fire_alert(device: Device, rule: AlertRule, value: float) -> None:
    # Deduplication: không tạo lại nếu alert đang active
    existing = Alert.objects.filter(device=device, rule=rule, is_active=True).first()
    if existing:
        return  # đã có alert, không gửi lại

    def _fmt_metric(metric: str, v: float) -> str:
        if metric in ("cpu_percent", "mem_percent"):
            return f"{v:.1f}%"
        if metric in ("uplink_in_mbps_max", "uplink_out_mbps_max"):
            return f"{v:.3f} Mbps"
        if metric == "fw_session_count":
            return f"{v:.0f}"
        if metric in ("vm_count_running", "vm_repl_unhealthy", "wifi_client_count"):
            return f"{v:.0f}"
        if metric == "if_status":
            return "DOWN" if v == 0 else "UP"
        if metric == "device_online":
            return "OFFLINE" if v == 0 else "ONLINE"
        return f"{v:.2f}"

    metric_value_str = _fmt_metric(rule.metric, float(value))
    threshold_str = _fmt_metric(rule.metric, float(rule.threshold))

    alert = Alert.objects.create(
        device=device,
        rule=rule,
        severity=rule.severity,
        message=f"{device.name}: {rule.metric} = {metric_value_str} (ngưỡng {rule.condition} {threshold_str})",
        metric_value=float(value),
        is_active=True,
    )
    if _is_flapping(device, rule):
        logger.warning("ALERT flapping — bỏ qua notification: %s", alert.message)
    else:
        _send_notifications(alert, rule.channels)
    logger.warning("ALERT fired: %s", alert.message)


def _resolve_alert(device: Device, rule: AlertRule) -> None:
    alerts_to_resolve = list(Alert.objects.filter(device=device, rule=rule, is_active=True))
    if not alerts_to_resolve:
        return
    for alert in alerts_to_resolve:
        _send_recovery_notifications(alert, rule.channels)
    Alert.objects.filter(pk__in=[a.pk for a in alerts_to_resolve]).update(
        is_active=False,
        resolved_at=timezone.now(),
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
