"""Custom Django model field — tự động mã hóa/giải mã khi đọc/ghi DB."""
import logging

from django.db import models

from .crypto import CredentialDecryptError, UnreadableCredential, decrypt_value, encrypt_value

logger = logging.getLogger(__name__)

# Mỗi (field, lý do) chỉ cảnh báo 1 lần/tiến trình: field được nạp mỗi lần query Device (hàng chục lần/phút).
# Việc báo lỗi mỗi poll do collector đảm nhiệm (ensure_credentials_readable), không phải lớp field.
_warned: set[tuple[str, str]] = set()


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
        """Gọi khi Django đọc giá trị từ DB → decrypt.

        Token không giải mã được KHÔNG làm hỏng việc nạp model (1 dòng hỏng sẽ hạ cả dashboard và
        mọi lượt poll): trả `UnreadableCredential` — chuỗi rỗng mang token gốc. Collector từ chối
        đăng nhập bằng `ensure_credentials_readable`.
        """
        if value is None:
            return value
        try:
            return decrypt_value(value)
        except CredentialDecryptError as exc:
            key = (self.name, str(exc))
            if key not in _warned:
                _warned.add(key)
                logger.error(
                    "Trường %s: giá trị đã lưu không giải mã được (%s) — thiết bị dùng credential này "
                    "sẽ không được đăng nhập cho tới khi khắc phục nguyên nhân trên hoặc nhập lại credential",
                    self.name, exc,
                )
            return UnreadableCredential(value, str(exc))

    def get_prep_value(self, value):
        """Gọi khi Django chuẩn bị ghi giá trị vào DB → encrypt.

        Giá trị không đọc được được ghi ngược nguyên vẹn (token gốc), không mã hoá chồng lên.
        """
        if isinstance(value, UnreadableCredential):
            return value.raw
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return encrypt_value(value)

    def save_form_data(self, instance, data):
        """ModelForm/admin: ô để trống không được xoá một credential đang không đọc được.

        Nhập giá trị mới thì thay thế bình thường.
        """
        if not data and isinstance(getattr(instance, self.attname, None), UnreadableCredential):
            return
        setattr(instance, self.attname, data)

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
