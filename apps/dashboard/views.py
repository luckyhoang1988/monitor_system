from collections import defaultdict
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.cache import never_cache
from django.http import JsonResponse
from django.utils import timezone
from apps.devices.models import Device
from apps.alerts.models import Alert


def health_check(request):
    """Lightweight health check — no auth required. Used by Docker/load balancer."""
    db_ok = False
    redis_ok = False
    try:
        from django.db import connection
        connection.ensure_connection()
        db_ok = True
    except Exception:
        pass
    try:
        import redis
        from django.conf import settings as _s
        r = redis.from_url(_s.CELERY_BROKER_URL, socket_connect_timeout=2)
        redis_ok = r.ping()
    except Exception:
        pass
    status = "ok" if (db_ok and redis_ok) else "degraded"
    return JsonResponse({"status": status, "db": db_ok, "redis": redis_ok},
                        status=200 if status == "ok" else 503)


GROUP_LABELS = {
    "switch": "Switch",
    "router": "Router",
    "firewall": "Firewall",
    "nas": "NAS",
    "hyperv": "HyperV",
    "wlan_controller": "WLAN Controller",
    "ap": "Access Point",
}

DEVICE_TYPE_META = [
    ("switch", "Switch", "bi-hdd-network", "text-primary", "#2563eb"),
    ("router", "Router", "bi-router", "text-warning", "#d97706"),
    ("firewall", "Firewall", "bi-shield-fill-check", "text-danger", "#dc2626"),
    ("nas", "NAS", "bi-hdd-stack", "text-success", "#16a34a"),
    ("hyperv", "HyperV Host", "bi-server", "text-info", "#0891b2"),
    ("wlan_controller", "WLAN AC", "bi-broadcast-pin", "text-primary", "#6366f1"),
    ("ap", "Access Point", "bi-wifi", "text-success", "#0d9488"),
]


def _dashboard_counts(all_devices):
    """device_type_stats + offline_count + active_alerts — dùng CHUNG cho index()
    (render) và alerts_summary() (AJAX cập nhật realtime, không reload). Để 1 nguồn
    sự thật, tránh lệch số liệu giữa lần render và lần cập nhật.
    """
    from apps.metrics.models import WifiApStats
    by_type: dict[str, list] = defaultdict(list)
    for d in all_devices:
        by_type[d.device_type].append(d)

    # AP không đăng ký như Device — nằm trong WifiApStats dưới AC. Gộp snapshot mới
    # nhất của từng wlan_controller để card "Access Point" phản ánh AP thật.
    ap_total = ap_online = 0
    offline_ap_rows = []
    for ac in by_type["wlan_controller"]:
        latest_ts = (WifiApStats.objects
                     .filter(device=ac)
                     .order_by("-timestamp")
                     .values_list("timestamp", flat=True)
                     .first())
        if not latest_ts:
            continue
        snapshot = WifiApStats.objects.filter(device=ac, timestamp=latest_ts)
        ap_total += snapshot.count()
        ap_online += snapshot.filter(is_online=True).count()
        # AP đang offline → đưa vào card "Thiết bị đang Offline" (kèm tên AP).
        for ap in snapshot.filter(is_online=False).order_by("ap_name"):
            offline_ap_rows.append({
                "name": ap.ap_name,
                "ip_address": ap.ap_ip or "—",
                "group_label": "Access Point · " + ac.name,
            })

    device_type_stats = []
    for dtype, label, icon, color_class, color in DEVICE_TYPE_META:
        if dtype == "ap":
            total, online = ap_total, ap_online
        else:
            devices = by_type.get(dtype, [])
            online = sum(1 for d in devices if d.is_online)
            total = len(devices)
        device_type_stats.append({
            "type": dtype,
            "label": label,
            "icon": icon,
            "color_class": color_class,
            "color": color,
            "total": total,
            "online": online,
            "offline": total - online,
        })

    # Card "Thiết bị đang Offline": Device offline + AP offline (theo tên).
    offline_device_rows = [
        {
            "name": d.name,
            "ip_address": d.ip_address,
            "group_label": GROUP_LABELS.get(d.device_type, d.device_type.title()),
        }
        for d in all_devices if not d.is_online
    ]
    offline_notice_rows = offline_device_rows + offline_ap_rows

    active_alerts = list(Alert.objects.filter(is_active=True)
                         .select_related("device", "rule")
                         .order_by("-triggered_at")[:20])
    return {
        "by_type": by_type,
        "device_type_stats": device_type_stats,
        "offline_count": len(offline_device_rows) + len(offline_ap_rows),
        "offline_notice_rows": offline_notice_rows,
        "active_alerts": active_alerts,
        "alert_count": len(active_alerts),
    }


