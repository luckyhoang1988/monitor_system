"""Mixin giới hạn Django Admin cho group Network Admins (RBAC 2 cấp)."""
from apps.accounts.roles import is_admin


class AdminRBACMixin:
    """Chỉ admin (Network Admins / superuser) được truy cập Django Admin."""

    def has_module_permission(self, request):
        return is_admin(request.user)

    def has_view_permission(self, request, obj=None):
        return is_admin(request.user)

    def has_add_permission(self, request):
        return is_admin(request.user)

    def has_change_permission(self, request, obj=None):
        return is_admin(request.user)

    def has_delete_permission(self, request, obj=None):
        return is_admin(request.user)
