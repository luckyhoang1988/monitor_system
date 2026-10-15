"""Hạng mục 1.5 — lỗi GHI metrics không được biến thành "thiết bị Offline".

Trước đây `collector.collect()` và `save_metrics()` nằm chung 1 `try` trong `_poll_device_once`:
`save_metrics` ném (vd `DataError` vì `ifAlias` dài hơn `Interface.description` max_length=200) thì
`snmp_valid` giữ False → `last_seen=None` → badge Off + alert `device_online` giả, dù SNMP khoẻ.
Đo prod 2026-10-15: 0/20 thiết bị có ifAlias > 200 (dài nhất 50) — đây là lỗ hổng tiềm ẩn.
"""
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from apps.collectors.base import InterfaceData, NormalizedData
from apps.collectors.tasks import poll_device
from apps.devices.models import Interface
from apps.metrics.models import InterfaceStats, VMStats
from apps.metrics.writer import save_metrics
from tests.conftest import CiscoSNMPDeviceFactory, HyperVDeviceFactory

pytestmark = pytest.mark.django_db


def _data(os_family: str = "cisco_ios", interfaces=None, extra=None) -> NormalizedData:
    return NormalizedData(
        device_name="t", ip_address="10.0.0.1", timestamp=datetime.now(tz=UTC),
        os_family=os_family, cpu_percent=30.0, mem_percent=45.0, uptime_secs=1,
        interfaces=interfaces if interfaces is not None else [],
        extra=extra if extra is not None else {},
    )


def _iface(name="Gi0/1", description="", idx=1) -> InterfaceData:
    return InterfaceData(
        name=name, if_index=idx, status="up", in_bytes=0, out_bytes=0, description=description,
    )


# ---------------------------------------------------------------------------
# Tầng task: lỗi ghi ≠ offline
# ---------------------------------------------------------------------------

class TestWriteFailureDoesNotMarkOffline:
    @pytest.fixture
    def polled(self, mocker):
        """Poll 1 switch SNMP khoẻ (ICMP ok, có interface) mà `save_metrics` ném lỗi."""
        device = CiscoSNMPDeviceFactory(last_seen=None, os_family="cisco_ios")
        data = _data("cisco_iosxe", interfaces=[_iface()])
        collector = MagicMock()
        collector.collect.return_value = data
        mocker.patch("apps.collectors.factory.CollectorFactory.create", return_value=collector)
        mocker.patch("apps.collectors.ping_util.icmp_ping", return_value=(True, 1.0))
        save = mocker.patch(
            "apps.metrics.writer.save_metrics", side_effect=RuntimeError("ghi DB hỏng"),
        )
        publish = mocker.patch("apps.realtime.publisher.publish_device_event")
        return device, save, publish

    def test_device_stays_online_when_write_fails(self, polled):
        device, save, _ = polled

        poll_device.run(device.pk)  # không được ném

        save.assert_called_once()
        device.refresh_from_db()
        assert device.last_seen is not None, "SNMP+ICMP khoẻ thì không được hiện Offline"
        assert device.last_ok_seen is not None
        assert device.os_family == "cisco_iosxe"

    def test_realtime_event_reports_online(self, polled):
        device, _, publish = polled

        poll_device.run(device.pk)

        assert publish.call_count == 1
        args, kwargs = publish.call_args
        online = kwargs.get("online", args[1] if len(args) > 1 else None)
        assert online is True

    def test_write_failure_is_logged_with_traceback(self, polled, caplog):
        device, _, _ = polled

        with caplog.at_level(logging.ERROR, logger="apps.collectors.tasks"):
            poll_device.run(device.pk)

        recs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(recs) == 1
        assert recs[0].exc_info is not None
        assert device.name in recs[0].getMessage()

    def test_soft_limit_during_write_is_warning_not_error(self, polled, caplog):
        """Thiết bị đã trả lời xong → cắt giờ lúc ghi vẫn là online, và không được báo ERROR giả
        (SoftTimeLimitExceeded kế thừa Exception nên `except Exception` sẽ bắt nhầm nó)."""
        from celery.exceptions import SoftTimeLimitExceeded
        device, save, _ = polled
        save.side_effect = SoftTimeLimitExceeded()

        with caplog.at_level(logging.WARNING, logger="apps.collectors.tasks"):
            poll_device.run(device.pk)

        device.refresh_from_db()
        assert device.last_seen is not None
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("soft_time_limit" in r.getMessage() for r in caplog.records)

    def test_collect_failure_still_marks_offline(self, mocker):
        """Bảo vệ hành vi cũ: lỗi THU THẬP (khác lỗi ghi) vẫn là offline."""
        device = CiscoSNMPDeviceFactory(last_seen=None)
        collector = MagicMock()
        collector.collect.side_effect = RuntimeError("SNMP timeout")
        mocker.patch("apps.collectors.factory.CollectorFactory.create", return_value=collector)
        mocker.patch("apps.collectors.ping_util.icmp_ping", return_value=(True, 1.0))
        save = mocker.patch("apps.metrics.writer.save_metrics")
        mocker.patch("apps.realtime.publisher.publish_device_event")

        poll_device.run(device.pk)

        save.assert_not_called()
        device.refresh_from_db()
        assert device.last_seen is None


