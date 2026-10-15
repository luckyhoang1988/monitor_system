"""Dọn token bot Telegram đã lỡ lưu trong AlertNotification.error (hạng mục 1.3, review 2026-10-15).

Đo prod 2026-10-15 (quét mọi cột text/varchar/json của schema public): token chỉ nằm ở
`alerts_alertnotification.error`, 294 dòng — 173 dạng `... for url: https://api.telegram.org/bot<token>/sendMessage`
(HTTPError 429/400) và 121 dạng `... Max retries exceeded with url: /bot<token>/sendMessage (Caused by ...)`.
`requests` nhét nguyên URL vào str(exc) và engine từng lưu thẳng str(exc) (hiện trong Django admin inline).

Chỉ thay phần token bằng dấu che, GIỮ phần còn lại của thông điệp để vẫn đọc được lỗi gì (429, timeout,
không phân giải được tên...). Không dùng `.save()`/`auto_now`: `.update()` theo pk nên `updated_at` — mốc
phát hiện dòng `processing` bị kẹt — không bị đổi.

Hàm tự chứa regex (KHÔNG import `apps.alerts.channels.redact`): migration phải giữ nghĩa cố định dù code
ứng dụng đổi về sau. Không đảo ngược được (token không khôi phục) → reverse là noop.

Lưu ý ngoài phạm vi migration: token vẫn nằm trong `.env.production`, trong các bản sao DB/backup đã tạo
TRƯỚC khi chạy migration này, và (nếu từng xem) trong log container cũ. Xoay token qua BotFather là
quyết định riêng của người vận hành.
"""
import re

from django.db import migrations

# Cùng ý nghĩa với `apps.alerts.channels._BOT_TOKEN_RE` tại thời điểm viết: <bot_id>:<35 ký tự>.
_TOKEN_RE = re.compile(r"(?<![0-9])[0-9]{6,}:[A-Za-z0-9_-]{20,}")
_DB_PREFILTER = r"[0-9]{6,}:[A-Za-z0-9_-]{20,}"
MASK = "<token-đã-che>"


def scrub_tokens(apps, schema_editor):
    AlertNotification = apps.get_model("alerts", "AlertNotification")
    candidates = AlertNotification.objects.filter(error__regex=_DB_PREFILTER)
    for pk, error in list(candidates.values_list("pk", "error")):
        cleaned = _TOKEN_RE.sub(MASK, error)
        if cleaned != error:
            AlertNotification.objects.filter(pk=pk).update(error=cleaned)


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0009_notification_retry_schedule_and_channel_cooldown"),
    ]

    operations = [
        migrations.RunPython(scrub_tokens, migrations.RunPython.noop),
    ]
