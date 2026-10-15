"""Celery tasks — polling định kỳ và lưu metrics vào DB."""
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime

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
        device.os_family = data.os_family
        snmp_valid = _has_valid_data(device, data)
    except Exception as exc:
        logger.warning("Poll collect failed %s (%s): %s", device.name, device.ip_address, exc)

    # Ghi metrics là việc của HỆ THỐNG, KHÔNG phải bằng chứng thiết bị có trả lời hay không (1.5,
    # 2026-10-15). Trước đây `save_metrics` nằm chung try với `collect()`: lỗi ghi (vd DataError vì
    # ifAlias dài hơn cột, DB nghẽn) giữ snmp_valid=False → last_seen=None → badge Off + alert
    # device_online giả dù SNMP khoẻ. Nay lỗi ghi chỉ mất điểm dữ liệu của vòng này, online giữ nguyên.
    if data is not None:
        try:
            save_metrics(device, data)
        except SoftTimeLimitExceeded:
            # Nuốt có chủ đích, không báo ERROR giả: thiết bị đã trả lời xong. Batch (hyperv/iLO)
            # KHÔNG dựa vào exception này để dừng — nó tự dừng theo deadline (xem poll_all_hyperv).
            logger.warning("Device %s: ghi metrics bị cắt bởi soft_time_limit", device.name)
        except Exception:
            logger.exception(
                "Device %s: ghi metrics thất bại — giữ nguyên trạng thái online của lần poll này",
                device.name,
            )

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
# time_limit — Celery SIGKILL cả task khi quá hard limit (log "ERROR/MainProcess Hard time
# limit exceeded"): không "Polled X"/"succeeded" nào được ghi, host chưa poll mất trắng 1
# chu kỳ. Verify runtime: 09:35-10:11 hard-kill liên tục ~10 lần/36 phút. Fix 2026-09-28:
# nới rộng hẳn (không chỉ tăng nhẹ) + tăng POLL_HYPERV_INTERVAL_SECS đi kèm (300s, xem
# config/settings/base.py) để có margin thật cho 3 host + 1 host đang bệnh. Trước khi thêm
# host HyperV thứ 4+ — đo lại timing thật rồi mới quyết định giữ nguyên hay tách kiến trúc
# (dispatch mỗi host 1 task riêng).
# ⚠️ Cơ chế hard-kill còn lại (1.7, 2026-10-15; đã chạy `__mro__`): SoftTimeLimitExceeded kế
# thừa `Exception`, KHÔNG phải SystemExit — nên bị `except Exception` nuốt (trong collect() của
# _poll_device_once, trong inline alert eval, và trong chính vòng lặp này) và batch CHẠY TIẾP
# sau soft limit cho tới hard-kill (prod 10,5 ngày: 8 soft-limit → 6 hard-kill, batch tới 269,8s).
# Vì vậy batch KHÔNG dựa vào exception để dừng: vòng lặp tự kiểm deadline bằng đồng hồ trước
# mỗi host (xem POLL_HYPERV_BATCH_START_MARGIN) và host lần trước lỗi được poll cuối.
POLL_HYPERV_BATCH_SOFT_LIMIT = 250
POLL_HYPERV_BATCH_HARD_LIMIT = 270
# Không BẮT ĐẦU host mới khi đồng hồ đã quá (soft − margin). Đo prod 2026-10-15 (5 lượt liên
# tiếp, 3 host khoẻ): 16–19s/host, batch 51,4–52,1s → 60s ≈ 3× một host khoẻ. Host đã bắt đầu
# trước deadline mà treo thì vẫn chịu soft limit (host đó coi như lỗi lần này, như trước).
POLL_HYPERV_BATCH_START_MARGIN = 60


def _monotonic() -> float:
    """Đồng hồ cho deadline của batch — tách hàm để test điều khiển được, không patch time toàn cục."""
    return time.monotonic()


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _order_hosts_healthy_first(
    rows: Iterable[tuple[int, str, bool, datetime | None]],
) -> list[tuple[int, str]]:
    """Thứ tự host trong 1 batch: host lần trước poll THÀNH CÔNG đứng trước, host lần trước lỗi cuối.

    rows = (pk, name, failed_last_time, last_ok_at). Host bệnh (treo tới hết timeout) dồn xuống cuối
    để nếu batch bị cắt thì cắt đúng host đó, không cắt host khoẻ. Trong mỗi nhóm, host LÂU CHƯA poll
    thành công nhất đứng trước (host bị deadline bỏ qua ở vòng trước không bị bỏ đói), rồi theo pk
    cho ổn định. KHÔNG dùng `-last_ok_seen` thuần: host bệnh vừa thành công 1 lần có last_ok_seen mới
    nhất → luôn đứng đầu, treo 250s, các host khoẻ phía sau bị bỏ đói liên tục.
    """
    ordered = sorted(rows, key=lambda r: (r[2], r[3] or _EPOCH, r[0]))
    return [(pk, name) for pk, name, _failed, _ok_at in ordered]


def _warn_batch_cut(
    task: str, soft_limit: int, margin: int, done: int, remaining: list[str], elapsed: float,
) -> None:
    logger.warning(
        "%s dừng sớm theo deadline %ds (soft_limit %ds − margin %ds) sau %d host, bỏ qua %d host "
        "chưa bắt đầu: %s — chờ chu kỳ sau (elapsed=%.1fs)",
        task, soft_limit - margin, soft_limit, margin, done, len(remaining), ", ".join(remaining), elapsed,
    )


