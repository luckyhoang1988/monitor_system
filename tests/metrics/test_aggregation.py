"""Tests cho module aggregation — rollup hourly/daily."""
import pytest
from datetime import datetime, timedelta, timezone as dt_tz
from django.utils import timezone
from apps.devices.models import Interface
from apps.metrics.models import (
    SystemHealth, InterfaceStats,
    SystemHealthHourly, SystemHealthDaily,
    InterfaceStatsHourly, InterfaceStatsDaily,
)
from apps.metrics.aggregation import (
    rollup_system_health_hourly,
    rollup_interface_stats_hourly,
    rollup_system_health_daily,
    rollup_interface_stats_daily,
    cleanup_rolled_up_raw_data,
    HOURLY_BUFFER_HOURS,
    RAW_RETENTION_HOURS,
)
from tests.conftest import CiscoSNMPDeviceFactory


def _hours_ago(hours: int) -> datetime:
    return timezone.now() - timedelta(hours=hours)


def _days_ago(days: int) -> datetime:
    return timezone.now() - timedelta(days=days)


@pytest.mark.django_db
class TestRollupSystemHealthHourly:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    def _create_raw(self, device, hours_ago: int, cpu: float, mem: float):
        return SystemHealth.objects.create(
            device=device,
            timestamp=_hours_ago(hours_ago),
            cpu_percent=cpu,
            mem_percent=mem,
        )

    def test_creates_hourly_record_from_raw(self, device):
        """Raw data cũ hơn buffer → rollup thành hourly."""
        ts = _hours_ago(HOURLY_BUFFER_HOURS + 1)
        SystemHealth.objects.create(
            device=device, timestamp=ts,
            cpu_percent=50.0, mem_percent=60.0,
        )
        SystemHealth.objects.create(
            device=device, timestamp=ts + timedelta(minutes=5),
            cpu_percent=70.0, mem_percent=80.0,
        )

        count = rollup_system_health_hourly()
        assert count >= 1

        hourly = SystemHealthHourly.objects.filter(device=device).first()
        assert hourly is not None
        assert hourly.cpu_avg == round((50.0 + 70.0) / 2, 2)
        assert hourly.cpu_max == 70.0
        assert hourly.mem_avg == round((60.0 + 80.0) / 2, 2)
        assert hourly.mem_max == 80.0
        assert hourly.sample_count == 2

    def test_skips_recent_data_within_buffer(self, device):
        """Data trong buffer (< 2 giờ) không bị rollup."""
        SystemHealth.objects.create(
            device=device, timestamp=_hours_ago(1),
            cpu_percent=50.0, mem_percent=60.0,
        )
        count = rollup_system_health_hourly()
        assert count == 0

    def test_update_existing_hourly_record(self, device):
        """Rollup lại cùng giờ → update_or_create (không tạo duplicate)."""
        ts = _hours_ago(HOURLY_BUFFER_HOURS + 1)
        SystemHealth.objects.create(
            device=device, timestamp=ts,
            cpu_percent=50.0, mem_percent=60.0,
        )
        rollup_system_health_hourly()
        assert SystemHealthHourly.objects.filter(device=device).count() == 1

        # Thêm raw data cùng giờ rồi rollup lại
        SystemHealth.objects.create(
            device=device, timestamp=ts + timedelta(minutes=3),
            cpu_percent=90.0, mem_percent=95.0,
        )
        rollup_system_health_hourly()
        assert SystemHealthHourly.objects.filter(device=device).count() == 1

        hourly = SystemHealthHourly.objects.get(device=device)
        assert hourly.cpu_max == 90.0
        assert hourly.sample_count == 2


@pytest.mark.django_db
class TestRollupInterfaceStatsHourly:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    @pytest.fixture
    def iface(self, device):
        return Interface.objects.create(device=device, if_index=1, name="Gi0/1")

    def test_creates_hourly_from_raw(self, iface):
        ts = _hours_ago(HOURLY_BUFFER_HOURS + 1)
        InterfaceStats.objects.create(
            interface=iface, timestamp=ts, status="up",
            in_mbps=10.5, out_mbps=5.2, in_errors=2, out_errors=1,
        )
        InterfaceStats.objects.create(
            interface=iface, timestamp=ts + timedelta(minutes=5), status="up",
            in_mbps=20.5, out_mbps=15.8, in_errors=3, out_errors=0,
        )

        count = rollup_interface_stats_hourly()
        assert count >= 1

        hourly = InterfaceStatsHourly.objects.filter(interface=iface).first()
        assert hourly is not None
        assert hourly.in_mbps_avg == round((10.5 + 20.5) / 2, 3)
        assert hourly.in_mbps_max == 20.5
        assert hourly.out_mbps_avg == round((5.2 + 15.8) / 2, 3)
        assert hourly.out_mbps_max == 15.8
        assert hourly.in_errors == 5  # sum
        assert hourly.out_errors == 1  # sum
        assert hourly.sample_count == 2


