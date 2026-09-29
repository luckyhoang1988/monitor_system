"""Tests cho transactional-outbox-lite của AlertNotification (_fire_alert/_resolve_alert
ghi status="pending" TRONG transaction commit trạng thái, gửi thật xong mới update — và task
retry_pending_alert_notifications nhặt lại row bị kẹt nếu worker chết giữa 2 bước).

Regression cho phát hiện: "_resolve_alert đánh dấu resolve+commit trước, gửi notification
sau — worker chết giữa 2 bước làm mất RECOVERED vĩnh viễn, không có cơ chế retry." Cùng root
cause cũng áp dụng cho _fire_alert (không được báo cáo nhưng sửa đối xứng).
"""
import pytest
from datetime import datetime, timezone, timedelta
from django.utils import timezone as dj_tz
from apps.alerts.engine import check_device_alerts, retry_pending_alert_notifications
from apps.alerts.models import AlertRule, Alert, AlertNotification
from apps.metrics.models import SystemHealth
from tests.conftest import CiscoSNMPDeviceFactory


def now():
    return datetime.now(tz=timezone.utc)


def since():
    return now() - timedelta(minutes=10)


def make_rule(**kwargs):
    defaults = dict(
        name="Test Rule", device_type="all", metric="cpu_percent", condition="gt",
        threshold=90.0, severity="WARNING", channels=["email"], enabled=True,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


@pytest.fixture
def device(db):
    return CiscoSNMPDeviceFactory(last_seen=dj_tz.now())


@pytest.mark.django_db
class TestFireLeavesNoLeftoverPending:
    def test_fire_success_ends_with_single_sent_row(self, mocker, device):
        mocker.patch("apps.alerts.channels.email_channel.send_email_alert")
        make_rule(channels=["email"])
        SystemHealth.objects.create(device=device, timestamp=now(),
                                    cpu_percent=95.0, mem_percent=50.0)
        check_device_alerts(device, since())
        notifs = AlertNotification.objects.filter(channel="email", kind="fire")
        assert notifs.count() == 1
        assert notifs.first().status == "sent"
        # Không được để sót row "pending" nào — đã update tại chỗ, không tạo thêm.
        assert not AlertNotification.objects.filter(status="pending").exists()


@pytest.mark.django_db
class TestResolveLeavesNoLeftoverPending:
    def test_resolve_success_ends_with_single_sent_recovery_row(self, mocker, device):
        mocker.patch("apps.alerts.channels.email_channel.send_email_alert")
        mock_recovery = mocker.patch("apps.alerts.channels.email_channel.send_email_recovery")
        rule = make_rule(channels=["email"])
        SystemHealth.objects.create(device=device, timestamp=now(),
                                    cpu_percent=95.0, mem_percent=50.0)
        check_device_alerts(device, since())  # fire

        SystemHealth.objects.create(device=device, timestamp=now(),
                                    cpu_percent=10.0, mem_percent=50.0)
        check_device_alerts(device, since())  # resolve

        mock_recovery.assert_called_once()
        recovery_notifs = AlertNotification.objects.filter(channel="email", kind="recovery")
        assert recovery_notifs.count() == 1
        assert recovery_notifs.first().status == "sent"
        assert not AlertNotification.objects.filter(status="pending").exists()


@pytest.mark.django_db
class TestRetryPendingAlertNotifications:
    """Mô phỏng trực tiếp "worker chết giữa lúc commit trạng thái Alert và lúc gửi thật":
    tạo sẵn 1 row AlertNotification(status="pending") KHÔNG thông qua _fire_alert/_resolve_alert
    (đúng như DB sẽ trông thế nào nếu process bị SIGKILL ngay sau transaction commit)."""

    def test_stuck_pending_recovery_gets_sent_on_retry(self, mocker, device):
        mock_recovery = mocker.patch("apps.alerts.channels.email_channel.send_email_recovery")
        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
        stuck = AlertNotification.objects.create(
            alert=alert, channel="email", kind="recovery", status="pending",
        )
        # sent_at auto_now_add lúc create = "vừa rồi" → phải lùi lại quá grace period
        # (mặc định 90s) để mô phỏng "đã kẹt đủ lâu", không phải 1 lần gửi đang chạy hợp lệ.
        AlertNotification.objects.filter(pk=stuck.pk).update(
            sent_at=dj_tz.now() - timedelta(seconds=200)
        )

        retried = retry_pending_alert_notifications(grace_secs=90)

        assert retried == 1
        mock_recovery.assert_called_once()
        stuck.refresh_from_db()
        assert stuck.status == "sent"

    def test_recent_pending_not_retried_yet(self, mocker, device):
        """Row pending mới tạo (<grace_secs) — có thể có tiến trình khác đang xử lý thật,
        KHÔNG được retry ngay kẻo gửi trùng."""
        mock_recovery = mocker.patch("apps.alerts.channels.email_channel.send_email_recovery")
        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
        AlertNotification.objects.create(
            alert=alert, channel="email", kind="recovery", status="pending",
        )

        retried = retry_pending_alert_notifications(grace_secs=90)

        assert retried == 0
        mock_recovery.assert_not_called()

    def test_stuck_pending_fire_gets_sent_on_retry(self, mocker, device):
        """kind lưu tường minh trên row — retry phải gửi ĐÚNG loại (fire, không phải recovery)
        dù is_active tại thời điểm retry có thể đã đổi."""
        mock_fire = mocker.patch("apps.alerts.channels.email_channel.send_email_alert")
        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=True,
        )
        stuck = AlertNotification.objects.create(
            alert=alert, channel="email", kind="fire", status="pending",
        )
        AlertNotification.objects.filter(pk=stuck.pk).update(
            sent_at=dj_tz.now() - timedelta(seconds=200)
        )

        retried = retry_pending_alert_notifications(grace_secs=90)

        assert retried == 1
        mock_fire.assert_called_once()
        stuck.refresh_from_db()
        assert stuck.status == "sent"

    def test_retry_failure_marks_failed_not_stuck_forever(self, mocker, device):
        mocker.patch(
            "apps.alerts.channels.email_channel.send_email_recovery",
            side_effect=Exception("SMTP timeout"),
        )
        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
        stuck = AlertNotification.objects.create(
            alert=alert, channel="email", kind="recovery", status="pending",
        )
        AlertNotification.objects.filter(pk=stuck.pk).update(
            sent_at=dj_tz.now() - timedelta(seconds=200)
        )

        retried = retry_pending_alert_notifications(grace_secs=90)

        assert retried == 1
        stuck.refresh_from_db()
        assert stuck.status == "failed"
        assert "SMTP timeout" in stuck.error
        # Không còn "pending" — sweep lần sau sẽ không nhặt lại nữa (tránh retry vô hạn cho
        # channel lỗi cấu hình vĩnh viễn).
        assert not AlertNotification.objects.filter(status="pending").exists()

    def test_double_claim_second_caller_skips(self, device):
        """Regression đúng phát hiện review — 'claim bằng UPDATE có điều kiện': gọi UPDATE
        pending→processing 2 LẦN liên tiếp trên CÙNG 1 row (mô phỏng 2 lệnh gọi
        retry_pending_alert_notifications chạy chồng nhau) — chỉ lần đầu thành công (trả 1),
        lần 2 phải trả 0 (không claim được) → đây chính là cơ chế chặn gửi trùng, không phụ
        thuộc vào việc test có dựng được 2 thread thật hay không."""
        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
        n = AlertNotification.objects.create(
            alert=alert, channel="email", kind="recovery", status="pending",
        )

        claimed_first = AlertNotification.objects.filter(pk=n.pk, status="pending").update(
            status="processing"
        )
        claimed_second = AlertNotification.objects.filter(pk=n.pk, status="pending").update(
            status="processing"
        )

        assert claimed_first == 1
        assert claimed_second == 0

    def test_fresh_processing_row_not_touched(self, mocker, device):
        """Row đang 'processing' (1 tiến trình khác/chính lệnh gọi gốc đang gửi thật, chưa xong)
        và updated_at còn mới (<stale_processing_secs) — sweep KHÔNG được đụng vào, tránh gửi
        trùng với tiến trình đang chạy hợp lệ."""
        mock_recovery = mocker.patch("apps.alerts.channels.email_channel.send_email_recovery")
        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
        n = AlertNotification.objects.create(
            alert=alert, channel="email", kind="recovery", status="processing",
        )

        retried = retry_pending_alert_notifications(grace_secs=90, stale_processing_secs=300)

        assert retried == 0
        mock_recovery.assert_not_called()
        n.refresh_from_db()
        assert n.status == "processing"

    def test_stale_processing_row_gets_reclaimed(self, mocker, device):
        """Row 'processing' bị bỏ rơi (tiến trình claim đã chết giữa chừng, không kịp chuyển
        sent/failed) — sweep phải thu hồi và gửi lại sau khi vượt stale_processing_secs, đúng
        yêu cầu review 'cần cơ chế thu hồi row processing bị kẹt khi worker chết'."""
        mock_recovery = mocker.patch("apps.alerts.channels.email_channel.send_email_recovery")
        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
        n = AlertNotification.objects.create(
            alert=alert, channel="email", kind="recovery", status="processing",
        )
        AlertNotification.objects.filter(pk=n.pk).update(
            updated_at=dj_tz.now() - timedelta(seconds=400)
        )

        retried = retry_pending_alert_notifications(grace_secs=90, stale_processing_secs=300)

        assert retried == 1
        mock_recovery.assert_called_once()
        n.refresh_from_db()
        assert n.status == "sent"

    def test_stale_owner_cannot_overwrite_reclaimed_result(self, mocker, device):
        """Regression cho review 2026-09-29 ('gửi trùng khi SMTP treo quá stale_processing_secs,
        row không có mã sở hữu để ngăn worker cũ ghi đè kết quả worker mới'). Mô phỏng: 1 row
        đang 'processing' với claim_token của 1 chủ CŨ (đã treo quá lâu, coi như kẹt) — sweep
        reclaim (token mới) + gửi + finalize thành công TRƯỚC. Khi chủ CŨ cuối cùng cũng "gửi
        xong" (dù chậm) rồi cố finalize bằng token cũ của nó, UPDATE phải khớp 0 dòng — không
        được ghi đè kết quả đúng mà sweep vừa ghi."""
        mock_recovery = mocker.patch("apps.alerts.channels.email_channel.send_email_recovery")
        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
        stale_token = "owner-a-token"
        n = AlertNotification.objects.create(
            alert=alert, channel="email", kind="recovery", status="processing",
            claim_token=stale_token,
        )
        AlertNotification.objects.filter(pk=n.pk).update(
            updated_at=dj_tz.now() - timedelta(seconds=400)
        )

        # Sweep coi row kẹt → reclaim (token mới) + gửi + finalize xong trước.
        retried = retry_pending_alert_notifications(grace_secs=90, stale_processing_secs=300)
        assert retried == 1
        mock_recovery.assert_called_once()
        n.refresh_from_db()
        assert n.status == "sent"
        assert n.claim_token != stale_token

        # Chủ CŨ (owner-a) cuối cùng cũng gửi xong (chậm) rồi cố finalize bằng token CŨ của nó —
        # đây chính là câu UPDATE finalize thật trong _dispatch_notifications/retry, viết lại
        # trực tiếp để mô phỏng "worker A tới muộn" mà không cần dựng thread thật.
        overwritten = AlertNotification.objects.filter(
            pk=n.pk, status="processing", claim_token=stale_token,
        ).update(status="failed", error="owner-a tới muộn")

        assert overwritten == 0  # không match được token cũ — không ghi đè
        n.refresh_from_db()
        assert n.status == "sent"  # vẫn giữ đúng kết quả của sweep, không bị ghi đè


