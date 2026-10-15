"""Hạng mục 1.1 (A1, 2026-10-15): flapping-suppress không được chặn VĨNH VIỄN thông báo của 1 sự cố thật.

Gốc: lần fire thứ N trong cửa sổ flapping tạo Alert `is_active` nhưng KHÔNG tạo AlertNotification nào
(`_fire_alert`); các vòng sau thấy alert đang active nên im lặng cho tới khi tự hồi phục — và vì fire
chưa từng gửi nên `_resolve_alert` cũng không gửi RECOVERED. Người trực không bao giờ biết.

Số đo prod (2026-10-15, 1.008 alert / 112 ngày): 6 alert bị suppress, 5 hồi phục trong <5 phút, 1 ca thật —
alert #662 (X4_SW1_CuoiX4_6U_NhaAn, "Thiết bị offline") active 19,7 ngày vì thiết bị thật sự mất kết nối
(không có dòng SystemHealthHourly nào 09-08→09-26), không một thông báo nào về nó sau lần fire thứ 4.

Hành vi mới: alert đang active, chưa từng có fire notification (= bị suppress), đã kéo dài
`ALERT_FLAP_RENOTIFY_MIN` phút (mặc định 30) mà điều kiện vẫn đúng → queue ĐÚNG 1 fire notification
(trần: có dòng fire thì không nhắc lại), qua outbox như fire thường để recovery/late-recovery giữ nguyên.
"""
from datetime import UTC, datetime, timedelta

import pytest
from django.db.models import F
from django.utils import timezone as dj_tz

from apps.alerts.engine import check_device_alerts, retry_pending_alert_notifications
from apps.alerts.models import Alert, AlertNotification, AlertRule
from apps.metrics.models import SystemHealth
from tests.conftest import CiscoSNMPDeviceFactory

SEND = "apps.alerts.channels.telegram.send_telegram_alert"
RECOVER = "apps.alerts.channels.telegram.send_telegram_recovery"


def now():
    return datetime.now(tz=UTC)


def since():
    return now() - timedelta(minutes=10)


def make_rule(**kwargs):
    defaults = dict(
        name="Flap Rule", device_type="all", metric="cpu_percent", condition="gt",
        threshold=90.0, severity="WARNING", channels=["telegram"], enabled=True,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


def set_cpu(device, value):
    SystemHealth.objects.create(device=device, timestamp=now(), cpu_percent=value, mem_percent=50.0)


def age_all_alerts(minutes):
    """Lùi MỌI alert đi `minutes` phút — giữ nguyên khoảng cách tương đối giữa các lần fire."""
    Alert.objects.update(triggered_at=F("triggered_at") - timedelta(minutes=minutes))


@pytest.fixture(autouse=True)
def flap_settings(settings):
    settings.ALERT_FLAP_THRESHOLD = 4
    settings.ALERT_FLAP_WINDOW_MIN = 30
    settings.ALERT_FLAP_RENOTIFY_MIN = 30


@pytest.fixture
def device(db):
    return CiscoSNMPDeviceFactory(last_seen=dj_tz.now())


def make_suppressed_alert(device, rule):
    """3 fire đã resolve trong cửa sổ + 1 fire đang chạy → lần thứ 4 bị flapping-suppress."""
    for _ in range(3):
        Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=False, resolved_at=now(),
        )
    set_cpu(device, 96.0)
    check_device_alerts(device, since())
    active = Alert.objects.get(device=device, rule=rule, is_active=True)
    assert not AlertNotification.objects.filter(alert=active).exists(), "fixture: alert phải bị suppress"
    return active