@pytest.mark.django_db
class TestRollupDaily:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    @pytest.fixture
    def iface(self, device):
        return Interface.objects.create(device=device, if_index=1, name="Gi0/1")

    def test_system_health_daily_from_hourly(self, device):
        """Hourly data cũ hơn buffer → rollup thành daily."""
        ts = _days_ago(3)
        SystemHealthHourly.objects.create(
            device=device, hour=ts,
            cpu_avg=40.0, cpu_max=60.0,
            mem_avg=50.0, mem_max=70.0, sample_count=12,
        )
        SystemHealthHourly.objects.create(
            device=device, hour=ts + timedelta(hours=1),
            cpu_avg=60.0, cpu_max=80.0,
            mem_avg=70.0, mem_max=90.0, sample_count=12,
        )

        count = rollup_system_health_daily()
        assert count >= 1

        daily = SystemHealthDaily.objects.filter(device=device).first()
        assert daily is not None
        assert daily.cpu_avg == round((40.0 + 60.0) / 2, 2)
        assert daily.cpu_max == 80.0
        assert daily.sample_count == 24

    def test_interface_stats_daily_from_hourly(self, iface):
        ts = _days_ago(3)
        InterfaceStatsHourly.objects.create(
            interface=iface, hour=ts,
            in_mbps_avg=10.0, in_mbps_max=15.0,
            out_mbps_avg=5.0, out_mbps_max=8.0,
            in_errors=10, out_errors=5, sample_count=12,
        )
        InterfaceStatsHourly.objects.create(
            interface=iface, hour=ts + timedelta(hours=1),
            in_mbps_avg=20.0, in_mbps_max=25.0,
            out_mbps_avg=10.0, out_mbps_max=12.0,
            in_errors=5, out_errors=3, sample_count=12,
        )

        count = rollup_interface_stats_daily()
        assert count >= 1

        daily = InterfaceStatsDaily.objects.filter(interface=iface).first()
        assert daily is not None
        assert daily.in_mbps_max == 25.0
        assert daily.in_errors == 15  # sum
        assert daily.sample_count == 24


@pytest.mark.django_db
class TestCleanupRolledUpRaw:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    def test_deletes_raw_data_older_than_retention(self, device):
        """Raw data cũ hơn RAW_RETENTION_HOURS bị xóa khi đã có hourly."""
        old_ts = _hours_ago(RAW_RETENTION_HOURS + 1)

        # Tạo raw data cũ
        SystemHealth.objects.create(
            device=device, timestamp=old_ts,
            cpu_percent=50.0, mem_percent=60.0,
        )
        # Tạo hourly tương ứng (giả lập đã rollup)
        SystemHealthHourly.objects.create(
            device=device, hour=old_ts.replace(minute=0, second=0, microsecond=0),
            cpu_avg=50.0, cpu_max=50.0,
            mem_avg=60.0, mem_max=60.0, sample_count=1,
        )

        del_sh, del_if = cleanup_rolled_up_raw_data()
        assert del_sh == 1
        assert SystemHealth.objects.filter(device=device).count() == 0

    def test_keeps_recent_raw_data(self, device):
        """Raw data gần (< RAW_RETENTION_HOURS) được giữ lại."""
        recent_ts = _hours_ago(1)
        SystemHealth.objects.create(
            device=device, timestamp=recent_ts,
            cpu_percent=50.0, mem_percent=60.0,
        )

        cleanup_rolled_up_raw_data()
        assert SystemHealth.objects.filter(device=device).count() == 1


@pytest.mark.django_db
class TestAPISourceSelection:
    """Test API tự động chọn raw/hourly/daily."""

    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    @pytest.fixture
    def client(self, db):
        from django.test import Client
        from django.contrib.auth.models import User
        client = Client()
        user = User.objects.create_user("testuser", password="testpass")
        client.login(username="testuser", password="testpass")
        return client

    def test_1h_range_uses_raw(self, client, device):
        resp = client.get(f"/api/metrics/{device.pk}/?range=1h")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "raw"

    def test_7d_range_uses_hourly(self, client, device):
        resp = client.get(f"/api/metrics/{device.pk}/?range=7d")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "hourly"

    def test_30d_range_uses_daily(self, client, device):
        resp = client.get(f"/api/metrics/{device.pk}/?range=30d")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "daily"

    def test_interface_1h_uses_raw(self, client, device):
        resp = client.get(f"/api/metrics/{device.pk}/interfaces/?range=1h")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "raw"

    def test_interface_7d_uses_hourly(self, client, device):
        resp = client.get(f"/api/metrics/{device.pk}/interfaces/?range=7d")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "hourly"

