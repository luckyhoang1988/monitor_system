"""Gửi alert qua email (SMTP)."""
import logging
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def send_email_alert(alert) -> None:
    recipients = getattr(settings, "ALERT_EMAIL_RECIPIENTS", [])
    if not recipients:
        logger.warning("ALERT_EMAIL_RECIPIENTS chưa cấu hình — bỏ qua email")
        return

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
    send_mail(subject=subject, message=body,
              from_email=settings.SMTP_FROM, recipient_list=recipients,
              fail_silently=False)
    logger.info("Email alert sent to %s", recipients)


def send_email_recovery(alert) -> None:
    recipients = getattr(settings, "ALERT_EMAIL_RECIPIENTS", [])
    if not recipients:
        return
    subject = f"✅ [RECOVERED] {alert.device.name} — {alert.rule.name}"
    triggered_local = timezone.localtime(alert.triggered_at)
    resolved_local = timezone.localtime(alert.resolved_at) if alert.resolved_at else None
    device_path = f"/{alert.device.device_type}/{alert.device.pk}/" if alert.device.device_type in ("switch", "router", "firewall", "hyperv") else "/"
    base = getattr(settings, "SITE_URL", "") or ""
    device_url = f"{base}{device_path}" if base else device_path
    body = (
        f"Cảnh báo đã được giải quyết\n"
        f"{'=' * 50}\n"
        f"Thiết bị  : {alert.device.name} ({alert.device.ip_address})\n"
        f"Rule      : {alert.rule.name}\n"
        f"Trigger   : {triggered_local.strftime('%Y-%m-%d %H:%M:%S')} ({settings.TIME_ZONE})\n"
        f"Recovered : {resolved_local.strftime('%Y-%m-%d %H:%M:%S') if resolved_local else 'N/A'} ({settings.TIME_ZONE})\n"
        f"Link thiết bị: {device_url}\n"
        f"{'=' * 50}\n"
    )
    send_mail(subject=subject, message=body,
              from_email=settings.SMTP_FROM, recipient_list=recipients,
              fail_silently=False)
    logger.info("Recovery email sent to %s", recipients)
