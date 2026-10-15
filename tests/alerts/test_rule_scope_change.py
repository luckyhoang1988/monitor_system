"""Đổi `device_type` của rule ĐANG BẬT làm alert loại cũ kẹt vĩnh viễn (ghi nhận sau review thứ ba của 1.4).

Gốc: `check_device_alerts` chỉ đánh giá rule `enabled=True` và lọc rule theo `device_type` của thiết bị
(`device_type__in=[loại, "all"]`); `_resolve_alert` chỉ được gọi từ đó. Rule đang bật đổi "Tất cả" → "HyperV"
thì alert đang active trên Switch không còn rule nào đánh giá nó → `is_active=True` mãi mãi.

Hướng đã chốt: rule đang bật thì KHÔNG đổi được loại thiết bị — tắt rule trước (lúc đó alert active được đóng,
lý do được ghi), đổi phạm vi, rồi bật lại. Áp cho mọi đường ghi: form UI, Django admin (cùng ModelForm →
`AlertRule.clean()`).
"""
import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.alerts.models import Alert, AlertRule
from tests.conftest import CiscoSNMPDeviceFactory

HINT = "Hãy tắt rule trước"          # cụm cố định trong thông điệp chặn — phải hiện ra cho người dùng


def make_rule(**kwargs):
    defaults = dict(
        name="Rule đổi phạm vi", device_type="all", metric="cpu_percent", condition="gt",
        threshold=90.0, severity="CRITICAL", channels=["telegram"], enabled=True,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


def make_active_alert(device, rule):
    return Alert.objects.create(
        device=device, rule=rule, severity=rule.severity, message=f"{device.name}: CPU cao",
        metric_value=95.0, is_active=True,
    )


@pytest.fixture
def switch(db):
    device = CiscoSNMPDeviceFactory()
    assert device.device_type == "switch"
    return device


def _form_data(rule, **overrides):
    data = {
        "name": rule.name, "device_type": rule.device_type, "metric": rule.metric,
        "condition": rule.condition, "threshold": rule.threshold, "severity": rule.severity,
        "duration_min": rule.duration_min, "channels": ["telegram"],
    }
    data.update(overrides)
    enabled = data.pop("enabled", rule.enabled)
    if enabled:
        data["enabled"] = "on"
    return data


def _post_ui(client, rule, **overrides):
    return client.post(reverse("alerts:rule_edit", args=[rule.pk]), data=_form_data(rule, **overrides))


def _post_admin(client, rule, **overrides):
    data = _form_data(rule, **overrides)
    data["channels"] = '["telegram"]'
    return client.post(reverse("admin:alerts_alertrule_change", args=[rule.pk]), data=data)


@pytest.mark.django_db
class TestEnabledRuleCannotChangeScopeViaUi:
    def test_changing_scope_of_enabled_rule_is_rejected_and_nothing_changes(self, logged_in_client, switch):
        rule = make_rule(device_type="all")
        alert = make_active_alert(switch, rule)

        response = _post_ui(logged_in_client, rule, device_type="hyperv")

        assert response.status_code == 200                       # form hiện lại, không redirect
        rule.refresh_from_db()
        assert rule.device_type == "all" and rule.enabled is True
        alert.refresh_from_db()
        assert alert.is_active is True and alert.closed_reason == ""

    def test_the_reason_is_shown_to_the_user(self, logged_in_client, switch):
        rule = make_rule(device_type="all")

        response = _post_ui(logged_in_client, rule, device_type="hyperv")

        assert HINT in response.content.decode("utf-8"), (
            "form bị chặn nhưng không nói lý do — người dùng chỉ thấy form hiện lại không đổi gì"
        )

    def test_editing_other_fields_of_enabled_rule_still_works(self, logged_in_client, switch):
        rule = make_rule(device_type="all")

        response = _post_ui(logged_in_client, rule, threshold=75.0)

        assert response.status_code == 302
        rule.refresh_from_db()
        assert rule.threshold == 75.0 and rule.device_type == "all" and rule.enabled is True


@pytest.mark.django_db
class TestScopeChangeThroughTheDisableFlow:
    """Đường được chỉ định: tắt → đổi phạm vi → bật lại."""

    def test_disable_then_change_scope_then_enable(self, logged_in_client, switch):
        rule = make_rule(device_type="all")
        alert = make_active_alert(switch, rule)

        # 1. tắt rule: alert active được đóng với lý do rõ ràng.
        assert _post_ui(logged_in_client, rule, enabled=False).status_code == 302
        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == Alert.CLOSED_RULE_DISABLED

        # 2. đổi phạm vi khi rule đang tắt.
        rule.refresh_from_db()
        assert _post_ui(logged_in_client, rule, device_type="hyperv", enabled=False).status_code == 302
        rule.refresh_from_db()
        assert rule.device_type == "hyperv" and rule.enabled is False

        # 3. bật lại (phạm vi giữ nguyên).
        assert _post_ui(logged_in_client, rule, enabled=True).status_code == 302
        rule.refresh_from_db()
        assert rule.device_type == "hyperv" and rule.enabled is True

    def test_disabling_and_changing_scope_in_one_save_is_allowed_and_closes_old_scope_alerts(
        self, logged_in_client, switch
    ):
        rule = make_rule(device_type="all")
        alert = make_active_alert(switch, rule)       # Switch: nằm NGOÀI phạm vi mới "hyperv"

        response = _post_ui(logged_in_client, rule, device_type="hyperv", enabled=False)

        assert response.status_code == 302
        rule.refresh_from_db()
        assert rule.device_type == "hyperv" and rule.enabled is False
        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == Alert.CLOSED_RULE_DISABLED

    def test_already_disabled_rule_can_change_scope_and_be_enabled_in_the_same_save(
        self, logged_in_client, switch
    ):
        # Rule đã tắt trước đó nên không còn alert active nào của nó bị kẹt do đổi phạm vi.
        rule = make_rule(device_type="all", enabled=False)

        response = _post_ui(logged_in_client, rule, device_type="hyperv", enabled=True)

        assert response.status_code == 302
        rule.refresh_from_db()
        assert rule.device_type == "hyperv" and rule.enabled is True


@pytest.mark.django_db
class TestEnabledRuleCannotChangeScopeViaAdmin:
    def test_admin_rejects_scope_change_of_enabled_rule(self, admin_client, switch):
        rule = make_rule(device_type="all")
        alert = make_active_alert(switch, rule)

        response = _post_admin(admin_client, rule, device_type="hyperv")

        assert response.status_code == 200                       # changeform hiện lại kèm lỗi
        rule.refresh_from_db()
        assert rule.device_type == "all" and rule.enabled is True
        alert.refresh_from_db()
        assert alert.is_active is True
        assert HINT in response.content.decode("utf-8")

    def test_admin_allows_disable_and_scope_change_in_one_save(
        self, admin_client, switch, django_capture_on_commit_callbacks
    ):
        rule = make_rule(device_type="all")
        alert = make_active_alert(switch, rule)

        with django_capture_on_commit_callbacks(execute=True):
            response = _post_admin(admin_client, rule, device_type="hyperv", enabled=False)

        assert response.status_code == 302
        rule.refresh_from_db()
        assert rule.device_type == "hyperv" and rule.enabled is False
        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == Alert.CLOSED_RULE_DISABLED


@pytest.mark.django_db
class TestModelLevelGuard:
    """Chặn đặt ở `AlertRule.clean()` để mọi ModelForm (UI, admin) đi chung 1 quy tắc."""

    def test_clean_rejects_scope_change_of_stored_enabled_rule(self):
        rule = make_rule(device_type="all")
        rule.device_type = "hyperv"

        with pytest.raises(ValidationError) as exc:
            rule.full_clean()
        assert "device_type" in exc.value.message_dict

    def test_clean_allows_scope_change_when_the_stored_rule_is_disabled(self):
        rule = make_rule(device_type="all", enabled=False)
        rule.device_type = "hyperv"

        rule.full_clean()                                        # không ném

    def test_clean_allows_scope_change_when_the_same_save_disables_it(self):
        rule = make_rule(device_type="all")
        rule.device_type = "hyperv"
        rule.enabled = False

        rule.full_clean()

    def test_new_rule_is_not_affected(self):
        rule = AlertRule(
            name="Rule mới", device_type="hyperv", metric="cpu_percent", condition="gt",
            threshold=80.0, severity="WARNING", channels=["telegram"], enabled=True,
        )

        rule.full_clean()

    def test_unchanged_scope_of_enabled_rule_passes(self):
        rule = make_rule(device_type="switch")
        rule.threshold = 70.0

        rule.full_clean()
