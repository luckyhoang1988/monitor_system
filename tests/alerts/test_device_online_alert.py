"""Tests cho metric device_online — cảnh báo AP/WLAN controller offline.

Nguồn sự thật cho cảnh báo offline là `last_ok_seen` (mốc poll THÀNH CÔNG gần nhất,
không bị xoá khi poll trượt) + grace — KHÔNG phải `last_seen` (xoá mỗi lần poll lỗi,
chỉ phục vụ hiển thị). Nhờ đó 1 vòng poll trượt không bắn offline giả/flapping.
"""
import pytest
from datetime import datetime, timezone, timedelta

from django.utils import timezone as dj_tz

from apps.alerts.engine import check_device_alerts, _device_online
from apps.alerts.models import AlertRule, Alert
from tests.conftest import ApPingDeviceFactory


def since():
    return datetime.now(tz=timezone.utc) - timedelta(minutes=10)


def old_ts():
    """Mốc cũ hơn grace (collect_interval=300 → grace 900s) → coi offline."""
    return dj_tz.now() - timedelta(hours=1)


def make_ap_offline_rule(**kwargs):
    defaults = dict(
        name="AP Offline",
        device_type="ap",
        metric="device_online",
        condition="eq",
        threshold=0.0,
        severity="CRITICAL",
        channels=[],
        enabled=True,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


@pytest.mark.django_db
class TestDeviceOnlineMetric:
    def test_device_online_offline_when_last_ok_seen_old(self, db):
        ap = ApPingDeviceFactory(last_seen=None, last_ok_seen=old_ts())
        assert _device_online(ap) == 0.0

    def test_device_online_online_when_recent_last_ok_seen(self, db):
        ap = ApPingDeviceFactory(last_seen=dj_tz.now(), last_ok_seen=dj_tz.now())
        assert _device_online(ap) == 1.0

    def test_single_failed_poll_does_not_flag_offline(self, db):
        """1 vòng poll trượt: last_seen bị xoá nhưng last_ok_seen còn mới → vẫn online."""
        ap = ApPingDeviceFactory(last_seen=None, last_ok_seen=dj_tz.now())
        assert _device_online(ap) == 1.0

    def test_fires_alert_when_ap_offline(self, db):
        ap = ApPingDeviceFactory(last_seen=None, last_ok_seen=old_ts())
        rule = make_ap_offline_rule()
        check_device_alerts(ap, since())
        assert Alert.objects.filter(device=ap, rule=rule, is_active=True).count() == 1
        alert = Alert.objects.get(device=ap, rule=rule)
        assert "OFFLINE" in alert.message

    def test_no_alert_on_single_blip(self, db):
        """Poll trượt 1 lần (last_seen=None, last_ok_seen mới) → KHÔNG bắn offline giả."""
        ap = ApPingDeviceFactory(last_seen=None, last_ok_seen=dj_tz.now())
        rule = make_ap_offline_rule()
        check_device_alerts(ap, since())
        assert Alert.objects.filter(device=ap, rule=rule).count() == 0

    def test_no_alert_when_ap_online(self, db):
        ap = ApPingDeviceFactory(last_seen=dj_tz.now(), last_ok_seen=dj_tz.now())
        make_ap_offline_rule()
        check_device_alerts(ap, since())
        assert Alert.objects.filter(device=ap).count() == 0

    def test_resolves_when_ap_back_online(self, db):
        ap = ApPingDeviceFactory(last_seen=None, last_ok_seen=old_ts())
        rule = make_ap_offline_rule()
        check_device_alerts(ap, since())
        assert Alert.objects.filter(device=ap, rule=rule, is_active=True).count() == 1
        # AP trở lại online → alert tự resolve.
        ap.last_seen = dj_tz.now()
        ap.last_ok_seen = dj_tz.now()
        ap.save(update_fields=["last_seen", "last_ok_seen"])
        check_device_alerts(ap, since())
        alert = Alert.objects.get(device=ap, rule=rule)
        assert alert.is_active is False
        assert alert.resolved_at is not None

    def test_sustained_offline_required_with_duration(self, db):
        """Rule duration_min: offline phải kéo dài hơn cửa sổ mới fire."""
        rule = make_ap_offline_rule(duration_min=5)
        # Vừa rớt (last_ok_seen 30s trước, < grace) → còn online → không fire.
        ap = ApPingDeviceFactory(last_seen=None, last_ok_seen=dj_tz.now() - timedelta(seconds=30))
        check_device_alerts(ap, since())
        assert Alert.objects.filter(device=ap, rule=rule).count() == 0
        # Offline đã lâu (1h > grace + cửa sổ) → fire.
        ap.last_ok_seen = old_ts()
        ap.save(update_fields=["last_ok_seen"])
        check_device_alerts(ap, since())
        assert Alert.objects.filter(device=ap, rule=rule, is_active=True).count() == 1
