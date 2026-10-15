"""Migration dữ liệu 0010: dọn token bot Telegram đã lỡ lưu trong AlertNotification.error (hạng mục 1.3).

Đo prod 2026-10-15: token chỉ có ở `alerts_alertnotification.error` — 294 dòng, 2 hình dạng (URL đầy đủ và
`url: /bot<token>/sendMessage` không có host). Token dưới đây là giá trị GIẢ, cùng độ dài/định dạng.
"""
import importlib
import re
from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from apps.alerts.models import Alert, AlertNotification, AlertRule
from tests.conftest import CiscoSNMPDeviceFactory

migration = importlib.import_module("apps.alerts.migrations.0010_scrub_bot_tokens_in_notification_errors")

TOKEN = "987654321:AAGfAKEtokenValue-0123456789_abcdefg"  # 9 chữ số : 35 ký tự, như token thật
LEAK = re.compile(r"[0-9]{6,}:[A-Za-z0-9_-]{20,}")

SHAPES = {
    "http429": f"429 Client Error: Too Many Requests for url: https://api.telegram.org/bot{TOKEN}/sendMessage",
    "http400": f"400 Client Error: Bad Request for url: https://api.telegram.org/bot{TOKEN}/sendMessage",
    "pool": (f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: "
             f"/bot{TOKEN}/sendMessage (Caused by ConnectTimeoutError('timed out'))"),
}


@pytest.fixture
def alert(db):
    device = CiscoSNMPDeviceFactory()
    rule = AlertRule.objects.create(
        name="R", device_type="all", metric="cpu_percent", condition="gt", threshold=90.0,
        severity="WARNING", channels=["telegram"], enabled=True,
    )
    return Alert.objects.create(device=device, rule=rule, severity="WARNING", message="m",
                                metric_value=95.0, is_active=False)


def make(alert, error, status="failed"):
    n = AlertNotification.objects.create(alert=alert, channel="telegram", kind="fire", status=status, error=error)
    AlertNotification.objects.filter(pk=n.pk).update(updated_at=timezone.now() - timedelta(days=30))
    n.refresh_from_db()
    return n


@pytest.mark.django_db
class TestScrubTokens:
    def test_every_real_shape_loses_the_token_but_keeps_the_diagnosis(self, alert):
        rows = {name: make(alert, text) for name, text in SHAPES.items()}

        migration.scrub_tokens(django_apps, None)

        for name, n in rows.items():
            n.refresh_from_db()
            assert not LEAK.search(n.error), name
            assert "AAGfAKEtokenValue" not in n.error
            assert "987654321" not in n.error
        rows["http429"].refresh_from_db()
        assert "429 Client Error: Too Many Requests" in rows["http429"].error
        assert "api.telegram.org/bot" in rows["http429"].error and "/sendMessage" in rows["http429"].error
        rows["pool"].refresh_from_db()
        assert "Max retries exceeded" in rows["pool"].error and "ConnectTimeoutError" in rows["pool"].error

    def test_rows_without_token_are_untouched(self, alert):
        clean = make(alert, "SMTP timeout")
        empty = make(alert, "")
        ts = make(alert, "Telegram HTTP 429 (chờ 35s)")
        before = {n.pk: (n.error, n.updated_at) for n in (clean, empty, ts)}

        migration.scrub_tokens(django_apps, None)

        for pk, (error, updated_at) in before.items():
            n = AlertNotification.objects.get(pk=pk)
            assert (n.error, n.updated_at) == (error, updated_at)

    def test_updated_at_is_not_bumped_on_scrubbed_rows(self, alert):
        """`updated_at` là mốc phát hiện dòng `processing` bị kẹt — dọn dữ liệu không được làm nó đổi."""
        n = make(alert, SHAPES["http429"])
        before = n.updated_at
        migration.scrub_tokens(django_apps, None)
        n.refresh_from_db()
        assert n.updated_at == before

    def test_second_run_changes_nothing(self, alert):
        n = make(alert, SHAPES["pool"])
        migration.scrub_tokens(django_apps, None)
        n.refresh_from_db()
        once = n.error
        migration.scrub_tokens(django_apps, None)
        n.refresh_from_db()
        assert n.error == once

    def test_unrelated_long_number_colon_text_is_left_alone(self, alert):
        """Chuỗi có dấu ':' nhưng không đủ dạng token (số ngắn / phần sau ngắn) không bị đụng."""
        n = make(alert, "timeout 12:30:45 host 10.0.0.1:443 port 12345:short")
        migration.scrub_tokens(django_apps, None)
        n.refresh_from_db()
        assert n.error == "timeout 12:30:45 host 10.0.0.1:443 port 12345:short"
