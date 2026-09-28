"""Celery task đánh giá alert rules sau mỗi poll cycle."""
import logging
from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task
def evaluate_alert_rules() -> None:
    from apps.devices.models import Device
    from apps.metrics.models import SystemHealth, InterfaceStats
    from .engine import check_device_alerts
    from django.utils import timezone
    from datetime import timedelta

    window_minutes = getattr(settings, "ALERT_EVAL_WINDOW_MINUTES", 10)
    since = timezone.now() - timedelta(minutes=window_minutes)
    devices = Device.objects.filter(enabled=True)
    for device in devices:
        try:
            check_device_alerts(device, since)
        except Exception as exc:
            logger.error("Alert check failed for %s: %s", device.name, exc)


@shared_task
def retry_pending_alert_notifications() -> None:
    """Safety net cho transactional-outbox-lite (xem apps/alerts/engine.py _fire_alert/
    _resolve_alert, 2026-09-28): nhặt lại AlertNotification status="pending" bị kẹt do worker
    chết giữa lúc commit trạng thái Alert và lúc gửi thật — không có cơ chế nào khác tự phát
    hiện/gửi lại các row này (evaluate_alert_rules coi alert đã is_active đúng là "đã xử lý",
    không biết notification có gửi thành công hay không)."""
    from .engine import retry_pending_alert_notifications as _retry
    try:
        _retry()
    except Exception as exc:
        logger.error("retry_pending_alert_notifications failed: %s", exc)
