"""Management command: mã hóa toàn bộ credentials (plain text) đã có trong DB.

Chạy 1 lần sau khi deploy code mới + đặt ENCRYPTION_KEY trong .env:
    python manage.py encrypt_credentials

Nếu chạy lại, những giá trị đã được mã hóa sẽ bị bỏ qua (an toàn).

Giá trị CÓ CẤU TRÚC Fernet token nhưng không giải mã được bằng ENCRYPTION_KEY hiện tại (khoá sai/thiếu), và giá
trị giống token đã hỏng (mất padding, cắt cụt) bị bỏ qua và được báo riêng — KHÔNG mã hoá chồng (sẽ mất
credential vĩnh viễn kể cả khi đặt lại đúng khoá).
"""
from django.core.management.base import BaseCommand
from apps.devices.crypto import encrypt_value, is_encrypted, looks_damaged_token, looks_encrypted


def _is_plain_text(value: str) -> bool:
    """Plain-text thật: không phải token (kể cả của khoá khác) và không phải token đã hỏng."""
    return not looks_encrypted(value) and not looks_damaged_token(value)


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
        unreadable_count = 0

        for device_id, snmp_community, ssh_password in rows:
            updates = {}

            # Chỉ mã hoá plain-text thật: không có cấu trúc token VÀ không giống token đã hỏng. Token của khoá
            # khác là "đã mã hoá", token mất padding/cắt cụt là "đã hỏng" — đụng vào cả hai đều là mã hoá chồng.
            if snmp_community and _is_plain_text(snmp_community):
                updates["snmp_community"] = encrypt_value(snmp_community)

            if ssh_password and _is_plain_text(ssh_password):
                updates["ssh_password"] = encrypt_value(ssh_password)

            for name, value in (("snmp_community", snmp_community), ("ssh_password", ssh_password)):
                if not value:
                    continue
                if looks_damaged_token(value):
                    unreadable_count += 1
                    self.stdout.write(self.style.WARNING(
                        f"  Device #{device_id}: {name} có dạng token nhưng đã bị hỏng (mất padding/cắt cụt) "
                        "— bỏ qua, không mã hoá chồng; cần nhập lại credential."
                    ))
                elif looks_encrypted(value) and not is_encrypted(value):
                    unreadable_count += 1
                    self.stdout.write(self.style.WARNING(
                        f"  Device #{device_id}: {name} là token nhưng KHÔNG giải mã được bằng "
                        "ENCRYPTION_KEY hiện tại — bỏ qua, không mã hoá chồng."
                    ))

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
        if unreadable_count:
            self.stdout.write(self.style.WARNING(
                f"{unreadable_count} giá trị không giải mã được — kiểm tra ENCRYPTION_KEY trước khi làm gì khác."
            ))
        if dry_run:
            self.stdout.write(
                self.style.WARNING("Chạy lại không có --dry-run để thực sự mã hóa.")
            )