@pytest.mark.django_db
class TestRenotifyWhenStillActive:
    def test_suppressed_alert_is_notified_once_after_renotify_minutes(self, mocker, device):
        send = mocker.patch(SEND)
        rule = make_rule()
        active = make_suppressed_alert(device, rule)
        send.assert_not_called()

        age_all_alerts(31)
        set_cpu(device, 97.0)
        check_device_alerts(device, since())

        send.assert_called_once()
        rows = AlertNotification.objects.filter(alert=active, kind="fire")
        assert rows.count() == 1
        assert rows.first().status == "sent"

    def test_message_says_why_it_was_held_back(self, mocker, device):
        mocker.patch(SEND)
        rule = make_rule()
        active = make_suppressed_alert(device, rule)
        original = active.message
        age_all_alerts(31)
        set_cpu(device, 97.0)
        check_device_alerts(device, since())

        active.refresh_from_db()
        assert "FLAPPING" in active.message
        assert "4 lần/30 phút" in active.message   # số lần fire trong cửa sổ + cửa sổ
        assert "31 phút" in active.message          # đã kéo dài bao lâu
        assert active.message.endswith(original)    # chi tiết gốc còn nguyên, chỉ thêm tiền tố

    def test_not_repeated_on_later_evaluations(self, mocker, device):
        """Trần: nhắc ĐÚNG 1 lần cho mỗi alert, các vòng sau (poll + safety net) không nhắc lại."""
        send = mocker.patch(SEND)
        rule = make_rule()
        make_suppressed_alert(device, rule)
        age_all_alerts(31)
        for _ in range(4):
            set_cpu(device, 97.0)
            check_device_alerts(device, since())
        assert send.call_count == 1
        assert AlertNotification.objects.filter(kind="fire").count() == 1

    def test_not_before_renotify_minutes(self, mocker, device):
        send = mocker.patch(SEND)
        rule = make_rule()
        make_suppressed_alert(device, rule)
        age_all_alerts(10)
        set_cpu(device, 97.0)
        check_device_alerts(device, since())
        send.assert_not_called()

    def test_resolved_before_renotify_stays_silent(self, mocker, device):
        """Dao động tự hết trong vài phút (5/6 ca trên prod) → vẫn im lặng, không nhắc."""
        send = mocker.patch(SEND)
        recover = mocker.patch(RECOVER)
        rule = make_rule()
        active = make_suppressed_alert(device, rule)
        age_all_alerts(5)
        set_cpu(device, 20.0)
        check_device_alerts(device, since())

        active.refresh_from_db()
        assert active.is_active is False
        send.assert_not_called()
        recover.assert_not_called()
        assert not AlertNotification.objects.exists()

    def test_disabled_when_setting_is_zero(self, mocker, device, settings):
        settings.ALERT_FLAP_RENOTIFY_MIN = 0
        send = mocker.patch(SEND)
        rule = make_rule()
        make_suppressed_alert(device, rule)
        age_all_alerts(120)
        set_cpu(device, 97.0)
        check_device_alerts(device, since())
        send.assert_not_called()


@pytest.mark.django_db
class TestNoDuplicateForAlertsAlreadyHandled:
    def test_normally_notified_alert_is_not_renotified(self, mocker, device):
        """Alert fire bình thường (đã có fire notification) kéo dài 2 giờ KHÔNG bị nhắc thêm."""
        send = mocker.patch(SEND)
        make_rule()
        set_cpu(device, 96.0)
        check_device_alerts(device, since())
        assert send.call_count == 1

        age_all_alerts(120)
        set_cpu(device, 97.0)
        check_device_alerts(device, since())
        assert send.call_count == 1

    def test_failed_fire_row_is_not_renotified(self, mocker, device):
        """Đã có dòng fire (dù failed) → không phải suppress; chuyện gửi lại thuộc 1.2, không phải 1.1."""
        send = mocker.patch(SEND)
        rule = make_rule()
        alert = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=True,
        )
        AlertNotification.objects.create(alert=alert, channel="telegram", kind="fire",
                                         status="failed", error="x")
        age_all_alerts(120)
        set_cpu(device, 97.0)
        check_device_alerts(device, since())
        send.assert_not_called()
        assert AlertNotification.objects.filter(alert=alert).count() == 1

    def test_rule_without_channels_creates_nothing(self, mocker, device):
        send = mocker.patch(SEND)
        rule = make_rule(channels=[])
        active = make_suppressed_alert(device, rule)
        original = active.message
        age_all_alerts(120)
        set_cpu(device, 97.0)
        check_device_alerts(device, since())
        send.assert_not_called()
        assert not AlertNotification.objects.exists()
        active.refresh_from_db()
        assert active.message == original   # không có gì được gửi thì cũng không đổi nội dung alert


@pytest.mark.django_db
class TestRecoveryPairsWithRenotify:
    def test_recovery_is_sent_after_renotify(self, mocker, device):
        """Nhắc xong rồi hồi phục → RECOVERED phải đi theo (trước 1.1: sự cố kéo dài không có cả 2)."""
        mocker.patch(SEND)
        recover = mocker.patch(RECOVER)
        rule = make_rule()
        make_suppressed_alert(device, rule)
        age_all_alerts(31)
        set_cpu(device, 97.0)
        check_device_alerts(device, since())

        set_cpu(device, 20.0)
        check_device_alerts(device, since())

        recover.assert_called_once()
        assert AlertNotification.objects.filter(kind="recovery", status="sent").count() == 1

    def test_renotify_resolved_before_send_still_gets_recovery(self, mocker, device):
        """Hàng đợi chậm: nhắc đã ghi pending nhưng chưa gửi, alert hồi phục TRƯỚC khi gửi được →
        fire gửi trễ rồi recovery đi sau (late-recovery sẵn có), không có recovery mồ côi."""
        rule = make_rule()
        active = make_suppressed_alert(device, rule)
        age_all_alerts(31)

        # Mô phỏng worker chết ngay sau commit: dispatch bị chặn, row pending nằm lại trong DB.
        mocker.patch("apps.alerts.engine._dispatch_notifications", side_effect=RuntimeError("boom"))
        set_cpu(device, 97.0)
        check_device_alerts(device, since())
        mocker.stopall()
        send = mocker.patch(SEND)
        recover = mocker.patch(RECOVER)

        pending = AlertNotification.objects.get(alert=active, kind="fire")
        assert pending.status == "pending"

        set_cpu(device, 20.0)
        check_device_alerts(device, since())   # resolve: fire chưa sent → chưa có recovery
        assert not AlertNotification.objects.filter(kind="recovery").exists()

        AlertNotification.objects.filter(pk=pending.pk).update(
            sent_at=dj_tz.now() - timedelta(seconds=200))
        assert retry_pending_alert_notifications(grace_secs=90) == 1
        send.assert_called_once()

        # Fire gửi trễ phát hiện alert đã hồi phục từ trước → tự queue recovery (late-recovery sẵn có);
        # sweep vòng kế tiếp mới gửi nốt (giống test_notification_outbox).
        late = AlertNotification.objects.get(alert=active, kind="recovery", channel="telegram")
        assert late.status == "pending"
        AlertNotification.objects.filter(pk=late.pk).update(
            sent_at=dj_tz.now() - timedelta(seconds=200))
        assert retry_pending_alert_notifications(grace_secs=90) == 1
        recover.assert_called_once()


