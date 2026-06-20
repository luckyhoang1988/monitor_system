"""Tests cho Alert engine — check_device_alerts, deduplication, notification."""
import pytest
from datetime import datetime, timezone, timedelta
from apps.alerts.engine import check_device_alerts, CONDITION_FN
from apps.alerts.models import AlertRule, Alert, AlertNotification
from apps.metrics.models import SystemHealth, InterfaceStats
from apps.devices.models import Interface
from tests.conftest import CiscoSNMPDeviceFactory


def now():
    return datetime.now(tz=timezone.utc)


def since():
    """Window 10 phút trước."""
    return now() - timedelta(minutes=10)


def make_rule(**kwargs):
    defaults = dict(
        name="Test Rule",
        device_type="all",
        metric="cpu_percent",
        condition="gt",
        threshold=90.0,
        severity="WARNING",
        channels=[],
        enabled=True,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


# ---------------------------------------------------------------------------
# CONDITION_FN — pure logic, không cần DB
# ---------------------------------------------------------------------------

class TestConditionFunctions:
    def test_gt_true(self):
        assert CONDITION_FN["gt"](95, 90) is True

    def test_gt_false(self):
        assert CONDITION_FN["gt"](80, 90) is False

    def test_lt_true(self):
        assert CONDITION_FN["lt"](5, 10) is True

    def test_lt_false(self):
        assert CONDITION_FN["lt"](15, 10) is False

    def test_gte_boundary(self):
        assert CONDITION_FN["gte"](90, 90) is True

    def test_lte_boundary(self):
        assert CONDITION_FN["lte"](90, 90) is True

    def test_eq_true(self):
        assert CONDITION_FN["eq"](5.0, 5.0) is True

    def test_ne_true(self):
        assert CONDITION_FN["ne"](5.0, 6.0) is True


# ---------------------------------------------------------------------------
# check_device_alerts — with real SQLite
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCheckDeviceAlerts:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    def test_fires_alert_when_cpu_exceeds_threshold(self, device):
        make_rule(metric="cpu_percent", condition="gt", threshold=90.0)
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=95.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device, is_active=True).count() == 1

    def test_alert_message_contains_device_name(self, device):
        make_rule(metric="cpu_percent", condition="gt", threshold=90.0)
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=95.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        alert = Alert.objects.get(device=device)
        assert device.name in alert.message

    def test_no_alert_when_cpu_below_threshold(self, device):
        make_rule(metric="cpu_percent", condition="gt", threshold=90.0)
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=70.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device).count() == 0

    def test_resolves_alert_when_cpu_drops(self, device):
        rule = make_rule(metric="cpu_percent", condition="gt", threshold=90.0)
        # Pre-create an active alert
        Alert.objects.create(
            device=device, rule=rule, severity="WARNING",
            message="High CPU", metric_value=95.0, is_active=True,
        )
        # Now CPU is normal
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=30.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        alert = Alert.objects.get(device=device)
        assert alert.is_active is False
        assert alert.resolved_at is not None

    def test_deduplication_does_not_create_duplicate_alert(self, device):
        rule = make_rule(metric="cpu_percent", condition="gt", threshold=90.0)
        # Pre-create an active alert
        Alert.objects.create(
            device=device, rule=rule, severity="WARNING",
            message="High CPU", metric_value=95.0, is_active=True,
        )
        # Call again with high CPU
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=96.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        # Still only 1 alert (not 2)
        assert Alert.objects.filter(device=device).count() == 1

    def test_skips_when_no_metric_data(self, device):
        make_rule(metric="cpu_percent", condition="gt", threshold=90.0)
        # No SystemHealth records
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device).count() == 0

    def test_fires_alert_for_mem_percent(self, device):
        make_rule(name="High Mem", metric="mem_percent",
                  condition="gt", threshold=85.0)
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=10.0, mem_percent=90.0,
        )
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device, is_active=True).count() == 1

    def test_fires_alert_for_if_status_down(self, device):
        device.uplink_ports = ["Gi0/1"]
        device.save()
        iface = Interface.objects.create(
            device=device, if_index=1, name="Gi0/1", is_uplink=True,
        )
        InterfaceStats.objects.create(
            interface=iface, timestamp=now(),
            status="down", in_bytes=0, out_bytes=0,
        )
        make_rule(name="Uplink Down", metric="if_status",
                  condition="lt", threshold=1.0)
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device, is_active=True).count() == 1

    def test_skips_disabled_rules(self, device):
        make_rule(metric="cpu_percent", condition="gt",
                  threshold=90.0, enabled=False)
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=95.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device).count() == 0

    def test_only_applies_rules_matching_device_type(self, device):
        # Rule for hyperv only — không áp dụng cho switch
        make_rule(metric="cpu_percent", condition="gt",
                  threshold=90.0, device_type="hyperv")
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=95.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device).count() == 0


# ---------------------------------------------------------------------------
# Notifications — mock send functions
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestNotifications:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    def test_email_notification_sent_on_fire(self, mocker, device):
        # Patch tại source module vì engine dùng lazy import bên trong function
        mock_email = mocker.patch(
            "apps.alerts.channels.email_channel.send_email_alert",
        )
        rule = make_rule(metric="cpu_percent", condition="gt",
                         threshold=90.0, channels=["email"])
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=95.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        mock_email.assert_called_once()

    def test_telegram_notification_sent_on_fire(self, mocker, device):
        mock_tg = mocker.patch("apps.alerts.channels.telegram.send_telegram_alert")
        make_rule(metric="cpu_percent", condition="gt",
                  threshold=90.0, channels=["telegram"])
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=95.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        mock_tg.assert_called_once()

    def test_notification_failure_records_failed_status(self, mocker, device):
        mocker.patch(
            "apps.alerts.channels.email_channel.send_email_alert",
            side_effect=Exception("SMTP timeout"),
        )
        make_rule(metric="cpu_percent", condition="gt",
                  threshold=90.0, channels=["email"])
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=95.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        notif = AlertNotification.objects.get(channel="email")
        assert notif.status == "failed"
        assert "SMTP timeout" in notif.error

    def test_no_notification_when_channels_empty(self, mocker, device):
        mock_email = mocker.patch("apps.alerts.channels.email_channel.send_email_alert")
        mock_tg    = mocker.patch("apps.alerts.channels.telegram.send_telegram_alert")
        make_rule(metric="cpu_percent", condition="gt",
                  threshold=90.0, channels=[])
        SystemHealth.objects.create(
            device=device, timestamp=now(),
            cpu_percent=95.0, mem_percent=50.0,
        )
        check_device_alerts(device, since())
        mock_email.assert_not_called()
        mock_tg.assert_not_called()
