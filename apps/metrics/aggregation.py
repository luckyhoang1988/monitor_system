"""Aggregation — rollup raw metrics thành bảng hourly/daily.

Chiến lược:
- Hourly rollup: chạy mỗi giờ, gom data raw cũ hơn 2 giờ thành avg/max theo giờ.
  Giữ lại 2 giờ gần nhất (buffer) để tránh rollup data chưa hoàn chỉnh.
- Daily rollup: chạy mỗi ngày (3:30AM), gom hourly data cũ hơn 2 ngày thành avg/max theo ngày.
- Cleanup raw: sau khi rollup hourly thành công, xóa raw data đã được rollup
  (giữ lại raw data trong 48 giờ gần nhất).
"""
import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Max, Count, Sum
from django.db.models.functions import TruncHour, TruncDate
from django.utils import timezone

from . import cache as metrics_cache

logger = logging.getLogger(__name__)

RAW_RETENTION_HOURS = 48
HOURLY_BUFFER_HOURS = getattr(settings, "HOURLY_ROLLUP_BUFFER_HOURS", 2)
DAILY_BUFFER_DAYS   = getattr(settings, "DAILY_ROLLUP_BUFFER_DAYS", 1)
# Cache-mode: mỗi lần rollup gom vài giờ đã hoàn tất gần nhất (idempotent upsert,
# tự lành nếu 1 lần chạy bị trượt). Không cần quét hết buffer 26h.
ROLLUP_EXTRA_LOOKBACK_HOURS = 3


def _hour_floor(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _rollup_system_health_hourly_cache() -> int:
    """Cache-mode: gom ring-buffer sys (Redis) → SystemHealthHourly."""
    from .models import SystemHealthHourly
    from apps.devices.models import Device

    now = timezone.now()
    cutoff_ts = (now - timedelta(hours=HOURLY_BUFFER_HOURS)).timestamp()
    window_start = now - timedelta(hours=HOURLY_BUFFER_HOURS + ROLLUP_EXTRA_LOOKBACK_HOURS)

    objs = []
    for dev_id in Device.objects.filter(enabled=True).values_list("id", flat=True):
        buckets: dict = defaultdict(list)
        for s in metrics_cache.get_sys_series(dev_id, window_start):
            ts = s.get("ts")
            if ts is None or ts >= cutoff_ts:
                continue
            buckets[_hour_floor(metrics_cache.epoch_to_dt(ts))].append(s)
        for hour, rows in buckets.items():
            cpus = [r["cpu"] for r in rows if r.get("cpu") is not None]
            mems = [r["mem"] for r in rows if r.get("mem") is not None]
            if not cpus and not mems:
                continue
            objs.append(SystemHealthHourly(
                device_id=dev_id,
                hour=hour,
                cpu_avg=round(sum(cpus) / len(cpus), 2) if cpus else 0,
                cpu_max=round(max(cpus), 2) if cpus else 0,
                mem_avg=round(sum(mems) / len(mems), 2) if mems else 0,
                mem_max=round(max(mems), 2) if mems else 0,
                sample_count=len(rows),
            ))
    if objs:
        SystemHealthHourly.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["device", "hour"],
            update_fields=["cpu_avg", "cpu_max", "mem_avg", "mem_max", "sample_count"],
        )
    logger.info("Hourly rollup SystemHealth (cache): %d records processed", len(objs))
    return len(objs)


def _rollup_interface_stats_hourly_cache() -> int:
    """Cache-mode: gom ring-buffer interface (Redis) → InterfaceStatsHourly."""
    from .models import InterfaceStatsHourly
    from apps.devices.models import Interface

    now = timezone.now()
    cutoff_ts = (now - timedelta(hours=HOURLY_BUFFER_HOURS)).timestamp()
    window_start = now - timedelta(hours=HOURLY_BUFFER_HOURS + ROLLUP_EXTRA_LOOKBACK_HOURS)

    objs = []
    for if_id in Interface.objects.values_list("id", flat=True):
        series = metrics_cache.get_if_series(if_id, window_start)
        if not series:
            continue
        buckets: dict = defaultdict(list)
        for s in series:
            ts = s.get("ts")
            if ts is None or ts >= cutoff_ts:
                continue
            buckets[_hour_floor(metrics_cache.epoch_to_dt(ts))].append(s)
        for hour, rows in buckets.items():
            ins = [r.get("in_mbps") or 0.0 for r in rows]
            outs = [r.get("out_mbps") or 0.0 for r in rows]
            objs.append(InterfaceStatsHourly(
                interface_id=if_id,
                hour=hour,
                in_mbps_avg=round(sum(ins) / len(ins), 3),
                in_mbps_max=round(max(ins), 3),
                out_mbps_avg=round(sum(outs) / len(outs), 3),
                out_mbps_max=round(max(outs), 3),
                # errors là counter tích luỹ → lấy max (giá trị mới nhất trong giờ).
                in_errors=max((r.get("in_errors") or 0) for r in rows),
                out_errors=max((r.get("out_errors") or 0) for r in rows),
                sample_count=len(rows),
            ))
    if objs:
        InterfaceStatsHourly.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["interface", "hour"],
            update_fields=["in_mbps_avg", "in_mbps_max", "out_mbps_avg",
                           "out_mbps_max", "in_errors", "out_errors", "sample_count"],
        )
    logger.info("Hourly rollup InterfaceStats (cache): %d records processed", len(objs))
    return len(objs)


