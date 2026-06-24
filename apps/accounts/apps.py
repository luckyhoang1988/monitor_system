from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Tài khoản & Người dùng"

    def ready(self):
        from django.contrib import admin
        from apps.accounts.roles import is_admin

        def has_permission(request):
            return request.user.is_active and is_admin(request.user)

        admin.site.has_permission = has_permission
