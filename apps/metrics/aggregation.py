"""Aggregation — rollup raw metrics thành bảng hourly/daily.

Chiến lược:
- Hourly rollup: chạy mỗi giờ, gom data raw cũ hơn 2 giờ thành avg/max theo giờ.
  Giữ lại 2 giờ gần nhất (buffer) để tránh rollup data chưa hoàn chỉnh.
- Daily rollup: chạy mỗi ngày (3:30AM), gom hourly data cũ hơn 2 ngày thành avg/max theo ngày.
- Cleanup raw: sau khi rollup hourly thành công, xóa raw data đã được rollup
  (giữ lại raw data trong 48 giờ gần nhất).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Max, Count, Sum
from django.db.models.functions import TruncHour, TruncDate
from django.utils import timezone

logger = logging.getLogger(__name__)

RAW_RETENTION_HOURS = 48
HOURLY_BUFFER_HOURS = getattr(settings, "HOURLY_ROLLUP_BUFFER_HOURS", 2)
DAILY_BUFFER_DAYS   = getattr(settings, "DAILY_ROLLUP_BUFFER_DAYS", 1)


def rollup_system_health_hourly() -> int:
    """Rollup SystemHealth raw → SystemHealthHourly.

    Returns: số lượng hourly records đã tạo/cập nhật.
    """
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

    count = 0
    for row in aggregated:
        _, created = SystemHealthHourly.objects.update_or_create(
            device_id=row["device_id"],
            hour=row["hour"],
            defaults={
                "cpu_avg": round(row["cpu_avg"], 2),
                "cpu_max": round(row["cpu_max"], 2),
                "mem_avg": round(row["mem_avg"], 2),
                "mem_max": round(row["mem_max"], 2),
                "sample_count": row["sample_count"],
            },
        )
        count += 1

    logger.info("Hourly rollup SystemHealth: %d records processed", count)
    return count


def rollup_interface_stats_hourly() -> int:
    """Rollup InterfaceStats raw → InterfaceStatsHourly.

    Returns: số lượng hourly records đã tạo/cập nhật.
    """
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

    count = 0
    for row in aggregated:
        InterfaceStatsHourly.objects.update_or_create(
            interface_id=row["interface_id"],
            hour=row["hour"],
            defaults={
                "in_mbps_avg": round(row["in_mbps_avg"], 3),
                "in_mbps_max": round(row["in_mbps_max"], 3),
                "out_mbps_avg": round(row["out_mbps_avg"], 3),
                "out_mbps_max": round(row["out_mbps_max"], 3),
                "in_errors": row["in_errors_sum"] or 0,
                "out_errors": row["out_errors_sum"] or 0,
                "sample_count": row["sample_count"],
            },
        )
        count += 1

    logger.info("Hourly rollup InterfaceStats: %d records processed", count)
    return count


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

    count = 0
    for row in aggregated:
        SystemHealthDaily.objects.update_or_create(
            device_id=row["device_id"],
            day=row["day"],
            defaults={
                "cpu_avg": round(row["cpu_avg"], 2),
                "cpu_max": round(row["cpu_max"], 2),
                "mem_avg": round(row["mem_avg"], 2),
                "mem_max": round(row["mem_max"], 2),
                "sample_count": row["sample_count"],
            },
        )
        count += 1

    logger.info("Daily rollup SystemHealth: %d records processed", count)
    return count


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

    count = 0
    for row in aggregated:
        InterfaceStatsDaily.objects.update_or_create(
            interface_id=row["interface_id"],
            day=row["day"],
            defaults={
                "in_mbps_avg": round(row["in_mbps_avg"], 3),
                "in_mbps_max": round(row["in_mbps_max"], 3),
                "out_mbps_avg": round(row["out_mbps_avg"], 3),
                "out_mbps_max": round(row["out_mbps_max"], 3),
                "in_errors": row["in_errors_sum"] or 0,
                "out_errors": row["out_errors_sum"] or 0,
                "sample_count": row["sample_count"],
            },
        )
        count += 1

    logger.info("Daily rollup InterfaceStats: %d records processed", count)
    return count


def cleanup_rolled_up_raw_data() -> tuple[int, int]:
    """Xóa raw data đã được rollup (cũ hơn RAW_RETENTION_HOURS).

    Chỉ xóa raw data thuộc các giờ đã có hourly record tương ứng.
    Returns: (deleted_health, deleted_interface_stats)
    """
    from .models import SystemHealth, InterfaceStats, SystemHealthHourly, InterfaceStatsHourly

    cutoff = timezone.now() - timedelta(hours=RAW_RETENTION_HOURS)

    # Chỉ xóa raw data mà giờ tương ứng đã có hourly rollup
    rolled_hours_health = set(
        SystemHealthHourly.objects
        .filter(hour__lt=cutoff)
        .values_list("device_id", "hour")
    )

    deleted_sh = 0
    if rolled_hours_health:
        # Xóa raw SystemHealth cũ hơn cutoff (đã có hourly backup)
        del_count, _ = SystemHealth.objects.filter(timestamp__lt=cutoff).delete()
        deleted_sh = del_count

    rolled_hours_iface = set(
        InterfaceStatsHourly.objects
        .filter(hour__lt=cutoff)
        .values_list("interface_id", "hour")
    )

    deleted_if = 0
    if rolled_hours_iface:
        del_count, _ = InterfaceStats.objects.filter(timestamp__lt=cutoff).delete()
        deleted_if = del_count

    logger.info(
        "Cleanup rolled-up raw data: deleted %d SystemHealth, %d InterfaceStats (cũ hơn %dh)",
        deleted_sh, deleted_if, RAW_RETENTION_HOURS,
    )
    return deleted_sh, deleted_if
