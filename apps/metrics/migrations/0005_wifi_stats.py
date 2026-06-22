import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('devices', '0011_add_wlan_device_types'),
        ('metrics', '0004_alter_vmstats_repl_health'),
    ]

    operations = [
        migrations.CreateModel(
            name='WifiApStats',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp', models.DateTimeField(db_index=True)),
                ('ap_name', models.CharField(max_length=200)),
                ('ap_mac', models.CharField(blank=True, max_length=32)),
                ('ap_ip', models.CharField(blank=True, max_length=64)),
                ('ap_group', models.CharField(blank=True, max_length=128)),
                ('is_online', models.BooleanField(default=False)),
                ('run_state', models.CharField(blank=True, max_length=32)),
                ('client_count', models.IntegerField(default=0)),
                ('device', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='wifi_ap_stats', to='devices.device', verbose_name='WLAN Controller')),
            ],
            options={
                'verbose_name': 'WiFi AP Stats',
                'ordering': ['-timestamp'],
            },
        ),
        migrations.CreateModel(
            name='WifiClientStats',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp', models.DateTimeField(db_index=True)),
                ('mac', models.CharField(db_index=True, max_length=32)),
                ('ip', models.CharField(blank=True, max_length=64)),
                ('ssid', models.CharField(blank=True, max_length=128)),
                ('ap_name', models.CharField(blank=True, max_length=200)),
                ('radio', models.CharField(blank=True, max_length=32)),
                ('rssi', models.IntegerField(blank=True, null=True)),
                ('online_secs', models.BigIntegerField(default=0)),
                ('device', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='wifi_clients', to='devices.device', verbose_name='WLAN Controller')),
            ],
            options={
                'verbose_name': 'WiFi Client Stats',
                'ordering': ['-timestamp'],
            },
        ),
        migrations.AddIndex(
            model_name='wifiapstats',
            index=models.Index(fields=['device', 'ap_name', '-timestamp'], name='metrics_wif_device__a81423_idx'),
        ),
        migrations.AddIndex(
            model_name='wificlientstats',
            index=models.Index(fields=['device', '-timestamp'], name='metrics_wif_device__efacd8_idx'),
        ),
        migrations.AddIndex(
            model_name='wificlientstats',
            index=models.Index(fields=['device', 'mac', '-timestamp'], name='metrics_wif_device__cc04b7_idx'),
        ),
    ]
