from django.contrib import admin
from apps.accounts.admin_mixins import AdminRBACMixin
from .models import Device, Interface


class InterfaceInline(admin.TabularInline):
    model = Interface
    extra = 0
    fields = ("if_index", "name", "description", "is_uplink")
    readonly_fields = ("if_index",)


@admin.register(Device)
class DeviceAdmin(AdminRBACMixin, admin.ModelAdmin):
    list_display  = ("name", "device_type", "ip_address", "vendor", "os_family", "enabled", "last_seen")
    list_filter   = ("device_type", "vendor", "enabled")
    search_fields = ("name", "ip_address", "location")
    readonly_fields = ("os_family", "last_seen", "created_at")
    inlines = [InterfaceInline]
    fieldsets = (
        ("Thông tin cơ bản", {
            "fields": ("name", "device_type", "ip_address", "vendor", "os_family", "location", "enabled")
        }),
        ("Kết nối", {
            "fields": ("protocol", "snmp_version", "snmp_community", "ssh_username", "ssh_password")
        }),
        ("Cấu hình monitor", {
            "fields": ("collect_interval", "uplink_ports", "notes")
        }),
        ("Thông tin hệ thống", {
            "fields": ("last_seen", "created_at"),
            "classes": ("collapse",)
        }),
    )
