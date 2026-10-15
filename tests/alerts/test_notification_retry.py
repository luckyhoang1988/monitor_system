"""Retry có giới hạn cho thông báo lỗi tạm thời (review 2026-10-15, hạng mục 1.2).

Bằng chứng prod (đo 2026-10-15, 1.843 dòng AlertNotification): 309 dòng `failed` — 172 là 429, 124 lỗi
kết nối, 12 SoftTimeLimitExceeded, 1 là 4xx — và sweep cũ chỉ nhặt `pending`/`processing` nên `failed` là
trạng thái cuối, KHÔNG BAO GIỜ thử lại. Toàn bộ 309 dòng đó thuộc alert đã hồi phục → thông báo cũ KHÔNG
được tự phát lại (test `test_historic_failed_rows_are_never_resent`).
"""
import threading
from datetime import timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.db import connection
from django.utils import timezone as dj_tz

from apps.alerts import cooldown, engine
from apps.alerts.channels import (
    ChannelNotConfigured,
    ChannelPermanentError,
    ChannelTransientError,
)
from apps.alerts.engine import _dispatch_notifications, _resolve_alert, retry_pending_alert_notifications
from apps.alerts.models import Alert, AlertNotification, AlertRule, ChannelCooldown
from tests.conftest import CiscoSNMPDeviceFactory

TOKEN = "123456789:AAH-secretTokenValue_xyz"
TG_FIRE = "apps.alerts.channels.telegram.send_telegram_alert"
TG_RECOVERY = "apps.alerts.channels.telegram.send_telegram_recovery"
EMAIL_FIRE = "apps.alerts.channels.email_channel.send_email_alert"


@pytest.fixture
def device(db):
    return CiscoSNMPDeviceFactory(last_seen=dj_tz.now())


@pytest.fixture
def rule(db):
    return AlertRule.objects.create(
        name="CPU", device_type="all", metric="cpu_percent", condition="gt", threshold=90.0,
        severity="WARNING", channels=["telegram"], enabled=True,
    )


@pytest.fixture(autouse=True)
def no_real_sleep(mocker):
    return mocker.patch("apps.alerts.engine.time.sleep")


@pytest.fixture(autouse=True)
def telegram_credentials(settings):
    """Cooldown khoá theo bot/chat nên phải có thông tin đăng nhập Telegram (môi trường test để trống)."""
    settings.TELEGRAM_BOT_TOKEN = TOKEN
    settings.TELEGRAM_CHAT_ID = "-1001234"


def make_alert(device, rule, *, active=True):
    return Alert.objects.create(
        device=device, rule=rule, severity="WARNING", message="High CPU", metric_value=95.0,
        is_active=active, resolved_at=None if active else dj_tz.now(),
    )


def make_row(alert, *, channel="telegram", kind="fire", status="pending", age_secs=200,
             next_in=None, attempts=0, error=""):
    """Row outbox ở trạng thái mong muốn. `age_secs` lùi sent_at; `next_in` (giây, âm = đã đến hạn)."""
    row = AlertNotification.objects.create(
        alert=alert, channel=channel, kind=kind, status=status, attempts=attempts, error=error,
        next_attempt_at=None if next_in is None else dj_tz.now() + timedelta(seconds=next_in),
    )
    AlertNotification.objects.filter(pk=row.pk).update(sent_at=dj_tz.now() - timedelta(seconds=age_secs))
    row.refresh_from_db()
    return row