def rollup_system_health_hourly() -> int:
    """Rollup SystemHealth raw → SystemHealthHourly.

    Returns: số lượng hourly records đã tạo/cập nhật.
    """
    if metrics_cache.is_cache_mode():
        return _rollup_system_health_hourly_cache()

    from .models import SystemHealth, SystemHealthHourly

    cutoff = timezone.now() - timedelta(hours=HOURLY_BUFFER_HOURS)

    # Tìm các giờ chưa được rollup: có raw data nhưng chưa có hourly record
    # Hoặc có raw data mới hơn hourly record (cần re-rollup)
    aggregated = (
        SystemHealth.objects
        .filter(timestamp__lt=cutoff)
        .annotate(hour=TruncHour("timestamp"))
        .values("device_id", "hour")
        .annotate(
            cpu_avg=Avg("cpu_percent"),
            cpu_max=Max("cpu_percent"),
            mem_avg=Avg("mem_percent"),
            mem_max=Max("mem_percent"),
            sample_count=Count("id"),
        )
    )

    objs = [
        SystemHealthHourly(
            device_id=row["device_id"],
            hour=row["hour"],
            cpu_avg=round(row["cpu_avg"], 2),
            cpu_max=round(row["cpu_max"], 2),
            mem_avg=round(row["mem_avg"], 2),
            mem_max=round(row["mem_max"], 2),
            sample_count=row["sample_count"],
        )
        for row in aggregated
    ]
    if objs:
        SystemHealthHourly.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["device", "hour"],
            update_fields=["cpu_avg", "cpu_max", "mem_avg", "mem_max", "sample_count"],
        )

    logger.info("Hourly rollup SystemHealth: %d records processed", len(objs))
    return len(objs)


def rollup_interface_stats_hourly() -> int:
    """Rollup InterfaceStats raw → InterfaceStatsHourly.

    Returns: số lượng hourly records đã tạo/cập nhật.
    """
    if metrics_cache.is_cache_mode():
        return _rollup_interface_stats_hourly_cache()

    from .models import InterfaceStats, InterfaceStatsHourly

    cutoff = timezone.now() - timedelta(hours=HOURLY_BUFFER_HOURS)

    aggregated = (
        InterfaceStats.objects
        .filter(timestamp__lt=cutoff)
        .annotate(hour=TruncHour("timestamp"))
        .values("interface_id", "hour")
        .annotate(
            in_mbps_avg=Avg("in_mbps"),
            in_mbps_max=Max("in_mbps"),
            out_mbps_avg=Avg("out_mbps"),
            out_mbps_max=Max("out_mbps"),
            in_errors_sum=Sum("in_errors"),
            out_errors_sum=Sum("out_errors"),
            sample_count=Count("id"),
        )
    )

    objs = [
        InterfaceStatsHourly(
            interface_id=row["interface_id"],
            hour=row["hour"],
            in_mbps_avg=round(row["in_mbps_avg"], 3),
            in_mbps_max=round(row["in_mbps_max"], 3),
            out_mbps_avg=round(row["out_mbps_avg"], 3),
            out_mbps_max=round(row["out_mbps_max"], 3),
            in_errors=row["in_errors_sum"] or 0,
            out_errors=row["out_errors_sum"] or 0,
            sample_count=row["sample_count"],
        )
        for row in aggregated
    ]
    if objs:
        InterfaceStatsHourly.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["interface", "hour"],
            update_fields=["in_mbps_avg", "in_mbps_max", "out_mbps_avg",
                           "out_mbps_max", "in_errors", "out_errors", "sample_count"],
        )

    logger.info("Hourly rollup InterfaceStats: %d records processed", len(objs))
    return len(objs)


