from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render

from .decorators import admin_required
from .forms import UserCreateForm, UserEditForm
from .roles import ROLE_ADMIN, ROLE_LABELS, active_admin_count, get_role


@admin_required
def user_list(request):
    users = list(User.objects.all().order_by("-is_active", "username"))
    for u in users:
        u.role_code = get_role(u)
        u.role_label = ROLE_LABELS[u.role_code]
    return render(request, "accounts/list.html", {
        "users": users,
        "total": len(users),
    })


@admin_required
def user_add(request):
    form = UserCreateForm(request.POST or None)
    if form.is_valid():
        user = form.save()
        messages.success(request, f"Đã tạo người dùng “{user.username}”.")
        return redirect("accounts:list")
    return render(request, "accounts/form.html", {"form": form, "title": "Thêm người dùng"})


@admin_required
def user_edit(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    editing_self = user_obj.pk == request.user.pk
    form = UserEditForm(request.POST or None, instance=user_obj)
    if form.is_valid():
        new_role = form.cleaned_data["role"]
        new_active = form.cleaned_data["is_active"]
        # Chặn tự khóa: không cho hạ quyền / vô hiệu hóa chính mình.
        if editing_self and (new_role != ROLE_ADMIN or not new_active):
            messages.error(request, "Bạn không thể tự hạ quyền hoặc vô hiệu hóa tài khoản của chính mình.")
            return render(request, "accounts/form.html",
                          {"form": form, "title": f"Sửa: {user_obj.username}", "user_obj": user_obj})
        # Chặn hạ quyền/khóa admin cuối cùng.
        is_currently_admin = get_role(user_obj) == ROLE_ADMIN
        will_be_admin = new_role == ROLE_ADMIN and new_active
        if is_currently_admin and not will_be_admin and active_admin_count(exclude_pk=user_obj.pk) == 0:
            messages.error(request, "Đây là admin hoạt động cuối cùng — không thể hạ quyền hoặc khóa.")
            return render(request, "accounts/form.html",
                          {"form": form, "title": f"Sửa: {user_obj.username}", "user_obj": user_obj})
        form.save()
        messages.success(request, f"Đã cập nhật người dùng “{user_obj.username}”.")
        return redirect("accounts:list")
    return render(request, "accounts/form.html",
                  {"form": form, "title": f"Sửa: {user_obj.username}", "user_obj": user_obj})


@admin_required
def user_delete(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    if user_obj.pk == request.user.pk:
        messages.error(request, "Bạn không thể tự xóa tài khoản của chính mình.")
        return redirect("accounts:list")
    if get_role(user_obj) == ROLE_ADMIN and active_admin_count(exclude_pk=user_obj.pk) == 0:
        messages.error(request, "Đây là admin hoạt động cuối cùng — không thể xóa.")
        return redirect("accounts:list")
    if request.method == "POST":
        username = user_obj.username
        user_obj.delete()
        messages.success(request, f"Đã xóa người dùng “{username}”.")
        return redirect("accounts:list")
    return render(request, "accounts/confirm_delete.html", {"user_obj": user_obj})


@login_required
def password_change_self(request):
    """Đổi mật khẩu của chính mình — dành cho mọi user (kể cả review)."""
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)  # giữ phiên đăng nhập sau khi đổi
        messages.success(request, "Đã đổi mật khẩu thành công.")
        return redirect("accounts:password_change")
    return render(request, "accounts/password_change.html", {"form": form})
