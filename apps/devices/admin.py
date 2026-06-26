from django.contrib import admin
from apps.accounts.admin_mixins import AdminRBACMixin
from .models import Device, Interface, TopologyLink


class InterfaceInline(admin.TabularInline):
    model = Interface
    extra = 0
    fields = ("if_index", "name", "description", "is_uplink")
    readonly_fields = ("if_index",)


@admin.register(TopologyLink)
class TopologyLinkAdmin(AdminRBACMixin, admin.ModelAdmin):
    list_display = (
        "local_device", "local_port", "link_kind", "remote_device",
        "remote_ap_name", "remote_ap_mac",
        "match_method", "is_confirmed", "is_stale", "last_seen",
    )
    list_filter = ("link_kind", "is_confirmed", "is_stale", "match_method", "local_device")
    search_fields = ("local_port", "remote_ap_name", "remote_ap_mac", "remote_sys_name")
    readonly_fields = ("first_seen", "last_seen")
    autocomplete_fields = ("local_device", "remote_device")


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
