"""Tạo PeriodicTask poll/alert/rollup cho Celery Beat (DatabaseScheduler)."""
from django.db import migrations


def create_monitor_periodic_tasks(apps, schema_editor):
    try:
        from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask
    except ImportError:
        return

    interval_tasks = [
        ("poll-all-network-devices", 120, "apps.collectors.tasks.poll_all_network_devices"),
        ("poll-all-ping-devices", 120, "apps.collectors.tasks.poll_all_ping_devices"),
        ("poll-all-hyperv", 300, "apps.collectors.tasks.poll_all_hyperv"),
        ("evaluate-alert-rules", 120, "apps.alerts.tasks.evaluate_alert_rules"),
    ]
    for name, every, task_path in interval_tasks:
        schedule, _ = IntervalSchedule.objects.get_or_create(
            every=every,
            period=IntervalSchedule.SECONDS,
        )
        PeriodicTask.objects.update_or_create(
            name=name,
            defaults={"task": task_path, "interval": schedule, "crontab": None, "enabled": True},
        )

    crontab_tasks = [
        ("cleanup-old-metrics", "0", "3", "*", "*", "*", "apps.metrics.tasks.cleanup_old_metrics"),
        ("rollup-hourly-metrics", "5", "*", "*", "*", "*", "apps.metrics.tasks.rollup_hourly_metrics"),
        ("rollup-daily-metrics", "30", "3", "*", "*", "*", "apps.metrics.tasks.rollup_daily_metrics"),
    ]
    for name, minute, hour, dow, dom, moy, task_path in crontab_tasks:
        schedule, _ = CrontabSchedule.objects.get_or_create(
            minute=minute,
            hour=hour,
            day_of_week=dow,
            day_of_month=dom,
            month_of_year=moy,
        )
        PeriodicTask.objects.update_or_create(
            name=name,
            defaults={"task": task_path, "crontab": schedule, "interval": None, "enabled": True},
        )


def remove_monitor_periodic_tasks(apps, schema_editor):
    try:
        from django_celery_beat.models import PeriodicTask
    except ImportError:
        return
    PeriodicTask.objects.filter(
        name__in=[
            "poll-all-network-devices",
            "poll-all-ping-devices",
            "poll-all-hyperv",
            "evaluate-alert-rules",
            "cleanup-old-metrics",
            "rollup-hourly-metrics",
            "rollup-daily-metrics",
        ]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("django_celery_beat", "0019_alter_periodictasks_options"),
        ("devices", "0012_interface_access_vlan"),
    ]

    operations = [
        migrations.RunPython(create_monitor_periodic_tasks, remove_monitor_periodic_tasks),
    ]
