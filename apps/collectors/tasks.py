"""Celery tasks — polling định kỳ và lưu metrics vào DB."""
import logging
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

logger = logging.getLogger(__name__)

# Trần thời gian cho 1 lần poll_device — chặn 1 thiết bị chậm/SNMP treo
# ngốn worker hàng trăm giây (từng thấy ~151s) làm nghẽn cả queue.
POLL_DEVICE_SOFT_LIMIT = 45   # raise SoftTimeLimitExceeded để task tự dừng "mềm"
POLL_DEVICE_HARD_LIMIT = 60   # bị kill cứng nếu soft limit không kịp dừng


ICMP_DEVICE_TYPES = ("switch", "router", "firewall", "nas", "ap")


def _has_valid_data(device, data) -> bool:
    """Dữ liệu SNMP/SSH có 'thật' không (tránh coi poll rỗng là thành công).

    - protocol=ping (AP, ...): online dựa trên kết quả ping của PingCollector
      (adapt() set cpu=0.0 khi online, -1.0 khi offline).
    - switch/router/firewall: phải có >=1 interface (walk thành công).
    - thiết bị khác (hyperv, wlan_controller...): chỉ cần collect không lỗi.
    """
    if device.protocol == "ping":
        return data is not None and data.cpu_percent >= 0
    if device.device_type == "nas":
        # NAS: hợp lệ khi có interface HOẶC đọc được memory (Synology qua UCD-SNMP).
        return len(data.interfaces) > 0 or data.mem_percent > 0
    if device.device_type in ICMP_DEVICE_TYPES:
        return len(data.interfaces) > 0
    return data is not None


def _poll_device_once(device_id: int) -> None:
    from django.conf import settings
    from apps.devices.models import Device
    from apps.collectors.factory import CollectorFactory
    from apps.collectors.ping_util import icmp_ping
    from apps.metrics.writer import save_metrics

    device = Device.objects.get(pk=device_id)
    collector = CollectorFactory.create(device)

    # ICMP độc lập với SNMP — chỉ áp dụng cho thiết bị mạng poll bằng SNMP/SSH.
    # Thiết bị protocol=ping tự ping trong PingCollector nên không cần lớp ICMP riêng.
    require_icmp = (
        bool(getattr(settings, "ONLINE_REQUIRE_ICMP", True))
        and device.device_type in ICMP_DEVICE_TYPES
        and device.protocol != "ping"
    )
    icmp_ok, rtt = (None, None)
    if require_icmp:
        icmp_ok, rtt = icmp_ping(
            device.ip_address,
            timeout_secs=int(getattr(settings, "PING_TIMEOUT_SECS", 1)),
        )
        # ICMP fail ⇒ thiết bị mạng chắc chắn offline (online = snmp_valid AND icmp_ok).
        # Bỏ qua collect SNMP đắt đỏ để 1 thiết bị chết không treo worker tới ~240s.
        if not icmp_ok:
            device.last_seen = None
            device.save(update_fields=["last_seen"])
            logger.info(
                "Polled %s — online=False (icmp down, bỏ qua SNMP collect)",
                device.name,
            )
            return

    data = None
    snmp_valid = False
    try:
        data = collector.collect()
        if require_icmp:
            data.extra["ping_ok"] = bool(icmp_ok)
            data.extra["ping_rtt_ms"] = rtt
        save_metrics(device, data)
        device.os_family = data.os_family
        snmp_valid = _has_valid_data(device, data)
    except Exception as exc:
        logger.warning("Poll collect failed %s (%s): %s", device.name, device.ip_address, exc)

    # Online = kết hợp. Với thiết bị mạng: CẢ ping VÀ SNMP-thật (AND).
    online = (snmp_valid and bool(icmp_ok)) if require_icmp else snmp_valid

    update_fields = []
    if data is not None:
        update_fields.append("os_family")
    if online:
        device.last_seen = timezone.now()
        update_fields.append("last_seen")
    if update_fields:
        device.save(update_fields=update_fields)

    logger.info(
        "Polled %s — online=%s (snmp_valid=%s icmp=%s) ifaces=%s",
        device.name, online, snmp_valid, icmp_ok,
        len(data.interfaces) if data is not None else "n/a",
    )


@shared_task(
    bind=True,
    max_retries=1,
    default_retry_delay=30,
    soft_time_limit=POLL_DEVICE_SOFT_LIMIT,
    time_limit=POLL_DEVICE_HARD_LIMIT,
)
def poll_device(self, device_id: int) -> None:
    from apps.devices.models import Device

    try:
        _poll_device_once(device_id)
    except Device.DoesNotExist:
        logger.error("Device id=%d không tồn tại", device_id)
    except SoftTimeLimitExceeded:
        # Thiết bị quá chậm (>%ds) — coi như offline lần này. KHÔNG retry để
        # tránh khuếch đại backlog; vòng poll kế tiếp sẽ thử lại.
        logger.warning(
            "Poll device id=%d vượt soft_time_limit %ds — bỏ qua, không retry",
            device_id, POLL_DEVICE_SOFT_LIMIT,
        )
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
    from django.conf import settings
    from apps.devices.models import Device
    device_ids = list(Device.objects.filter(
        device_type__in=["switch", "router", "firewall", "nas", "wlan_controller"],
        enabled=True,
        protocol__in=["snmp", "ssh"],
    ).values_list('pk', flat=True))
    # expires = 1 chu kỳ: task chưa chạy kịp trước vòng kế thì tự rớt,
    # tránh đùn đống poll_device cũ làm nghẽn queue (snowball).
    expires = int(getattr(settings, "POLL_NETWORK_INTERVAL_SECS", 120))
    for pk in device_ids:
        poll_device.apply_async(args=[pk], expires=expires)
    logger.info("Dispatched poll tasks for %d network devices (snmp/ssh)", len(device_ids))


@shared_task
def poll_all_ping_devices() -> None:
    """Poll thiết bị dùng giao thức ping mỗi 3 phút."""
    from django.conf import settings
    from apps.devices.models import Device
    device_ids = list(Device.objects.filter(
        device_type__in=["switch", "router", "firewall", "nas", "ap"],
        enabled=True,
        protocol="ping",
    ).values_list("pk", flat=True))
    expires = int(getattr(settings, "POLL_PING_INTERVAL_SECS", 120))
    for pk in device_ids:
        poll_device.apply_async(args=[pk], expires=expires)
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