# ---------------------------------------------------------------------------
# Tầng writer: văn bản quá dài không được làm hỏng cả lần ghi
# ---------------------------------------------------------------------------

class TestOverlongTextIsClamped:
    @pytest.fixture(autouse=True)
    def _db_mode(self, settings):
        settings.METRICS_WRITE_MODE = "db"

    def test_interface_name_and_description_clamped(self):
        device = CiscoSNMPDeviceFactory()
        data = _data(interfaces=[_iface(name="G" * 150, description="d" * 300)])

        save_metrics(device, data)  # trước đây: DataError value too long

        iface = Interface.objects.get(device=device)
        assert len(iface.name) == 100
        assert len(iface.description) == 200

    def test_second_poll_reuses_same_interface(self):
        """Khoá tra cứu phải theo tên ĐÃ cắt, nếu không mỗi poll cố tạo lại interface
        → vi phạm unique_together (device, name)."""
        device = CiscoSNMPDeviceFactory()

        for _ in range(2):
            save_metrics(device, _data(interfaces=[_iface(name="G" * 150)]))

        assert Interface.objects.filter(device=device).count() == 1
        assert InterfaceStats.objects.filter(interface__device=device).count() == 2

    def test_names_colliding_after_clamp_do_not_violate_unique(self):
        device = CiscoSNMPDeviceFactory()
        data = _data(interfaces=[
            _iface(name="X" * 100 + "a", idx=1),
            _iface(name="X" * 100 + "b", idx=2),
        ])

        save_metrics(device, data)

        assert Interface.objects.filter(device=device).count() == 1

    def test_cache_mode_receives_clamped_names(self, settings, mocker):
        settings.METRICS_WRITE_MODE = "cache"
        seen = {}

        def spy(dev, data):
            seen["names"] = [i.name for i in data.interfaces]
            seen["descs"] = [i.description for i in data.interfaces]
            return True

        mocker.patch("apps.metrics.writer._save_metrics_cache", side_effect=spy)
        device = CiscoSNMPDeviceFactory()

        save_metrics(device, _data(interfaces=[_iface(name="G" * 150, description="d" * 300)]))

        assert [len(n) for n in seen["names"]] == [100]
        assert [len(d) for d in seen["descs"]] == [200]

    def test_vm_name_and_state_clamped(self):
        device = HyperVDeviceFactory()
        vm = {"name": "v" * 250, "state": "S" * 40, "cpu_percent": 1, "mem_mb": 1}
        data = _data("hyperv", extra={"vms": [vm]})

        save_metrics(device, data)

        row = VMStats.objects.get(device=device)
        assert len(row.vm_name) == 200
        assert len(row.state) == 20
