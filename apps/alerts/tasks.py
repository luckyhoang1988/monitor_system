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
