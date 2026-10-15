from django.contrib import admin
from django.db import transaction
from apps.accounts.admin_mixins import AdminRBACMixin
from .engine import close_alerts_for_disabled_rule
from .models import AlertRule, Alert, AlertNotification, AlertConfig


@admin.register(AlertConfig)
class AlertConfigAdmin(AdminRBACMixin, admin.ModelAdmin):
    list_display = ("telegram_enabled", "telegram_chat_id", "updated_at")


@admin.register(AlertRule)
class AlertRuleAdmin(AdminRBACMixin, admin.ModelAdmin):
    list_display = ("name", "device_type", "metric", "condition", "threshold", "severity", "enabled")
    list_filter  = ("severity", "device_type", "enabled")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Cùng hệ quả với nút "Tắt rule" ở UI chính (hạng mục 1.4): rule tắt không còn được engine đánh
        # giá nên alert đang active của nó sẽ không bao giờ được resolve.
        if change and "enabled" in form.changed_data and not obj.enabled:
            # Django admin bọc cả request trong `transaction.atomic()`: đóng NGAY ở đây sẽ chạy khi
            # `enabled=False` còn chưa commit — worker vẫn đọc thấy `enabled=True` và tạo alert mới sau lần
            # đóng này (review sau 30b09aa). Chờ commit rồi mới đóng.
            by = request.user.username
            transaction.on_commit(lambda: close_alerts_for_disabled_rule(obj, by=by))


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
