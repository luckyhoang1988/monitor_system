"""Gửi alert qua Telegram Bot API."""
import hashlib
import html
import logging

from django.conf import settings
from django.utils import timezone

from . import ChannelNotConfigured, post_json

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _esc(value) -> str:
    """HTML-escape giá trị động (tên thiết bị/AP/rule có thể chứa _ * ` [ <).

    Dùng parse_mode=HTML thay vì Markdown legacy: Markdown vỡ khi text có số
    ký tự _ hoặc * lẻ (vd 'ACL_Wlan: ... (X2_GiuaX2_ITRoom)') → 400 Bad Request
    và alert không bao giờ gửi được.
    """
    return html.escape(str(value), quote=False)


def _resolve_chat_id() -> str:
    """Chat ID nhận cảnh báo: ưu tiên AlertConfig (UI), fallback .env."""
    try:
        from apps.alerts.models import AlertConfig
        cfg = AlertConfig.load()
        if not cfg.telegram_enabled:
            return ""
        if cfg.telegram_chat_id.strip():
            return cfg.telegram_chat_id.strip()
    except Exception:
        logger.exception("Đọc AlertConfig lỗi — fallback .env")
    return getattr(settings, "TELEGRAM_CHAT_ID", "")


def cooldown_key() -> str | None:
    """Khoá cooldown dùng chung theo bot + chat (xem apps/alerts/cooldown.py). Băm token — không lưu bí mật
    vào DB. None khi chưa đủ thông tin đăng nhập (lúc đó gửi sẽ ném ChannelNotConfigured, không cần chờ)."""
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = _resolve_chat_id()
    if not token or not chat_id:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"telegram:{digest}:{chat_id}"


def _credentials() -> tuple[str, str]:
    """(token, chat_id) hoặc ChannelNotConfigured — trước đây `return` im lặng và engine ghi "sent" giả."""
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ChannelNotConfigured("Telegram chưa cấu hình: thiếu TELEGRAM_BOT_TOKEN")
    chat_id = _resolve_chat_id()
    if not chat_id:
        raise ChannelNotConfigured(
            "Telegram chưa sẵn sàng: kênh đang tắt trong Cấu hình cảnh báo hoặc thiếu Chat ID"
        )
    return token, chat_id


def send_telegram_alert(alert) -> None:
    token, chat_id = _credentials()

    emoji = "🔴" if alert.severity == "CRITICAL" else "⚠️"
    triggered_local = timezone.localtime(alert.triggered_at)
    device_path = f"/{alert.device.device_type}/{alert.device.pk}/" if alert.device.device_type in ("switch", "router", "firewall", "hyperv") else "/"
    base = getattr(settings, "SITE_URL", "") or ""
    device_url = f"{base}{device_path}" if base else device_path
    alerts_url = f"{base}/alerts/" if base else "/alerts/"
    text = (
        f"{emoji} <b>{_esc(alert.severity)}</b>\n"
        f"<b>Thiết bị:</b> <code>{_esc(alert.device.name)}</code> ({_esc(alert.device.ip_address)})\n"
        f"<b>Rule:</b> {_esc(alert.rule.name)}\n"
        f"<b>Chi tiết:</b> {_esc(alert.message)}\n"
        f"<b>Thời gian:</b> {triggered_local.strftime('%H:%M:%S %d/%m/%Y')} ({_esc(settings.TIME_ZONE)})\n"
        f"<b>Link:</b> {_esc(device_url)}\n"
        f"<b>Alerts:</b> {_esc(alerts_url)}"
    )

    _post(token, chat_id, text)
    logger.info("Telegram alert sent (chat_id=%s)", chat_id)


def _post(token: str, chat_id: str, text: str) -> None:
    # post_json phân loại lỗi (429 + retry_after / 5xx / mạng → tạm thời; 4xx → vĩnh viễn) và KHÔNG để
    # URL `/bot<token>/sendMessage` lọt vào thông điệp lỗi.
    post_json(TELEGRAM_API.format(token=token),
              {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, service="Telegram")


def send_telegram_recovery(alert) -> None:
    token, chat_id = _credentials()

    resolved_str = (timezone.localtime(alert.resolved_at).strftime("%H:%M:%S %d/%m/%Y")
                    if alert.resolved_at else "N/A")
    device_path = f"/{alert.device.device_type}/{alert.device.pk}/" if alert.device.device_type in ("switch", "router", "firewall", "hyperv") else "/"
    base = getattr(settings, "SITE_URL", "") or ""
    device_url = f"{base}{device_path}" if base else device_path
    text = (
        f"✅ <b>RECOVERED</b>\n"
        f"<b>Thiết bị:</b> <code>{_esc(alert.device.name)}</code> ({_esc(alert.device.ip_address)})\n"
        f"<b>Rule:</b> {_esc(alert.rule.name)}\n"
        f"<b>Recovered lúc:</b> {resolved_str} ({_esc(settings.TIME_ZONE)})\n"
        f"<b>Link:</b> {_esc(device_url)}"
    )
    _post(token, chat_id, text)
    logger.info("Recovery telegram sent (chat_id=%s)", chat_id)