@pytest.mark.django_db
class TestInlineFailureClassification:
    """_dispatch_notifications (lần gửi ĐẦU, ngay trong tác vụ poll)."""

    def dispatch(self, mocker, device, rule, side_effect):
        mocker.patch(TG_FIRE, side_effect=side_effect)
        alert = make_alert(device, rule)
        row = make_row(alert, age_secs=0)
        _dispatch_notifications(alert, ["telegram"], "fire")
        row.refresh_from_db()
        return row

    def test_429_is_rescheduled_with_retry_after_not_failed(self, mocker, device, rule):
        before = dj_tz.now()
        row = self.dispatch(mocker, device, rule, ChannelTransientError("Telegram HTTP 429", retry_after=40))
        assert row.status == "pending"
        assert row.attempts == 1
        assert before + timedelta(seconds=40) <= row.next_attempt_at <= before + timedelta(seconds=120)
        assert "429" in row.error

    def test_transient_without_retry_after_uses_backoff_schedule(self, mocker, device, rule):
        before = dj_tz.now()
        row = self.dispatch(mocker, device, rule, ChannelTransientError("Telegram: lỗi mạng"))
        assert row.status == "pending"
        assert before + timedelta(seconds=25) <= row.next_attempt_at <= before + timedelta(seconds=90)

    def test_permanent_error_is_final(self, mocker, device, rule):
        row = self.dispatch(mocker, device, rule, ChannelPermanentError("Telegram HTTP 400: chat not found"))
        assert row.status == "failed"
        assert row.next_attempt_at is None
        assert "chat not found" in row.error

    def test_not_configured_is_failed_with_reason_not_fake_sent(self, mocker, device, rule):
        row = self.dispatch(mocker, device, rule, ChannelNotConfigured("Chưa cấu hình TELEGRAM_BOT_TOKEN"))
        assert row.status == "failed"
        assert "TELEGRAM_BOT_TOKEN" in row.error

    def test_unknown_exception_is_final_and_token_is_scrubbed(self, mocker, device, rule):
        row = self.dispatch(
            mocker, device, rule,
            Exception(f"boom https://api.telegram.org/bot{TOKEN}/sendMessage"),
        )
        assert row.status == "failed"
        assert TOKEN not in row.error
        assert "AAH-secretTokenValue_xyz" not in row.error

    def test_soft_time_limit_reschedules_and_reraises(self, mocker, device, rule):
        """A9: poll task bị cắt đúng lúc đang gọi Telegram. Trước đây `except Exception` nuốt và ghi
        `failed` vĩnh viễn (12 dòng prod). Nay: giữ ở pending để sweep gửi lại, và raise lại để tác vụ
        poll dừng đúng nghĩa."""
        mocker.patch(TG_FIRE, side_effect=SoftTimeLimitExceeded())
        alert = make_alert(device, rule)
        row = make_row(alert, age_secs=0)
        with pytest.raises(SoftTimeLimitExceeded):
            _dispatch_notifications(alert, ["telegram"], "fire")
        row.refresh_from_db()
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.next_attempt_at is not None

    def test_soft_time_limit_leaves_other_channels_for_the_sweep(self, mocker, device, rule):
        mocker.patch(TG_FIRE, side_effect=SoftTimeLimitExceeded())
        mocker.patch(EMAIL_FIRE)
        alert = make_alert(device, rule)
        make_row(alert, channel="telegram", age_secs=0)
        email = make_row(alert, channel="email", age_secs=0)
        with pytest.raises(SoftTimeLimitExceeded):
            _dispatch_notifications(alert, ["telegram", "email"], "fire")
        email.refresh_from_db()
        assert email.status == "pending" and email.attempts == 0  # chưa claim → sweep nhặt sau grace


@pytest.mark.django_db
class TestSweepHonoursSchedule:
    def test_row_scheduled_in_future_is_not_retried_early(self, mocker, device, rule):
        send = mocker.patch(TG_FIRE)
        row = make_row(make_alert(device, rule), age_secs=600, next_in=300, attempts=1)
        assert retry_pending_alert_notifications() == 0
        send.assert_not_called()
        row.refresh_from_db()
        assert row.status == "pending" and row.attempts == 1

    def test_row_due_is_sent_and_marked_sent(self, mocker, device, rule):
        send = mocker.patch(TG_FIRE)
        row = make_row(make_alert(device, rule), age_secs=600, next_in=-5, attempts=1)
        assert retry_pending_alert_notifications() == 1
        send.assert_called_once()
        row.refresh_from_db()
        assert row.status == "sent"
        assert row.attempts == 2

    def test_transient_failure_on_sweep_reschedules_with_growing_attempts(self, mocker, device, rule):
        mocker.patch(TG_FIRE, side_effect=ChannelTransientError("Telegram HTTP 503"))
        row = make_row(make_alert(device, rule), age_secs=600, next_in=-5, attempts=1)
        before = dj_tz.now()
        assert retry_pending_alert_notifications() == 1
        row.refresh_from_db()
        assert row.status == "pending"
        assert row.attempts == 2
        assert row.next_attempt_at > before + timedelta(seconds=50)  # bậc backoff thứ 2 > bậc 1

    def test_gives_up_after_max_attempts(self, mocker, device, rule, settings):
        settings.ALERT_NOTIFY_MAX_ATTEMPTS = 3
        mocker.patch(TG_FIRE, side_effect=ChannelTransientError("Telegram HTTP 503"))
        row = make_row(make_alert(device, rule), age_secs=600, next_in=-5, attempts=2)
        assert retry_pending_alert_notifications() == 1
        row.refresh_from_db()
        assert row.status == "failed"
        assert row.next_attempt_at is None
        assert "3" in row.error  # nói rõ đã thử mấy lần

    def test_row_already_over_max_attempts_is_not_sent_again(self, mocker, device, rule, settings):
        """Tiến trình bị SIGKILL giữa chừng nhiều lần: mỗi lần reclaim vẫn tăng attempts nên không
        thể quay vô hạn."""
        settings.ALERT_NOTIFY_MAX_ATTEMPTS = 3
        send = mocker.patch(TG_FIRE)
        row = make_row(make_alert(device, rule), status="processing", age_secs=900, attempts=3)
        AlertNotification.objects.filter(pk=row.pk).update(updated_at=dj_tz.now() - timedelta(seconds=900))
        retry_pending_alert_notifications()
        send.assert_not_called()
        row.refresh_from_db()
        assert row.status == "failed"

    def test_historic_failed_rows_are_never_resent(self, mocker, device, rule):
        """309 dòng failed cũ trên prod đều thuộc alert đã hồi phục — phát lại là bắn cảnh báo hết hiệu
        lực. Sweep chỉ làm việc với pending/processing."""
        send = mocker.patch(TG_FIRE)
        row = make_row(make_alert(device, rule, active=False), status="failed", age_secs=86400 * 3,
                       error="429 Client Error")
        assert retry_pending_alert_notifications() == 0
        send.assert_not_called()
        row.refresh_from_db()
        assert row.status == "failed"


