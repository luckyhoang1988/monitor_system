# Generated for port_mode (Q-BRIDGE trunk/access detection)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('devices', '0014_device_last_ok_seen'),
    ]

    operations = [
        migrations.AddField(
            model_name='interface',
            name='port_mode',
            field=models.CharField(
                blank=True,
                choices=[('access', 'Access'), ('trunk', 'Trunk'), ('hybrid', 'Hybrid')],
                default='',
                max_length=10,
                verbose_name='Chế độ cổng (SNMP Q-BRIDGE)',
            ),
        ),
    ]
