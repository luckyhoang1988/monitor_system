import os
import shutil
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.conf import settings
from django.db import connection
from django.utils import timezone
from .models import Alert, AlertRule, AlertConfig
from .forms import AlertRuleForm, AlertConfigForm


def _db_size_bytes() -> int:
    """Kích thước database hiện tại (PostgreSQL prod / SQLite dev)."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            row = cur.fetchone()
            return int(row[0]) if row else 0
    if connection.vendor == "sqlite":
        name = connection.settings_dict.get("NAME")
        try:
            return os.path.getsize(name) if name and os.path.exists(name) else 0
        except OSError:
            return 0
    return 0


def _fmt_bytes(n: float) -> str:
    n = float(n or 0)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{int(n)} B" if i == 0 else f"{n:.1f} {units[i]}"


def _purge_metrics(start, end) -> int:
    """Xóa metrics (raw + aggregated) trong [start, end). Trả tổng số bản ghi đã xóa.

    KHÔNG đụng tới lịch sử cảnh báo (Alert/AlertNotification).
    """
    from apps.metrics.models import (
        InterfaceStats, SystemHealth, VMStats,
        SystemHealthHourly, SystemHealthDaily,
        InterfaceStatsHourly, InterfaceStatsDaily,
    )
    total = 0
    for model in (InterfaceStats, SystemHealth, VMStats):
        total += model.objects.filter(timestamp__gte=start, timestamp__lt=end).delete()[0]
    for model in (SystemHealthHourly, InterfaceStatsHourly):
        total += model.objects.filter(hour__gte=start, hour__lt=end).delete()[0]
    for model in (SystemHealthDaily, InterfaceStatsDaily):
        total += model.objects.filter(day__gte=start.date(), day__lt=end.date()).delete()[0]
    return total


@login_required
def storage(request):
    """Theo dõi dung lượng + xóa log metrics theo khoảng ngày (POST)."""
    can_write = _can_write(request)

    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("Bạn không có quyền xóa dữ liệu.")
        tz = ZoneInfo(getattr(settings, "TIME_ZONE", "UTC"))
        from_raw = (request.POST.get("from_date") or "").strip()
        to_raw   = (request.POST.get("to_date") or "").strip()
        try:
            from_d = datetime.strptime(from_raw, "%Y-%m-%d").date()
            to_d   = datetime.strptime(to_raw, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Vui lòng chọn Từ ngày và Đến ngày hợp lệ.")
            return redirect("alerts:storage")
        if from_d > to_d:
            messages.error(request, "Từ ngày phải ≤ Đến ngày.")
            return redirect("alerts:storage")
        start = datetime.combine(from_d, time.min, tzinfo=tz)
        end   = datetime.combine(to_d, time.min, tzinfo=tz) + timedelta(days=1)
        deleted = _purge_metrics(start, end)
        messages.success(
            request,
            f"Đã xóa {deleted:,} bản ghi metrics từ {from_d:%d/%m/%Y} đến {to_d:%d/%m/%Y}.",
        )
        return redirect("alerts:storage")

    path = getattr(settings, "STORAGE_MONITOR_PATH", "/") or "/"
    try:
        total, used, free = shutil.disk_usage(path)
    except OSError:
        total = used = free = 0

    db_bytes = _db_size_bytes()
    disk_pct = round(used / total * 100, 1) if total else 0.0
    db_vs_disk_pct = round(db_bytes / total * 100, 1) if total else 0.0

    if disk_pct < 70:
        bar_class = "bg-success"
    elif disk_pct < 85:
        bar_class = "bg-warning"
    else:
        bar_class = "bg-danger"

    return render(request, "alerts/storage.html", {
        "db_size":        _fmt_bytes(db_bytes),
        "db_vs_disk_pct": db_vs_disk_pct,
        "disk_total":     _fmt_bytes(total),
        "disk_used":      _fmt_bytes(used),
        "disk_free":      _fmt_bytes(free),
        "disk_pct":       disk_pct,
        "bar_class":      bar_class,
        "monitor_path":   path,
        "db_vendor":      connection.vendor,
        "can_write":      can_write,
        "auto_cleanup":   getattr(settings, "METRICS_AUTO_CLEANUP", False),
    })


def _can_write(request) -> bool:
    return bool(
        request.user
        and request.user.is_authenticated
        and (request.user.is_superuser or request.user.groups.filter(name="Network Admins").exists())
    )


@login_required
def alert_list(request):
    active   = Alert.objects.filter(is_active=True).select_related("device", "rule")
    resolved = Alert.objects.filter(is_active=False).select_related("device", "rule")[:50]
    return render(request, "alerts/list.html", {"active": active, "resolved": resolved})


@login_required
def alert_acknowledge(request, pk):
    alert = get_object_or_404(Alert, pk=pk)
    if request.method == "POST":
        alert.acknowledged_by = request.user.username
        alert.acknowledged_at = timezone.now()
        alert.save(update_fields=["acknowledged_by", "acknowledged_at"])
    return redirect("alerts:list")


# ── AlertRule CRUD ──────────────────────────────────────────────────────────

@login_required
def rule_list(request):
    rules = AlertRule.objects.all().order_by("severity", "name")
    return render(request, "alerts/rules/list.html", {"rules": rules})


@login_required
def rule_create(request):
    if not _can_write(request):
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    form = AlertRuleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("alerts:rule_list")
    return render(request, "alerts/rules/form.html", {"form": form, "title": "Thêm Alert Rule"})


@login_required
def rule_edit(request, pk):
    if not _can_write(request):
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    rule = get_object_or_404(AlertRule, pk=pk)
    form = AlertRuleForm(request.POST or None, instance=rule)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("alerts:rule_list")
    return render(request, "alerts/rules/form.html", {"form": form, "title": f"Sửa: {rule.name}"})


@login_required
def notification_config(request):
    """Cấu hình kênh Telegram nhận cảnh báo (singleton, admin-only)."""
    if not _can_write(request):
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    cfg = AlertConfig.load()
    form = AlertConfigForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Đã lưu cấu hình Telegram.")
        return redirect("alerts:config")
    return render(request, "alerts/config.html", {"form": form})


@login_required
def rule_delete(request, pk):
    if not _can_write(request):
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    rule = get_object_or_404(AlertRule, pk=pk)
    if request.method == "POST":
        rule.delete()
        return redirect("alerts:rule_list")
    return render(request, "alerts/rules/confirm_delete.html", {"rule": rule})
