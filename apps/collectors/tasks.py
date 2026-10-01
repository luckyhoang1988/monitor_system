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
    from apps.realtime.publisher import publish_device_event

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
            publish_device_event(device, online=False, data=None)
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
        now = timezone.now()
        device.last_seen = now
        # last_ok_seen chỉ ghi khi poll THÀNH CÔNG, KHÔNG bị xoá khi lỗi → làm mốc
        # grace cho cảnh báo offline (xem Device.is_online_for_alert). Tránh báo giả.
        device.last_ok_seen = now
        update_fields.append("last_ok_seen")
    else:
        # Đồng bộ is_online với kết quả poll/SSE — tránh badge Off nhưng thẻ đếm vẫn "on".
        # CHỈ xoá last_seen (hiển thị); GIỮ NGUYÊN last_ok_seen để alert có grace.
        device.last_seen = None
    update_fields.append("last_seen")
    if update_fields:
        device.save(update_fields=update_fields)

    # Phát event realtime SAU khi commit (ngoài atomic của save_metrics).
    publish_device_event(device, online, data)

    # Đánh giá alert ngay sau khi poll xong để notification (Telegram/Email) sát dữ liệu
    # hơn, thay vì đợi task evaluate_alert_rules chạy lệch pha. Beat task vẫn giữ làm safety net.
    try:
        from django.utils import timezone as _tz
        from datetime import timedelta
        from apps.alerts.engine import check_device_alerts

        window_minutes = getattr(settings, "ALERT_EVAL_WINDOW_MINUTES", 10)
        since = _tz.now() - timedelta(minutes=int(window_minutes))
        check_device_alerts(device, since)
    except Exception as exc:
        logger.warning("Inline alert eval failed for %s: %s", device.name, exc)

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


# Trần cho TOÀN BỘ batch poll_all_hyperv — task chạy inline nhiều host trong 1 lời gọi,
# KHÔNG qua poll_device nên không có soft/hard time_limit per-device bảo vệ. 1 host WinRM
# treo có thể chiếm tới ~280s (2 script host+volume × 2 endpoint http/https × operation
# 60s/read 70s timeout mỗi cái) — không giới hạn tổng thì 1 host xấu đủ nuốt hết chu kỳ,
# y hệt cơ chế "poll queue snowball" đã fix cho poll_device (commit fe1dac1) nhưng CHƯA
# từng áp cho nhánh hyperv (gap ghi nhận ở memory poll-queue-snowball-slow-device.md
# 2026-07-07, chưa fix).
# ⚠️ 100s/110s (bản 2026-07-07) tính cho **2 host healthy** (~52.5s đo thật). Dính thật
# 2026-09-28: thêm host thứ 3 (Hyprver03) + Hyperv-02 đang có sự cố RAID/HpSAMD thật
# (xem memory hyperv02-winrm-instability.md, ssacli/cage đĩa 2 lỏng) khiến 1 lệnh WinRM
# trên Hyperv-02 có thể tự treo tới hết timeout socket riêng (60-70s) DÙ đã hết soft
# time_limit — SoftTimeLimitExceeded là 1 exception Python (SystemExit) chỉ raise được
# khi interpreter quay lại bytecode; nếu đang kẹt trong 1 lệnh blocking dài hơn khoảng
# (hard-soft) còn lại thì Celery buộc SIGKILL cả task (log "ERROR/MainProcess Hard time
# limit exceeded") — không "Polled X"/"succeeded" nào được ghi, mọi host trong vòng đó
# (kể cả Hyperv-01/Hyprver03 đang khoẻ) mất trắng 1 chu kỳ → last_seen rớt quá grace →
# Offline giả. Verify runtime: 09:35-10:11 hard-kill liên tục ~10 lần/36 phút. Fix: nới
# rộng hẳn (không chỉ tăng nhẹ) + tăng POLL_HYPERV_INTERVAL_SECS đi kèm (300s, xem
# config/settings/base.py) để có margin thật cho 3 host + 1 host đang bệnh, tránh vá lại
# đúng bẫy này khi fleet tăng tiếp. Trước khi thêm host HyperV thứ 4+ — đo lại timing thật
# rồi mới quyết định giữ nguyên hay tách kiến trúc (dispatch mỗi host 1 task riêng).
POLL_HYPERV_BATCH_SOFT_LIMIT = 250
POLL_HYPERV_BATCH_HARD_LIMIT = 270


