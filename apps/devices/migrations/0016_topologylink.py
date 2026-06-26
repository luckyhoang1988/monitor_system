# Generated manually for TopologyLink model

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("devices", "0015_interface_port_mode"),
    ]

    operations = [
        migrations.CreateModel(
            name="TopologyLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("local_port", models.CharField(max_length=100, verbose_name="Port switch")),
                ("remote_ap_mac", models.CharField(blank=True, max_length=32, verbose_name="MAC AP")),
                ("remote_ap_name", models.CharField(blank=True, max_length=200, verbose_name="Tên AP")),
                ("remote_sys_name", models.CharField(blank=True, max_length=200, verbose_name="LLDP sysName")),
                ("remote_chassis_id", models.CharField(blank=True, max_length=200, verbose_name="LLDP chassis")),
                ("remote_port_id", models.CharField(blank=True, max_length=200, verbose_name="LLDP portId")),
                ("remote_mgmt_ip", models.GenericIPAddressField(blank=True, null=True, verbose_name="IP quản lý (LLDP)")),
                ("protocol", models.CharField(default="lldp", max_length=10, verbose_name="Giao thức")),
                ("match_method", models.CharField(
                    choices=[
                        ("mac", "MAC"),
                        ("name", "Tên"),
                        ("ip", "IP"),
                        ("manual", "Thủ công"),
                        ("lldp", "LLDP (chưa match AC)"),
                    ],
                    default="lldp",
                    max_length=10,
                    verbose_name="Cách ghép",
                )),
                ("is_confirmed", models.BooleanField(default=False, verbose_name="Đã xác nhận với AC")),
                ("is_stale", models.BooleanField(default=False, verbose_name="Link cũ/stale")),
                ("miss_count", models.PositiveSmallIntegerField(default=0, verbose_name="Số lần không thấy")),
                ("first_seen", models.DateTimeField(auto_now_add=True, verbose_name="Lần đầu thấy")),
                ("last_seen", models.DateTimeField(auto_now=True, verbose_name="Lần cuối thấy")),
                ("local_device", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="topology_links",
                    to="devices.device",
                    verbose_name="Switch",
                )),
                ("local_interface", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="topology_links",
                    to="devices.interface",
                    verbose_name="Interface",
                )),
            ],
            options={
                "verbose_name": "Liên kết topology",
                "verbose_name_plural": "Liên kết topology",
                "ordering": ["local_device__name", "local_port"],
                "unique_together": {("local_device", "local_port")},
            },
        ),
    ]
