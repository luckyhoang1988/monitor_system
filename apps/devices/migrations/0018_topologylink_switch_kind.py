# Generated manually — link_kind + remote_device for switch↔switch topology

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("devices", "0017_topology_periodic_task"),
    ]

    operations = [
        migrations.AddField(
            model_name="topologylink",
            name="link_kind",
            field=models.CharField(
                choices=[("ap", "AP"), ("switch", "Switch")],
                default="ap",
                max_length=10,
                verbose_name="Loại liên kết",
            ),
        ),
        migrations.AddField(
            model_name="topologylink",
            name="remote_device",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="topology_incoming_links",
                to="devices.device",
                verbose_name="Thiết bị đích (switch)",
            ),
        ),
        migrations.AlterField(
            model_name="topologylink",
            name="remote_ap_mac",
            field=models.CharField(blank=True, max_length=32, verbose_name="MAC AP"),
        ),
    ]
