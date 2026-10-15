"""Gửi alert qua email (SMTP)."""
import logging
import smtplib

from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone

from . import ChannelNotConfigured, ChannelTransientError, port_context

logger = logging.getLogger(__name__)


def _recipients() -> list:
    """Danh sách nhận hoặc ChannelNotConfigured (trước đây `return` im lặng → engine ghi "sent" giả)."""
    recipients = getattr(settings, "ALERT_EMAIL_RECIPIENTS", [])
    if not recipients:
        raise ChannelNotConfigured("Chưa cấu hình ALERT_EMAIL_RECIPIENTS")
    return recipients


def _send(subject: str, body: str, recipients: list) -> None:
    """send_mail + phân loại lỗi SMTP. Mã 4xx và lỗi tầng kết nối là tạm thời; còn lại (xác thực sai,
    người nhận bị từ chối, 5xx) giữ nguyên exception → engine coi là vĩnh viễn như trước đây.

    Thứ tự except có chủ đích: `SMTPException` là con của `OSError` nên phải xử lý TRƯỚC `OSError`,
    nếu không `SMTPRecipientsRefused`/`SMTPAuthenticationError` bị coi nhầm là lỗi mạng."""
    try:
        send_mail(subject=subject, message=body, from_email=settings.SMTP_FROM,
                  recipient_list=recipients, fail_silently=False)
    except smtplib.SMTPResponseException as exc:
        if 400 <= exc.smtp_code < 500:
            raise ChannelTransientError(f"SMTP {exc.smtp_code}: máy chủ thư tạm thời từ chối") from None
        raise
    except smtplib.SMTPRecipientsRefused as exc:
        # KHÔNG phải SMTPResponseException: smtplib ném lớp này khi MỌI người nhận bị từ chối (hoặc ngay khi
        # gặp 421) và mã nằm trong `exc.recipients = {addr: (code, resp)}` (review 2026-10-15). Có ít nhất
        # 1 mã 4xx (hộp thư bận, greylisting) thì thử lại có thể gửi được; toàn 5xx là vĩnh viễn.
        codes = [v[0] for v in (exc.recipients or {}).values()
                 if isinstance(v, tuple) and v and isinstance(v[0], int)]
        if any(400 <= code < 500 for code in codes):
            raise ChannelTransientError(
                f"SMTP: người nhận bị từ chối tạm thời (mã {', '.join(str(c) for c in sorted(set(codes)))})"
            ) from None
        raise
    except smtplib.SMTPServerDisconnected:
        raise ChannelTransientError("SMTP: máy chủ thư ngắt kết nối giữa chừng") from None
    except smtplib.SMTPException:
        raise
    except OSError as exc:  # socket.timeout, ConnectionError, gaierror...
        raise ChannelTransientError(f"SMTP: lỗi kết nối ({type(exc).__name__})") from None


def send_email_alert(alert) -> None:
    recipients = _recipients()

    emoji = "🔴" if alert.severity == "CRITICAL" else "⚠️"
    subject = f"{emoji} [{alert.severity}] {alert.device.name} — {alert.rule.name}"
    triggered_local = timezone.localtime(alert.triggered_at)
    device_path = f"/{alert.device.device_type}/{alert.device.pk}/" if alert.device.device_type in ("switch", "router", "firewall", "hyperv") else "/"
    base = getattr(settings, "SITE_URL", "") or ""
    device_url = f"{base}{device_path}" if base else device_path
    alerts_url = f"{base}/alerts/" if base else "/alerts/"
    body = (
        f"Cảnh báo từ Monitor System\n"
        f"{'=' * 50}\n"
        f"Thiết bị : {alert.device.name} ({alert.device.ip_address})\n"
        f"Mức độ   : {alert.severity}\n"
        f"Rule     : {alert.rule.name}\n"
        f"Nội dung : {alert.message}\n"
        f"Thời gian: {triggered_local.strftime('%Y-%m-%d %H:%M:%S')} ({settings.TIME_ZONE})\n"
        f"Link thiết bị: {device_url}\n"
        f"Alerts: {alerts_url}\n"
        f"{'=' * 50}\n"
        f"Truy cập dashboard để xem chi tiết và Acknowledge.\n"
    )
    _send(subject, body, recipients)
    logger.info("Email alert sent to %s", recipients)


def send_email_recovery(alert) -> None:
    recipients = _recipients()
    subject = f"✅ [RECOVERED] {alert.device.name} — {alert.rule.name}"
    triggered_local = timezone.localtime(alert.triggered_at)
    resolved_local = timezone.localtime(alert.resolved_at) if alert.resolved_at else None
    device_path = f"/{alert.device.device_type}/{alert.device.pk}/" if alert.device.device_type in ("switch", "router", "firewall", "hyperv") else "/"
    base = getattr(settings, "SITE_URL", "") or ""
    device_url = f"{base}{device_path}" if base else device_path
    port_name, peer_name = port_context(alert)
    port_lines = ""
    if port_name:
        port_lines = f"Cổng      : {port_name}\n"
        if peer_name:
            port_lines += f"Nối tới   : {peer_name}\n"
    body = (
        f"Cảnh báo đã được giải quyết\n"
        f"{'=' * 50}\n"
        f"Thiết bị  : {alert.device.name} ({alert.device.ip_address})\n"
        f"{port_lines}"
        f"Rule      : {alert.rule.name}\n"
        f"Trigger   : {triggered_local.strftime('%Y-%m-%d %H:%M:%S')} ({settings.TIME_ZONE})\n"
        f"Recovered : {resolved_local.strftime('%Y-%m-%d %H:%M:%S') if resolved_local else 'N/A'} ({settings.TIME_ZONE})\n"
        f"Link thiết bị: {device_url}\n"
        f"{'=' * 50}\n"
    )
    _send(subject, body, recipients)
    logger.info("Recovery email sent to %s", recipients)
