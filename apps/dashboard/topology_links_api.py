"""API CRUD cho TopologyLink THỦ CÔNG (admin tự khai báo switch↔switch & AP↔switch).

Link thủ công dùng ``match_method="manual"`` — collector tự động đã ``.exclude(match_method=
"manual")`` ở mọi nhánh đánh dấu stale nên link này không bao giờ bị xoá/stale tự động
(xem apps/collectors/topology_writer.py). Cả 2 đường render (build_topology_graph cho AP,
build_switch_uplink_edges cho switch) đọc thẳng TopologyLink nên link hiện ngay sau khi tạo.
"""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseNotAllowed, JsonResponse
from django.views.decorators.http import require_GET

from apps.accounts.roles import is_admin
from apps.collectors.topology_lldp import normalize_mac
from apps.collectors.topology_writer import _upsert_ap_link, _upsert_switch_link
from apps.devices.models import Device, TopologyLink


def _forbidden() -> JsonResponse:
    return JsonResponse(
        {"success": False, "message": "Bạn không có quyền thực hiện thao tác này."},
        status=403,
    )


def _bad(msg: str) -> JsonResponse:
    return JsonResponse({"success": False, "message": msg}, status=400)


def _parse_body(request) -> dict:
    try:
        return json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}


@login_required
@require_GET
def ports_for_device(request):
    """Danh sách cổng (Interface đã poll SNMP) của 1 switch — đổ dropdown chọn cổng."""
    dev_id = request.GET.get("device")
    if not (dev_id and dev_id.isdigit()):
        return JsonResponse({"ports": []})
    dev = Device.objects.filter(pk=int(dev_id)).first()
    if not dev:
        return JsonResponse({"ports": []})
    ports = [
        {
            "name": i.name,
            "if_index": i.if_index,
            "port_mode": i.port_mode,
            "is_uplink": i.is_uplink,
        }
        for i in dev.interfaces.all()
    ]
    return JsonResponse({"ports": ports})


@login_required
@require_GET
def aps_for_ac(request):
    """Danh sách AP từ snapshot AC, có cờ ``mapped`` (đã có AP link) — ưu tiên AP chưa map."""
    from apps.dashboard.topology_api import list_ac_aps

    ac = None
    ac_param = request.GET.get("ac")
    if ac_param and ac_param.isdigit():
        ac = Device.objects.filter(pk=int(ac_param), device_type="wlan_controller").first()
    if ac is None:
        ac = (
            Device.objects.filter(device_type="wlan_controller", enabled=True)
            .order_by("name")
            .first()
        )

    aps = list_ac_aps(ac)
    mapped = {
        normalize_mac(m)
        for m in TopologyLink.objects.filter(link_kind="ap", is_stale=False)
        .exclude(remote_ap_mac="")
        .values_list("remote_ap_mac", flat=True)
    }
    for ap in aps:
        ap["mapped"] = bool(ap["mac"]) and ap["mac"] in mapped
    # Chưa map lên đầu, rồi theo tên.
    aps.sort(key=lambda a: (a["mapped"], (a["name"] or "").lower()))
    return JsonResponse({"aps": aps, "ac_id": ac.id if ac else None})


@login_required
def links_collection(request):
    """GET: list link thủ công (mọi user). POST: tạo link (chỉ admin)."""
    if request.method == "GET":
        return _list_manual_links()
    if request.method == "POST":
        if not is_admin(request.user):
            return _forbidden()
        return _create_link(request)
    return HttpResponseNotAllowed(["GET", "POST"])


@login_required
def link_detail(request, pk: int):
    """POST: sửa link thủ công. DELETE: xoá. Chỉ admin, chỉ link match_method=manual."""
    if request.method in ("POST", "PUT"):
        if not is_admin(request.user):
            return _forbidden()
        return _update_link(request, pk)
    if request.method == "DELETE":
        if not is_admin(request.user):
            return _forbidden()
        return _delete_link(pk)
    return HttpResponseNotAllowed(["POST", "DELETE"])


