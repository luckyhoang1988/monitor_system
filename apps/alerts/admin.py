from django.contrib import admin
from apps.accounts.admin_mixins import AdminRBACMixin
from .models import AlertRule, Alert, AlertNotification, AlertConfig


@admin.register(AlertConfig)
class AlertConfigAdmin(AdminRBACMixin, admin.ModelAdmin):
    list_display = ("telegram_enabled", "telegram_chat_id", "updated_at")


@admin.register(AlertRule)
class AlertRuleAdmin(AdminRBACMixin, admin.ModelAdmin):
    list_display = ("name", "device_type", "metric", "condition", "threshold", "severity", "enabled")
    list_filter  = ("severity", "device_type", "enabled")


class AlertNotificationInline(admin.TabularInline):
    model = AlertNotification
    extra = 0
    # kind/attempts/next_attempt_at: để người vận hành thấy thông báo đang chờ gửi lại lúc nào
    # (lịch hẹn sau 429/lỗi mạng, hạng mục 1.2) — chỉ đọc, engine là nơi duy nhất ghi.
    readonly_fields = ("channel", "kind", "sent_at", "status", "attempts", "next_attempt_at", "error")


@admin.register(Alert)
class AlertAdmin(AdminRBACMixin, admin.ModelAdmin):
    list_display   = ("device", "rule", "severity", "metric_value", "is_active", "triggered_at")
    list_filter    = ("severity", "is_active", "device__device_type")
    search_fields  = ("device__name",)
    readonly_fields = ("triggered_at", "resolved_at")
    inlines = [AlertNotificationInline]