@pytest.mark.django_db
class TestStaleAlertState:
    """'Các thông báo cũ cần xét riêng theo trạng thái hiện tại để tránh phát cảnh báo đã hết hiệu lực.'"""

    def test_old_fire_for_resolved_alert_expires_without_sending(self, mocker, device, rule, settings):
        settings.ALERT_NOTIFY_FIRE_RESOLVED_MAX_AGE_SECS = 3600
        send = mocker.patch(TG_FIRE)
        alert = make_alert(device, rule, active=False)
        row = make_row(alert, age_secs=7200)
        retry_pending_alert_notifications()
        send.assert_not_called()
        row.refresh_from_db()
        assert row.status == "failed"
        assert "hồi phục" in row.error
        assert not AlertNotification.objects.filter(alert=alert, kind="recovery").exists()

    def test_recent_fire_for_resolved_alert_is_still_sent_then_recovery_follows(self, mocker, device, rule):
        """Hành vi đã được review 2026-09-29 duyệt (late recovery) — không đổi trong cửa sổ hợp lệ."""
        mocker.patch(TG_FIRE)
        alert = make_alert(device, rule, active=False)
        row = make_row(alert, age_secs=300)
        retry_pending_alert_notifications()
        row.refresh_from_db()
        assert row.status == "sent"
        assert AlertNotification.objects.filter(alert=alert, kind="recovery", status="pending").exists()

    def test_old_fire_for_still_active_alert_is_sent(self, mocker, device, rule, settings):
        """Sự cố còn đang diễn ra thì thông báo cũ vẫn còn giá trị — không được âm thầm bỏ."""
        settings.ALERT_NOTIFY_FIRE_RESOLVED_MAX_AGE_SECS = 3600
        send = mocker.patch(TG_FIRE)
        row = make_row(make_alert(device, rule, active=True), age_secs=7200)
        retry_pending_alert_notifications()
        send.assert_called_once()
        row.refresh_from_db()
        assert row.status == "sent"

    def test_old_recovery_is_still_sent(self, mocker, device, rule, settings):
        """RECOVERED luôn đúng — bỏ nó khiến người trực tưởng sự cố còn đó."""
        settings.ALERT_NOTIFY_FIRE_RESOLVED_MAX_AGE_SECS = 3600
        send = mocker.patch(TG_RECOVERY)
        row = make_row(make_alert(device, rule, active=False), kind="recovery", age_secs=7200)
        retry_pending_alert_notifications()
        send.assert_called_once()
        row.refresh_from_db()
        assert row.status == "sent"


