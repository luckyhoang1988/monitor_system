"""Tests cho metric device_online — cảnh báo AP/WLAN controller offline."""
import pytest
from datetime import datetime, timezone, timedelta

from django.utils import timezone as dj_tz

from apps.alerts.engine import check_device_alerts, _device_online
from apps.alerts.models import AlertRule, Alert
from tests.conftest import ApPingDeviceFactory


def since():
    return datetime.now(tz=timezone.utc) - timedelta(minutes=10)


def make_ap_offline_rule():
    return AlertRule.objects.create(
        name="AP Offline",
        device_type="ap",
        metric="device_online",
        condition="eq",
        threshold=0.0,
        severity="CRITICAL",
        channels=[],
        enabled=True,
    )


@pytest.mark.django_db
class TestDeviceOnlineMetric:
    def test_device_online_offline_when_no_last_seen(self, db):
        ap = ApPingDeviceFactory(last_seen=None)
        assert _device_online(ap) == 0.0

    def test_device_online_online_when_recent_last_seen(self, db):
        ap = ApPingDeviceFactory(last_seen=dj_tz.now())
        assert _device_online(ap) == 1.0

    def test_fires_alert_when_ap_offline(self, db):
        ap = ApPingDeviceFactory(last_seen=None)
        rule = make_ap_offline_rule()
        check_device_alerts(ap, since())
        assert Alert.objects.filter(device=ap, rule=rule, is_active=True).count() == 1
        alert = Alert.objects.get(device=ap, rule=rule)
        assert "OFFLINE" in alert.message

    def test_no_alert_when_ap_online(self, db):
        ap = ApPingDeviceFactory(last_seen=dj_tz.now())
        make_ap_offline_rule()
        check_device_alerts(ap, since())
        assert Alert.objects.filter(device=ap).count() == 0

    def test_resolves_when_ap_back_online(self, db):
        ap = ApPingDeviceFactory(last_seen=None)
        rule = make_ap_offline_rule()
        check_device_alerts(ap, since())
        assert Alert.objects.filter(device=ap, rule=rule, is_active=True).count() == 1
        # AP trở lại online → alert tự resolve.
        ap.last_seen = dj_tz.now()
        ap.save(update_fields=["last_seen"])
        check_device_alerts(ap, since())
        alert = Alert.objects.get(device=ap, rule=rule)
        assert alert.is_active is False
        assert alert.resolved_at is not None