def rollup_system_health_daily() -> int:
    """Rollup SystemHealthHourly → SystemHealthDaily.

    Returns: số lượng daily records đã tạo/cập nhật.
    """
    from .models import SystemHealthHourly, SystemHealthDaily

    cutoff = timezone.now() - timedelta(days=DAILY_BUFFER_DAYS)

    aggregated = (
        SystemHealthHourly.objects
        .filter(hour__lt=cutoff)
        .annotate(day=TruncDate("hour"))
        .values("device_id", "day")
        .annotate(
            cpu_avg=Avg("cpu_avg"),
            cpu_max=Max("cpu_max"),
            mem_avg=Avg("mem_avg"),
            mem_max=Max("mem_max"),
            sample_count=Sum("sample_count"),
        )
    )

    objs = [
        SystemHealthDaily(
            device_id=row["device_id"],
            day=row["day"],
            cpu_avg=round(row["cpu_avg"], 2),
            cpu_max=round(row["cpu_max"], 2),
            mem_avg=round(row["mem_avg"], 2),
            mem_max=round(row["mem_max"], 2),
            sample_count=row["sample_count"],
        )
        for row in aggregated
    ]
    if objs:
        SystemHealthDaily.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["device", "day"],
            update_fields=["cpu_avg", "cpu_max", "mem_avg", "mem_max", "sample_count"],
        )

    logger.info("Daily rollup SystemHealth: %d records processed", len(objs))
    return len(objs)


def rollup_interface_stats_daily() -> int:
    """Rollup InterfaceStatsHourly → InterfaceStatsDaily.

    Returns: số lượng daily records đã tạo/cập nhật.
    """
    from .models import InterfaceStatsHourly, InterfaceStatsDaily

    cutoff = timezone.now() - timedelta(days=DAILY_BUFFER_DAYS)

    aggregated = (
        InterfaceStatsHourly.objects
        .filter(hour__lt=cutoff)
        .annotate(day=TruncDate("hour"))
        .values("interface_id", "day")
        .annotate(
            in_mbps_avg=Avg("in_mbps_avg"),
            in_mbps_max=Max("in_mbps_max"),
            out_mbps_avg=Avg("out_mbps_avg"),
            out_mbps_max=Max("out_mbps_max"),
            in_errors_sum=Sum("in_errors"),
            out_errors_sum=Sum("out_errors"),
            sample_count=Sum("sample_count"),
        )
    )

    objs = [
        InterfaceStatsDaily(
            interface_id=row["interface_id"],
            day=row["day"],
            in_mbps_avg=round(row["in_mbps_avg"], 3),
            in_mbps_max=round(row["in_mbps_max"], 3),
            out_mbps_avg=round(row["out_mbps_avg"], 3),
            out_mbps_max=round(row["out_mbps_max"], 3),
            in_errors=row["in_errors_sum"] or 0,
            out_errors=row["out_errors_sum"] or 0,
            sample_count=row["sample_count"],
        )
        for row in aggregated
    ]
    if objs:
        InterfaceStatsDaily.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["interface", "day"],
            update_fields=["in_mbps_avg", "in_mbps_max", "out_mbps_avg",
                           "out_mbps_max", "in_errors", "out_errors", "sample_count"],
        )

    logger.info("Daily rollup InterfaceStats: %d records processed", len(objs))
    return len(objs)


def _delete_raw_for_rolled_hours(model, rolled_pairs: set, cutoff, id_field: str) -> int:
    """Xóa raw rows cũ hơn cutoff CHỈ khi (entity_id, hour) đã có hourly rollup."""
    if not rolled_pairs:
        return 0
    from django.db.models import Q

    q = Q()
    for entity_id, hour in rolled_pairs:
        hour_end = hour + timedelta(hours=1)
        q |= Q(**{id_field: entity_id, "timestamp__gte": hour, "timestamp__lt": hour_end})
    del_count, _ = model.objects.filter(timestamp__lt=cutoff).filter(q).delete()
    return del_count


def cleanup_rolled_up_raw_data() -> tuple[int, int]:
    """Xóa raw data đã được rollup (cũ hơn RAW_RETENTION_HOURS).

    Chỉ xóa raw data thuộc các giờ đã có hourly record tương ứng.
    Returns: (deleted_health, deleted_interface_stats)
    """
    from .models import SystemHealth, InterfaceStats, SystemHealthHourly, InterfaceStatsHourly

    cutoff = timezone.now() - timedelta(hours=RAW_RETENTION_HOURS)

    rolled_hours_health = set(
        SystemHealthHourly.objects
        .filter(hour__lt=cutoff)
        .values_list("device_id", "hour")
    )
    deleted_sh = _delete_raw_for_rolled_hours(
        SystemHealth, rolled_hours_health, cutoff, "device_id",
    )

    rolled_hours_iface = set(
        InterfaceStatsHourly.objects
        .filter(hour__lt=cutoff)
        .values_list("interface_id", "hour")
    )
    deleted_if = _delete_raw_for_rolled_hours(
        InterfaceStats, rolled_hours_iface, cutoff, "interface_id",
    )

    logger.info(
        "Cleanup rolled-up raw data: deleted %d SystemHealth, %d InterfaceStats (cũ hơn %dh)",
        deleted_sh, deleted_if, RAW_RETENTION_HOURS,
    )
    return deleted_sh, deleted_if
