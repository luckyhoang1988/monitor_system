import ipaddress
import json
import time
import subprocess
import sys
import socket
import os
from pathlib import Path
from django.conf import settings
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, Http404, HttpResponse, HttpResponseForbidden
from django.utils import timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from .models import Device, DiscoveredDevice
from .forms import DeviceForm
from .backup import run_ssh_backup, save_backup_file, get_device_backups

DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "debug-f05be0.log"


def _debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": "f05be0",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _can_write(request) -> bool:
    return bool(
        request.user
        and request.user.is_authenticated
        and (request.user.is_superuser or request.user.groups.filter(name="Network Admins").exists())
    )


# Helper functions for Auto-Discovery Subnet Scanner
def _ping_ip(ip: str) -> tuple[str, bool]:
    if sys.platform.startswith("win"):
        # Windows ping: 1 gói, timeout 500ms
        cmd = ["ping", "-n", "1", "-w", "500", ip]
    else:
        # Linux ping: 1 gói, timeout 500ms
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1.5)
        return ip, res.returncode == 0
    except Exception:
        return ip, False


def _probe_snmp(ip: str, community: str = "public") -> tuple[bool, str]:
    try:
        from easysnmp import Session
        session = Session(hostname=ip, community=community, version=2, timeout=1, retries=1)
        descr = session.get("1.3.6.1.2.1.1.1.0").value or ""
        name = session.get("1.3.6.1.2.1.1.5.0").value or ""
        return True, f"{name} - {descr}"[:300]
    except Exception:
        return False, ""


# Views
@login_required
def device_list(request):
    devices = Device.objects.all().order_by("device_type", "name")
    return render(request, "devices/list.html", {"devices": devices})


@login_required
def device_add(request):
    if not _can_write(request):
        # region agent log
        _debug_log(
            run_id="post-fix",
            hypothesis_id="H3",
            location="apps/devices/views.py:77",
            message="device_add denied by RBAC",
            data={"username": request.user.username},
        )
        # endregion
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    initial = {}
    if request.method == "GET":
        initial = {
            "name": request.GET.get("name", ""),
            "ip_address": request.GET.get("ip_address", ""),
            "protocol": request.GET.get("protocol", "snmp"),
            "snmp_community": request.GET.get("snmp_community", ""),
        }
    form = DeviceForm(request.POST or None, initial=initial)
    if request.method == "POST":
        # region agent log
        _debug_log(
            run_id="pre-fix",
            hypothesis_id="H3",
            location="apps/devices/views.py:86",
            message="device_add POST permission context",
            data={
                "username": request.user.username,
                "is_superuser": request.user.is_superuser,
                "is_network_admin": request.user.groups.filter(name="Network Admins").exists(),
            },
        )
        # endregion
    if form.is_valid():
        device = form.save()
        # Đánh dấu DiscoveredDevice là đã được imported
        ip_addr = form.cleaned_data.get("ip_address")
        if ip_addr:
            DiscoveredDevice.objects.filter(ip_address=ip_addr).update(is_imported=True)
        return redirect("devices:list")
    return render(request, "devices/add.html", {"form": form, "title": "Thêm thiết bị"})


@login_required
def device_edit(request, pk):
    if not _can_write(request):
        # region agent log
        _debug_log(
            run_id="post-fix",
            hypothesis_id="H3",
            location="apps/devices/views.py:111",
            message="device_edit denied by RBAC",
            data={"username": request.user.username, "device_id": pk},
        )
        # endregion
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    device = get_object_or_404(Device, pk=pk)
    form = DeviceForm(request.POST or None, instance=device)
    if request.method == "POST":
        # region agent log
        _debug_log(
            run_id="pre-fix",
            hypothesis_id="H3",
            location="apps/devices/views.py:108",
            message="device_edit POST permission context",
            data={
                "username": request.user.username,
                "is_superuser": request.user.is_superuser,
                "is_network_admin": request.user.groups.filter(name="Network Admins").exists(),
                "device_id": device.id,
            },
        )
        # endregion
    if form.is_valid():
        form.save()
        return redirect("devices:list")
    return render(request, "devices/add.html", {"form": form, "title": f"Sửa: {device.name}"})


