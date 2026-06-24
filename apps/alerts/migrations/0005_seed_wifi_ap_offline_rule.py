from django.db import migrations


def seed_wifi_ap_offline_rule(apps, schema_editor):
    """Tạo sẵn rule 'AP offline (dưới WLAN AC) → Telegram' (idempotent)."""
    AlertRule = apps.get_model("alerts", "AlertRule")
    AlertRule.objects.get_or_create(
        name="AP offline (dưới WLAN AC)",
        defaults=dict(device_type="wlan_controller", metric="wifi_ap_offline",
                      condition="gt", threshold=0, severity="WARNING",
                      duration_min=0, channels=["telegram"], enabled=True),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('alerts', '0004_alertconfig'),
    ]

    operations = [
        migrations.RunPython(seed_wifi_ap_offline_rule, migrations.RunPython.noop),
    ]