@pytest.mark.django_db
class TestSweepPacing:
    def test_flood_control_stops_further_telegram_sends_in_the_same_sweep(self, mocker, device, rule):
        send = mocker.patch(TG_FIRE, side_effect=ChannelTransientError("Telegram HTTP 429", retry_after=30))
        mocker.patch(EMAIL_FIRE)
        a = make_alert(device, rule)
        first = make_row(a, age_secs=600, next_in=-10, attempts=1)
        second = make_row(make_alert(device, rule), age_secs=500, next_in=-10, attempts=1)
        email = make_row(make_alert(device, rule), channel="email", age_secs=400, next_in=-10, attempts=1)
        before = dj_tz.now()

        retry_pending_alert_notifications()

        assert send.call_count == 1  # 429 rồi thì không dồn thêm tin nào vào Telegram trong vòng này
        first.refresh_from_db(); second.refresh_from_db(); email.refresh_from_db()
        assert first.status == "pending" and first.next_attempt_at >= before + timedelta(seconds=30)
        assert second.status == "pending" and second.attempts == 1  # không bị claim, không tốn lượt
        assert email.status == "sent"  # kênh khác vẫn chạy bình thường

    def test_consecutive_transient_failures_stop_the_channel_for_the_rest_of_the_sweep(
        self, mocker, device, rule
    ):
        """Mạng/Telegram đang sập: không có retry_after để nghe theo, nhưng thử tiếp 5 dòng chỉ đốt 5
        lượt thử và mất cả ngân sách vòng. Sau chuỗi lỗi liên tiếp thì dừng kênh đó đến hết vòng."""
        send = mocker.patch(TG_FIRE, side_effect=ChannelTransientError("Telegram: không kết nối được"))
        rows = [
            make_row(make_alert(device, rule), age_secs=600 - i, next_in=-10, attempts=1)
            for i in range(5)
        ]
        retry_pending_alert_notifications()
        assert send.call_count == engine.NOTIFY_SWEEP_TRANSIENT_STREAK
        for r in rows:
            r.refresh_from_db()
        assert sorted(r.attempts for r in rows) == [1, 1, 2, 2, 2]  # 2 dòng sau cùng không bị claim
        assert all(r.status == "pending" for r in rows)

    def test_paces_consecutive_telegram_sends(self, mocker, device, rule, settings, no_real_sleep):
        settings.ALERT_NOTIFY_TELEGRAM_MIN_INTERVAL_SECS = 3.0
        mocker.patch(TG_FIRE)
        make_row(make_alert(device, rule), age_secs=600, next_in=-10, attempts=1)
        make_row(make_alert(device, rule), age_secs=500, next_in=-10, attempts=1)
        assert retry_pending_alert_notifications() == 2
        waits = [c.args[0] for c in no_real_sleep.call_args_list]
        assert len(waits) == 1 and 2.0 < waits[0] <= 3.0  # chỉ chờ GIỮA 2 tin, không chờ trước tin đầu

    def test_sweep_budget_leaves_the_rest_for_next_run(self, mocker, device, rule, settings):
        settings.ALERT_NOTIFY_SWEEP_BUDGET_SECS = 0
        send = mocker.patch(TG_FIRE)
        row = make_row(make_alert(device, rule), age_secs=600, next_in=-10, attempts=1)
        assert retry_pending_alert_notifications() == 0
        send.assert_not_called()
        row.refresh_from_db()
        assert row.status == "pending" and row.attempts == 1


