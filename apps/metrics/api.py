"""API endpoints cho metrics — tự động chọn raw/hourly/daily theo time range."""
import math
from collections import defaultdict
from django.conf import settings
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import OuterRef, Subquery
from datetime import datetime, timedelta
from .models import (
    SystemHealth, InterfaceStats,
    SystemHealthHourly, SystemHealthDaily,
    InterfaceStatsHourly, InterfaceStatsDaily,
    WifiApStats, WifiClientStats,
)
from apps.devices.models import Device, Interface


# Mapping range string → (timedelta, data_source)
# data_source: "raw" | "hourly" | "daily"
RANGE_CONFIG = {
    "1h":  (timedelta(hours=1),  "raw"),
    "6h":  (timedelta(hours=6),  "raw"),
    "24h": (timedelta(hours=24), "raw"),
    "7d":  (timedelta(days=7),   "hourly"),
    "30d": (timedelta(days=30),  "daily"),
    "90d": (timedelta(days=90),  "daily"),
}


def _parse_range(range_str: str) -> tuple[timedelta, str]:
    """Parse range string → (timedelta, data_source)."""
    delta, source = RANGE_CONFIG.get(range_str, (timedelta(hours=1), "raw"))
    return delta, source


def _parse_local(s: str) -> datetime | None:
    """Parse chuỗi datetime-local (vd '2026-06-26T08:00') → aware datetime theo tz hiện hành."""
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _select_source(since: datetime, until: datetime) -> str:
    """Chọn nguồn dữ liệu theo độ tuổi + độ rộng khoảng (raw chỉ giữ ~48h)."""
    now = timezone.now()
    age = now - since
    span = until - since
    if age <= timedelta(hours=48) and span <= timedelta(hours=48):
        return "raw"
    if span <= timedelta(days=14):
        return "hourly"
    return "daily"


def _resolve_window(request) -> tuple[datetime, datetime, str]:
    """Trả về (since, until, source) từ ?from/?to (datetime-local) hoặc ?range preset."""
    frm = request.GET.get("from")
    to = request.GET.get("to")
    if frm and to:
        since = _parse_local(frm)
        until = _parse_local(to)
        if since and until and since < until:
            return since, until, _select_source(since, until)
    delta, source = _parse_range(request.GET.get("range", "1h"))
    now = timezone.now()
    return now - delta, now, source


def _downsample(rows: list, max_points: int | None = None) -> list:
    """Lấy mẫu thưa list (đã sort theo thời gian) xuống ≤ max_points, giữ điểm cuối.

    Giảm tải Chart.js khi collect_interval ngắn (vd 60s → 1440 điểm/24h).
    """
    if max_points is None:
        max_points = int(getattr(settings, "CHART_MAX_POINTS", 500))
    n = len(rows)
    if max_points <= 0 or n <= max_points:
        return rows
    step = math.ceil(n / max_points)
    sampled = rows[::step]
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return sampled


def _format_timestamp(ts, source: str, span: timedelta | None = None) -> str:
    """Format timestamp label tùy theo data source (và độ rộng khoảng với raw)."""
    if source == "daily":
        return ts.strftime("%d/%m")
    if source == "hourly":
        return ts.strftime("%d %H:00")
    if span is not None and span > timedelta(hours=24):
        return ts.strftime("%d %H:%M")
    return ts.strftime("%H:%M")


def _timeline_step_seconds(source: str, device: Device) -> int:
    if source == "daily":
        return 86400
    if source == "hourly":
        return 3600
    return max(int(getattr(device, "collect_interval", 300) or 300), 60)


@login_required
def device_metrics(request, device_id: int):
    """Trả về CPU/Memory time-series cho Chart.js.

    Tự động chọn nguồn dữ liệu:
    - range ≤ 24h: raw data (SystemHealth)
    - range 7d: hourly aggregated (SystemHealthHourly)
    - range ≥ 30d: daily aggregated (SystemHealthDaily)

    If SystemHealth.extra contains vendor-specific fields (e.g. Fortinet session_count),
    we include them when present so UI can optionally chart them.
    """
    device = get_object_or_404(Device, pk=device_id)
    since, until, source = _resolve_window(request)

    if source == "daily":
        return _device_metrics_daily(device, since, until)
    if source == "hourly":
        return _device_metrics_hourly(device, since, until)
    return _device_metrics_raw(device, since, until)