@login_required
def device_delete(request, pk):
    if not _can_write(request):
        # region agent log
        _debug_log(
            run_id="post-fix",
            hypothesis_id="H3",
            location="apps/devices/views.py:140",
            message="device_delete denied by RBAC",
            data={"username": request.user.username, "device_id": pk},
        )
        # endregion
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    device = get_object_or_404(Device, pk=pk)
    if request.method == "POST":
        # region agent log
        _debug_log(
            run_id="pre-fix",
            hypothesis_id="H3",
            location="apps/devices/views.py:129",
            message="device_delete POST permission context",
            data={
                "username": request.user.username,
                "is_superuser": request.user.is_superuser,
                "is_network_admin": request.user.groups.filter(name="Network Admins").exists(),
                "device_id": device.id,
            },
        )
        # endregion
        device.delete()
        return redirect("devices:list")
    return render(request, "devices/confirm_delete.html", {"device": device})


@login_required
def device_test_connection(request, pk):
    """AJAX endpoint — test SNMP/SSH kết nối và trả về kết quả JSON."""
    device = get_object_or_404(Device, pk=pk)
    try:
        from apps.collectors.factory import CollectorFactory
        collector = CollectorFactory.create(device)
        os_family = collector.test_connection()
        device.last_seen = timezone.now()
        device.os_family = os_family
        device.save(update_fields=["last_seen", "os_family"])
        return JsonResponse({"success": True, "os_family": os_family,
                             "is_online": True,
                             "message": f"Kết nối OK — {os_family}"})
    except Exception as exc:
        # region agent log
        _debug_log(
            run_id="pre-fix-hyperv",
            hypothesis_id="HYP-6",
            location="apps/devices/views.py:187",
            message="device_test_connection failed",
            data={
                "device_id": device.id,
                "device_name": device.name,
                "device_type": device.device_type,
                "protocol": device.protocol,
                "exc_type": type(exc).__name__,
                "exc": str(exc)[:300],
            },
        )
        # endregion
        return JsonResponse({"success": False, "message": str(exc)}, status=200)


# Auto-Discovery Views
@login_required
def device_discovery(request):
    discovered = DiscoveredDevice.objects.all().order_by("is_imported", "-discovered_at")
    return render(request, "devices/discovery.html", {"discovered": discovered})


