"""Celery tasks cho metrics: dọn dẹp cũ + rollup aggregation."""
import logging
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)


@shared_task
def cleanup_old_metrics() -> None:
    """Xóa raw/hourly/daily data quá thời hạn retention."""
    from .models import (
        InterfaceStats, SystemHealth, VMStats,
        SystemHealthHourly, SystemHealthDaily,
        InterfaceStatsHourly, InterfaceStatsDaily,
    )
    retention = getattr(settings, "METRICS_RETENTION_DAYS", 90)
    cutoff    = timezone.now() - timedelta(days=retention)

    deleted_if, _  = InterfaceStats.objects.filter(timestamp__lt=cutoff).delete()
    deleted_sh, _  = SystemHealth.objects.filter(timestamp__lt=cutoff).delete()
    deleted_vm, _  = VMStats.objects.filter(timestamp__lt=cutoff).delete()

    # Xóa aggregated data cũ hơn 2x retention (hourly/daily giữ lâu hơn raw)
    agg_cutoff = timezone.now() - timedelta(days=retention * 2)
    deleted_sh_h, _ = SystemHealthHourly.objects.filter(hour__lt=agg_cutoff).delete()
    deleted_sh_d, _ = SystemHealthDaily.objects.filter(day__lt=agg_cutoff.date()).delete()
    deleted_if_h, _ = InterfaceStatsHourly.objects.filter(hour__lt=agg_cutoff).delete()
    deleted_if_d, _ = InterfaceStatsDaily.objects.filter(day__lt=agg_cutoff.date()).delete()

    logger.info(
        "Cleanup: xóa %d InterfaceStats, %d SystemHealth, %d VMStats (raw, cũ hơn %d ngày) | "
        "Aggregated: %d SH_hourly, %d SH_daily, %d IF_hourly, %d IF_daily (cũ hơn %d ngày)",
        deleted_if, deleted_sh, deleted_vm, retention,
        deleted_sh_h, deleted_sh_d, deleted_if_h, deleted_if_d, retention * 2,
    )


@shared_task
def rollup_hourly_metrics() -> None:
    """Chạy mỗi giờ: rollup raw data → hourly, sau đó dọn dẹp raw đã rollup."""
    from .aggregation import (
        rollup_system_health_hourly,
        rollup_interface_stats_hourly,
        cleanup_rolled_up_raw_data,
    )

    sh_count = rollup_system_health_hourly()
    if_count = rollup_interface_stats_hourly()

    # Dọn dẹp raw data đã được rollup (giữ lại 48h gần nhất)
    del_sh, del_if = cleanup_rolled_up_raw_data()

    logger.info(
        "Hourly rollup hoàn tất: %d SH + %d IF records tổng hợp, "
        "dọn dẹp %d SH + %d IF raw records",
        sh_count, if_count, del_sh, del_if,
    )


@shared_task
def rollup_daily_metrics() -> None:
    """Chạy mỗi ngày (3:30AM): rollup hourly → daily."""
    from .aggregation import (
        rollup_system_health_daily,
        rollup_interface_stats_daily,
    )

    sh_count = rollup_system_health_daily()
    if_count = rollup_interface_stats_daily()

    logger.info(
        "Daily rollup hoàn tất: %d SH + %d IF daily records tổng hợp",
        sh_count, if_count,
    )