@login_required
def device_status_timeline(request, device_id: int) -> JsonResponse:
    """Trả về timeline Online/Offline dạng 1/0 cho biểu đồ trạng thái."""
    device = get_object_or_404(Device, pk=device_id)
    since, until, source = _resolve_window(request)
    span = until - since
    step_secs = _timeline_step_seconds(source, device)
    grace_secs = max(
        int(getattr(settings, "ALERT_GRACE_PERIOD_SECS", 120)),
        int(getattr(device, "collect_interval", 300) or 300) * 3,
    )

    # Include previous sample before the window to infer initial state.
    sample_qs = (SystemHealth.objects
                 .filter(device=device, timestamp__lte=until, timestamp__gte=since - timedelta(seconds=grace_secs))
                 .order_by("timestamp")
                 .values_list("timestamp", flat=True))
    samples = list(sample_qs)

    labels: list[str] = []
    online_series: list[int] = []
    idx = 0
    last_seen = None
    cursor = since
    while cursor <= until:
        while idx < len(samples) and samples[idx] <= cursor:
            last_seen = samples[idx]
            idx += 1
        online = int(bool(last_seen and (cursor - last_seen).total_seconds() <= grace_secs))
        labels.append(_format_timestamp(cursor, source, span))
        online_series.append(online)
        cursor += timedelta(seconds=step_secs)

    return JsonResponse({
        "labels": labels,
        "online": online_series,
        "source": source,
        "grace_secs": grace_secs,
    })


def _device_metrics_raw(device: Device, since, until) -> JsonResponse:
    """Query raw SystemHealth data."""
    qs = (SystemHealth.objects
          .filter(device=device, timestamp__gte=since, timestamp__lte=until)
          .order_by("timestamp")
          .values("timestamp", "cpu_percent", "mem_percent", "extra"))

    rows = _downsample(list(qs))
    span = until - since
    data = {
        "labels":      [_format_timestamp(r["timestamp"], "raw", span) for r in rows],
        "cpu_percent": [r["cpu_percent"] for r in rows],
        "mem_percent": [r["mem_percent"] for r in rows],
        "source":      "raw",
    }
    # Optional vendor metric: Fortinet session count stored in JSON extra
    _attach_session_count(data, rows)
    return JsonResponse(data)


def _device_metrics_hourly(device: Device, since, until) -> JsonResponse:
    """Query hourly aggregated data — avg + max."""
    qs = (SystemHealthHourly.objects
          .filter(device=device, hour__gte=since, hour__lte=until)
          .order_by("hour")
          .values("hour", "cpu_avg", "cpu_max", "mem_avg", "mem_max"))

    rows = list(qs)
    data = {
        "labels":      [r["hour"].strftime("%d %H:00") for r in rows],
        "cpu_percent": [r["cpu_avg"] for r in rows],
        "cpu_max":     [r["cpu_max"] for r in rows],
        "mem_percent": [r["mem_avg"] for r in rows],
        "mem_max":     [r["mem_max"] for r in rows],
        "source":      "hourly",
    }
    return JsonResponse(data)


def _device_metrics_daily(device: Device, since, until) -> JsonResponse:
    """Query daily aggregated data — avg + max."""
    qs = (SystemHealthDaily.objects
          .filter(device=device, day__gte=since.date(), day__lte=until.date())
          .order_by("day")
          .values("day", "cpu_avg", "cpu_max", "mem_avg", "mem_max"))

    rows = list(qs)
    data = {
        "labels":      [r["day"].strftime("%d/%m") for r in rows],
        "cpu_percent": [r["cpu_avg"] for r in rows],
        "cpu_max":     [r["cpu_max"] for r in rows],
        "mem_percent": [r["mem_avg"] for r in rows],
        "mem_max":     [r["mem_max"] for r in rows],
        "source":      "daily",
    }
    return JsonResponse(data)


def _attach_session_count(data: dict, rows: list[dict]) -> None:
    """Gắn Fortinet session_count series vào data nếu có."""
    session_series = []
    has_any_session = False
    for r in rows:
        extra = r.get("extra") or {}
        val = extra.get("session_count")
        if val is None:
            session_series.append(None)
        else:
            has_any_session = True
            try:
                session_series.append(float(val))
            except (TypeError, ValueError):
                session_series.append(None)
    if has_any_session:
        data["session_count"] = session_series