@shared_task(
    soft_time_limit=POLL_HYPERV_BATCH_SOFT_LIMIT,
    time_limit=POLL_HYPERV_BATCH_HARD_LIMIT,
)
def poll_all_hyperv() -> None:
    from django.conf import settings
    from apps.devices.models import Device
    rows = Device.objects.filter(device_type="hyperv", enabled=True).values_list(
        "pk", "name", "last_seen", "last_ok_seen",
    )
    # last_seen=None ⇔ lần poll trước lỗi (xem _poll_device_once: poll lỗi luôn xoá last_seen).
    hosts = _order_hosts_healthy_first(
        (pk, name, last_seen is None, last_ok_seen) for pk, name, last_seen, last_ok_seen in rows
    )
    # Chạy inline để tránh mất task poll_device trong môi trường Celery/Windows không ổn định.
    success = 0
    failed = 0
    started = _monotonic()
    deadline = started + POLL_HYPERV_BATCH_SOFT_LIMIT - POLL_HYPERV_BATCH_START_MARGIN
    try:
        for index, (pk, _name) in enumerate(hosts):
            if _monotonic() >= deadline:
                _warn_batch_cut(
                    "poll_all_hyperv", POLL_HYPERV_BATCH_SOFT_LIMIT, POLL_HYPERV_BATCH_START_MARGIN,
                    success + failed, [n for _pk, n in hosts[index:]], _monotonic() - started,
                )
                break
            try:
                _poll_device_once(pk)
                success += 1
            except SoftTimeLimitExceeded:
                raise  # kế thừa Exception — phải đứng TRƯỚC `except Exception`, nếu không batch chạy tiếp
            except Exception as exc:
                failed += 1
                logger.warning("Inline HyperV poll failed for device %s: %s", pk, exc)
    except SoftTimeLimitExceeded:
        logger.warning(
            "poll_all_hyperv vượt soft_time_limit %ds sau %d/%d host (elapsed=%.1fs) — "
            "dừng batch, host còn lại chờ chu kỳ sau",
            POLL_HYPERV_BATCH_SOFT_LIMIT, success + failed, len(hosts), _monotonic() - started,
        )
        return
    elapsed = _monotonic() - started
    logger.info("Polled %d/%d hyperv hosts inline (failed=%d, elapsed=%.1fs)",
                success, len(hosts), failed, elapsed)
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
# Không BẮT ĐẦU host mới khi đồng hồ đã quá (soft − margin) — cùng cơ chế poll_all_hyperv (1.7).
# Đo prod 2026-10-15 (5 lượt liên tiếp, 3 iLO khoẻ): ~10s/host, batch 30,2–30,4s → 20s = 2× một host.
POLL_ILO_BATCH_START_MARGIN = 20


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
    rows = (
        Device.objects.filter(device_type="hyperv", enabled=True)
        .exclude(ilo_ip_address__isnull=True)
        .values_list("pk", "name", "ilo_last_error")
    )
    # Host lần poll iLO trước lỗi (ilo_last_error còn) đứng cuối. Device không lưu mốc "iLO thành công
    # gần nhất" nên trong mỗi nhóm giữ thứ tự pk — chấp nhận: fleet 3 host, deadline chỉ chạm khi
    # 1 iLO treo, mà iLO treo thì đã bị đẩy xuống cuối bởi ilo_last_error.
    hosts = _order_hosts_healthy_first(
        (pk, name, bool(ilo_last_error), None) for pk, name, ilo_last_error in rows
    )
    success = 0
    failed = 0
    started = _monotonic()
    deadline = started + POLL_ILO_BATCH_SOFT_LIMIT - POLL_ILO_BATCH_START_MARGIN
    try:
        for index, (pk, _name) in enumerate(hosts):
            if _monotonic() >= deadline:
                _warn_batch_cut(
                    "poll_all_ilo", POLL_ILO_BATCH_SOFT_LIMIT, POLL_ILO_BATCH_START_MARGIN,
                    success + failed, [n for _pk, n in hosts[index:]], _monotonic() - started,
                )
                break
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
            except SoftTimeLimitExceeded:
                # Hết giờ của batch KHÔNG phải lỗi của iLO: không ghi lên Device (UI sẽ hiện "Mất kết nối")
                # và không đi tiếp — kế thừa Exception nên phải đứng TRƯỚC `except Exception`.
                raise
            except Exception as exc:
                failed += 1
                _set_ilo_error(pk, f"Lỗi xử lý: {type(exc).__name__}")
                logger.warning("iLO poll lỗi device id=%s: %s", pk, exc, exc_info=True)
    except SoftTimeLimitExceeded:
        logger.warning(
            "poll_all_ilo vượt soft_time_limit %ds sau %d/%d host (elapsed=%.1fs) — "
            "dừng batch, host còn lại chờ chu kỳ sau",
            POLL_ILO_BATCH_SOFT_LIMIT, success + failed, len(hosts), _monotonic() - started,
        )
        return
    elapsed = _monotonic() - started
    logger.info("Polled %d/%d iLO hosts (failed=%d, elapsed=%.1fs)", success, len(hosts), failed, elapsed)
