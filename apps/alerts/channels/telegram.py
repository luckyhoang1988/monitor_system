"""Gửi alert qua Telegram Bot API."""
import html
import logging
import requests
from django.conf import settings
from django.utils import timezone

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


def send_telegram_alert(alert) -> None:
    token   = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = _resolve_chat_id()

    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID chưa cấu hình")
        return

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

    url  = TELEGRAM_API.format(token=token)
    resp = requests.post(url, json={"chat_id": chat_id, "text": text,
                                    "parse_mode": "HTML"}, timeout=10)
    resp.raise_for_status()
    logger.info("Telegram alert sent (chat_id=%s)", chat_id)


def send_telegram_recovery(alert) -> None:
    token   = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = _resolve_chat_id()
    if not token or not chat_id:
        return

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
    url  = TELEGRAM_API.format(token=token)
    resp = requests.post(url, json={"chat_id": chat_id, "text": text,
                                    "parse_mode": "HTML"}, timeout=10)
    resp.raise_for_status()
    logger.info("Recovery telegram sent (chat_id=%s)", chat_id)
