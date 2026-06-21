import os
import shutil
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.conf import settings
from django.db import connection
from django.utils import timezone
from .models import Alert, AlertRule
from .forms import AlertRuleForm


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


@login_required
def storage(request):
    """Theo dõi dung lượng: kích thước database + disk của host nơi đặt volume DB."""
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
def rule_delete(request, pk):
    if not _can_write(request):
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    rule = get_object_or_404(AlertRule, pk=pk)
    if request.method == "POST":
        rule.delete()
        return redirect("alerts:rule_list")
    return render(request, "alerts/rules/confirm_delete.html", {"rule": rule})
