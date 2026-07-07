"""Đồng bộ options.expires trong CELERY_BEAT_SCHEDULE vào PeriodicTask.expire_seconds.

DatabaseScheduler (django-celery-beat) chỉ ghi options.expires -> expire_seconds
lúc PeriodicTask được TẠO MỚI lần đầu; sửa CELERY_BEAT_SCHEDULE trong code sau đó
KHÔNG tự re-sync cho entry đã tồn tại sẵn trong DB (regression phát hiện 2026-07-07:
expire_seconds=None dù code đã khai báo expires cho poll-all-hyperv/network/ping
từ commit fe1dac1 — mất tác dụng chống snowball khi 1 batch chạy quá 1 chu kỳ).
Idempotent, chạy an toàn mỗi lần deploy (xem entrypoint.sh).
"""
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Đồng bộ options.expires của CELERY_BEAT_SCHEDULE vào PeriodicTask.expire_seconds trong DB"

    def handle(self, *args, **options):
        from django_celery_beat.models import PeriodicTask

        changed = 0
        skipped_missing = 0
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            expires = entry.get("options", {}).get("expires")
            if expires is None:
                continue
            try:
                task = PeriodicTask.objects.get(name=name)
            except PeriodicTask.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f"  {name}: chưa có PeriodicTask trong DB, bỏ qua "
                    "(beat sẽ tự tạo ở lần chạy đầu)"
                ))
                skipped_missing += 1
                continue

            if task.expire_seconds != expires:
                old = task.expire_seconds
                task.expire_seconds = expires
                task.save(update_fields=["expire_seconds"])
                self.stdout.write(self.style.SUCCESS(
                    f"  {name}: expire_seconds {old} -> {expires}"
                ))
                changed += 1
            else:
                self.stdout.write(f"  {name}: expire_seconds={expires} (đã đúng)")

        self.stdout.write(self.style.SUCCESS(
            f"Done. Cập nhật {changed} task, bỏ qua {skipped_missing} task chưa có trong DB."
        ))
