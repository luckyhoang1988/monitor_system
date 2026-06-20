"""
Kiểm tra cấu hình thông báo email và/hoặc Telegram.

Usage:
    python manage.py test_notifications --email
    python manage.py test_notifications --telegram
    python manage.py test_notifications --email --telegram
"""
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone


class Command(BaseCommand):
    help = "Gửi thông báo test để kiểm tra cấu hình email/telegram"

    def add_arguments(self, parser):
        parser.add_argument("--email",    action="store_true", help="Test email")
        parser.add_argument("--telegram", action="store_true", help="Test telegram")

    def handle(self, *args, **options):
        if not options["email"] and not options["telegram"]:
            self.stderr.write("Cần chỉ định ít nhất --email hoặc --telegram")
            return

        fake_alert = _FakeAlert()

        if options["email"]:
            self._test_email(fake_alert)

        if options["telegram"]:
            self._test_telegram(fake_alert)

    def _test_email(self, fake_alert):
        recipients = getattr(settings, "ALERT_EMAIL_RECIPIENTS", [])
        if not recipients:
            self.stderr.write("ALERT_EMAIL_RECIPIENTS chưa cấu hình trong .env")
            return
        try:
            from apps.alerts.channels.email_channel import send_email_alert
            send_email_alert(fake_alert)
            self.stdout.write(self.style.SUCCESS(f"Email OK → {recipients}"))
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Email FAILED: {exc}"))

    def _test_telegram(self, fake_alert):
        token   = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
        chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            self.stderr.write("TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID chưa cấu hình")
            return
        try:
            from apps.alerts.channels.telegram import send_telegram_alert
            send_telegram_alert(fake_alert)
            self.stdout.write(self.style.SUCCESS(f"Telegram OK → chat_id={chat_id}"))
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Telegram FAILED: {exc}"))


class _FakeAlert:
    """Đối tượng giả để test channel mà không cần DB."""
    severity     = "WARNING"
    message      = "Đây là thông báo test từ Monitor System."
    metric_value = 0.0
    triggered_at = timezone.now()
    resolved_at  = None

    class device:
        name       = "TEST-DEVICE"
        ip_address = "127.0.0.1"

    class rule:
        name = "Test Notification Rule"
