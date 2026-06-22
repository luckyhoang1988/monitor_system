from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('alerts', '0002_alter_alertrule_device_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='alertrule',
            name='device_type',
            field=models.CharField(
                choices=[
                    ('all', 'Tất cả'),
                    ('switch', 'Switch'),
                    ('router', 'Router'),
                    ('firewall', 'Firewall'),
                    ('hyperv', 'HyperV Host'),
                    ('wlan_controller', 'WLAN Controller (AC)'),
                    ('ap', 'Access Point'),
                ],
                default='all',
                max_length=20,
                verbose_name='Loại thiết bị',
            ),
        ),
    ]
