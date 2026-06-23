from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden

from .roles import is_admin


def admin_required(view_func):
    """Yêu cầu đăng nhập + là admin. Chưa login → redirect login; login nhưng
    không phải admin → 403."""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_admin(request.user):
            return HttpResponseForbidden(
                "Bạn không có quyền truy cập chức năng quản lý người dùng."
            )
        return view_func(request, *args, **kwargs)

    return login_required(_wrapped)
