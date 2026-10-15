"""Hạng mục 1.4 (A4, 2026-10-15): xoá AlertRule không được âm thầm xoá lịch sử, và không được nói sai.

Gốc: `Alert.rule` và `AlertNotification.alert` đều `on_delete=CASCADE` nên `rule.delete()` xoá luôn mọi
Alert + AlertNotification của rule — trong khi hộp thoại xác nhận cũ ghi "alerts đã trigger từ rule này sẽ
vẫn còn trong lịch sử". 1 cú nhấp là mất dữ liệu, UI nói ngược lại. Số đo prod (2026-10-15): rule
"Thiết bị offline" kéo theo 765 alert + 1.384 notification.

Hành vi mới:
* "Tắt rule" là hành động chính: `enabled=False`, lịch sử giữ nguyên. Vì engine chỉ đánh giá rule đang bật
  (`check_device_alerts`), alert ĐANG ACTIVE của rule vừa tắt sẽ không bao giờ được resolve → đóng chúng
  ngay khi tắt (không gửi "RECOVERED" vì chưa hề hồi phục).
* "Xoá vĩnh viễn" là hành động phụ: trang xác nhận nêu tên rule + số Alert/Notification thật sự sẽ mất, và
  chỉ xoá khi người dùng gõ đúng tên rule (kiểm ở server). Mở trang hoặc POST rỗng không xoá gì.
"""
from datetime import UTC, datetime

import pytest
from django.test import Client
from django.urls import reverse

from apps.alerts.engine import _fire_alert
from apps.alerts.models import Alert, AlertNotification, AlertRule
from tests.conftest import CiscoSNMPDeviceFactory

RULE_NAME = "Thiết bị offline (thử nghiệm)"