@login_required
def device_discovery_scan(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Method not allowed"}, status=405)

    subnet = request.POST.get("subnet", "").strip()
    community = request.POST.get("community", "public").strip() or "public"

    if not subnet:
        return JsonResponse({"success": False, "message": "Vui lòng nhập dải mạng (Subnet)"})

    try:
        network = ipaddress.ip_network(subnet, strict=False)
        ips = [str(ip) for ip in network.hosts()]
    except Exception as exc:
        return JsonResponse({"success": False, "message": f"Dải mạng không hợp lệ: {str(exc)}"})

    max_ips           = getattr(settings, "DISCOVERY_MAX_IPS", 256)
    ping_workers      = getattr(settings, "DISCOVERY_PING_WORKERS", 100)
    snmp_workers      = getattr(settings, "DISCOVERY_SNMP_WORKERS", 80)

    if len(ips) > max_ips:
        return JsonResponse({"success": False, "message": f"Để tối ưu hiệu năng, vui lòng quét dải mạng tối đa /{max_ips} IPs"})

    discovered_hosts: list[dict] = []

    # Step 1: Ping sweep in parallel
    workers = min(len(ips), ping_workers)
    alive_ips: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_ping_ip, ip): ip for ip in ips}
        for future in as_completed(futures):
            ip, is_alive = future.result()
            if is_alive:
                alive_ips.append(ip)

    if not alive_ips:
        return JsonResponse({"success": True, "count": 0, "devices": []})

    # Step 2: Probe SNMP + reverse DNS in parallel for alive IPs
    def _enrich(ip: str) -> tuple[str, bool, str, str]:
        has_snmp, sys_descr = _probe_snmp(ip, community)
        try:
            hostname = socket.gethostbyaddr(ip)[0]
        except Exception:
            hostname = f"Host-{ip.replace('.', '-')}"
        return ip, has_snmp, sys_descr, hostname

    # Collect enriched data from threads first, then batch-upsert
    enriched: list[tuple[str, bool, str, str]] = []
    workers2 = min(len(alive_ips), snmp_workers)
    with ThreadPoolExecutor(max_workers=workers2) as executor:
        futures2 = {executor.submit(_enrich, ip): ip for ip in alive_ips}
        for future in as_completed(futures2):
            enriched.append(future.result())

    # 1 query to check which IPs already have a Device record
    enriched_ips = [r[0] for r in enriched]
    imported_ips = set(
        Device.objects.filter(ip_address__in=enriched_ips).values_list("ip_address", flat=True)
    )

    # Bulk upsert — Django 4.2+ bulk_create with update_conflicts
    objs = [
        DiscoveredDevice(
            ip_address=ip,
            hostname=hostname,
            snmp_status=has_snmp,
            sys_descr=sys_descr,
            is_imported=(ip in imported_ips),
        )
        for ip, has_snmp, sys_descr, hostname in enriched
    ]
    DiscoveredDevice.objects.bulk_create(
        objs,
        update_conflicts=True,
        unique_fields=["ip_address"],
        update_fields=["hostname", "snmp_status", "sys_descr", "is_imported"],
    )

    # Re-fetch saved objects to get PKs for response
    saved = {
        obj.ip_address: obj
        for obj in DiscoveredDevice.objects.filter(ip_address__in=enriched_ips)
    }
    for ip, has_snmp, sys_descr, hostname in enriched:
        obj = saved.get(ip)
        if obj:
            discovered_hosts.append({
                "ip_address": obj.ip_address,
                "hostname":   obj.hostname,
                "snmp_status": obj.snmp_status,
                "sys_descr":  obj.sys_descr,
                "is_imported": obj.is_imported,
                "pk":          obj.pk,
            })

    return JsonResponse({"success": True, "count": len(discovered_hosts), "devices": discovered_hosts})


# Backup Views
@login_required
def device_backups(request, pk):
    device = get_object_or_404(Device, pk=pk)
    backups = get_device_backups(device)
    return render(request, "devices/backups.html", {"device": device, "backups": backups})


@login_required
def device_run_backup(request, pk):
    device = get_object_or_404(Device, pk=pk)
    if device.protocol != "ssh":
        return JsonResponse({"success": False, "message": "Sao lưu tự động chỉ hỗ trợ giao thức SSH"}, status=200)
    try:
        content = run_ssh_backup(device)
        filepath = save_backup_file(device, content)
        return JsonResponse({"success": True, "message": "Sao lưu cấu hình thành công!", "filename": os.path.basename(filepath)})
    except Exception as exc:
        return JsonResponse({"success": False, "message": f"Sao lưu thất bại: {str(exc)}"}, status=200)


@login_required
def device_download_backup(request, pk, filename):
    device = get_object_or_404(Device, pk=pk)
    # Ngăn chặn Directory Traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise Http404("Tên tệp tin không hợp lệ")

    prefix = f"device_{device.id}_"
    if not filename.startswith(prefix):
        raise Http404("Tệp tin không thuộc về thiết bị này")

    backup_dir = os.path.join(settings.BASE_DIR, "backups")
    filepath = os.path.join(backup_dir, filename)

    if not os.path.exists(filepath):
        raise Http404("Không tìm thấy tệp tin sao lưu")

    with open(filepath, "r", encoding="utf-8") as f:
        response = HttpResponse(f.read(), content_type="text/plain")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