@login_required
def interface_metrics(request, device_id: int):
    """Trả về traffic theo interface cho Chart.js.

    Tự động chọn nguồn dữ liệu:
    - range ≤ 24h: raw data (InterfaceStats)
    - range 7d: hourly aggregated (InterfaceStatsHourly)
    - range ≥ 30d: daily aggregated (InterfaceStatsDaily)
    """
    device = get_object_or_404(Device, pk=device_id)
    since, until, source = _resolve_window(request)
    port = request.GET.get("port")

    iface_qs = Interface.objects.filter(device=device)
    if port:
        iface_qs = iface_qs.filter(name=port)

    if source == "daily":
        return _interface_metrics_daily(iface_qs, since, until)
    if source == "hourly":
        return _interface_metrics_hourly(iface_qs, since, until)
    return _interface_metrics_raw(iface_qs, since, until)


def _interface_metrics_raw(iface_qs, since, until) -> JsonResponse:
    """Query raw InterfaceStats — bulk fetch, then group in Python."""
    ifaces = list(iface_qs)
    iface_ids = [i.id for i in ifaces]
    span = until - since

    all_stats = (InterfaceStats.objects
                 .filter(interface_id__in=iface_ids, timestamp__gte=since, timestamp__lte=until)
                 .order_by("interface_id", "timestamp")
                 .values("interface_id", "timestamp", "status", "in_mbps", "out_mbps"))
    stats_by_iface: dict[int, list] = defaultdict(list)
    for row in all_stats:
        stats_by_iface[row["interface_id"]].append(row)

    results = []
    for iface in ifaces:
        stats = _downsample(stats_by_iface[iface.id])
        results.append({
            "port":           iface.name,
            "is_uplink":      iface.is_uplink,
            "labels":         [_format_timestamp(r["timestamp"], "raw", span) for r in stats],
            "in_mbps":        [r["in_mbps"] for r in stats],
            "out_mbps":       [r["out_mbps"] for r in stats],
            "current_status": stats[-1]["status"] if stats else "unknown",
        })
    return JsonResponse({"interfaces": results, "source": "raw"})


def _interface_metrics_hourly(iface_qs, since, until) -> JsonResponse:
    """Query hourly aggregated InterfaceStats — bulk fetch, then group in Python."""
    latest_raw_sq = (InterfaceStats.objects
                     .filter(interface=OuterRef("pk"))
                     .order_by("-timestamp")
                     .values("status")[:1])
    ifaces = list(iface_qs.annotate(latest_raw_status=Subquery(latest_raw_sq)))
    iface_ids = [i.id for i in ifaces]

    all_hourly = (InterfaceStatsHourly.objects
                  .filter(interface_id__in=iface_ids, hour__gte=since, hour__lte=until)
                  .order_by("interface_id", "hour")
                  .values("interface_id", "hour", "in_mbps_avg", "in_mbps_max",
                          "out_mbps_avg", "out_mbps_max"))
    hourly_by_iface: dict[int, list] = defaultdict(list)
    for row in all_hourly:
        hourly_by_iface[row["interface_id"]].append(row)

    results = []
    for iface in ifaces:
        stats = hourly_by_iface[iface.id]
        results.append({
            "port":           iface.name,
            "is_uplink":      iface.is_uplink,
            "labels":         [r["hour"].strftime("%d %H:00") for r in stats],
            "in_mbps":        [r["in_mbps_avg"] for r in stats],
            "in_mbps_max":    [r["in_mbps_max"] for r in stats],
            "out_mbps":       [r["out_mbps_avg"] for r in stats],
            "out_mbps_max":   [r["out_mbps_max"] for r in stats],
            "current_status": iface.latest_raw_status or "unknown",
        })
    return JsonResponse({"interfaces": results, "source": "hourly"})


