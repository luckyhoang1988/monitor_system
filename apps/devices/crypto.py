"""Mã hóa/giải mã credentials (SSH password, SNMP community) bằng Fernet.

Fernet = AES-128-CBC + HMAC-SHA256.
Key được derive từ ENCRYPTION_KEY trong .env qua PBKDF2 → 32 bytes → base64 → Fernet key.

Giá trị là Fernet token mà KHÔNG giải mã được (thiếu/đổi ENCRYPTION_KEY, dữ liệu hỏng) tuyệt đối không
được trả về như thể đó là mật khẩu: collector sẽ đăng nhập bằng chuỗi `gAAAA...` (mỗi lần poll là một lần
sai mật khẩu → khoá tài khoản AD/AAA/iLO) và mọi `save()` đầy đủ sẽ mã hoá chồng lên token cũ, mất
credential vĩnh viễn. `decrypt_value` ném `CredentialDecryptError`; lớp field (fields.py) biến nó thành
`UnreadableCredential` để một dòng hỏng không làm hỏng cả queryset.
"""
import base64
import logging
import re
from collections.abc import Iterable
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Salt cố định — chỉ dùng để derive Fernet key từ user-provided passphrase.
# Thay đổi salt sẽ khiến toàn bộ dữ liệu đã mã hóa không giải mã được.
_SALT = b"monitor_system_v1_salt_2024"

# Cấu trúc Fernet token: version(1, = 0x80) + timestamp(8) + IV(16) + ciphertext(>=16, bội số 16) + HMAC(32).
_FERNET_VERSION_BYTE = 0x80
_FERNET_MIN_BYTES = 1 + 8 + 16 + 16 + 32
_URLSAFE_B64 = re.compile(r"^[A-Za-z0-9_\-]+={0,2}$")
# Phần đầu của mọi Fernet token thực tế: base64 của version byte 0x80 + 3 byte cao của timestamp (bằng 0 cho
# tới năm 2106). Chỉ dùng để NHẬN RA token đã hỏng — token hợp lệ vẫn kiểm theo cấu trúc (`looks_encrypted`).
_FERNET_HEAD = "gAAAAA"


class CredentialDecryptError(Exception):
    """Giá trị đã mã hoá nhưng không giải mã được (ENCRYPTION_KEY thiếu/sai, hoặc token hỏng).

    Thông điệp chỉ nêu nguyên nhân — KHÔNG bao giờ chứa giá trị đã lưu.
    """


class UnreadableCredential(str):
    """Giá trị của một cột mã hoá mà không giải mã được.

    Là chuỗi RỖNG (falsy) để mọi `if not device.ssh_password` hiện có từ chối đăng nhập thay vì gửi
    ciphertext đi làm mật khẩu. Mang `raw` (token gốc) để `get_prep_value` ghi ngược NGUYÊN VẸN — lưu một
    thiết bị không được phá cột này — và `reason` để báo lỗi rõ cho người vận hành.
    """

    def __new__(cls, raw: str = "", reason: str = "") -> "UnreadableCredential":
        obj = super().__new__(cls, "")
        obj.raw = raw
        obj.reason = reason
        return obj


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


def looks_encrypted(value: object) -> bool:
    """Giá trị CÓ CẤU TRÚC của một Fernet token (không cần khoá, không biết giải mã được hay không).

    Dùng để phân biệt "plain-text cũ chưa migrate" với "token của khoá khác/bị hỏng". Kiểm theo cấu trúc
    (base64 url-safe, byte đầu 0x80, đủ độ dài tối thiểu) thay vì tiền tố `gAAAAA` — tiền tố đó chỉ đúng
    khi timestamp còn nằm trong 32 bit thấp.
    """
    if not isinstance(value, str) or not value or not _URLSAFE_B64.match(value):
        return False
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except ValueError:
        return False
    return len(raw) >= _FERNET_MIN_BYTES and raw[0] == _FERNET_VERSION_BYTE


