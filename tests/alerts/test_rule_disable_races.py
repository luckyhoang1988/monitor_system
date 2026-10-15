"""Review ngoài sau commit 30b09aa (hạng mục 1.4) — 2 lỗi P2 còn lại ở đường "tắt rule".

1. **Vẫn tạo được alert active sau khi tắt rule.** `_fire_alert` kiểm `enabled=True` rồi mới tạo Alert (dưới
   khoá Device); thao tác tắt rule đóng alert theo danh sách "Device ĐÃ có alert active" — alert đang được tạo
   chưa commit nên không thấy, đóng 0 alert; worker tạo tiếp → alert active của rule đã tắt, không còn đường nào
   tới `_resolve_alert`. Sửa: đóng alert phải đi qua CÙNG khoá Device với chỗ tạo (khoá mọi Device mà rule có
   thể nổ), và đường Django admin (bọc cả request trong 1 transaction) chỉ đóng SAU khi `enabled=False` đã commit.

2. **Tắt rule vẫn gửi RECOVERED giả.** Alert bị đóng bằng `is_active=False` nên cơ chế late-recovery coi đó là
   "đã hồi phục": fire còn pending/processing thì sweep gửi ALERT rồi RECOVERED dù chưa có dữ liệu nào cho thấy
   sự cố đã hết. Sửa: alert mang lý do đóng (`closed_reason`) và late-recovery bỏ qua alert đóng vì rule bị tắt —
   kể cả khi rule được bật lại sau đó, và vẫn queue recovery cho alert thật sự đã hồi phục.
"""
import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone as dj_tz

from apps.alerts.engine import (
    _finalize_fire_sent,
    _fire_alert,
    _resolve_alert,
    close_alerts_for_disabled_rule,
    retry_pending_alert_notifications,
)
from apps.alerts.models import Alert, AlertNotification, AlertRule
from tests.conftest import CiscoSNMPDeviceFactory

SEND_FIRE = "apps.alerts.channels.telegram.send_telegram_alert"
SEND_RECOVERY = "apps.alerts.channels.telegram.send_telegram_recovery"


@pytest.fixture(autouse=True)
def _never_reach_telegram(mocker):
    """Mọi test ở đây đi qua engine thật — không để test nào chạm mạng (`_resolve_alert` gửi RECOVERED thật)."""
    mocker.patch(SEND_FIRE)
    mocker.patch(SEND_RECOVERY)


