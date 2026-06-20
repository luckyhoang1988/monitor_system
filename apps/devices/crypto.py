"""Mã hóa/giải mã credentials (SSH password, SNMP community) bằng Fernet.

Fernet = AES-128-CBC + HMAC-SHA256.
Key được derive từ ENCRYPTION_KEY trong .env qua PBKDF2 → 32 bytes → base64 → Fernet key.
"""
import base64
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Salt cố định — chỉ dùng để derive Fernet key từ user-provided passphrase.
# Thay đổi salt sẽ khiến toàn bộ dữ liệu đã mã hóa không giải mã được.
_SALT = b"monitor_system_v1_salt_2024"


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Tạo Fernet instance từ ENCRYPTION_KEY (cache singleton)."""
    from django.conf import settings

    raw_key = getattr(settings, "ENCRYPTION_KEY", "")
    if not raw_key:
        raise RuntimeError(
            "ENCRYPTION_KEY chưa được cấu hình trong .env. "
            "Hãy chạy: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "và thêm kết quả vào file .env"
        )

    # Derive a 32-byte key from any-length passphrase
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=480_000,
    )
    derived = kdf.derive(raw_key.encode("utf-8"))
    fernet_key = base64.urlsafe_b64encode(derived)
    return Fernet(fernet_key)


def encrypt_value(plaintext: str) -> str:
    """Mã hóa chuỗi plaintext → ciphertext base64 string.

    Trả về chuỗi rỗng nếu input rỗng (không cần mã hóa).
    """
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_value(ciphertext: str) -> str:
    """Giải mã ciphertext → plaintext.

    Trả về chuỗi rỗng nếu input rỗng.
    Nếu giải mã thất bại (key sai hoặc dữ liệu chưa được mã hóa),
    trả về nguyên giá trị gốc kèm cảnh báo log.
    """
    if not ciphertext:
        return ""
    try:
        plaintext = _get_fernet().decrypt(ciphertext.encode("utf-8"))
        return plaintext.decode("utf-8")
    except (InvalidToken, Exception) as exc:
        # Graceful fallback: nếu dữ liệu chưa được mã hóa (plain text cũ),
        # trả về nguyên giá trị để hệ thống vẫn hoạt động trong quá trình migration.
        logger.warning(
            "Không thể giải mã giá trị (có thể là plain text chưa migrate): %s",
            type(exc).__name__,
        )
        return ciphertext


def is_encrypted(value: str) -> bool:
    """Kiểm tra xem giá trị đã được mã hóa Fernet chưa.

    Fernet tokens bắt đầu bằng 'gAAAAA' (base64 của version byte + timestamp).
    """
    if not value:
        return False
    try:
        _get_fernet().decrypt(value.encode("utf-8"))
        return True
    except Exception:
        return False