@pytest.mark.django_db
class TestLateRecoveryAfterResolve:
    """Regression cho review 2026-09-29 ('có thể gửi cảnh báo sau khi sự cố đã hồi phục mà
    không gửi thông báo hồi phục'). Kịch bản: worker chết SAU khi _fire_alert commit outbox
    fire=pending nhưng TRƯỚC khi kịp dispatch — vòng eval kế tiếp (trên worker khác) thấy metric
    đã hồi phục nên resolve alert. _resolve_alert chỉ queue recovery cho alert có fire đã "sent"
    TẠI THỜI ĐIỂM đó — fire vẫn "pending" nên KHÔNG queue gì. Sau đó sweep mới gửi được fire trễ
    (đã lỗi thời) — nếu không có fix, alert này vĩnh viễn không có recovery theo sau dù đã hồi
    phục thật."""

    def test_fire_confirmed_sent_after_resolve_still_gets_recovery(self, mocker, device):
        mock_fire = mocker.patch("apps.alerts.channels.email_channel.send_email_alert")
        mock_recovery = mocker.patch("apps.alerts.channels.email_channel.send_email_recovery")
        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=True,
        )
        # Outbox fire đã commit (như _fire_alert làm) nhưng dispatch CHƯA chạy — mô phỏng
        # worker chết ngay sau commit transaction, trước khi kịp gửi.
        fire_notif = AlertNotification.objects.create(
            alert=alert, channel="email", kind="fire", status="pending",
        )

        # Vòng eval kế tiếp (worker khác) thấy đã hồi phục → resolve TRƯỚC khi fire kịp gửi.
        from apps.alerts.engine import _resolve_alert
        _resolve_alert(device, rule)
        alert.refresh_from_db()
        assert alert.is_active is False
        mock_recovery.assert_not_called()  # đúng hành vi hiện tại: fire chưa "sent" nên chưa queue
        assert not AlertNotification.objects.filter(alert=alert, kind="recovery").exists()

        # sweep cuối cùng cũng gửi được fire trễ (lỗi thời nhưng vẫn gửi, theo đúng thiết kế
        # "kind lưu tường minh, không suy đoán lại theo is_active hiện tại").
        AlertNotification.objects.filter(pk=fire_notif.pk).update(
            sent_at=dj_tz.now() - timedelta(seconds=200)
        )
        retried = retry_pending_alert_notifications(grace_secs=90)
        assert retried == 1
        mock_fire.assert_called_once()
        fire_notif.refresh_from_db()
        assert fire_notif.status == "sent"

        # Fix: fire "sent" trễ này phải tự phát hiện alert đã resolve từ trước và queue recovery
        # — nếu không có fix, dòng dưới đây fail (không có row recovery nào được tạo).
        recovery_notif = AlertNotification.objects.get(alert=alert, kind="recovery", channel="email")
        assert recovery_notif.status == "pending"

        # Sweep vòng kế tiếp gửi nốt recovery vừa được queue trễ.
        AlertNotification.objects.filter(pk=recovery_notif.pk).update(
            sent_at=dj_tz.now() - timedelta(seconds=200)
        )
        retried_2 = retry_pending_alert_notifications(grace_secs=90)
        assert retried_2 == 1
        mock_recovery.assert_called_once()