def make_rule(**kwargs):
    defaults = dict(
        name="Rule thử đua tắt/nổ", device_type="all", metric="cpu_percent", condition="gt",
        threshold=90.0, severity="CRITICAL", channels=["telegram"], enabled=True,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


def make_active_alert(device, rule, *, fire_status="pending", token=""):
    alert = Alert.objects.create(
        device=device, rule=rule, severity=rule.severity, message=f"{device.name}: CPU cao",
        metric_value=95.0, is_active=True,
    )
    fire = AlertNotification.objects.create(
        alert=alert, channel="telegram", kind="fire", status=fire_status,
        claim_token=token, attempts=1 if token else 0,
    )
    return alert, fire


def recovery_rows(alert):
    return AlertNotification.objects.filter(alert=alert, kind="recovery")


def _thread_read(func):
    """Chạy `func()` trong thread riêng (connection Postgres riêng) và trả kết quả — để quan sát đúng thứ
    mà một tiến trình KHÁC nhìn thấy, không phải transaction của chính test."""
    box: dict = {}

    def run():
        try:
            box["value"] = func()
        finally:
            connection.close()

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=10)
    return box.get("value")


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Lỗi 1 — tạo alert đua với thao tác tắt rule
# ─────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db(transaction=True)
class TestDisableRacingWithFire:
    """2 thread + 2 connection Postgres thật (có commit thật). Thread `fire` dừng ĐÚNG sau khi đã kiểm
    `enabled=True` và đang giữ khoá Device, ngay trước `Alert.objects.create` — đó là cửa sổ review đã tái hiện."""

    def test_alert_created_while_rule_is_being_disabled_is_not_left_active(self, mocker):
        mocker.patch(SEND_FIRE)
        mocker.patch(SEND_RECOVERY)
        device = CiscoSNMPDeviceFactory(last_seen=dj_tz.now())
        rule = make_rule()
        stale_rule = AlertRule.objects.get(pk=rule.pk)   # vòng eval đã nạp rule lúc còn bật

        at_create, release = threading.Event(), threading.Event()
        real_create = Alert.objects.create

        def gated_create(*args, **kwargs):
            at_create.set()                  # `enabled=True` đã được kiểm; thread này giữ khoá Device
            release.wait(timeout=10)
            return real_create(*args, **kwargs)

        mocker.patch.object(Alert.objects, "create", side_effect=gated_create)
        errors: list = []

        def fire():
            try:
                _fire_alert(device, stale_rule, 97.0)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()

        def disable():
            try:
                AlertRule.objects.filter(pk=rule.pk).update(enabled=False)   # commit ngay (autocommit)
                close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()

        t_fire = threading.Thread(target=fire)
        t_fire.start()
        assert at_create.wait(timeout=10), "thread fire không tới được chỗ tạo alert"
        t_disable = threading.Thread(target=disable)
        t_disable.start()
        # Mã đúng: thread disable bị chặn ở khoá Device tới khi fire commit. Mã sai: xong ngay vì thấy 0 alert.
        t_disable.join(timeout=1.5)
        release.set()
        t_fire.join(timeout=15)
        t_disable.join(timeout=15)

        assert not errors, errors
        assert not t_fire.is_alive() and not t_disable.is_alive()
        assert AlertRule.objects.get(pk=rule.pk).enabled is False
        leftovers = Alert.objects.filter(rule=rule, is_active=True)
        assert leftovers.count() == 0, (
            "alert tạo đúng lúc rule bị tắt vẫn active — rule tắt không còn được đánh giá nên nó kẹt vĩnh viễn"
        )

    def test_alert_that_slipped_through_is_marked_as_closed_by_disable_not_resolved(self, mocker):
        # Cùng kịch bản, nhưng kiểm tra cách nó được đóng: lý do đóng phải là "rule bị tắt" (để late-recovery
        # không coi là hồi phục).
        mocker.patch(SEND_FIRE)
        device = CiscoSNMPDeviceFactory(last_seen=dj_tz.now())
        rule = make_rule(name="Rule thử đua #2")
        stale_rule = AlertRule.objects.get(pk=rule.pk)
        at_create, release = threading.Event(), threading.Event()
        real_create = Alert.objects.create

        def gated_create(*args, **kwargs):
            at_create.set()
            release.wait(timeout=10)
            return real_create(*args, **kwargs)

        mocker.patch.object(Alert.objects, "create", side_effect=gated_create)

        def fire():
            try:
                _fire_alert(device, stale_rule, 97.0)
            finally:
                connection.close()

        def disable():
            try:
                AlertRule.objects.filter(pk=rule.pk).update(enabled=False)
                close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")
            finally:
                connection.close()

        t_fire = threading.Thread(target=fire)
        t_fire.start()
        assert at_create.wait(timeout=10)
        t_disable = threading.Thread(target=disable)
        t_disable.start()
        t_disable.join(timeout=1.5)
        release.set()
        t_fire.join(timeout=15)
        t_disable.join(timeout=15)

        alert = Alert.objects.get(rule=rule)
        assert alert.is_active is False
        assert alert.closed_reason == Alert.CLOSED_RULE_DISABLED
        assert recovery_rows(alert).count() == 0


