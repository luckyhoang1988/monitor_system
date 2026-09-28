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
