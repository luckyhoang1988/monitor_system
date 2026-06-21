"""Tests cho hysteresis, flapping detection và tối ưu N+1 trong alert engine."""
import pytest
from datetime import datetime, timezone, timedelta

from apps.alerts.engine import (
    check_device_alerts, _decide_transition, _recovered,
    _sustained_vm_metric, _sustained_uplink_traffic_max,
)
from apps.alerts.models import AlertRule, Alert
from apps.metrics.models import SystemHealth, VMStats, InterfaceStats
from apps.devices.models import Interface
from tests.conftest import CiscoSNMPDeviceFactory, HyperVDeviceFactory


def now():
    return datetime.now(tz=timezone.utc)


def since():
    return now() - timedelta(minutes=10)


def make_rule(**kwargs):
    defaults = dict(
        name="Test Rule", device_type="all", metric="cpu_percent",
        condition="gt", threshold=90.0, severity="WARNING",
        channels=[], enabled=True,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


# ---------------------------------------------------------------------------
# _recovered / _decide_transition — pure logic
# ---------------------------------------------------------------------------

class TestHysteresisDecision:
    def test_recovered_gt_inside_band_is_false(self, settings):
        settings.ALERT_HYSTERESIS_PCT = 0.1
        rule = AlertRule(metric="cpu_percent", condition="gt", threshold=90.0)
        # 85 nằm trong vùng đệm (81..90) → chưa phục hồi
        assert _recovered(rule, 85.0) is False

    def test_recovered_gt_below_band_is_true(self, settings):
        settings.ALERT_HYSTERESIS_PCT = 0.1
        rule = AlertRule(metric="cpu_percent", condition="gt", threshold=90.0)
        assert _recovered(rule, 80.0) is True

    def test_recovered_lt_inside_band_is_false(self, settings):
        settings.ALERT_HYSTERESIS_PCT = 0.1
        rule = AlertRule(metric="cpu_percent", condition="lt", threshold=10.0)
        # rule lt 10 → recovery khi value > 11; 10.5 vẫn trong band
        assert _recovered(rule, 10.5) is False

    def test_binary_metric_recovers_immediately(self):
        rule = AlertRule(metric="if_status", condition="lt", threshold=1.0)
        assert _recovered(rule, 1.0) is True

    def test_decide_fire_hold_resolve(self, settings):
        settings.ALERT_HYSTERESIS_PCT = 0.1
        rule = AlertRule(metric="cpu_percent", condition="gt", threshold=90.0)
        assert _decide_transition(rule, 95.0, has_active=False) == "fire"
        assert _decide_transition(rule, 85.0, has_active=True) == "hold"
        assert _decide_transition(rule, 80.0, has_active=True) == "resolve"
        assert _decide_transition(rule, 85.0, has_active=False) == "none"


# ---------------------------------------------------------------------------
# check_device_alerts — hysteresis end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestHysteresisEndToEnd:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    def test_alert_held_inside_hysteresis_band(self, device, settings):
        settings.ALERT_HYSTERESIS_PCT = 0.1
        rule = make_rule(metric="cpu_percent", condition="gt", threshold=90.0)
        Alert.objects.create(
            device=device, rule=rule, severity="WARNING",
            message="High CPU", metric_value=95.0, is_active=True,
        )
        # CPU = 85 (trong band 81..90) → KHÔNG resolve
        SystemHealth.objects.create(device=device, timestamp=now(),
                                    cpu_percent=85.0, mem_percent=50.0)
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device, is_active=True).count() == 1

    def test_alert_resolved_below_recovery(self, device, settings):
        settings.ALERT_HYSTERESIS_PCT = 0.1
        rule = make_rule(metric="cpu_percent", condition="gt", threshold=90.0)
        Alert.objects.create(
            device=device, rule=rule, severity="WARNING",
            message="High CPU", metric_value=95.0, is_active=True,
        )
        SystemHealth.objects.create(device=device, timestamp=now(),
                                    cpu_percent=70.0, mem_percent=50.0)
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device, is_active=True).count() == 0


# ---------------------------------------------------------------------------
# Flapping detection
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestFlapping:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    def test_notification_suppressed_when_flapping(self, mocker, device, settings):
        settings.ALERT_FLAP_THRESHOLD = 4
        settings.ALERT_FLAP_WINDOW_MIN = 30
        mock_email = mocker.patch("apps.alerts.channels.email_channel.send_email_alert")
        rule = make_rule(metric="cpu_percent", condition="gt",
                         threshold=90.0, channels=["email"])
        # 3 lần fire trước đó (đã resolve) trong cửa sổ
        for _ in range(3):
            Alert.objects.create(
                device=device, rule=rule, severity="WARNING",
                message="High CPU", metric_value=95.0, is_active=False,
                resolved_at=now(),
            )
        # Lần thứ 4 → flapping → bỏ qua notification nhưng vẫn tạo Alert
        SystemHealth.objects.create(device=device, timestamp=now(),
                                    cpu_percent=96.0, mem_percent=50.0)
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device, is_active=True).count() == 1
        mock_email.assert_not_called()

    def test_notification_sent_when_not_flapping(self, mocker, device, settings):
        settings.ALERT_FLAP_THRESHOLD = 4
        settings.ALERT_FLAP_WINDOW_MIN = 30
        mock_email = mocker.patch("apps.alerts.channels.email_channel.send_email_alert")
        make_rule(metric="cpu_percent", condition="gt",
                  threshold=90.0, channels=["email"])
        SystemHealth.objects.create(device=device, timestamp=now(),
                                    cpu_percent=96.0, mem_percent=50.0)
        check_device_alerts(device, since())
        mock_email.assert_called_once()


# ---------------------------------------------------------------------------
# N+1 query optimization — số query cố định theo số snapshot
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSustainedQueryCount:
    def _make_vm_snapshots(self, device, n: int):
        base = now() - timedelta(minutes=n + 1)
        for i in range(n):
            ts = base + timedelta(minutes=i)
            VMStats.objects.create(device=device, timestamp=ts,
                                   vm_name="vm1", state="Running", cpu_percent=10)
            VMStats.objects.create(device=device, timestamp=ts,
                                   vm_name="vm2", state="Running", cpu_percent=10)

    def test_sustained_vm_metric_constant_queries(self, db, django_assert_num_queries):
        device = HyperVDeviceFactory()
        rule = AlertRule(metric="vm_count_running", condition="gt", threshold=1.0)
        window = now() - timedelta(minutes=30)

        self._make_vm_snapshots(device, 3)
        with django_assert_num_queries(2):
            v3 = _sustained_vm_metric(device, rule, window)

        self._make_vm_snapshots(device, 5)  # nhiều snapshot hơn
        with django_assert_num_queries(2):
            v8 = _sustained_vm_metric(device, rule, window)

        # 2 VM Running mỗi snapshot, sustained → trả 2.0
        assert v3 == 2.0
        assert v8 == 2.0

    def test_sustained_uplink_constant_queries(self, db, django_assert_num_queries):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1,
                                         name="Gi0/1", is_uplink=True)
        rule = AlertRule(metric="uplink_in_mbps_max", condition="gt", threshold=0.0)
        window = now() - timedelta(minutes=30)
        base = now() - timedelta(minutes=10)
        for i in range(6):
            InterfaceStats.objects.create(interface=iface,
                                          timestamp=base + timedelta(minutes=i),
                                          status="up", in_mbps=5.0, out_mbps=1.0)
        # uplink_ids query + grouped query = 2
        with django_assert_num_queries(2):
            val = _sustained_uplink_traffic_max(device, rule, window)
        assert val == 5.0
