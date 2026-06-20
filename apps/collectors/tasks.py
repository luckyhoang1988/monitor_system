"""Celery tasks — polling định kỳ và lưu metrics vào DB."""
import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


def _poll_device_once(device_id: int) -> None:
    from apps.devices.models import Device
    from apps.collectors.factory import CollectorFactory
    from apps.metrics.writer import save_metrics

    device = Device.objects.get(pk=device_id)
    collector = CollectorFactory.create(device)
    data = collector.collect()
    save_metrics(device, data)
    device.last_seen = timezone.now()
    device.os_family = data.os_family
    device.save(update_fields=["last_seen", "os_family"])
    logger.info("Polled %s (%s) — CPU %.1f%% MEM %.1f%%",
                device.name, data.os_family, data.cpu_percent, data.mem_percent)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def poll_device(self, device_id: int) -> None:
    from apps.devices.models import Device

    try:
        _poll_device_once(device_id)
    except Device.DoesNotExist:
        logger.error("Device id=%d không tồn tại", device_id)
    except Exception as exc:
        logger.warning("Poll failed %s (attempt %d): %s",
                       device_id, self.request.retries + 1, exc)
        raise self.retry(exc=exc)


@shared_task
def poll_all_switches() -> None:
    """Giữ backward-compat — gọi poll_all_network_devices."""
    poll_all_network_devices.delay()


@shared_task
def poll_all_network_devices() -> None:
    """Poll thiết bị mạng SNMP/SSH (không bao gồm ping)."""
    from apps.devices.models import Device
    device_ids = list(Device.objects.filter(
        device_type__in=["switch", "router", "firewall"],
        enabled=True,
        protocol__in=["snmp", "ssh"],
    ).values_list('pk', flat=True))
    for pk in device_ids:
        poll_device.delay(pk)
    logger.info("Dispatched poll tasks for %d network devices (snmp/ssh)", len(device_ids))


@shared_task
def poll_all_ping_devices() -> None:
    """Poll thiết bị dùng giao thức ping mỗi 3 phút."""
    from apps.devices.models import Device
    device_ids = list(Device.objects.filter(
        device_type__in=["switch", "router", "firewall"],
        enabled=True,
        protocol="ping",
    ).values_list("pk", flat=True))
    for pk in device_ids:
        poll_device.delay(pk)
    logger.info("Dispatched poll tasks for %d ping devices", len(device_ids))


@shared_task
def poll_all_hyperv() -> None:
    from apps.devices.models import Device
    device_ids = list(Device.objects.filter(
        device_type="hyperv",
        enabled=True,
    ).values_list('pk', flat=True))
    # Chạy inline để tránh mất task poll_device trong môi trường Celery/Windows không ổn định.
    success = 0
    failed = 0
    for pk in device_ids:
        try:
            _poll_device_once(pk)
            success += 1
        except Exception as exc:
            failed += 1
            logger.warning("Inline HyperV poll failed for device %s: %s", pk, exc)
    logger.info("Polled %d/%d hyperv hosts inline (failed=%d)", success, len(device_ids), failed)
