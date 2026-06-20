"""Tests for Alert Views."""
import pytest
from django.contrib.auth.models import User, Group
from django.urls import reverse
from tests.conftest import CiscoSNMPDeviceFactory
from apps.alerts.models import Alert, AlertRule
from django.utils import timezone


@pytest.fixture
def logged_in_client(client, db):
    user = User.objects.create_user(username="admin", password="password123")
    group, _ = Group.objects.get_or_create(name="Network Admins")
    user.groups.add(group)
    client.login(username="admin", password="password123")
    return client


@pytest.fixture
def alert_rule(db):
    return AlertRule.objects.create(
        name="High CPU",
        device_type="switch",
        metric="cpu_percent",
        condition="gt",
        threshold=80.0,
        severity="CRITICAL",
        enabled=True,
    )


@pytest.fixture
def alert(db, alert_rule):
    device = CiscoSNMPDeviceFactory()
    return Alert.objects.create(
        device=device,
        rule=alert_rule,
        severity="CRITICAL",
        message="Test alert message",
        metric_value=85.0,
        is_active=True,
    )


@pytest.mark.django_db
class TestAlertViews:
    def test_alert_list_view(self, logged_in_client, alert):
        response = logged_in_client.get(reverse("alerts:list"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert alert.rule.name in content
        assert alert.device.name in content

    def test_alert_acknowledge_view(self, logged_in_client, alert):
        response = logged_in_client.post(reverse("alerts:ack", args=[alert.pk]))
        assert response.status_code == 302
        alert.refresh_from_db()
        assert alert.acknowledged_by == "admin"
        assert alert.acknowledged_at is not None

    def test_rule_list_view(self, logged_in_client, alert_rule):
        response = logged_in_client.get(reverse("alerts:rule_list"))
        assert response.status_code == 200
        assert alert_rule.name in response.content.decode("utf-8")

    def test_rule_create_view_get(self, logged_in_client):
        response = logged_in_client.get(reverse("alerts:rule_create"))
        assert response.status_code == 200
        assert "Thêm Alert Rule" in response.content.decode("utf-8")

    def test_rule_create_view_post(self, logged_in_client):
        data = {
            "name": "New Alert Rule",
            "device_type": "switch",
            "metric": "cpu_percent",
            "condition": "gt",
            "threshold": 90.0,
            "severity": "WARNING",
            "duration_min": 0,
            "channels": ["email"],
            "enabled": True,
        }
        response = logged_in_client.post(reverse("alerts:rule_create"), data=data)
        assert response.status_code == 302
        assert AlertRule.objects.filter(name="New Alert Rule").exists()

    def test_rule_edit_view(self, logged_in_client, alert_rule):
        response = logged_in_client.get(reverse("alerts:rule_edit", args=[alert_rule.pk]))
        assert response.status_code == 200
        assert f"Sửa: {alert_rule.name}" in response.content.decode("utf-8")

        data = {
            "name": "Updated Rule Name",
            "device_type": alert_rule.device_type,
            "metric": alert_rule.metric,
            "condition": alert_rule.condition,
            "threshold": 75.0,
            "severity": alert_rule.severity,
            "duration_min": alert_rule.duration_min,
            "channels": ["email"],
            "enabled": True,
        }
        response = logged_in_client.post(reverse("alerts:rule_edit", args=[alert_rule.pk]), data=data)
        assert response.status_code == 302
        alert_rule.refresh_from_db()
        assert alert_rule.name == "Updated Rule Name"
        assert alert_rule.threshold == 75.0

    def test_rule_delete_view(self, logged_in_client, alert_rule):
        response = logged_in_client.get(reverse("alerts:rule_delete", args=[alert_rule.pk]))
        assert response.status_code == 200
        assert alert_rule.name in response.content.decode("utf-8")

        response = logged_in_client.post(reverse("alerts:rule_delete", args=[alert_rule.pk]))
        assert response.status_code == 302
        assert not AlertRule.objects.filter(pk=alert_rule.pk).exists()