def make_rule(**kwargs):
    defaults = dict(
        name=RULE_NAME, device_type="all", metric="cpu_percent", condition="gt",
        threshold=90.0, severity="CRITICAL", channels=["telegram"], enabled=True,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


def make_alert(device, rule, *, active, notifications=("fire",)):
    alert = Alert.objects.create(
        device=device, rule=rule, severity=rule.severity, message=f"{device.name}: CPU cao",
        metric_value=95.0, is_active=active,
        resolved_at=None if active else datetime.now(tz=UTC),
    )
    for kind in notifications:
        AlertNotification.objects.create(alert=alert, channel="telegram", kind=kind, status="sent")
    return alert


@pytest.fixture
def dev_a(db):
    return CiscoSNMPDeviceFactory()


@pytest.fixture
def dev_b(db):
    return CiscoSNMPDeviceFactory()


@pytest.fixture
def rule(db):
    return make_rule()


@pytest.fixture
def other_rule(db):
    return make_rule(name="Rule khác không liên quan", metric="mem_percent")


@pytest.fixture
def history(rule, other_rule, dev_a, dev_b):
    """Rule chính: 3 alert (2 active, 1 đã resolve) + 5 notification. Rule khác: 1 active + 2 notification."""
    mine = [
        make_alert(dev_a, rule, active=True, notifications=("fire",)),
        make_alert(dev_b, rule, active=True, notifications=("fire",)),
        make_alert(dev_a, rule, active=False, notifications=("fire", "recovery", "fire")),
    ]
    theirs = [make_alert(dev_a, other_rule, active=True, notifications=("fire", "fire"))]
    return {"mine": mine, "theirs": theirs}


MY_ALERTS, MY_ACTIVE, MY_NOTIFS = 3, 2, 5


def url(name, rule):
    return reverse(f"alerts:{name}", args=[rule.pk])


def counts_for(rule):
    return (
        Alert.objects.filter(rule=rule).count(),
        AlertNotification.objects.filter(alert__rule=rule).count(),
    )


def total_counts():
    return Alert.objects.count(), AlertNotification.objects.count()


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Tắt rule — hành động chính
# ─────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestDisableRule:
    def test_disable_turns_rule_off_and_keeps_all_history(self, logged_in_client, rule, history):
        before = total_counts()

        response = logged_in_client.post(url("rule_disable", rule))

        assert response.status_code == 302
        rule.refresh_from_db()
        assert rule.enabled is False
        assert total_counts() == before, "Tắt rule không được xoá Alert/AlertNotification nào"

    def test_disable_closes_active_alerts_that_engine_could_never_resolve(
        self, logged_in_client, rule, history
    ):
        # check_device_alerts chỉ xét rule enabled=True → alert active của rule đã tắt sẽ kẹt vĩnh viễn.
        notifs_before = AlertNotification.objects.count()

        logged_in_client.post(url("rule_disable", rule))

        for alert in history["mine"]:
            alert.refresh_from_db()
            assert alert.is_active is False
        closed = [a for a in history["mine"][:2]]
        for alert in closed:
            assert alert.resolved_at is not None
            assert "rule bị tắt" in alert.message, "phải ghi lý do đóng, không giả làm 'đã hồi phục'"
        assert AlertNotification.objects.count() == notifs_before, (
            "đóng vì tắt rule không được gửi/ghi RECOVERED — chưa hề hồi phục"
        )

    def test_disable_does_not_touch_other_rules(self, logged_in_client, rule, other_rule, history):
        logged_in_client.post(url("rule_disable", rule))

        other_rule.refresh_from_db()
        assert other_rule.enabled is True
        theirs = history["theirs"][0]
        theirs.refresh_from_db()
        assert theirs.is_active is True

    def test_disable_is_idempotent(self, logged_in_client, rule, history):
        logged_in_client.post(url("rule_disable", rule))
        before = total_counts()

        response = logged_in_client.post(url("rule_disable", rule))

        assert response.status_code == 302
        assert total_counts() == before
        rule.refresh_from_db()
        assert rule.enabled is False

    def test_get_does_not_disable(self, logged_in_client, rule, history):
        response = logged_in_client.get(url("rule_disable", rule))

        assert response.status_code == 405
        rule.refresh_from_db()
        assert rule.enabled is True

    def test_readonly_user_cannot_disable(self, readonly_client, rule, history):
        response = readonly_client.post(url("rule_disable", rule))

        assert response.status_code == 403
        rule.refresh_from_db()
        assert rule.enabled is True
        history["mine"][0].refresh_from_db()
        assert history["mine"][0].is_active is True

    def test_anonymous_cannot_disable(self, client, rule, history):
        response = client.post(url("rule_disable", rule))

        assert response.status_code == 302 and "/login" in response["Location"]
        rule.refresh_from_db()
        assert rule.enabled is True

    def test_csrf_is_enforced(self, db, rule, history, django_user_model):
        from django.contrib.auth.models import Group
        user = django_user_model.objects.create_user(username="csrfadmin", password="password123")
        user.groups.add(Group.objects.get_or_create(name="Network Admins")[0])
        client = Client(enforce_csrf_checks=True)
        client.login(username="csrfadmin", password="password123")

        response = client.post(url("rule_disable", rule))

        assert response.status_code == 403
        rule.refresh_from_db()
        assert rule.enabled is True

    def test_missing_rule_is_404(self, logged_in_client, db):
        response = logged_in_client.post(reverse("alerts:rule_disable", args=[999999]))

        assert response.status_code == 404


@pytest.mark.django_db
class TestDisableViaEditForm:
    """Cùng trạng thái cuối (rule tắt) thì cùng hệ quả — bỏ tick "Kích hoạt" ở form sửa cũng để lại alert kẹt."""

    def _post_edit(self, client, rule, *, enabled):
        data = {
            "name": rule.name, "device_type": rule.device_type, "metric": rule.metric,
            "condition": rule.condition, "threshold": rule.threshold, "severity": rule.severity,
            "duration_min": rule.duration_min, "channels": ["telegram"],
        }
        if enabled:
            data["enabled"] = "on"
        return client.post(reverse("alerts:rule_edit", args=[rule.pk]), data=data)

    def test_unticking_enabled_closes_active_alerts(self, logged_in_client, rule, history):
        response = self._post_edit(logged_in_client, rule, enabled=False)

        assert response.status_code == 302
        rule.refresh_from_db()
        assert rule.enabled is False
        for alert in history["mine"]:
            alert.refresh_from_db()
            assert alert.is_active is False

    def test_editing_without_disabling_leaves_alerts_active(self, logged_in_client, rule, history):
        self._post_edit(logged_in_client, rule, enabled=True)

        rule.refresh_from_db()
        assert rule.enabled is True
        for alert in history["mine"][:2]:
            alert.refresh_from_db()
            assert alert.is_active is True


@pytest.mark.django_db
class TestDisableViaAdmin:
    """Đường thứ ba tới cùng trạng thái cuối: Django admin (chỉ admin/superuser)."""

    def _post(self, client, rule, *, enabled):
        data = {
            "name": rule.name, "device_type": rule.device_type, "metric": rule.metric,
            "condition": rule.condition, "threshold": rule.threshold, "severity": rule.severity,
            "duration_min": rule.duration_min, "channels": '["telegram"]',
        }
        if enabled:
            data["enabled"] = "on"
        return client.post(reverse("admin:alerts_alertrule_change", args=[rule.pk]), data=data)

    def test_unticking_enabled_in_admin_closes_active_alerts(
        self, admin_client, rule, history, django_capture_on_commit_callbacks
    ):
        # Admin chỉ đóng alert SAU khi `enabled=False` đã commit (transaction.on_commit) — test bọc trong 1
        # transaction nên phải chủ động chạy các callback on_commit. Thứ tự "đóng sau commit" được chứng minh
        # bằng kết nối thứ hai ở test_rule_disable_races.py.
        with django_capture_on_commit_callbacks(execute=True):
            response = self._post(admin_client, rule, enabled=False)

        assert response.status_code == 302, getattr(response, "context", None) and response.context["adminform"].form.errors
        rule.refresh_from_db()
        assert rule.enabled is False
        for alert in history["mine"]:
            alert.refresh_from_db()
            assert alert.is_active is False

    def test_saving_in_admin_without_disabling_leaves_alerts_active(self, admin_client, rule, history):
        response = self._post(admin_client, rule, enabled=True)

        assert response.status_code == 302
        for alert in history["mine"][:2]:
            alert.refresh_from_db()
            assert alert.is_active is True


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Engine: không để rule vừa bị tắt sinh alert mới (race giữa eval đang chạy và thao tác tắt)
# ─────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestFireAfterDisable:
    def test_fire_for_rule_disabled_in_db_creates_no_alert(self, dev_a, rule):
        # Vòng eval đã nạp `rule` lúc còn enabled=True; người dùng tắt rule rồi alert cũ bị đóng; đến lúc
        # _fire_alert chạy (dưới khoá Device) thì rule trong DB đã tắt → không được tạo alert mới, vì rule
        # tắt sẽ không bao giờ resolve nó.
        stale_in_memory = AlertRule.objects.get(pk=rule.pk)
        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)

        _fire_alert(dev_a, stale_in_memory, 95.0)

        assert Alert.objects.filter(rule=rule).count() == 0
        assert AlertNotification.objects.count() == 0

    def test_enabled_rule_still_fires(self, dev_a, rule, mocker):
        mocker.patch("apps.alerts.channels.telegram.send_telegram_alert")

        _fire_alert(dev_a, rule, 95.0)

        assert Alert.objects.filter(rule=rule, is_active=True).count() == 1


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Trang xác nhận xoá
# ─────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestDeleteConfirmationPage:
    def test_page_names_the_rule_and_shows_real_counts(self, logged_in_client, rule, history):
        response = logged_in_client.get(url("rule_delete", rule))

        assert response.status_code == 200
        body = response.content.decode("utf-8")
        assert RULE_NAME in body
        assert response.context["alert_count"] == MY_ALERTS
        assert response.context["active_alert_count"] == MY_ACTIVE
        assert response.context["notification_count"] == MY_NOTIFS
        # Số hiển thị cho người dùng đúng là các số đó (không lẫn alert/notification của rule khác).
        assert f">{MY_ALERTS}<" in body and f">{MY_NOTIFS}<" in body

    def test_page_leads_with_disable_and_marks_delete_as_secondary(self, logged_in_client, rule, history):
        body = logged_in_client.get(url("rule_delete", rule)).content.decode("utf-8")

        assert "Tắt rule" in body
        assert "Xóa vĩnh viễn" in body
        assert body.index("Tắt rule") < body.index("Xóa vĩnh viễn"), "hành động chính phải đứng trước"
        assert url("rule_disable", rule) in body

    def test_page_no_longer_claims_history_is_kept(self, logged_in_client, rule, history):
        # Câu cũ: "Tất cả alerts đã trigger từ rule này sẽ vẫn còn trong lịch sử" — sai: CASCADE xoá hết.
        body = logged_in_client.get(url("rule_delete", rule)).content.decode("utf-8")

        assert "vẫn còn trong lịch sử" not in body
        assert "không thể hoàn tác" in body

    def test_opening_the_page_deletes_nothing(self, logged_in_client, rule, history):
        before = total_counts()

        logged_in_client.get(url("rule_delete", rule))

        assert AlertRule.objects.filter(pk=rule.pk).exists()
        assert total_counts() == before

    def test_already_disabled_rule_is_shown_as_such(self, logged_in_client, rule, history):
        AlertRule.objects.filter(pk=rule.pk).update(enabled=False)

        body = logged_in_client.get(url("rule_delete", rule)).content.decode("utf-8")

        assert url("rule_disable", rule) not in body, "rule đã tắt thì không mời tắt lần nữa"

    def test_readonly_user_cannot_open_the_page(self, readonly_client, rule, history):
        response = readonly_client.get(url("rule_delete", rule))

        assert response.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Xoá vĩnh viễn — hành động phụ, phải xác nhận tường minh
