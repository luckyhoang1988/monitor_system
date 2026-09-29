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

    def test_incomplete_poll_does_not_falsely_resolve_missing_disk_alert(self):
        """Regression 2026-09-29: poll không đầy đủ (endpoint con /LogicalDrives//DataDrives/
        iLO lỗi) từng khiến normalize() ghi missing_disk_count=0 GIẢ (xem
        apps/collectors/ilo_redfish.py) -> alert bị resolve dù RAID thật chưa chắc đã hồi phục.
        Nay poll không đầy đủ phải ghi None (không phải 0) -- _latest_ilo lọc
        `missing_disk_count__isnull=False` nên tự rơi về giá trị KHÔNG-null gần nhất (2) thay vì
        coi None là "đã về 0", alert phải VẪN active."""
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        rule = make_rule(metric="raid_missing_disk_count", condition="gte", threshold=1.0)
        HardwareHealth.objects.create(
            device=device, timestamp=now() - timedelta(minutes=5), missing_disk_count=2,
        )
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

        # Poll kế tiếp KHÔNG đầy đủ (giả lập endpoint con lỗi) -- field None, KHÔNG phải 0.
        HardwareHealth.objects.create(device=device, timestamp=now(), missing_disk_count=None)
        check_device_alerts(device, since())

        assert Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

    def test_does_not_fire_for_non_hyperv_device(self):
        from tests.conftest import CiscoSNMPDeviceFactory
        device = CiscoSNMPDeviceFactory()
        rule = make_rule()  # device_type="hyperv"
        # Không tạo HardwareHealth cho device switch -> getter trả None -> không fire, nhưng
        # cũng xác nhận rule không áp dụng nhầm device_type khác qua device_type filter.
        check_device_alerts(device, since())
        assert not Alert.objects.filter(device=device, rule=rule).exists()

    def test_controller_critical_message_is_human_readable(self):
        """Regression: _fmt_metric() từng không có nhánh raid_* (rơi về f"{v:.2f}") và message
        dùng rule.metric thô — tin nhắn thực tế trước fix là
        "Hyperv-02: raid_controller_health = 2.00 (ngưỡng gte 2.00)". Nay phải đọc được: dùng
        metric_label + tên health code (OK/Warning/Critical) thay vì số thô."""
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        rule = make_rule(
            name="HyperV RAID Controller Critical",
            metric="raid_controller_health", condition="gte", threshold=2.0,
        )
        HardwareHealth.objects.create(device=device, timestamp=now(), controller_health_code=2)

        check_device_alerts(device, since())

        alert = Alert.objects.get(device=device, rule=rule, is_active=True)
        assert "raid_controller_health" not in alert.message
        assert "RAID Controller Health (iLO)" in alert.message
        assert "Critical" in alert.message
        assert "2.00" not in alert.message


@pytest.mark.django_db
class TestLatestIloExtended:
    """Regression 2026-09-29: mở rộng ngoài RAID (Battery/Processor/Memory/Fan/Temperature/
    PowerSupply/Power Redundancy) — _ILO_FIELD_MAP/_latest_ilo đã generic theo field name, chỉ
    cần verify wiring đúng, không cần sửa logic."""

    def test_returns_latest_value_for_power_supply_and_redundancy(self):
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        HardwareHealth.objects.create(
            device=device, timestamp=now() - timedelta(minutes=5),
            power_supply_worst_code=2, power_redundancy_ok=False,
            battery_health_code=1, fan_worst_code=0,
        )
        assert _latest_ilo(device, since(), "ilo_power_supply_health") == 2.0
        # BooleanField False -> float(False) == 0.0 qua values_list, không cần sửa _latest_ilo.
        assert _latest_ilo(device, since(), "ilo_power_redundancy") == 0.0
        assert _latest_ilo(device, since(), "ilo_battery_health") == 1.0
        assert _latest_ilo(device, since(), "ilo_fan_health") == 0.0

    def test_none_when_field_is_null(self):
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        HardwareHealth.objects.create(device=device, timestamp=now(), power_redundancy_ok=None)
        assert _latest_ilo(device, since(), "ilo_power_redundancy") is None


@pytest.mark.django_db
class TestCheckDeviceAlertsIloExtended:
    def test_fires_critical_on_power_not_redundant_matches_real_hyprver03_case(self):
        """Mirror sự cố PSU thật Hyprver03 2026-09-29 (PSU Bay1 Critical/Offline ACPowerLost,
        Bay2 OK, MinNumNeeded=2) -> power_redundancy_ok=False -> rule 'HyperV Power Not
        Redundant' (condition=eq, threshold=0.0) phải fire CRITICAL."""
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        rule = make_rule(
            name="HyperV Power Not Redundant", metric="ilo_power_redundancy",
            condition="eq", threshold=0.0, severity="CRITICAL",
        )
        HardwareHealth.objects.create(
            device=device, timestamp=now(), power_redundancy_ok=False, power_supply_worst_code=2,
        )

        check_device_alerts(device, since())

        alert = Alert.objects.get(device=device, rule=rule, is_active=True)
        assert "ilo_power_redundancy" not in alert.message
        assert "Power Redundancy (iLO)" in alert.message
        assert "DEGRADED" in alert.message

    def test_resolves_when_redundancy_restored(self):
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        rule = make_rule(
            name="HyperV Power Not Redundant", metric="ilo_power_redundancy",
            condition="eq", threshold=0.0, severity="CRITICAL",
        )
        HardwareHealth.objects.create(device=device, timestamp=now() - timedelta(minutes=5), power_redundancy_ok=False)
        check_device_alerts(device, since())
        assert Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

        HardwareHealth.objects.create(device=device, timestamp=now(), power_redundancy_ok=True)
        check_device_alerts(device, since())

        assert not Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

    def test_battery_degraded_message_is_human_readable(self):
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        rule = make_rule(
            name="HyperV Battery Warning+", metric="ilo_battery_health",
            condition="gte", threshold=1.0, severity="WARNING",
        )
        HardwareHealth.objects.create(device=device, timestamp=now(), battery_health_code=1)

        check_device_alerts(device, since())

        alert = Alert.objects.get(device=device, rule=rule, is_active=True)
        assert "ilo_battery_health" not in alert.message
        assert "Smart Storage Battery Health (iLO)" in alert.message
        assert "Warning" in alert.message


class TestMetricChoicesIncludeIlo:
    """Regression: apps/alerts/forms.py::METRIC_CHOICES từng tự chép tay và thiếu 4 metric
    raid_* (+ 11 metric host-perf HyperV) — dropdown sửa rule không có option khớp giá trị đang
    lưu, HTML <select> tự chọn option đầu tiên, bấm Lưu âm thầm đổi sai metric của rule. Nay
    forms.METRIC_CHOICES phải đọc thẳng từ AlertRule.METRIC_LABELS (nguồn sự thật duy nhất)."""

    def test_metric_choices_is_built_from_alertrule_metric_labels(self):
        from apps.alerts.forms import METRIC_CHOICES

        assert dict(METRIC_CHOICES) == AlertRule.METRIC_LABELS

    def test_metric_choices_contains_all_ilo_metrics(self):
        from apps.alerts.forms import METRIC_CHOICES

        keys = {k for k, _ in METRIC_CHOICES}
        assert {
            "raid_controller_health",
            "raid_logical_drive_health",
            "raid_missing_disk_count",
            "raid_enclosure_mismatch",
            "ilo_battery_health",
            "ilo_processor_health",
            "ilo_memory_health",
            "ilo_fan_health",
            "ilo_temperature_health",
            "ilo_power_supply_health",
            "ilo_power_redundancy",
        } <= keys
