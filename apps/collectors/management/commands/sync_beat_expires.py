"""Đồng bộ options.expires trong CELERY_BEAT_SCHEDULE vào PeriodicTask.expire_seconds.

⚠️ ROOT CAUSE thật (tìm ra 2026-07-07 bằng cách đọc source `django_celery_beat.schedulers`):
`ModelEntry._unpack_options()` chỉ đọc key `expire_seconds` trong `options` (KHÔNG phải
`expires` — đó là key celery gốc dùng cho `apply_async`, django-celery-beat không đọc).
`DatabaseScheduler.setup_schedule()` gọi `update_from_dict(beat_schedule)` **mỗi lần beat
process khởi động** (không chỉ lần tạo đầu) → nếu `options` chỉ có `expires` mà thiếu
`expire_seconds`, mỗi lần beat restart sẽ RESET `expire_seconds` về None ngay sau khi
command này vừa set đúng giá trị vài giây trước (verify runtime: log entrypoint container
`beat` in "None -> 120" lúc boot, nhưng `SELECT expire_seconds` ngay sau đó vẫn NULL vì
`exec celery beat` chạy `setup_schedule()` đè lại). **Đã fix tại gốc**: thêm key
`expire_seconds` vào từng `options` trong `config/settings/base.py` `CELERY_BEAT_SCHEDULE`
— giờ `update_from_dict` tự ghi đúng giá trị mỗi lần beat boot, không cần command này nữa
về mặt lý thuyết. Giữ lại làm lớp phòng thủ idempotent (chạy vô hại, không ai xoá nếu lỡ
quên đồng bộ `options` sau này) — chạy an toàn mỗi lần deploy (xem entrypoint.sh).
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
