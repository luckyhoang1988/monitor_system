from django.contrib import admin
from .models import AlertRule, Alert, AlertNotification, AlertConfig


@admin.register(AlertConfig)
class AlertConfigAdmin(admin.ModelAdmin):
    list_display = ("telegram_enabled", "telegram_chat_id", "updated_at")


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "device_type", "metric", "condition", "threshold", "severity", "enabled")
    list_filter  = ("severity", "device_type", "enabled")


class AlertNotificationInline(admin.TabularInline):
    model = AlertNotification
    extra = 0
    readonly_fields = ("channel", "sent_at", "status", "error")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display   = ("device", "rule", "severity", "metric_value", "is_active", "triggered_at")
    list_filter    = ("severity", "is_active", "device__device_type")
    search_fields  = ("device__name",)
    readonly_fields = ("triggered_at", "resolved_at")
    inlines = [AlertNotificationInline]
