from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.utils import timezone
from .models import Alert, AlertRule
from .forms import AlertRuleForm


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