@never_cache  # luôn lấy HTML mới — tránh trình duyệt/bfcache hiển thị dashboard cũ
@login_required
def index(request):
    # 1 query instead of 4 separate device_type queries
    all_devices = list(Device.objects.filter(enabled=True).order_by("device_type", "name"))
    counts = _dashboard_counts(all_devices)
    by_type = counts["by_type"]
    switches  = by_type["switch"]
    routers   = by_type["router"]
    firewalls = by_type["firewall"]
    nas_list  = by_type["nas"]
    hyperv    = by_type["hyperv"]
    wlan_controllers = by_type["wlan_controller"]

    offline_devices = [d for d in all_devices if not d.is_online]
    online_count = len(all_devices) - len(offline_devices)
    latest_seen = max((d.last_seen for d in all_devices if d.last_seen), default=None)

    def _offline(devices):
        return sum(1 for d in devices if not d.is_online)

    context = {
        "switches":       switches,
        "routers":        routers,
        "firewalls":      firewalls,
        "nas_devices":    nas_list,
        "hyperv_hosts":   hyperv,
        "wlan_controllers": wlan_controllers,
        "switches_off":   _offline(switches),
        "routers_off":    _offline(routers),
        "firewalls_off":  _offline(firewalls),
        "nas_off":        _offline(nas_list),
        "hyperv_off":     _offline(hyperv),
        "wlan_off":       _offline(wlan_controllers),
        "active_alerts":  counts["active_alerts"],
        "device_type_stats": counts["device_type_stats"],
        "total_devices":  len(all_devices),
        "online_count":   online_count,
        "offline_count":  counts["offline_count"],
        "offline_notice_rows": counts["offline_notice_rows"],
        "alert_count":    counts["alert_count"],
        # Mốc dữ liệu mới nhất lúc render (epoch) — baseline để JS tự đồng bộ reload.
        "poll_fresh":     latest_seen.timestamp() if latest_seen else 0,
    }
    return render(request, "dashboard/index.html", context)


@never_cache
@login_required
def alerts_summary(request):
    """JSON tóm tắt cảnh báo + số đếm cho dashboard cập nhật TẠI CHỖ (không reload).

    Alert do task eval (120s) sinh ra, KHÔNG đi qua SSE per-device → dashboard poll
    nhẹ endpoint này (~25s). Panel Active Alerts render qua cùng partial với index
    nên markup không lệch.
    """
    from django.template.loader import render_to_string
    all_devices = list(Device.objects.filter(enabled=True))
    counts = _dashboard_counts(all_devices)
    alerts_html = render_to_string(
        "dashboard/_active_alerts_body.html",
        {"active_alerts": counts["active_alerts"]},
        request=request,
    )
    offline_notice_html = render_to_string(
        "dashboard/_offline_notice.html",
        {"offline_notice_rows": counts["offline_notice_rows"]},
        request=request,
    )
    return JsonResponse({
        "alert_count":   counts["alert_count"],
        "offline_count": counts["offline_count"],
        "stats": [
            {"type": s["type"], "total": s["total"],
             "online": s["online"], "offline": s["offline"]}
            for s in counts["device_type_stats"]
        ],
        "alerts_html": alerts_html,
        "offline_notice_html": offline_notice_html,
    })


@login_required
def poll_status(request):
    """Mốc dữ liệu mới nhất của fleet (max last_seen, epoch) cho dashboard tự reload.

    Frontend theo dõi giá trị này: mỗi lần tiến lên = có thêm thiết bị vừa poll
    xong; khi đứng yên 10s (cả fleet đã poll xong) → reload. Thay timer 120s cố
    định vốn lệch pha với chu kỳ poll.
    """
    from django.db.models import Max
    latest = (Device.objects.filter(enabled=True, last_seen__isnull=False)
              .aggregate(m=Max("last_seen"))["m"])
    return JsonResponse({
        "fresh": latest.timestamp() if latest else 0,
        "now": timezone.now().timestamp(),
    })


