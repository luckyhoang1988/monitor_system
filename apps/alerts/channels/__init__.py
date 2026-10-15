"""Hợp đồng lỗi chung của các channel thông báo (Telegram/Slack/Teams/Email).

Một channel khi gửi KHÔNG thành công phải nói rõ nó thuộc loại nào — engine dựa vào đó để quyết định
thử lại hay bỏ (apps/alerts/engine.py `_settle_failed_attempt`):

* `ChannelTransientError` — lỗi tạm thời (429/5xx/timeout/mất kết nối), thử lại được; `retry_after` (giây)
  nếu bên kia chỉ định (Telegram `parameters.retry_after`, header `Retry-After`).
* `ChannelPermanentError` — thử lại cũng vô ích (4xx: sai chat id, webhook bị thu hồi, group đã nâng
  cấp supergroup...).
* `ChannelNotConfigured` — thiếu cấu hình (token/chat id/URL/recipient). Trước đây channel `return` im
  lặng và engine ghi "sent" giả.

Thông điệp của các lớp này do channel TỰ DỰNG, không lấy từ `str(requests.RequestException)`: requests
nhét nguyên URL vào thông điệp (`.../bot<token>/sendMessage`, URL webhook) và nó từng lọt vào
`AlertNotification.error` + log (294 dòng trên prod đo 2026-10-15).

Số đo prod (309 thông báo failed): 429 = 172, kết nối/pool = 124, SoftTimeLimit = 12, 4xx = 1.
"""
from __future__ import annotations

import re

import requests

MAX_DETAIL_LEN = 200


class ChannelError(Exception):
    """Gốc chung — str(exc) luôn là thông điệp tiếng Việt an toàn (không token/URL)."""


class ChannelNotConfigured(ChannelError):
    pass


class ChannelPermanentError(ChannelError):
    pass


class ChannelTransientError(ChannelError):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# Bot token Telegram: "<bot_id>:<35 ký tự>". URL (webhook Slack/Teams, api.telegram.org/bot<token>/...)
# mang bí mật trong path → che cả path, giữ lại host để còn đọc được lỗi của dịch vụ nào.
_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}")
_URL_PATH_RE = re.compile(r"(https?://[^/\s'\"<>]+)/[^\s'\"<>]*")


def redact(text: object) -> str:
    """Che token bot và path URL khỏi 1 chuỗi trước khi lưu DB/ghi log."""
    out = _URL_PATH_RE.sub(r"\1/<đã-che>", str(text))
    return _BOT_TOKEN_RE.sub("<token-đã-che>", out)


# Cấu hình sai (không phải sự cố tạm thời): thử lại không giúp gì.
_CONFIG_REQUEST_ERRORS = (
    requests.exceptions.InvalidURL,
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
    requests.exceptions.URLRequired,
    requests.exceptions.InvalidHeader,
)


def _json_body(resp) -> dict:
    try:
        body = resp.json()
    except Exception:  # không phải JSON (Slack trả text thuần) hoặc body rỗng
        return {}
    return body if isinstance(body, dict) else {}


def _to_seconds(value) -> int | None:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _retry_after(resp, body: dict) -> int | None:
    params = body.get("parameters")
    if isinstance(params, dict):
        seconds = _to_seconds(params.get("retry_after"))  # Telegram ResponseParameters.retry_after
        if seconds:
            return seconds
    headers = getattr(resp, "headers", None) or {}
    return _to_seconds(headers.get("Retry-After"))  # Slack/Teams; dạng HTTP-date → bỏ qua


def post_json(url: str, payload: dict, *, service: str, timeout: int = 10):
    """POST JSON, trả response khi 2xx/3xx; còn lại ném lỗi đã phân loại (xem docstring module)."""
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except _CONFIG_REQUEST_ERRORS:
        raise ChannelPermanentError(f"{service}: URL gửi không hợp lệ — kiểm tra cấu hình") from None
    except requests.Timeout:
        raise ChannelTransientError(f"{service}: hết thời gian chờ phản hồi") from None
    except requests.ConnectionError:
        raise ChannelTransientError(f"{service}: không kết nối được") from None
    except requests.RequestException as exc:
        raise ChannelTransientError(f"{service}: lỗi mạng ({type(exc).__name__})") from None

    if resp.ok:
        return resp

    status = resp.status_code
    body = _json_body(resp)
    detail = redact(body.get("description") or "")[:MAX_DETAIL_LEN]
    params = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}

    if status == 429 or status == 408 or status >= 500:
        retry_after = _retry_after(resp, body)
        suffix = f" (chờ {retry_after}s)" if retry_after else ""
        raise ChannelTransientError(f"{service} HTTP {status}{suffix}", retry_after=retry_after)

    if params.get("migrate_to_chat_id"):
        raise ChannelPermanentError(
            f"{service} HTTP {status}: nhóm đã nâng cấp thành supergroup — cập nhật Chat ID mới "
            f"({params['migrate_to_chat_id']})"
        )
    raise ChannelPermanentError(f"{service} HTTP {status}" + (f": {detail}" if detail else ""))
