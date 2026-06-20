"""Management command: mã hóa toàn bộ credentials (plain text) đã có trong DB.

Chạy 1 lần sau khi deploy code mới + đặt ENCRYPTION_KEY trong .env:
    python manage.py encrypt_credentials

Nếu chạy lại, những giá trị đã được mã hóa sẽ bị bỏ qua (an toàn).
"""
from django.core.management.base import BaseCommand
from apps.devices.crypto import encrypt_value, is_encrypted


class Command(BaseCommand):
    help = "Mã hóa toàn bộ SSH password và SNMP community chưa được encrypt trong DB."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Chỉ kiểm tra, không thực sự thay đổi dữ liệu.",
        )

    def handle(self, *args, **options):
        # Import trực tiếp để tránh EncryptedCharField tự decrypt khi đọc
        from django.db import connection

        dry_run = options["dry_run"]
        cursor = connection.cursor()

        # Đọc raw data từ DB (bỏ qua EncryptedCharField.from_db_value)
        cursor.execute("SELECT id, snmp_community, ssh_password FROM devices_device")
        rows = cursor.fetchall()

        total = len(rows)
        encrypted_count = 0
        skipped_count = 0

        for device_id, snmp_community, ssh_password in rows:
            updates = {}

            # Kiểm tra và encrypt snmp_community
            if snmp_community and not is_encrypted(snmp_community):
                updates["snmp_community"] = encrypt_value(snmp_community)

            # Kiểm tra và encrypt ssh_password
            if ssh_password and not is_encrypted(ssh_password):
                updates["ssh_password"] = encrypt_value(ssh_password)

            if updates:
                if not dry_run:
                    set_clause = ", ".join(f"{k} = %s" for k in updates)
                    values = list(updates.values()) + [device_id]
                    cursor.execute(
                        f"UPDATE devices_device SET {set_clause} WHERE id = %s",
                        values,
                    )
                encrypted_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  {'[DRY-RUN] ' if dry_run else ''}Device #{device_id}: "
                        f"encrypted {list(updates.keys())}"
                    )
                )
            else:
                skipped_count += 1

        prefix = "[DRY-RUN] " if dry_run else ""
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Hoàn tất: {encrypted_count}/{total} devices đã encrypt, "
                f"{skipped_count} bỏ qua (rỗng hoặc đã encrypt)."
            )
        )
        if dry_run:
            self.stdout.write(
                self.style.WARNING("Chạy lại không có --dry-run để thực sự mã hóa.")
            )