@pytest.mark.django_db(transaction=True)
class TestScopeChangedInTheSameSaveThatDisablesTheRule:
    """Review thứ ba sau 30b09aa: lưu rule ĐỔI `device_type` và TẮT cùng lúc.

    Worker đang nổ alert cho 1 Switch theo rule "Tất cả" (đã đọc `enabled=True`, đang giữ khoá Device của Switch).
    Admin đổi rule sang "HyperV" rồi tắt. Bên đóng tính phạm vi khoá theo `device_type` MỚI của rule nên chỉ khoá
    Device loại HyperV — Switch không bị khoá và alert đang tạo chưa commit nên cũng không có trong truy vấn
    "Device đã có alert active" → đóng 0 alert, worker tạo tiếp → alert active của rule đã tắt kẹt mãi.
    Phạm vi khoá của bên đóng phải bao cả phạm vi CŨ mà worker có thể đang xử lý."""

    @pytest.mark.parametrize("old_scope", ["all", "switch"])
    def test_alert_being_created_for_the_old_scope_is_not_left_active(self, mocker, old_scope):
        mocker.patch(SEND_FIRE)
        mocker.patch(SEND_RECOVERY)
        device = CiscoSNMPDeviceFactory(last_seen=dj_tz.now())
        assert device.device_type == "switch"          # khác loại "hyperv" mà rule sắp bị đổi sang
        rule = make_rule(device_type=old_scope, name=f"Rule đổi phạm vi từ {old_scope}")
        stale_rule = AlertRule.objects.get(pk=rule.pk)  # vòng eval nạp rule lúc còn là phạm vi cũ, còn bật

        at_create, release = threading.Event(), threading.Event()
        real_create = Alert.objects.create

        def gated_create(*args, **kwargs):
            at_create.set()
            release.wait(timeout=10)
            return real_create(*args, **kwargs)

        mocker.patch.object(Alert.objects, "create", side_effect=gated_create)
        errors: list = []

        def fire():
            try:
                _fire_alert(device, stale_rule, 97.0)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()

        def edit_and_disable():
            try:
                # Một lần lưu duy nhất: phạm vi mới KHÔNG chứa loại thiết bị đang nổ, đồng thời tắt.
                AlertRule.objects.filter(pk=rule.pk).update(device_type="hyperv", enabled=False)
                close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()

        t_fire = threading.Thread(target=fire)
        t_fire.start()
        assert at_create.wait(timeout=10), "thread fire không tới được chỗ tạo alert"
        t_close = threading.Thread(target=edit_and_disable)
        t_close.start()
        # Mã đúng: bị chặn ở khoá Device của Switch tới khi fire commit. Mã sai: xong ngay vì không khoá Device đó.
        t_close.join(timeout=1.5)
        release.set()
        t_fire.join(timeout=15)
        t_close.join(timeout=15)

        assert not errors, errors
        assert not t_fire.is_alive() and not t_close.is_alive()
        rule_now = AlertRule.objects.get(pk=rule.pk)
        assert rule_now.enabled is False and rule_now.device_type == "hyperv"
        assert Alert.objects.filter(rule=rule, is_active=True).count() == 0, (
            "alert nổ cho phạm vi cũ vẫn active sau khi rule bị đổi phạm vi và tắt — không còn ai đánh giá nó nữa"
        )
        alert = Alert.objects.get(rule=rule)
        assert alert.closed_reason == Alert.CLOSED_RULE_DISABLED
        assert recovery_rows(alert).count() == 0