@shared_task(
    soft_time_limit=POLL_HYPERV_BATCH_SOFT_LIMIT,
    time_limit=POLL_HYPERV_BATCH_HARD_LIMIT,
)
def poll_all_hyperv() -> None:
    from django.conf import settings
    from apps.devices.models import Device
    device_ids = list(Device.objects.filter(
        device_type="hyperv",
        enabled=True,
    ).values_list('pk', flat=True))
    # Chạy inline để tránh mất task poll_device trong môi trường Celery/Windows không ổn định.
    success = 0
    failed = 0
    t0 = timezone.now()
    try:
        for pk in device_ids:
            try:
                _poll_device_once(pk)
                success += 1
            except Exception as exc:
                failed += 1
                logger.warning("Inline HyperV poll failed for device %s: %s", pk, exc)
    except SoftTimeLimitExceeded:
        elapsed = (timezone.now() - t0).total_seconds()
        logger.warning(
            "poll_all_hyperv vượt soft_time_limit %ds sau %d/%d host (elapsed=%.1fs) — "
            "dừng batch, host còn lại chờ chu kỳ sau",
            POLL_HYPERV_BATCH_SOFT_LIMIT, success + failed, len(device_ids), elapsed,
        )
        return
    elapsed = (timezone.now() - t0).total_seconds()
    logger.info("Polled %d/%d hyperv hosts inline (failed=%d, elapsed=%.1fs)",
                success, len(device_ids), failed, elapsed)
    # Get-Counter burst (~10-12s/host) thêm tải đáng kể — tự cảnh báo nếu tick áp sát
    # interval, tránh lặp sự cố "poll queue snowball" (memory poll-queue-snowball-slow-device.md).
    interval = getattr(settings, "POLL_HYPERV_INTERVAL_SECS", 120)
    if interval and elapsed > interval * 0.5:
        logger.warning(
            "HyperV poll tick chiếm %.1fs — vượt 50%% của interval %ds, cân nhắc tăng "
            "POLL_HYPERV_INTERVAL_SECS hoặc giảm MaxSamples trong Get-Counter burst.",
            elapsed, interval,
        )


TOPOLOGY_DISCOVER_SOFT_LIMIT = 120
TOPOLOGY_DISCOVER_HARD_LIMIT = 150


@shared_task(
    soft_time_limit=TOPOLOGY_DISCOVER_SOFT_LIMIT,
    time_limit=TOPOLOGY_DISCOVER_HARD_LIMIT,
)
def discover_topology_links() -> None:
    """Walk LLDP trên mọi switch SNMP → upsert TopologyLink (chu kỳ 30 phút)."""
    from apps.collectors.topology_writer import discover_all_switches

    stats = discover_all_switches()
    logger.info(
        "Topology discovery done: switches=%d links=%d confirmed=%d errors=%d",
        stats["switches"], stats["links"], stats["confirmed"], stats["errors"],
    )


# Trần cho TOÀN BỘ batch poll_all_ilo — chạy inline nhiều host trong 1 lời gọi (giống
# poll_all_hyperv), KHÔNG qua poll_device nên cần soft/hard time_limit riêng ở tầng batch.
# Redfish GET nhanh hơn WinRM PowerShell rất nhiều (REST đơn giản, không burst Get-Counter)
# nên giới hạn thấp hơn nhiều — đo lại timing thật sau khi có vài chu kỳ chạy thật trên prod,
# tăng nếu fleet iLO mở rộng (theo đúng bài học poll_all_hyperv — không ngoại suy tuyến tính).
POLL_ILO_BATCH_SOFT_LIMIT = 60
POLL_ILO_BATCH_HARD_LIMIT = 70