@pytest.mark.django_db
class TestOrderingAndOwnership:
    def test_fire_retried_after_alert_resolved_still_precedes_recovery(self, mocker, device, rule):
        """ALERT → RECOVERED: fire gửi lỗi tạm thời, alert hồi phục trong lúc chờ → resolve không có fire
        'sent' nên chưa tạo recovery; khi fire gửi được thì recovery trễ được xếp và đi SAU nó."""
        calls = []
        mocker.patch(TG_FIRE, side_effect=lambda a: calls.append("fire"))
        mocker.patch(TG_RECOVERY, side_effect=lambda a: calls.append("recovery"))
        alert = make_alert(device, rule, active=True)
        fire = make_row(alert, age_secs=120, next_in=-1, attempts=1)

        _resolve_alert(device, rule)
        assert not AlertNotification.objects.filter(alert=alert, kind="recovery").exists()

        retry_pending_alert_notifications()
        fire.refresh_from_db()
        assert fire.status == "sent"
        recovery = AlertNotification.objects.get(alert=alert, kind="recovery")
        assert recovery.status == "pending"

        AlertNotification.objects.filter(pk=recovery.pk).update(sent_at=dj_tz.now() - timedelta(seconds=200))
        retry_pending_alert_notifications()
        recovery.refresh_from_db()
        assert recovery.status == "sent"
        assert calls == ["fire", "recovery"]

    def test_stale_owner_cannot_reschedule_over_a_newer_owner(self, mocker, device, rule):
        """claim_token: chủ cũ gửi chậm rồi báo lỗi tạm thời — không được ghi đè kết quả của chủ mới."""
        alert = make_alert(device, rule)
        row = make_row(alert, age_secs=600, next_in=-5, attempts=1)

        def newer_owner_wins(_alert):
            AlertNotification.objects.filter(pk=row.pk).update(
                status="sent", claim_token="newer-owner-token",
            )
            raise ChannelTransientError("Telegram HTTP 503")

        mocker.patch(TG_FIRE, side_effect=newer_owner_wins)
        retry_pending_alert_notifications()
        row.refresh_from_db()
        assert row.status == "sent"
        assert row.claim_token == "newer-owner-token"
        assert row.next_attempt_at is not None  # giá trị cũ không bị đụng tới (không reschedule thêm)

    def test_every_claim_issues_a_new_token(self, mocker, device, rule):
        mocker.patch(TG_FIRE, side_effect=ChannelTransientError("Telegram HTTP 503"))
        row = make_row(make_alert(device, rule), age_secs=600, next_in=-5, attempts=0)
        row.claim_token = "t0"
        row.save(update_fields=["claim_token"])
        retry_pending_alert_notifications()
        row.refresh_from_db()
        assert row.claim_token not in ("", "t0")


@pytest.mark.django_db
class TestSharedCooldown:
    """Review 2026-10-15 (P2): `held_until` chỉ sống trong 1 vòng quét. Đã tái hiện: nhận retry_after=600
    nhưng 60s sau vòng kế vẫn gửi tin khác, và gửi trực tiếp (từ tác vụ poll) bỏ qua cooldown hoàn toàn →
    tiếp tục 429 và đốt lượt thử. Cooldown phải dùng chung theo bot/chat cho MỌI đường gửi."""

    @staticmethod
    def key():
        return engine._channel_cooldown_key("telegram")

    def test_cooldown_survives_into_the_next_sweep(self, mocker, device, rule):
        send = mocker.patch(TG_FIRE, side_effect=[
            ChannelTransientError("Telegram HTTP 429", retry_after=600), None,
        ])
        make_row(make_alert(device, rule), age_secs=600, next_in=-10, attempts=1)
        second = make_row(make_alert(device, rule), age_secs=500, next_in=-10, attempts=1)

        retry_pending_alert_notifications()   # vòng 1 nhận 429 (retry_after=600)
        assert send.call_count == 1
        retry_pending_alert_notifications()   # vòng kế (60s sau trong thực tế): cooldown vẫn còn
        assert send.call_count == 1

        second.refresh_from_db()
        assert second.status == "pending"
        assert second.attempts == 1  # không bị claim → không tốn lượt thử

    def test_direct_dispatch_respects_cooldown_and_spends_no_attempt(self, mocker, device, rule):
        send = mocker.patch(TG_FIRE)
        cooldown.register(self.key(), 600)
        alert = make_alert(device, rule)
        row = make_row(alert, age_secs=0)
        before = dj_tz.now()

        _dispatch_notifications(alert, ["telegram"], "fire")

        send.assert_not_called()
        row.refresh_from_db()
        assert row.status == "pending"
        assert row.attempts == 0
        # hẹn đúng lúc hết cooldown để sweep không phải đoán
        assert row.next_attempt_at >= before + timedelta(seconds=590)

    def test_direct_dispatch_that_gets_429_registers_a_shared_cooldown(self, mocker, device, rule):
        mocker.patch(TG_FIRE, side_effect=ChannelTransientError("Telegram HTTP 429", retry_after=600))
        alert = make_alert(device, rule)
        make_row(alert, age_secs=0)
        before = dj_tz.now()

        _dispatch_notifications(alert, ["telegram"], "fire")

        until = ChannelCooldown.objects.get(key=self.key()).until
        assert before + timedelta(seconds=595) <= until <= before + timedelta(seconds=620)

    def test_cooldown_applies_to_the_next_alert_sent_directly(self, mocker, device, rule):
        """Đúng kịch bản review: lần gửi trước nhận 429, tác vụ poll khác gửi tin mới ngay sau đó."""
        send = mocker.patch(TG_FIRE, side_effect=[
            ChannelTransientError("Telegram HTTP 429", retry_after=600), None,
        ])
        a1 = make_alert(device, rule)
        make_row(a1, age_secs=0)
        _dispatch_notifications(a1, ["telegram"], "fire")
        a2 = make_alert(device, rule)
        r2 = make_row(a2, age_secs=0)

        _dispatch_notifications(a2, ["telegram"], "fire")

        assert send.call_count == 1  # tin thứ hai KHÔNG chạm Telegram
        r2.refresh_from_db()
        assert r2.status == "pending" and r2.attempts == 0

    def test_cooldown_is_scoped_to_bot_and_chat(self, mocker, device, rule, settings):
        send = mocker.patch(TG_FIRE)
        cooldown.register(self.key(), 600)
        settings.TELEGRAM_CHAT_ID = "-999888"   # nhóm khác → không bị chặn
        alert = make_alert(device, rule)
        make_row(alert, age_secs=0)

        _dispatch_notifications(alert, ["telegram"], "fire")

        send.assert_called_once()

    def test_cooldown_does_not_block_other_channels(self, mocker, device, rule):
        mocker.patch(TG_FIRE)
        email = mocker.patch(EMAIL_FIRE)
        cooldown.register(self.key(), 600)
        alert = make_alert(device, rule)
        make_row(alert, channel="telegram", age_secs=0)
        make_row(alert, channel="email", age_secs=0)

        _dispatch_notifications(alert, ["telegram", "email"], "fire")

        email.assert_called_once()

    def test_sends_resume_after_cooldown_expires(self, mocker, device, rule):
        send = mocker.patch(TG_FIRE)
        cooldown.register(self.key(), 600)
        ChannelCooldown.objects.filter(key=self.key()).update(until=dj_tz.now() - timedelta(seconds=1))
        row = make_row(make_alert(device, rule), age_secs=600, next_in=-10, attempts=1)

        assert retry_pending_alert_notifications() == 1

        send.assert_called_once()
        row.refresh_from_db()
        assert row.status == "sent"

    def test_shorter_retry_after_never_shortens_an_existing_cooldown(self, db):
        cooldown.register("telegram:test", 600)
        cooldown.register("telegram:test", 5)
        assert cooldown.remaining_secs("telegram:test") > 590

    def test_absurd_retry_after_is_capped(self, mocker, device, rule):
        mocker.patch(TG_FIRE, side_effect=ChannelTransientError("Telegram HTTP 429", retry_after=999999))
        alert = make_alert(device, rule)
        make_row(alert, age_secs=0)
        _dispatch_notifications(alert, ["telegram"], "fire")
        assert cooldown.remaining_secs(self.key()) <= engine.NOTIFY_RETRY_AFTER_CAP_SECS + 5


