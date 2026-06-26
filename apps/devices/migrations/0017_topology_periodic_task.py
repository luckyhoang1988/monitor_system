"""Thêm PeriodicTask discover-topology-links (30 phút)."""
from django.db import migrations


def add_topology_periodic_task(apps, schema_editor):
    try:
        from django_celery_beat.models import IntervalSchedule, PeriodicTask
    except ImportError:
        return

    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=1800,
        period=IntervalSchedule.SECONDS,
    )
    PeriodicTask.objects.update_or_create(
        name="discover-topology-links",
        defaults={
            "task": "apps.collectors.tasks.discover_topology_links",
            "interval": schedule,
            "crontab": None,
            "enabled": True,
        },
    )


def remove_topology_periodic_task(apps, schema_editor):
    try:
        from django_celery_beat.models import PeriodicTask
    except ImportError:
        return
    PeriodicTask.objects.filter(name="discover-topology-links").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("django_celery_beat", "0019_alter_periodictasks_options"),
        ("devices", "0016_topologylink"),
    ]

    operations = [
        migrations.RunPython(add_topology_periodic_task, remove_topology_periodic_task),
    ]