def _set_ilo_error(pk: int, message: str) -> None:
    """Ghi/xoá lỗi poll iLO gần nhất lên Device (update() — không đụng field khác/last_seen)."""
    from apps.devices.models import Device
    try:
        Device.objects.filter(pk=pk).update(
            ilo_last_error=message[:200],
            ilo_last_error_at=timezone.now() if message else None,
        )
    except Exception:
        logger.warning("Không ghi được ilo_last_error cho device id=%s", pk, exc_info=True)


@shared_task(
    soft_time_limit=POLL_ILO_BATCH_SOFT_LIMIT,
    time_limit=POLL_ILO_BATCH_HARD_LIMIT,
)
def poll_all_ilo() -> None:
    """Poll RAID/disk health qua iLO Redfish cho HyperV host có cấu hình ilo_ip_address.

    Độc lập hoàn toàn _poll_device_once/WinRM (task/chu kỳ riêng) — KHÔNG đụng
    device.last_seen/is_online: iLO reachable/unreachable là tín hiệu khác với online/offline
    của _poll_device_once, không được lẫn vào semantics last_seen/last_ok_seen đã tách bạch
    (xem CLAUDE.md "Online/offline"). Host chưa cấu hình ilo_ip_address (để trống) tự bị lọc
    ra ở query, không lỗi.
    """
    from apps.devices.models import Device
    from apps.collectors.ilo_redfish import IloRedfishClient
    from apps.metrics.models import HardwareHealth

    # LƯU Ý: GenericIPAddressField (Postgres inet) không lưu được "" — Django tự coi "" là
    # None khi build query. .exclude(ilo_ip_address__isnull=True) là ĐỦ; thêm
    # .exclude(ilo_ip_address="") sinh SQL "NOT (x = NULL AND x IS NOT NULL)" luôn NULL cho
    # mọi row -> loại bỏ hết kể cả row có IP hợp lệ (verify runtime 2026-09-28, dính bug này
    # ở đúng bước probe trước khi code — xem CLAUDE.md/deploy skill).
    device_ids = list(
        Device.objects.filter(device_type="hyperv", enabled=True)
        .exclude(ilo_ip_address__isnull=True)
        .values_list("pk", flat=True)
    )
    success = 0
    failed = 0
    t0 = timezone.now()
    try:
        for pk in device_ids:
            try:
                device = Device.objects.get(pk=pk)
                client = IloRedfishClient(device)
                raw = client.collect_raw()
                if raw is None:
                    failed += 1
                    _set_ilo_error(pk, client.last_error or "Poll iLO thất bại")
                    continue
                data = client.normalize(raw)
                HardwareHealth.objects.create(device=device, timestamp=timezone.now(), **data)
                success += 1
                if device.ilo_last_error or device.ilo_last_error_at:
                    _set_ilo_error(pk, "")
            except Exception as exc:
                failed += 1
                _set_ilo_error(pk, f"Lỗi xử lý: {type(exc).__name__}")
                logger.warning("iLO poll lỗi device id=%s: %s", pk, exc, exc_info=True)
    except SoftTimeLimitExceeded:
        elapsed = (timezone.now() - t0).total_seconds()
        logger.warning(
            "poll_all_ilo vượt soft_time_limit %ds sau %d/%d host (elapsed=%.1fs) — "
            "dừng batch, host còn lại chờ chu kỳ sau",
            POLL_ILO_BATCH_SOFT_LIMIT, success + failed, len(device_ids), elapsed,
        )
        return
    elapsed = (timezone.now() - t0).total_seconds()
    logger.info("Polled %d/%d iLO hosts (failed=%d, elapsed=%.1fs)", success, len(device_ids), failed, elapsed)
