"""Migration: Chuyển snmp_community và ssh_password sang EncryptedCharField.

Thay đổi max_length từ 200 → 500 (Fernet ciphertext dài hơn plain text).
Sau khi migrate, chạy: python manage.py encrypt_credentials
"""
from django.db import migrations
import apps.devices.fields


class Migration(migrations.Migration):

    dependencies = [
        ("devices", "0003_discovereddevice_device_backup_enabled_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="device",
            name="snmp_community",
            field=apps.devices.fields.EncryptedCharField(
                blank=True, verbose_name="SNMP Community"
            ),
        ),
        migrations.AlterField(
            model_name="device",
            name="ssh_password",
            field=apps.devices.fields.EncryptedCharField(
                blank=True, verbose_name="SSH Password"
            ),
        ),
    ]