@login_required
def switch_detail(request, pk):
    from apps.metrics.models import InterfaceStats, SystemHealth
    from django.db.models import Subquery, OuterRef

    device     = get_object_or_404(Device, pk=pk, device_type="switch")
    interfaces = device.interfaces.all().order_by("if_index")

    # Latest health for summary badges
    latest_health = (SystemHealth.objects
                     .filter(device=device)
                     .order_by("-timestamp")
                     .first())

    # Latest stats per interface — annotate with subquery to avoid N+1
    latest_status   = (InterfaceStats.objects
                       .filter(interface=OuterRef("pk"))
                       .order_by("-timestamp"))
    interfaces = interfaces.annotate(
        cur_status   =Subquery(latest_status.values("status")[:1]),
        cur_in_mbps  =Subquery(latest_status.values("in_mbps")[:1]),
        cur_out_mbps =Subquery(latest_status.values("out_mbps")[:1]),
    )
    interfaces = sorted(
        interfaces,
        key=lambda i: (
            -int(i.is_uplink),
            -float(i.cur_in_mbps or 0) - float(i.cur_out_mbps or 0),
            i.if_index,
        ),
    )

    return render(request, "dashboard/switch_detail.html", {
        "device":         device,
        "interfaces":     interfaces,
        "latest_health":  latest_health,
    })


@login_required
def hyperv_detail(request, pk):
    from apps.metrics.models import VMStats, SystemHealth

    device = get_object_or_404(Device, pk=pk, device_type="hyperv")

    latest_health = (SystemHealth.objects
                     .filter(device=device)
                     .order_by("-timestamp")
                     .first())

    # Lấy snapshot mới nhất mỗi VM bằng Postgres DISTINCT ON — dùng đúng index
    # (device_id, vm_name, timestamp DESC) → 1 lần index-scan, ~0.04s.
    # (Trước đây dùng pk__in + correlated Subquery → Postgres bỏ index, quét
    #  lặp trên toàn bộ rows của device → ~240s → nginx 504 Gateway Time-out.)
    from django.db import connection

    base_qs = VMStats.objects.filter(device=device)
    if connection.vendor == "postgresql":
        latest_vms = list(
            base_qs.order_by("vm_name", "-timestamp").distinct("vm_name")
        )
    else:
        # SQLite (dev): DISTINCT ON không hỗ trợ → dedup theo vm_name trong Python.
        seen: set[str] = set()
        latest_vms = []
        for v in base_qs.order_by("vm_name", "-timestamp"):
            if v.vm_name not in seen:
                seen.add(v.vm_name)
                latest_vms.append(v)

    running_count = sum(1 for v in latest_vms if v.state == "Running")
    unhealthy_vms = [v for v in latest_vms if v.repl_health not in ("", "Normal", "NotConfigured")]

    return render(request, "dashboard/hyperv_detail.html", {
        "device":         device,
        "vms":            latest_vms,
        "latest_health":  latest_health,
        "running_count":  running_count,
        "unhealthy_vms":  unhealthy_vms,
    })


def _switch_like_detail(request, pk: int, device_type: str, template: str):
    """Logic chung cho switch_detail và router_detail (cùng có interfaces + CPU/RAM)."""
    from apps.metrics.models import InterfaceStats, SystemHealth
    from django.db.models import Subquery, OuterRef

    device     = get_object_or_404(Device, pk=pk, device_type=device_type)
    interfaces = device.interfaces.all().order_by("if_index")

    latest_health = (SystemHealth.objects
                     .filter(device=device)
                     .order_by("-timestamp")
                     .first())

    latest_status = (InterfaceStats.objects
                     .filter(interface=OuterRef("pk"))
                     .order_by("-timestamp"))
    interfaces = interfaces.annotate(
        cur_status   =Subquery(latest_status.values("status")[:1]),
        cur_in_mbps  =Subquery(latest_status.values("in_mbps")[:1]),
        cur_out_mbps =Subquery(latest_status.values("out_mbps")[:1]),
    )
    interfaces = sorted(
        interfaces,
        key=lambda i: (
            -int(i.is_uplink),
            -float(i.cur_in_mbps or 0) - float(i.cur_out_mbps or 0),
            i.if_index,
        ),
    )
    return render(request, template, {
        "device":        device,
        "interfaces":    interfaces,
        "latest_health": latest_health,
    })


@login_required
def router_detail(request, pk):
    return _switch_like_detail(request, pk, "router", "dashboard/router_detail.html")


@login_required
def nas_detail(request, pk):
    # NAS (Synology) giống switch: có interface + CPU/RAM → tái dùng template switch.
    return _switch_like_detail(request, pk, "nas", "dashboard/switch_detail.html")


