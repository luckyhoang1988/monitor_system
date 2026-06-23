from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password

from .roles import ROLE_CHOICES, ROLE_REVIEW, get_role, set_role


class UserCreateForm(UserCreationForm):
    """Tạo user mới + chọn role (admin/review)."""

    email = forms.EmailField(required=False, label="Email")
    role = forms.ChoiceField(choices=ROLE_CHOICES, initial=ROLE_REVIEW, label="Phân quyền")
    is_active = forms.BooleanField(required=False, initial=True, label="Kích hoạt")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get("email", "")
        user.is_active = self.cleaned_data.get("is_active", True)
        if commit:
            user.save()
            set_role(user, self.cleaned_data["role"])
        return user


class UserEditForm(forms.ModelForm):
    """Sửa user: email, kích hoạt, role + (tùy chọn) đặt lại mật khẩu."""

    role = forms.ChoiceField(choices=ROLE_CHOICES, label="Phân quyền")
    new_password1 = forms.CharField(
        required=False, widget=forms.PasswordInput,
        label="Mật khẩu mới", help_text="Để trống nếu không đổi mật khẩu.",
    )
    new_password2 = forms.CharField(
        required=False, widget=forms.PasswordInput, label="Nhập lại mật khẩu mới",
    )

    class Meta:
        model = User
        fields = ("username", "email", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["role"].initial = get_role(self.instance)

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("new_password1")
        p2 = cleaned.get("new_password2")
        if p1 or p2:
            if p1 != p2:
                self.add_error("new_password2", "Mật khẩu nhập lại không khớp.")
            else:
                try:
                    validate_password(p1, self.instance)
                except forms.ValidationError as exc:
                    self.add_error("new_password1", exc)
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        p1 = self.cleaned_data.get("new_password1")
        if p1:
            user.set_password(p1)
        if commit:
            user.save()
            set_role(user, self.cleaned_data["role"])
        return user
