"""Tests cho MetricWriter — _calc_mbps và save_metrics."""
import pytest
from datetime import datetime, timezone, timedelta
from apps.collectors.base import NormalizedData, InterfaceData
from apps.devices.models import Interface
from apps.metrics.models import InterfaceStats, SystemHealth
from apps.metrics.writer import _calc_mbps, save_metrics
from tests.conftest import CiscoSNMPDeviceFactory


def make_timestamp(offset_secs: int = 0) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(seconds=offset_secs)


# ---------------------------------------------------------------------------
# _calc_mbps — pure delta logic
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCalcMbps:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    @pytest.fixture
    def iface(self, device):
        return Interface.objects.create(
            device=device, if_index=1, name="Gi0/1",
        )

    def test_no_previous_record_returns_zeros(self, iface):
        new = InterfaceData(name="Gi0/1", if_index=1, status="up",
                            in_bytes=1_000_000, out_bytes=500_000)
        assert _calc_mbps(None, new, make_timestamp(), fallback_interval_secs=300) == (0.0, 0.0)

    def test_prev_in_bytes_zero_still_calculates_delta(self, iface):
        prev = InterfaceStats.objects.create(
            interface=iface, timestamp=make_timestamp(300),
            status="up", in_bytes=0, out_bytes=0,
        )
        new = InterfaceData(name="Gi0/1", if_index=1, status="up",
                            in_bytes=1_000_000, out_bytes=500_000)
        in_mbps, out_mbps = _calc_mbps(prev, new, make_timestamp(), fallback_interval_secs=300)
        assert in_mbps > 0
        assert out_mbps > 0

    def test_normal_delta_in(self, iface):
        prev = InterfaceStats.objects.create(
            interface=iface, timestamp=make_timestamp(300),
            status="up", in_bytes=1_000_000, out_bytes=0,
        )
        new = InterfaceData(name="Gi0/1", if_index=1, status="up",
                            in_bytes=2_000_000, out_bytes=0)
        in_mbps, _ = _calc_mbps(prev, new, make_timestamp(), fallback_interval_secs=300)
        # delta = 1_000_000 bytes, (1_000_000 * 8) / (300 * 1_000_000) ≈ 0.027
        assert abs(in_mbps - 0.027) < 0.001

    def test_normal_delta_out(self, iface):
        prev = InterfaceStats.objects.create(
            interface=iface, timestamp=make_timestamp(300),
            status="up", in_bytes=100, out_bytes=1_500_000,
        )
        new = InterfaceData(name="Gi0/1", if_index=1, status="up",
                            in_bytes=100, out_bytes=3_000_000)
        _, out_mbps = _calc_mbps(prev, new, make_timestamp(), fallback_interval_secs=300)
        # delta = 1_500_000 bytes, (1_500_000 * 8) / (300 * 1_000_000) = 0.04
        assert abs(out_mbps - 0.04) < 0.001

    def test_counter_reset_returns_zero(self, iface):
        prev = InterfaceStats.objects.create(
            interface=iface, timestamp=make_timestamp(300),
            status="up", in_bytes=5_000_000, out_bytes=0,
        )
        # new < prev → counter reset, delta clamped to 0 via max(0, ...)
        new = InterfaceData(name="Gi0/1", if_index=1, status="up",
                            in_bytes=100, out_bytes=0)
        in_mbps, _ = _calc_mbps(prev, new, make_timestamp(), fallback_interval_secs=300)
        assert in_mbps == 0.0

    def test_result_rounded_to_3_decimal_places(self, iface):
        prev = InterfaceStats.objects.create(
            interface=iface, timestamp=make_timestamp(300),
            status="up", in_bytes=1_000_000, out_bytes=1_000_000,
        )
        new = InterfaceData(name="Gi0/1", if_index=1, status="up",
                            in_bytes=2_123_456, out_bytes=2_123_456)
        in_mbps, out_mbps = _calc_mbps(prev, new, make_timestamp(), fallback_interval_secs=300)
        assert in_mbps == round(in_mbps, 3)
        assert out_mbps == round(out_mbps, 3)