@pytest.mark.django_db
class TestDurableIntent:
    def test_pending_row_survives_crash_and_is_swept(self, mocker, device):
        """Crash giữa commit và gửi: ý định gửi nằm trong DB và sweep định kỳ gửi nốt."""
        rule = make_rule()
        active = make_suppressed_alert(device, rule)
        age_all_alerts(31)

        mocker.patch("apps.alerts.engine._dispatch_notifications", side_effect=RuntimeError("boom"))
        set_cpu(device, 97.0)
        check_device_alerts(device, since())
        mocker.stopall()

        row = AlertNotification.objects.get(alert=active, kind="fire")
        assert row.status == "pending"

        send = mocker.patch(SEND)
        AlertNotification.objects.filter(pk=row.pk).update(sent_at=dj_tz.now() - timedelta(seconds=200))
        assert retry_pending_alert_notifications(grace_secs=90) == 1
        send.assert_called_once()
        row.refresh_from_db()
        assert row.status == "sent"


@pytest.mark.django_db(transaction=True)
class TestConcurrentEvaluationsNotifyOnce:
    """Inline sau poll + `evaluate_alert_rules` (safety net) có thể chạy cùng lúc trên 2 worker. Kiểm tra
    "chưa có dòng fire" và ghi dòng pending phải nằm dưới CÙNG khoá Device — nếu không, cả 2 cùng thấy
    "chưa có" rồi cùng ghi/gửi (nhắc đôi). 2 thread + 2 connection Postgres thật; rào chắn đặt ở ĐẦU hàm
    kiểm tra để ép 2 bên đi vào cùng lúc khi KHÔNG có khoá (có khoá thì thread kia chờ ở khoá, rào hết giờ
    rồi vẫn chạy tuần tự)."""

    def test_two_threads_create_exactly_one_renotification(self, mocker):
        import threading

        from django.db import connection

        from apps.alerts import engine

        send = mocker.patch(SEND)
        device = CiscoSNMPDeviceFactory(last_seen=dj_tz.now())
        rule = make_rule()
        active = Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message="High CPU",
            metric_value=95.0, is_active=True,
        )
        Alert.objects.filter(pk=active.pk).update(triggered_at=dj_tz.now() - timedelta(minutes=45))

        barrier = threading.Barrier(2)
        real = engine._queue_renotify_if_suppressed

        def raced(alert, r):
            try:
                barrier.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                pass          # có khoá: bên kia đang chờ ở select_for_update, không tới được rào
            return real(alert, r)

        mocker.patch("apps.alerts.engine._queue_renotify_if_suppressed", side_effect=raced)
        errors: list = []

        def run():
            try:
                engine._fire_alert(device, rule, 97.0)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, errors
        assert AlertNotification.objects.filter(alert=active, kind="fire").count() == 1
        assert send.call_count == 1


@pytest.mark.django_db
class TestRealDeviceOnlineRule:
    """Đúng ca đã xảy ra trên prod (#662): rule "Thiết bị offline" (device_online eq 0)."""

    def test_offline_device_flapping_then_down_is_reported(self, mocker):
        send = mocker.patch(SEND)
        down_since = dj_tz.now() - timedelta(hours=1)   # quá grace → offline thật
        dev = CiscoSNMPDeviceFactory(last_seen=None, last_ok_seen=down_since)
        rule = make_rule(name="Offline (flap test)", metric="device_online", condition="eq",
                         threshold=0.0, severity="CRITICAL", duration_min=0)
        # Migration seed sẵn rule "Thiết bị offline" (cùng metric) — loại ra để chỉ đo rule của test.
        AlertRule.objects.exclude(pk=rule.pk).delete()
        for _ in range(3):
            Alert.objects.create(device=dev, rule=rule, severity="CRITICAL", message="x",
                                 metric_value=0.0, is_active=False, resolved_at=now())

        check_device_alerts(dev, since())
        active = Alert.objects.get(device=dev, rule=rule, is_active=True)
        assert not AlertNotification.objects.filter(alert=active).exists()
        send.assert_not_called()

        age_all_alerts(31)
        check_device_alerts(dev, since())

        send.assert_called_once()
        assert AlertNotification.objects.get(alert=active, kind="fire").status == "sent"
