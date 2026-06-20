from django.db import migrations


def create_backup_periodic_task(apps, schema_editor):
    try:
        from django_celery_beat.models import CrontabSchedule, PeriodicTask
    except ImportError:
        return
        
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="2",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*"
    )
    
    PeriodicTask.objects.get_or_create(
        crontab=schedule,
        name="Auto Backup Config (2:00 AM)",
        task="apps.devices.tasks.auto_backup_all_devices",
    )


class Migration(migrations.Migration):

    dependencies = [
        ('django_celery_beat', '0019_alter_periodictasks_options'),
        ('devices', '0005_devicebackup'),
    ]

    operations = [
        migrations.RunPython(create_backup_periodic_task, migrations.RunPython.noop),
    ]
