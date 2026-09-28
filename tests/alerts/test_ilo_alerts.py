"""Tests cho alert engine + iLO RAID/disk health (HardwareHealth) — wiring _ILO_FIELD_MAP/
_latest_ilo/_sustained_ilo vào check_device_alerts. Không cần thiết bị iLO thật."""
from datetime import datetime, timezone, timedelta

import pytest

from apps.alerts.engine import check_device_alerts, _latest_ilo
from apps.alerts.models import AlertRule, Alert
from apps.metrics.models import HardwareHealth
from tests.conftest import HyperVDeviceFactory


def now():
    return datetime.now(tz=timezone.utc)


def since():
    return now() - timedelta(minutes=10)


def make_rule(**kwargs):
    defaults = dict(
        name="Test iLO Rule", device_type="hyperv", metric="raid_missing_disk_count",
        condition="gte", threshold=1.0, severity="CRITICAL", channels=[], enabled=True,
        duration_min=0,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


@pytest.mark.django_db
class TestLatestIlo:
    def test_returns_latest_field_value(self):
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        HardwareHealth.objects.create(
            device=device, timestamp=now() - timedelta(minutes=5),
            controller_health_code=2, logical_drive_worst_code=1,
            missing_disk_count=2, enclosure_mismatch_count=1,
        )
        assert _latest_ilo(device, since(), "raid_missing_disk_count") == 2.0
        assert _latest_ilo(device, since(), "raid_controller_health") == 2.0

    def test_none_when_no_data(self):
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        assert _latest_ilo(device, since(), "raid_missing_disk_count") is None

    def test_none_when_data_older_than_since(self):
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        HardwareHealth.objects.create(
            device=device, timestamp=now() - timedelta(hours=2),
            missing_disk_count=2,
        )
        assert _latest_ilo(device, since(), "raid_missing_disk_count") is None


@pytest.mark.django_db
class TestCheckDeviceAlertsIlo:
    def test_fires_on_missing_disk(self):
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        rule = make_rule()
        HardwareHealth.objects.create(
            device=device, timestamp=now(), missing_disk_count=2,
        )

        check_device_alerts(device, since())

        assert Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

    def test_resolves_immediately_when_disk_recovered_duration_min_0(self):
        """duration_min=0 -> đường 'else: value = getter(...)' (instant), resolve ngay khi
        HardwareHealth mới nhất báo missing_disk_count=0 — không cần sustain window."""
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        rule = make_rule()
        HardwareHealth.objects.create(device=device, timestamp=now() - timedelta(minutes=5), missing_disk_count=2)
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

        HardwareHealth.objects.create(device=device, timestamp=now(), missing_disk_count=0)
        check_device_alerts(device, since())

        assert not Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

    def test_does_not_fire_for_non_hyperv_device(self):
        from tests.conftest import CiscoSNMPDeviceFactory
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()  # device_type="hyperv"
        # Không tạo HardwareHealth cho device switch -> getter trả None -> không fire, nhưng
        # cũng xác nhận rule không áp dụng nhầm device_type khác qua device_type filter.
        check_device_alerts(device, since())
        assert not Alert.objects.filter(device=device, rule=rule).exists()
