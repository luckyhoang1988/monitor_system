from .roles import get_role, is_admin


def user_role(request):
    """Cung cấp is_admin_user / user_role cho mọi template (gate menu, nút...)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"is_admin_user": False, "user_role": None}
    return {"is_admin_user": is_admin(user), "user_role": get_role(user)}