def looks_damaged_token(value: object) -> bool:
    """Không phải Fernet token hợp lệ, nhưng rõ ràng là một token đã bị hỏng (mất `=`, cắt cụt, lẫn khoảng trắng).

    Phân biệt với plain-text cũ: "không đạt kiểm tra cấu trúc" KHÔNG đủ để kết luận là mật khẩu — token mất
    padding cũng không đạt, mà đem nó đi đăng nhập hay mã hoá chồng đều phá credential. Dấu hiệu duy nhất
    dùng ở đây là phần đầu `gAAAAA`: không mật khẩu do người đặt nào bắt đầu như vậy.

    Hai chiều nhầm không đối xứng: plain-text cũ bắt đầu bằng `gAAAAA` bị từ chối (nhập lại 1 lần, form có cảnh
    báo); chiều ngược lại là gửi chuỗi rác làm mật khẩu và mã hoá chồng. Chọn chiều an toàn.
    Chưa phát hiện được token mất phần ĐẦU chuỗi (không còn dấu hiệu nào để phân biệt với plain-text).
    """
    if not isinstance(value, str) or looks_encrypted(value):
        return False
    return value.strip().startswith(_FERNET_HEAD)


def decrypt_value(ciphertext: str) -> str:
    """Giải mã ciphertext → plaintext.

    - Rỗng → "".
    - KHÔNG có cấu trúc Fernet token và không giống token hỏng (plain-text cũ chưa migrate) → trả nguyên giá trị.
    - Là Fernet token và giải mã được → plaintext.
    - Là Fernet token mà KHÔNG giải mã được (thiếu/sai ENCRYPTION_KEY, token hỏng), hoặc giống token đã hỏng
      (mất padding, cắt cụt) → `CredentialDecryptError`. Không bao giờ trả ciphertext.
    """
    if not ciphertext:
        return ""
    if not looks_encrypted(ciphertext):
        if looks_damaged_token(ciphertext):
            raise CredentialDecryptError(
                "giá trị đã lưu có dạng bản mã Fernet nhưng bị hỏng (mất padding, cắt cụt hoặc lẫn ký tự lạ) "
                "— không dùng làm mật khẩu được, cần nhập lại credential"
            )
        return ciphertext
    try:
        fernet = _get_fernet()
    except RuntimeError:
        raise CredentialDecryptError(
            "ENCRYPTION_KEY chưa được cấu hình — không giải mã được credential đã lưu"
        ) from None
    try:
        return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError):
        raise CredentialDecryptError(
            "ENCRYPTION_KEY không khớp với khoá đã dùng để mã hoá credential (hoặc dữ liệu đã hỏng)"
        ) from None


def unreadable_credentials(obj: object, fields: Iterable[str]) -> dict[str, str]:
    """{tên field: lý do} của các field mã hoá trên `obj` mà không giải mã được."""
    found: dict[str, str] = {}
    for name in fields:
        value = getattr(obj, name, None)
        if isinstance(value, UnreadableCredential):
            found[name] = value.reason or "không giải mã được"
    return found


def ensure_credentials_readable(obj: object, *fields: str) -> None:
    """Ném `CredentialDecryptError` nếu BẤT KỲ field nào trong `fields` của `obj` không giải mã được.

    Gọi TRƯỚC khi mở phiên (SNMP/SSH/WinRM/iLO): thà không đăng nhập còn hơn gửi mật khẩu sai và bị khoá
    tài khoản. Thông điệp nêu tên thiết bị + tên field + nguyên nhân, không chứa giá trị nào.
    """
    bad = unreadable_credentials(obj, fields)
    if not bad:
        return
    reasons = "; ".join(sorted(set(bad.values())))
    raise CredentialDecryptError(
        f"Thiết bị {getattr(obj, 'name', '?')}: không giải mã được {', '.join(bad)} — {reasons}. "
        "Chưa gửi credential tới thiết bị (tránh khoá tài khoản)."
    )


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
