"""Celery tasks — polling định kỳ và lưu metrics vào DB."""
import json
import logging
import time
from pathlib import Path
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)
DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "debug-f05be0.log"


def _debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": "f05be0",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _poll_device_once(device_id: int) -> None:
    from apps.devices.models import Device
    from apps.collectors.factory import CollectorFactory
    from apps.metrics.writer import save_metrics

    device = Device.objects.get(pk=device_id)
    # region agent log
    _debug_log(
        run_id="pre-fix-hyperv",
        hypothesis_id="HYP-1",
        location="apps/collectors/tasks.py:40",
        message="poll_device started",
        data={
            "device_id": device.id,
            "device_type": device.device_type,
            "protocol": device.protocol,
            "enabled": device.enabled,
            "collect_interval": device.collect_interval,
            "last_seen_is_null": device.last_seen is None,
        },
    )
    # endregion
    collector = CollectorFactory.create(device)
    # region agent log
    _debug_log(
        run_id="pre-fix-hyperv",
        hypothesis_id="HYP-2",
        location="apps/collectors/tasks.py:55",
        message="collector created",
        data={"device_id": device.id, "collector": collector.__class__.__name__},
    )
    # endregion
    data = collector.collect()
    save_metrics(device, data)
    device.last_seen = timezone.now()
    device.os_family = data.os_family
    device.save(update_fields=["last_seen", "os_family"])
    # region agent log
    _debug_log(
        run_id="pre-fix-hyperv",
        hypothesis_id="HYP-4",
        location="apps/collectors/tasks.py:66",
        message="poll_device success updated last_seen",
        data={
            "device_id": device.id,
            "os_family": data.os_family,
            "last_seen": device.last_seen.isoformat(),
        },
    )
    # endregion
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
        # region agent log
        _debug_log(
            run_id="pre-fix-hyperv",
            hypothesis_id="HYP-3",
            location="apps/collectors/tasks.py:82",
            message="poll_device exception",
            data={
                "device_id": device_id,
                "attempt": self.request.retries + 1,
                "exc_type": type(exc).__name__,
                "exc_msg": str(exc)[:300],
            },
        )
        # endregion
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
    # region agent log
    _debug_log(
        run_id="pre-fix-hyperv",
        hypothesis_id="HYP-5",
        location="apps/collectors/tasks.py:124",
        message="poll_all_hyperv dispatching",
        data={"hyperv_count": len(device_ids), "device_ids": device_ids[:20]},
    )
    # endregion
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