def _list_manual_links() -> JsonResponse:
    links = (
        TopologyLink.objects.filter(match_method="manual")
        .select_related("local_device", "remote_device")
        .order_by("link_kind", "local_device__name", "local_port")
    )
    out = []
    for link in links:
        if link.link_kind == "ap":
            remote = link.remote_ap_name or link.remote_ap_mac or "AP"
        else:
            remote = (link.remote_device.name if link.remote_device else link.remote_sys_name) or "—"
        out.append({
            "id": link.id,
            "kind": link.link_kind,
            "local_device": link.local_device.name,
            "local_device_id": link.local_device_id,
            "local_port": link.local_port,
            "remote": remote,
            "remote_port": link.remote_port_id,
            "is_stale": link.is_stale,
        })
    return JsonResponse({"links": out})


def _create_link(request) -> JsonResponse:
    data = _parse_body(request)
    kind = data.get("kind")
    local_id = data.get("local_device")
    local_port = (data.get("local_port") or "").strip()

    if kind not in ("ap", "switch"):
        return _bad("Loại link không hợp lệ.")
    if not (local_id and str(local_id).isdigit()):
        return _bad("Thiếu switch nguồn.")
    if not local_port:
        return _bad("Thiếu cổng trên switch.")

    device = Device.objects.filter(pk=int(local_id)).first()
    if not device:
        return _bad("Switch nguồn không tồn tại.")

    # Cảnh báo nếu cổng đã có link tự động — tạo manual sẽ ghi đè (unique theo device, port).
    existing = TopologyLink.objects.filter(local_device=device, local_port=local_port).first()
    overwrote_auto = bool(existing and existing.match_method != "manual")

    if kind == "ap":
        mac = normalize_mac(data.get("remote_ap_mac") or "")
        ap_name = (data.get("remote_ap_name") or "").strip()
        if not mac and not ap_name:
            return _bad("Cần chọn AP hoặc nhập MAC/tên AP.")
        _upsert_ap_link(
            device,
            local_port,
            mac=mac,
            ap_name=ap_name,
            protocol="lldp",
            match_method="manual",
            is_confirmed=True,
        )
    else:
        remote_id = data.get("remote_device")
        if not (remote_id and str(remote_id).isdigit()):
            return _bad("Thiếu switch đích.")
        remote = Device.objects.filter(pk=int(remote_id)).first()
        if not remote:
            return _bad("Switch đích không tồn tại.")
        if remote.id == device.id:
            return _bad("Không thể nối switch với chính nó.")
        if remote.device_type != "switch":
            return _bad("Thiết bị đích phải là switch.")
        remote_port = (data.get("remote_port") or "").strip()
        _upsert_switch_link(
            device,
            local_port,
            remote,
            remote_port_id=remote_port,
            match_method="manual",
            is_confirmed=True,
        )

    msg = "Đã lưu liên kết thủ công."
    if overwrote_auto:
        msg += " (Ghi đè link tự động sẵn có trên cổng này.)"
    return JsonResponse({"success": True, "message": msg})


def _update_link(request, pk: int) -> JsonResponse:
    existing = TopologyLink.objects.filter(pk=pk, match_method="manual").first()
    if not existing:
        return JsonResponse(
            {"success": False, "message": "Không tìm thấy link thủ công."}, status=404
        )
    # Key unique = (local_device, local_port) có thể đổi khi sửa → xoá cũ rồi tạo lại từ body.
    existing.delete()
    return _create_link(request)


def _delete_link(pk: int) -> JsonResponse:
    deleted, _ = TopologyLink.objects.filter(pk=pk, match_method="manual").delete()
    if not deleted:
        return JsonResponse(
            {"success": False, "message": "Không tìm thấy link thủ công."}, status=404
        )
    return JsonResponse({"success": True, "message": "Đã xoá liên kết."})