@login_required
def wifi_metrics(request, device_id: int) -> JsonResponse:
    """Trả về snapshot WiFi mới nhất của 1 WLAN controller cho UI.

    Gồm: danh sách AP (online/offline + số client) và danh sách client đang kết nối.
    Lọc tùy chọn theo ?ap=<ap_name> và ?ssid=<ssid>.
    """
    device = get_object_or_404(Device, pk=device_id)
    ap_filter = (request.GET.get("ap") or "").strip()
    ssid_filter = (request.GET.get("ssid") or "").strip()

    # AP snapshot mới nhất.
    latest_ap_ts = (WifiApStats.objects
                    .filter(device=device)
                    .order_by("-timestamp")
                    .values_list("timestamp", flat=True)
                    .first())
    aps = []
    if latest_ap_ts:
        ap_qs = WifiApStats.objects.filter(device=device, timestamp=latest_ap_ts)
        if ap_filter:
            ap_qs = ap_qs.filter(ap_name=ap_filter)
        aps = [
            {
                "ap_name": a.ap_name,
                "ap_mac": a.ap_mac,
                "ap_ip": a.ap_ip,
                "ap_group": a.ap_group,
                "is_online": a.is_online,
                "run_state": a.run_state,
                "client_count": a.client_count,
            }
            for a in ap_qs.order_by("ap_name")
        ]

    # Client snapshot mới nhất.
    latest_cl_ts = (WifiClientStats.objects
                    .filter(device=device)
                    .order_by("-timestamp")
                    .values_list("timestamp", flat=True)
                    .first())
    clients = []
    if latest_cl_ts:
        cl_qs = WifiClientStats.objects.filter(device=device, timestamp=latest_cl_ts)
        if ap_filter:
            cl_qs = cl_qs.filter(ap_name=ap_filter)
        if ssid_filter:
            cl_qs = cl_qs.filter(ssid=ssid_filter)
        clients = [
            {
                "mac": c.mac,
                "ip": c.ip,
                "ssid": c.ssid,
                "ap_name": c.ap_name,
                "radio": c.radio,
                "rssi": c.rssi,
                "online_secs": c.online_secs,
            }
            for c in cl_qs.order_by("ap_name", "mac")
        ]

    ap_online = sum(1 for a in aps if a["is_online"])
    # AC6508 không liệt kê từng client qua SNMP — tổng client = tổng client_count
    # trên các AP. Dùng len(clients) nếu sau này có bảng station thật.
    client_total = len(clients) if clients else sum(a["client_count"] for a in aps)
    return JsonResponse({
        "ap_total": len(aps),
        "ap_online": ap_online,
        "ap_offline": len(aps) - ap_online,
        "client_total": client_total,
        "aps": aps,
        "clients": clients,
        "ap_updated": latest_ap_ts.strftime("%d/%m %H:%M") if latest_ap_ts else None,
        "client_updated": latest_cl_ts.strftime("%d/%m %H:%M") if latest_cl_ts else None,
    })


def _interface_metrics_daily(iface_qs, since, until) -> JsonResponse:
    """Query daily aggregated InterfaceStats — bulk fetch, then group in Python."""
    latest_raw_sq = (InterfaceStats.objects
                     .filter(interface=OuterRef("pk"))
                     .order_by("-timestamp")
                     .values("status")[:1])
    ifaces = list(iface_qs.annotate(latest_raw_status=Subquery(latest_raw_sq)))
    iface_ids = [i.id for i in ifaces]

    all_daily = (InterfaceStatsDaily.objects
                 .filter(interface_id__in=iface_ids, day__gte=since.date(), day__lte=until.date())
                 .order_by("interface_id", "day")
                 .values("interface_id", "day", "in_mbps_avg", "in_mbps_max",
                         "out_mbps_avg", "out_mbps_max"))
    daily_by_iface: dict[int, list] = defaultdict(list)
    for row in all_daily:
        daily_by_iface[row["interface_id"]].append(row)

    results = []
    for iface in ifaces:
        stats = daily_by_iface[iface.id]
        results.append({
            "port":           iface.name,
            "is_uplink":      iface.is_uplink,
            "labels":         [r["day"].strftime("%d/%m") for r in stats],
            "in_mbps":        [r["in_mbps_avg"] for r in stats],
            "in_mbps_max":    [r["in_mbps_max"] for r in stats],
            "out_mbps":       [r["out_mbps_avg"] for r in stats],
            "out_mbps_max":   [r["out_mbps_max"] for r in stats],
            "current_status": iface.latest_raw_status or "unknown",
        })
    return JsonResponse({"interfaces": results, "source": "daily"})
