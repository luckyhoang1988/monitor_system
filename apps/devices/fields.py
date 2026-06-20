"""Custom Django model field — tự động mã hóa/giải mã khi đọc/ghi DB."""
from django.db import models
from .crypto import encrypt_value, decrypt_value


class EncryptedCharField(models.CharField):
    """CharField tự động encrypt khi lưu vào DB và decrypt khi đọc ra.

    Hoạt động hoàn toàn trong suốt (transparent) với phần code còn lại:
    - Code Python đọc `device.ssh_password` → nhận plain text.
    - Database lưu ciphertext.
    - Admin/Form hiển thị plain text bình thường.

    max_length nên đặt >= 500 vì Fernet token dài hơn plain text gốc
    (khoảng 2-3x sau base64 encoding).
    """

    def __init__(self, *args, **kwargs):
        # Đảm bảo max_length đủ lớn cho ciphertext
        kwargs.setdefault("max_length", 500)
        super().__init__(*args, **kwargs)

    def from_db_value(self, value, expression, connection):
        """Gọi khi Django đọc giá trị từ DB → decrypt."""
        if value is None:
            return value
        return decrypt_value(value)

    def get_prep_value(self, value):
        """Gọi khi Django chuẩn bị ghi giá trị vào DB → encrypt."""
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return encrypt_value(value)

    def value_from_object(self, obj):
        """Trả về plain text khi serialize (admin, forms, dumpdata)."""
        return getattr(obj, self.attname)

    def deconstruct(self):
        """Hỗ trợ Django migrations — serialize field definition."""
        name, path, args, kwargs = super().deconstruct()
        # Trả về path đầy đủ để migration biết import từ đâu
        path = "apps.devices.fields.EncryptedCharField"
        # Loại bỏ max_length mặc định nếu giữ nguyên 500
        if kwargs.get("max_length") == 500:
            del kwargs["max_length"]
        return name, path, args, kwargs