@login_required
def wlan_detail(request, pk):
    """Trang WLAN Controller (Huawei AC): AP online/offline + client đang kết nối."""
    from apps.metrics.models import SystemHealth, WifiApStats, WifiClientStats

    device = get_object_or_404(Device, pk=pk, device_type="wlan_controller")

    latest_health = (SystemHealth.objects
                     .filter(device=device)
                     .order_by("-timestamp")
                     .first())

    # AP snapshot mới nhất.
    latest_ap_ts = (WifiApStats.objects
                    .filter(device=device)
                    .order_by("-timestamp")
                    .values_list("timestamp", flat=True)
                    .first())
    aps = list(WifiApStats.objects.filter(device=device, timestamp=latest_ap_ts)
               .order_by("ap_name")) if latest_ap_ts else []

    # Client snapshot mới nhất.
    latest_cl_ts = (WifiClientStats.objects
                    .filter(device=device)
                    .order_by("-timestamp")
                    .values_list("timestamp", flat=True)
                    .first())
    clients = list(WifiClientStats.objects.filter(device=device, timestamp=latest_cl_ts)
                   .order_by("ap_name", "mac")) if latest_cl_ts else []

    ap_online = sum(1 for a in aps if a.is_online)
    # AC6508 không liệt kê từng client qua SNMP — tổng client lấy bằng tổng số
    # client đang kết nối trên các AP (WifiApStats.client_count). Nếu sau này có
    # bảng station thật thì ưu tiên đếm theo danh sách client.
    client_total = len(clients) if clients else sum(a.client_count for a in aps)
    return render(request, "dashboard/wlan_detail.html", {
        "device":        device,
        "latest_health": latest_health,
        "aps":           aps,
        "clients":       clients,
        "ap_total":      len(aps),
        "ap_online":     ap_online,
        "ap_offline":    len(aps) - ap_online,
        "client_total":  client_total,
        "ap_updated":    latest_ap_ts,
        "client_updated": latest_cl_ts,
    })


@login_required
def firewall_detail(request, pk):
    from apps.metrics.models import SystemHealth

    device = get_object_or_404(Device, pk=pk, device_type="firewall")
    interfaces = device.interfaces.all().order_by("if_index")

    latest_health = (SystemHealth.objects
                     .filter(device=device)
                     .order_by("-timestamp")
                     .first())

    # Lấy session_count từ extra nếu Fortinet lưu vào SystemHealth
    session_count = None
    if latest_health and latest_health.extra:
        session_count = latest_health.extra.get("session_count")

    from apps.metrics.models import InterfaceStats
    from django.db.models import Subquery, OuterRef
    latest_status = (InterfaceStats.objects
                     .filter(interface=OuterRef("pk"))
                     .order_by("-timestamp"))
    interfaces = interfaces.annotate(
        cur_status   =Subquery(latest_status.values("status")[:1]),
        cur_in_mbps  =Subquery(latest_status.values("in_mbps")[:1]),
        cur_out_mbps =Subquery(latest_status.values("out_mbps")[:1]),
    )
    return render(request, "dashboard/firewall_detail.html", {
        "device":         device,
        "interfaces":     interfaces,
        "latest_health":  latest_health,
        "session_count":  session_count,
    })


@never_cache
@login_required
def topology(request):
    """Sơ đồ AP kết nối về switch (Cytoscape.js)."""
    from apps.devices.models import Device

    ac_devices = list(
        Device.objects.filter(device_type="wlan_controller", enabled=True).order_by("name")
    )
    switches = list(
        Device.objects.filter(device_type="switch", enabled=True).order_by("name")
    )
    ac_id = request.GET.get("ac")
    switch_id = request.GET.get("switch")
    return render(request, "dashboard/topology.html", {
        "ac_devices": ac_devices,
        "switches": switches,
        "initial_ac_id": int(ac_id) if ac_id and ac_id.isdigit() else None,
        "initial_switch_id": int(switch_id) if switch_id and switch_id.isdigit() else None,
    })


@login_required
def topology_data(request):
    """JSON nodes/edges cho Cytoscape — ghép TopologyLink + WifiApStats."""
    from apps.dashboard.topology_api import build_topology_graph
    from apps.devices.models import Device

    ac = None
    ac_param = request.GET.get("ac")
    if ac_param and ac_param.isdigit():
        ac = Device.objects.filter(pk=int(ac_param), device_type="wlan_controller").first()
    switch_filter = None
    sw_param = request.GET.get("switch")
    if sw_param and sw_param.isdigit():
        switch_filter = int(sw_param)

    return JsonResponse(build_topology_graph(ac=ac, switch_filter=switch_filter))
