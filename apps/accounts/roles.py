"""Ánh xạ 2 cấp tài khoản (admin / review) sang Django Group có sẵn.

Hệ thống đã enforce write-permission qua group "Network Admins"
(apps/devices/views.py `_can_write`, apps/devices/permissions.py). Module này
là nguồn sự thật duy nhất cho việc đọc/ghi role, để UI quản lý người dùng và
phần enforce dùng chung một định nghĩa.
"""
from django.contrib.auth.models import Group, User

# Tên group đã được tạo sẵn trong migration devices/0007_create_rbac_groups.
ADMIN_GROUP = "Network Admins"
REVIEW_GROUP = "Read-Only Operators"

ROLE_ADMIN = "admin"
ROLE_REVIEW = "review"
ROLE_CHOICES = [
    (ROLE_ADMIN, "Admin — toàn quyền (thêm/sửa/xóa, quản lý người dùng)"),
    (ROLE_REVIEW, "Review — chỉ xem"),
]
ROLE_LABELS = {ROLE_ADMIN: "Admin", ROLE_REVIEW: "Review"}


def is_admin(user) -> bool:
    """True nếu user là admin: superuser HOẶC thuộc group Network Admins."""
    return bool(
        user
        and user.is_authenticated
        and (user.is_superuser or user.groups.filter(name=ADMIN_GROUP).exists())
    )


def get_role(user) -> str:
    """Trả về 'admin' hoặc 'review' cho 1 user."""
    return ROLE_ADMIN if is_admin(user) else ROLE_REVIEW


def set_role(user: User, role: str) -> None:
    """Gán role bằng cách bật/tắt group tương ứng (idempotent)."""
    admin_g, _ = Group.objects.get_or_create(name=ADMIN_GROUP)
    review_g, _ = Group.objects.get_or_create(name=REVIEW_GROUP)
    if role == ROLE_ADMIN:
        user.groups.add(admin_g)
        user.groups.remove(review_g)
    else:
        user.groups.add(review_g)
        user.groups.remove(admin_g)


def active_admin_count(exclude_pk: int | None = None) -> int:
    """Số admin đang active (superuser hoặc Network Admins). Dùng để chặn xóa/hạ
    quyền admin cuối cùng gây khóa hệ thống."""
    from django.db.models import Q

    qs = User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(groups__name=ADMIN_GROUP)
    ).distinct()
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.count()