@pytest.mark.django_db
class TestFinalizeFireSentAtomicity:
    """Regression cho review vòng 2 (2026-09-29), điểm 'Cao' đầu tiên: bản fix trước gọi
    _queue_late_recovery_if_resolved bằng 1 câu DB RIÊNG, SAU KHI đã commit fire="sent" ở 1 câu
    khác — worker chết đúng giữa 2 câu làm mất VĨNH VIỄN cơ hội tạo recovery (fire đã "sent" là
    trạng thái cuối, không còn gì kích hoạt lại). Fix: _finalize_fire_sent gộp cả 2 vào CHUNG 1
    transaction.atomic() — mô phỏng "crash giữa 2 bước" bằng cách cho bước tạo recovery ném lỗi,
    rồi assert TOÀN BỘ phải rollback (fire KHÔNG được kẹt lại ở "sent" một mình)."""

    def test_crash_between_finalize_and_recovery_rolls_back_both(self, mocker, device):
        from apps.alerts.engine import _finalize_fire_sent

        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
        token = "tok-fire-1"
        n = AlertNotification.objects.create(
            alert=alert, channel="email", kind="fire", status="processing", claim_token=token,
        )
        mocker.patch(
            "apps.alerts.engine._queue_late_recovery_if_resolved",
            side_effect=RuntimeError("mô phỏng worker chết giữa 2 bước"),
        )

        with pytest.raises(RuntimeError):
            _finalize_fire_sent(alert, "email", token)

        n.refresh_from_db()
        # Rollback đúng — fire KHÔNG được kẹt lại ở "sent" khi bước tạo recovery chưa xong, để
        # sweep sau reclaim (row vẫn "processing") và làm lại nguyên vẹn cả 2 bước.
        assert n.status == "processing"
        assert not AlertNotification.objects.filter(alert=alert, kind="recovery").exists()

    def test_success_path_still_finalizes_and_queues_recovery_together(self, device):
        """Đường thành công (không crash) vẫn phải cho ra đúng kết quả cũ — cả sent lẫn recovery
        cùng có mặt sau 1 lệnh gọi, không phải chỉ khi lỗi mới rollback."""
        from apps.alerts.engine import _finalize_fire_sent

        rule = make_rule(channels=["email"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
        token = "tok-fire-2"
        n = AlertNotification.objects.create(
            alert=alert, channel="email", kind="fire", status="processing", claim_token=token,
        )

        finalized = _finalize_fire_sent(alert, "email", token)

        assert finalized is True
        n.refresh_from_db()
        assert n.status == "sent"
        recovery = AlertNotification.objects.get(alert=alert, channel="email", kind="recovery")
        assert recovery.status == "pending"


@pytest.mark.django_db
class TestResolvePerChannelRecoveryDecision:
    """Regression cho review vòng 2 (2026-09-29), điểm bổ sung: rule nhiều channel — bản cũ tạo
    recovery cho MỌI channel của rule chỉ cần 1 channel đã gửi fire xong, kể cả channel khác còn
    chưa từng gửi fire (SMTP treo, hàng đợi chậm...) → channel đó có thể nhận RECOVERED TRƯỚC khi
    từng nhận ALERT. Fix: quyết định theo từng (alert, channel)."""

    def test_resolve_only_queues_recovery_for_channels_whose_fire_was_sent(self, mocker, device):
        mock_recovery_email = mocker.patch("apps.alerts.channels.email_channel.send_email_recovery")
        mock_recovery_tg = mocker.patch("apps.alerts.channels.telegram.send_telegram_recovery")
        rule = make_rule(channels=["email", "telegram"])
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=True,
        )
        # telegram đã gửi fire xong; email vẫn "pending" (mô phỏng SMTP treo, chưa gửi được).
        AlertNotification.objects.create(alert=alert, channel="telegram", kind="fire", status="sent")
        AlertNotification.objects.create(alert=alert, channel="email", kind="fire", status="pending")

        from apps.alerts.engine import _resolve_alert
        _resolve_alert(device, rule)

        mock_recovery_tg.assert_called_once()
        mock_recovery_email.assert_not_called()
        assert AlertNotification.objects.filter(
            alert=alert, channel="telegram", kind="recovery"
        ).exists()
        assert not AlertNotification.objects.filter(
            alert=alert, channel="email", kind="recovery"
        ).exists()