@pytest.mark.django_db(transaction=True)
class TestAdminClosesOnlyAfterDisableIsCommitted:
    """Django admin bọc cả request trong `transaction.atomic()`: đóng alert NGAY trong `save_model` chạy khi
    `enabled=False` còn chưa commit — worker vẫn đọc thấy `enabled=True` và tạo alert mới sau khi ta đã đóng."""

    def test_close_runs_after_commit_and_is_visible_to_other_connections(self, admin_client, mocker):
        device = CiscoSNMPDeviceFactory(last_seen=dj_tz.now())
        rule = make_rule(name="Rule admin thử")
        alert, _ = make_active_alert(device, rule, fire_status="sent")

        import apps.alerts.admin as admin_module

        real = admin_module.close_alerts_for_disabled_rule
        seen: list = []

        def spy(r, by=""):
            seen.append(_thread_read(lambda: AlertRule.objects.get(pk=r.pk).enabled))
            return real(r, by=by)

        mocker.patch.object(admin_module, "close_alerts_for_disabled_rule", side_effect=spy)
        data = {
            "name": rule.name, "device_type": rule.device_type, "metric": rule.metric,
            "condition": rule.condition, "threshold": rule.threshold, "severity": rule.severity,
            "duration_min": rule.duration_min, "channels": '["telegram"]',
        }   # không có "enabled" → bỏ tick

        response = admin_client.post(reverse("admin:alerts_alertrule_change", args=[rule.pk]), data=data)

        assert response.status_code == 302
        assert seen == [False], (
            "khi đóng alert, kết nối KHÁC phải đã thấy enabled=False; nếu còn thấy True thì worker vẫn có "
            "thể tạo alert mới sau lần đóng này"
        )
        alert.refresh_from_db()
        assert alert.is_active is False


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Lỗi 2 — tắt rule không được dẫn tới RECOVERED
# ─────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestDisabledRuleNeverSendsRecovery:
    def test_pending_fire_is_sent_but_no_recovery_follows(self, mocker):
        send_fire = mocker.patch(SEND_FIRE)
        send_recovery = mocker.patch(SEND_RECOVERY)
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()
        alert, fire = make_active_alert(device, rule, fire_status="pending")
        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)
        close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")
        AlertNotification.objects.filter(pk=fire.pk).update(sent_at=dj_tz.now() - timedelta(seconds=200))

        assert retry_pending_alert_notifications(grace_secs=90) == 1

        send_fire.assert_called_once()
        fire.refresh_from_db()
        assert fire.status == "sent"
        send_recovery.assert_not_called()
        assert recovery_rows(alert).count() == 0, "chưa có dữ liệu nào cho thấy sự cố đã hết"

    def test_fire_in_flight_when_rule_is_disabled_does_not_queue_recovery(self):
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()
        alert, fire = make_active_alert(device, rule, fire_status="processing", token="tok-1")
        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)
        close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")

        finalized = _finalize_fire_sent(alert, "telegram", "tok-1")

        assert finalized is True
        fire.refresh_from_db()
        assert fire.status == "sent"
        assert recovery_rows(alert).count() == 0

    def test_re_enabling_the_rule_afterwards_does_not_revive_the_recovery(self):
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()
        alert, _ = make_active_alert(device, rule, fire_status="processing", token="tok-2")
        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)
        close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")
        AlertRule.objects.filter(pk=rule.pk).update(enabled=True)   # bật lại trước khi fire gửi xong

        _finalize_fire_sent(alert, "telegram", "tok-2")

        assert recovery_rows(alert).count() == 0, "alert đã đóng vì tắt rule — bật lại rule không làm nó 'hồi phục'"

    def test_end_to_end_through_the_disable_button(self, logged_in_client, mocker):
        send_fire = mocker.patch(SEND_FIRE)
        send_recovery = mocker.patch(SEND_RECOVERY)
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()
        alert, fire = make_active_alert(device, rule, fire_status="pending")

        response = logged_in_client.post(reverse("alerts:rule_disable", args=[rule.pk]))
        assert response.status_code == 302
        AlertNotification.objects.filter(pk=fire.pk).update(sent_at=dj_tz.now() - timedelta(seconds=200))
        retry_pending_alert_notifications(grace_secs=90)

        send_fire.assert_called_once()
        send_recovery.assert_not_called()
        assert recovery_rows(alert).count() == 0

    def test_closed_alert_carries_the_reason_resolved_alert_does_not(self):
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()
        closed, _ = make_active_alert(device, rule, fire_status="sent")
        other_device = CiscoSNMPDeviceFactory()
        resolved, _ = make_active_alert(other_device, rule, fire_status="sent")
        _resolve_alert(other_device, rule)          # hồi phục thật

        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)
        close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")

        closed.refresh_from_db()
        resolved.refresh_from_db()
        assert closed.closed_reason == Alert.CLOSED_RULE_DISABLED
        assert resolved.closed_reason == "", "alert đã hồi phục thật từ trước không được đổi lý do đóng"
        assert resolved.is_active is False