@pytest.mark.django_db(transaction=True)
def test_cooldown_register_is_safe_when_many_processes_hit_a_429_at_once():
    """Nhiều tác vụ poll cùng nhận 429 ngay lần đầu (chưa có dòng cooldown nào): chỉ 1 dòng được tạo,
    không ai ném IntegrityError, và mốc DÀI NHẤT thắng — chạy thật trên Postgres bằng nhiều luồng."""
    errors = []
    barrier = threading.Barrier(8)

    def worker(seconds):
        try:
            barrier.wait()
            cooldown.register("telegram:race", seconds)
        except Exception as exc:  # noqa: BLE001 — ghi lại để assert bên dưới
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(60 + i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert ChannelCooldown.objects.filter(key="telegram:race").count() == 1
    assert cooldown.remaining_secs("telegram:race") > 65  # 60..67 → thắng là 67


def test_retry_task_is_scheduled_with_matching_expire_seconds():
    """Bẫy đã dính 2026-07-07: thiếu `expire_seconds` thì mỗi lần beat restart reset expires về None."""
    from django.conf import settings
    entry = settings.CELERY_BEAT_SCHEDULE["retry-pending-alert-notifications"]
    assert entry["schedule"] <= 60
    assert entry["options"]["expire_seconds"] == entry["options"]["expires"]


def test_every_beat_entry_with_expires_also_sets_expire_seconds():
    from django.conf import settings
    missing = [
        name for name, entry in settings.CELERY_BEAT_SCHEDULE.items()
        if "expires" in entry.get("options", {}) and "expire_seconds" not in entry["options"]
    ]
    assert missing == []
