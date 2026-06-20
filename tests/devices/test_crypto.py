"""Unit tests cho module mã hóa credentials."""
import pytest
from apps.devices.crypto import encrypt_value, decrypt_value, is_encrypted, _get_fernet


# Test key — chỉ dùng trong test, KHÔNG dùng production
TEST_ENCRYPTION_KEY = "test-key-for-unit-tests-only-32chars!"


@pytest.fixture(autouse=True)
def set_encryption_key(settings):
    """Set ENCRYPTION_KEY cho mọi test và clear Fernet cache."""
    _get_fernet.cache_clear()
    settings.ENCRYPTION_KEY = TEST_ENCRYPTION_KEY
    yield
    _get_fernet.cache_clear()


class TestCryptoModule:
    """Test encrypt_value / decrypt_value / is_encrypted."""

    def test_encrypt_then_decrypt_returns_original(self):
        plaintext = "my_secret_password_123!"
        ciphertext = encrypt_value(plaintext)

        assert ciphertext != plaintext
        assert len(ciphertext) > len(plaintext)
        assert decrypt_value(ciphertext) == plaintext

    def test_encrypt_empty_returns_empty(self):
        assert encrypt_value("") == ""

    def test_decrypt_empty_returns_empty(self):
        assert decrypt_value("") == ""

    def test_is_encrypted_true_for_ciphertext(self):
        ciphertext = encrypt_value("secret")
        assert is_encrypted(ciphertext) is True

    def test_is_encrypted_false_for_plaintext(self):
        assert is_encrypted("plain_password") is False

    def test_is_encrypted_false_for_empty(self):
        assert is_encrypted("") is False

    def test_encrypt_produces_different_tokens(self):
        """Fernet nên tạo ra ciphertext khác nhau mỗi lần (chứa timestamp + IV)."""
        ct1 = encrypt_value("same_input")
        ct2 = encrypt_value("same_input")
        assert ct1 != ct2  # different ciphertext
        # Nhưng decrypt đều ra cùng kết quả
        assert decrypt_value(ct1) == "same_input"
        assert decrypt_value(ct2) == "same_input"

    def test_unicode_password(self):
        """Test mã hóa mật khẩu chứa Unicode (ví dụ tiếng Việt)."""
        password = "mật_khẩu_Cisco@2024"
        ct = encrypt_value(password)
        assert decrypt_value(ct) == password

    def test_decrypt_graceful_fallback_for_plain_text(self):
        """Khi giải mã plain text (chưa được encrypt), trả về nguyên giá trị."""
        plain = "just_a_plain_password"
        result = decrypt_value(plain)
        assert result == plain


class TestEncryptedCharField:
    """Test EncryptedCharField tích hợp với Django ORM."""

    @pytest.mark.django_db
    def test_field_encrypts_on_save_and_decrypts_on_read(self):
        from apps.devices.models import Device

        device = Device.objects.create(
            name="test-encrypted-001",
            device_type="switch",
            ip_address="10.0.0.1",
            vendor="cisco",
            protocol="snmp",
            snmp_community="my_community_string",
            ssh_password="my_ssh_pass_123",
        )

        # Đọc lại từ DB → phải trả về plain text
        device.refresh_from_db()
        assert device.snmp_community == "my_community_string"
        assert device.ssh_password == "my_ssh_pass_123"

        # Kiểm tra dữ liệu RAW trong DB → phải là ciphertext
        from django.db import connection
        cursor = connection.cursor()
        cursor.execute(
            "SELECT snmp_community, ssh_password FROM devices_device WHERE id = %s",
            [device.id],
        )
        raw_community, raw_password = cursor.fetchone()

        assert raw_community != "my_community_string", "snmp_community chưa được mã hóa trong DB!"
        assert raw_password != "my_ssh_pass_123", "ssh_password chưa được mã hóa trong DB!"
        assert is_encrypted(raw_community), "snmp_community raw value không phải Fernet token!"
        assert is_encrypted(raw_password), "ssh_password raw value không phải Fernet token!"

    @pytest.mark.django_db
    def test_field_handles_empty_values(self):
        from apps.devices.models import Device

        device = Device.objects.create(
            name="test-encrypted-002",
            device_type="switch",
            ip_address="10.0.0.2",
            vendor="cisco",
            protocol="ping",
            snmp_community="",
            ssh_password="",
        )
        device.refresh_from_db()
        assert device.snmp_community == ""
        assert device.ssh_password == ""

    @pytest.mark.django_db
    def test_field_update_works(self):
        from apps.devices.models import Device

        device = Device.objects.create(
            name="test-encrypted-003",
            device_type="switch",
            ip_address="10.0.0.3",
            vendor="cisco",
            protocol="ssh",
            ssh_password="old_password",
        )

        # Cập nhật password
        device.ssh_password = "new_password_2024"
        device.save(update_fields=["ssh_password"])

        device.refresh_from_db()
        assert device.ssh_password == "new_password_2024"