@pytest.mark.django_db
class TestClosureCoversEveryAlertOfTheRule:
    def test_alert_on_device_outside_the_rules_current_scope_is_still_closed(self):
        # Rule đã bị đổi device_type (hoặc thiết bị đổi loại) sau khi alert nổ: phạm vi khoá theo loại thiết bị
        # sẽ không chứa Device này, nhưng alert active của nó vẫn phải được đóng.
        switch = CiscoSNMPDeviceFactory()
        rule = make_rule(device_type="hyperv")
        alert, _ = make_active_alert(switch, rule, fire_status="sent")
        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)

        closed = close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")

        assert closed == 1
        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == Alert.CLOSED_RULE_DISABLED

    def test_only_this_rules_alerts_are_closed(self):
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()
        other = make_rule(name="Rule khác", metric="mem_percent")
        mine, _ = make_active_alert(device, rule, fire_status="sent")
        theirs, _ = make_active_alert(device, other, fire_status="sent")
        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)

        close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")

        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.is_active is False
        assert theirs.is_active is True and theirs.closed_reason == ""


@pytest.mark.django_db
class TestHistoryPageDoesNotPassClosedAlertsOffAsRecovered:
    def test_closed_alert_is_labelled_and_recovered_one_is_not(self, logged_in_client):
        device = CiscoSNMPDeviceFactory()
        other_device = CiscoSNMPDeviceFactory()
        rule = make_rule()
        make_active_alert(device, rule, fire_status="sent")
        make_active_alert(other_device, rule, fire_status="sent")
        _resolve_alert(other_device, rule)              # hồi phục thật
        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)
        close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester")

        body = logged_in_client.get(reverse("alerts:list")).content.decode("utf-8")

        assert body.count("Rule bị tắt") == 1, "chỉ alert đóng vì tắt rule mới mang nhãn, alert hồi phục thật thì không"


@pytest.mark.django_db
class TestGenuineRecoveryStillGetsItsRecovery:
    """Đối chứng: chặn recovery cho alert đóng vì tắt rule KHÔNG được làm mất recovery của alert thật sự đã hồi phục."""

    def test_resolved_before_fire_was_sent_still_queues_late_recovery(self):
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()
        alert, _ = make_active_alert(device, rule, fire_status="processing", token="tok-3")

        _resolve_alert(device, rule)                       # hồi phục thật, fire còn đang gửi
        _finalize_fire_sent(alert, "telegram", "tok-3")

        assert recovery_rows(alert).filter(status="pending").count() == 1

    def test_resolved_then_rule_disabled_then_fire_sent_still_queues_recovery(self):
        # Alert hồi phục thật TRƯỚC khi rule bị tắt: việc tắt rule không được tước recovery của nó (đừng suy
        # "rule đang tắt ⇒ không gửi recovery" — phải theo lý do đóng của TỪNG alert).
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()
        alert, _ = make_active_alert(device, rule, fire_status="processing", token="tok-4")
        _resolve_alert(device, rule)

        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)
        assert close_alerts_for_disabled_rule(AlertRule.objects.get(pk=rule.pk), by="tester") == 0
        _finalize_fire_sent(alert, "telegram", "tok-4")

        assert recovery_rows(alert).filter(status="pending").count() == 1