# ---------------------------------------------------------------------------
# save_metrics — integration với real SQLite
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSaveMetrics:
    @pytest.fixture
    def device(self, db):
        return CiscoSNMPDeviceFactory()

    def _make_normalized(self, device, interfaces=None, vms=None):
        extra = {}
        if vms is not None:
            extra["vms"] = vms
        return NormalizedData(
            device_name=device.name,
            ip_address=device.ip_address,
            timestamp=datetime.now(tz=timezone.utc),
            os_family="cisco_ios",
            cpu_percent=25.0,
            mem_percent=50.0,
            uptime_secs=7200,
            interfaces=interfaces or [],
            extra=extra,
        )

    def test_save_system_health_creates_record(self, device):
        data = self._make_normalized(device)
        save_metrics(device, data)
        assert SystemHealth.objects.filter(device=device).count() == 1

    def test_save_system_health_values(self, device):
        data = self._make_normalized(device)
        save_metrics(device, data)
        health = SystemHealth.objects.get(device=device)
        assert health.cpu_percent == 25.0
        assert health.mem_percent == 50.0
        assert health.uptime_secs == 7200

    def test_save_interface_stats_creates_records(self, device):
        ifaces = [
            InterfaceData(name="Gi0/1", if_index=1, status="up",
                          in_bytes=1000, out_bytes=2000),
            InterfaceData(name="Gi0/2", if_index=2, status="down",
                          in_bytes=0, out_bytes=0),
        ]
        data = self._make_normalized(device, interfaces=ifaces)
        save_metrics(device, data)

        assert Interface.objects.filter(device=device).count() == 2
        assert InterfaceStats.objects.count() == 2

    def test_mbps_computed_when_poll_interval_exceeds_collect_interval(self, device):
        """Regression: beat poll mỗi 300s nhưng collect_interval=60 → prev vẫn phải tìm thấy.

        Trước đây cửa sổ prev = collect_interval*3 = 180s < 300s → prev=None → mbps=0 giả.
        """
        device.collect_interval = 60
        device.save()
        t0 = datetime.now(tz=timezone.utc) - timedelta(seconds=300)
        t1 = datetime.now(tz=timezone.utc)

        data0 = NormalizedData(
            device_name=device.name, ip_address=device.ip_address, timestamp=t0,
            os_family="cisco_ios", cpu_percent=10.0, mem_percent=20.0,
            interfaces=[InterfaceData(name="Gi0/1", if_index=1, status="up",
                                      in_bytes=1_000_000_000, out_bytes=2_000_000_000)],
        )
        save_metrics(device, data0)

        # 300s sau: counter tăng ~37.5MB in → ~1 Mbps
        data1 = NormalizedData(
            device_name=device.name, ip_address=device.ip_address, timestamp=t1,
            os_family="cisco_ios", cpu_percent=10.0, mem_percent=20.0,
            interfaces=[InterfaceData(name="Gi0/1", if_index=1, status="up",
                                      in_bytes=1_037_500_000, out_bytes=2_037_500_000)],
        )
        save_metrics(device, data1)

        latest = (InterfaceStats.objects
                  .filter(interface__device=device, interface__if_index=1)
                  .order_by("-timestamp").first())
        assert latest.in_mbps > 0
        assert latest.out_mbps > 0

    def test_save_metrics_does_not_duplicate_interface(self, device):
        iface_data = InterfaceData(name="Gi0/1", if_index=1, status="up",
                                   in_bytes=1000, out_bytes=2000)
        data = self._make_normalized(device, interfaces=[iface_data])
        save_metrics(device, data)
        save_metrics(device, data)  # second poll — same interface

        # Interface row should be get_or_created, not duplicated
        assert Interface.objects.filter(device=device, if_index=1).count() == 1
        # But two stats records (one per poll)
        assert InterfaceStats.objects.count() == 2

    def test_no_interface_stats_when_no_interfaces(self, device):
        data = self._make_normalized(device, interfaces=[])
        save_metrics(device, data)
        assert InterfaceStats.objects.count() == 0

    def test_vm_stats_skipped_when_no_vms_key(self, device):
        from apps.metrics.models import VMStats
        data = self._make_normalized(device)
        save_metrics(device, data)
        assert VMStats.objects.count() == 0

    def test_vm_stats_saved_when_present(self, device):
        from apps.metrics.models import VMStats
        vms = [{"name": "VM-Test", "state": "Running",
                "cpu_percent": 10.0, "mem_mb": 4096, "repl_health": "Normal"}]
        data = self._make_normalized(device, vms=vms)
        save_metrics(device, data)
        assert VMStats.objects.filter(device=device, vm_name="VM-Test").count() == 1

    def test_auto_detects_trunk_by_description_keyword(self, device):
        ifaces = [
            InterfaceData(
                name="Gi1/0/24",
                if_index=24,
                status="up",
                in_bytes=1000,
                out_bytes=2000,
                description="Uplink to CORE-SW",
                speed_mbps=1000,
            )
        ]
        data = self._make_normalized(device, interfaces=ifaces)
        save_metrics(device, data)
        iface = Interface.objects.get(device=device, if_index=24)
        assert iface.is_uplink is True

    def test_auto_detects_trunk_by_aggregated_port_name(self, device):
        ifaces = [
            InterfaceData(
                name="Port-channel1",
                if_index=1001,
                status="up",
                in_bytes=1000,
                out_bytes=2000,
                speed_mbps=1000,
            )
        ]
        data = self._make_normalized(device, interfaces=ifaces)
        save_metrics(device, data)
        iface = Interface.objects.get(device=device, if_index=1001)
        assert iface.is_uplink is True

    def test_auto_detects_trunk_by_high_speed(self, device):
        ifaces = [
            InterfaceData(
                name="Gi1/0/48",
                if_index=48,
                status="up",
                in_bytes=1000,
                out_bytes=2000,
                speed_mbps=10000,
            )
        ]
        data = self._make_normalized(device, interfaces=ifaces)
        save_metrics(device, data)
        iface = Interface.objects.get(device=device, if_index=48)
        assert iface.is_uplink is True

    def test_updates_existing_interface_uplink_flag_after_new_poll(self, device):
        iface = Interface.objects.create(
            device=device,
            if_index=1,
            name="Gi1/0/1",
            description="",
            is_uplink=False,
        )
        InterfaceStats.objects.create(
            interface=iface,
            timestamp=make_timestamp(300),
            status="up",
            in_bytes=100,
            out_bytes=100,
        )

        ifaces = [
            InterfaceData(
                name="Gi1/0/1",
                if_index=1,
                status="up",
                in_bytes=200,
                out_bytes=300,
                description="TRUNK to Dist-SW",
                speed_mbps=1000,
            )
        ]
        data = self._make_normalized(device, interfaces=ifaces)
        save_metrics(device, data)
        iface.refresh_from_db()
        assert iface.is_uplink is True
