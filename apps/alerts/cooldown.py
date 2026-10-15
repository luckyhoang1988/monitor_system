"""Cooldown (flood control) dùng chung cho các kênh thông báo.

Vấn đề (review 2026-10-15, P2): Telegram trả `429 retry_after=N` — trong N giây mọi yêu cầu cho bot/chat
đó đều bị từ chối. Bản đầu giữ thời hạn này trong biến cục bộ của 1 vòng sweep, nên: (1) vòng sau 60s vẫn
gửi tiếp, (2) lần gửi trực tiếp từ tác vụ poll (đường chính!) không biết gì về nó. Cả hai tiếp tục gây 429
và tiêu hao lượt thử của dòng.

Giải pháp: lưu mốc `until` trong DB (bảng `ChannelCooldown`), khoá theo bot/chat. Mọi đường gửi kiểm tra
TRƯỚC khi claim (không tốn lượt thử) và mọi lần nhận 429 ghi vào đây. Postgres là nguồn duy nhất — cùng
chỗ với lịch gửi lại (`next_attempt_at`), sống sót khi worker restart và không cần Redis.

Khoá do từng channel dựng (`telegram.cooldown_key`, `webhook.cooldown_key`): băm token/URL, KHÔNG lưu bí mật.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import DateTimeField, F, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from .models import ChannelCooldown


def remaining_secs(key: str | None) -> float:
    """Số giây còn phải chờ (0 nếu không có/đã hết). `key=None` (kênh không có khoá) = không cooldown."""
    if not key:
        return 0.0
    until = ChannelCooldown.objects.filter(key=key).values_list("until", flat=True).first()
    if until is None:
        return 0.0
    return max(0.0, (until - timezone.now()).total_seconds())


def register(key: str | None, seconds: float) -> None:
    """Đặt cooldown cho `key` thêm `seconds` giây kể từ bây giờ. Chỉ KÉO DÀI, không bao giờ rút ngắn
    (2 tiến trình cùng nhận 429 với retry_after khác nhau, tiến trình nào ghi sau cũng không hạ mốc)."""
    if not key or seconds <= 0:
        return
    until = timezone.now() + timedelta(seconds=seconds)
    longer = Greatest(F("until"), Value(until, output_field=DateTimeField()))
    if ChannelCooldown.objects.filter(key=key).update(until=longer):
        return
    try:
        with transaction.atomic():  # savepoint: IntegrityError không làm hỏng transaction của caller
            ChannelCooldown.objects.create(key=key, until=until)
    except IntegrityError:  # tiến trình khác vừa tạo — cập nhật theo mốc dài hơn
        ChannelCooldown.objects.filter(key=key).update(until=longer)
