from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('devices', '0010_interface_unique_by_name'),
    ]

    operations = [
        migrations.AlterField(
            model_name='device',
            name='device_type',
            field=models.CharField(
                choices=[
                    ('switch', 'Switch'),
                    ('router', 'Router'),
                    ('firewall', 'Firewall'),
                    ('nas', 'NAS'),
                    ('hyperv', 'HyperV Host'),
                    ('wlan_controller', 'WLAN Controller (AC)'),
                    ('ap', 'Access Point'),
                ],
                max_length=20,
                verbose_name='Loại',
            ),
        ),
    ]
