from rest_framework import permissions

class IsAdminOrReadOnly(permissions.BasePermission):
    """
    Cho phép Read-Only Operators xem.
    Chỉ Network Admins hoặc Superuser mới có quyền sửa/xóa/thêm.
    """

    def has_permission(self, request, view):
        # Ai cũng có quyền Read nếu đã đăng nhập
        if request.method in permissions.SAFE_METHODS:
            return True
            
        # Write permission: Superuser hoặc thuộc nhóm Network Admins
        if request.user and request.user.is_superuser:
            return True
            
        if request.user and request.user.groups.filter(name='Network Admins').exists():
            return True
            
        return False
