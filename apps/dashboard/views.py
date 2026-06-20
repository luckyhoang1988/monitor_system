from collections import defaultdict
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
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


@login_required
def index(request):
    # 1 query instead of 4 separate device_type queries
    all_devices = list(Device.objects.filter(enabled=True).order_by("device_type", "name"))
    by_type: dict[str, list] = defaultdict(list)
    for d in all_devices:
        by_type[d.device_type].append(d)
    switches  = by_type["switch"]
    routers   = by_type["router"]
    firewalls = by_type["firewall"]
    hyperv    = by_type["hyperv"]
    online_count = sum(1 for d in all_devices if d.is_online)
    active_alerts = list(Alert.objects.filter(is_active=True)
                         .select_related("device", "rule")
                         .order_by("-triggered_at")[:20])
    context = {
        "switches":       switches,
        "routers":        routers,
        "firewalls":      firewalls,
        "hyperv_hosts":   hyperv,
        "active_alerts":  active_alerts,
        "total_devices":  len(all_devices),
        "online_count":   online_count,
        "offline_count":  len(all_devices) - online_count,
        "alert_count":    len(active_alerts),
    }
    return render(request, "dashboard/index.html", context)


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

    from django.db.models import Subquery, OuterRef

    latest_vms_subquery = VMStats.objects.filter(
        device=device,
        vm_name=OuterRef("vm_name")
    ).order_by("-timestamp").values("pk")[:1]

    latest_vms = list(VMStats.objects.filter(
        pk__in=Subquery(latest_vms_subquery)
    ).order_by("vm_name"))

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
    return render(request, template, {
        "device":        device,
        "interfaces":    interfaces,
        "latest_health": latest_health,
    })


@login_required
def router_detail(request, pk):
    return _switch_like_detail(request, pk, "router", "dashboard/router_detail.html")


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