# ─────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestDeleteRule:
    @pytest.mark.parametrize("posted", [
        {},                                         # POST rỗng (Enter / nút mặc định)
        {"confirm_name": ""},
        {"confirm_name": "xoá"},                    # gõ nhầm sang từ khác
        {"confirm_name": RULE_NAME.lower()},        # sai chữ hoa/thường
        {"confirm_name": RULE_NAME + " x"},         # thừa ký tự
        {"confirm_name": "Rule khác không liên quan"},  # tên của rule KHÁC
    ])
    def test_post_without_exact_name_deletes_nothing(self, logged_in_client, rule, history, posted):
        before = total_counts()

        response = logged_in_client.post(url("rule_delete", rule), data=posted)

        assert response.status_code == 200, "hiện lại trang xác nhận, không redirect như đã xoá"
        assert AlertRule.objects.filter(pk=rule.pk).exists()
        assert total_counts() == before
        assert RULE_NAME in response.content.decode("utf-8")

    def test_confirmed_post_deletes_the_rule_and_only_its_history(
        self, logged_in_client, rule, other_rule, history, dev_a, dev_b
    ):
        theirs_alerts, theirs_notifs = counts_for(other_rule)
        page = logged_in_client.get(url("rule_delete", rule))
        shown = (page.context["alert_count"], page.context["notification_count"])
        before = total_counts()

        response = logged_in_client.post(url("rule_delete", rule), data={"confirm_name": RULE_NAME})

        assert response.status_code == 302
        assert response["Location"] == reverse("alerts:rule_list")
        assert not AlertRule.objects.filter(pk=rule.pk).exists()
        assert counts_for(other_rule) == (theirs_alerts, theirs_notifs), "không được xoá lịch sử rule khác"
        after = total_counts()
        removed = (before[0] - after[0], before[1] - after[1])
        assert removed == shown == (MY_ALERTS, MY_NOTIFS), (
            "số trên trang xác nhận phải đúng bằng số thật sự bị xoá"
        )
        assert AlertRule.objects.filter(pk=other_rule.pk).exists()
        from apps.devices.models import Device
        assert Device.objects.filter(pk__in=[dev_a.pk, dev_b.pk]).count() == 2

    def test_confirmed_post_ignores_surrounding_whitespace(self, logged_in_client, rule, history):
        response = logged_in_client.post(
            url("rule_delete", rule), data={"confirm_name": f"  {RULE_NAME}  "},
        )

        assert response.status_code == 302
        assert not AlertRule.objects.filter(pk=rule.pk).exists()

    def test_success_message_states_what_was_deleted(self, logged_in_client, rule, history):
        response = logged_in_client.post(
            url("rule_delete", rule), data={"confirm_name": RULE_NAME}, follow=True,
        )

        text = " ".join(str(m) for m in response.context["messages"])
        assert RULE_NAME in text
        assert str(MY_ALERTS) in text and str(MY_NOTIFS) in text

    def test_readonly_user_cannot_delete_even_with_correct_name(self, readonly_client, rule, history):
        before = total_counts()

        response = readonly_client.post(url("rule_delete", rule), data={"confirm_name": RULE_NAME})

        assert response.status_code == 403
        assert AlertRule.objects.filter(pk=rule.pk).exists()
        assert total_counts() == before

    def test_anonymous_cannot_delete(self, client, rule, history):
        response = client.post(url("rule_delete", rule), data={"confirm_name": RULE_NAME})

        assert response.status_code == 302 and "/login" in response["Location"]
        assert AlertRule.objects.filter(pk=rule.pk).exists()

    def test_csrf_is_enforced(self, db, rule, history, django_user_model):
        from django.contrib.auth.models import Group
        user = django_user_model.objects.create_user(username="csrfadmin2", password="password123")
        user.groups.add(Group.objects.get_or_create(name="Network Admins")[0])
        client = Client(enforce_csrf_checks=True)
        client.login(username="csrfadmin2", password="password123")
        before = total_counts()

        response = client.post(url("rule_delete", rule), data={"confirm_name": RULE_NAME})

        assert response.status_code == 403
        assert AlertRule.objects.filter(pk=rule.pk).exists()
        assert total_counts() == before

    def test_missing_rule_is_404(self, logged_in_client, db):
        response = logged_in_client.post(
            reverse("alerts:rule_delete", args=[999999]), data={"confirm_name": "x"},
        )

        assert response.status_code == 404


@pytest.mark.django_db
class TestCountsCoverEveryCascade:
    def test_only_alert_and_notification_hang_off_a_rule(self):
        # Trang xác nhận đếm Alert + AlertNotification. Nếu ai thêm 1 model khác trỏ vào AlertRule (hoặc vào
        # Alert) với CASCADE thì số trên trang sẽ thiếu — test này buộc người đó cập nhật view + template.
        def cascading(model):
            return {
                rel.related_model for rel in model._meta.related_objects
                if getattr(rel.on_delete, "__name__", "") == "CASCADE"
            }

        assert cascading(AlertRule) == {Alert}
        assert cascading(Alert) == {AlertNotification}
        assert cascading(AlertNotification) == set()
