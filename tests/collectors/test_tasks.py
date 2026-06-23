"""Tests cho Celery tasks — chạy synchronously qua .run(), không cần Redis/worker."""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from apps.collectors.tasks import poll_device, poll_all_switches, poll_all_hyperv, poll_all_ping_devices
from apps.collectors.base import NormalizedData
from tests.conftest import CiscoSNMPDeviceFactory, HyperVDeviceFactory


def _make_normalized_data(os_family: str = "cisco_ios") -> NormalizedData:
    return NormalizedData(
        device_name="test-device",
        ip_address="10.0.0.1",
        timestamp=datetime.now(tz=timezone.utc),
        os_family=os_family,
        cpu_percent=30.0,
        mem_percent=45.0,
        uptime_secs=86400,
        interfaces=[],
        extra={},
    )


# ---------------------------------------------------------------------------
# TestPollDevice
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPollDevice:
    def test_poll_device_saves_metrics_and_updates_last_seen(self, mocker):
        from apps.collectors.base import InterfaceData
        device = CiscoSNMPDeviceFactory()
        data = _make_normalized_data("cisco_ios")
        # Switch online cần SNMP có interface thật + ping thông (AND).
        data.interfaces = [InterfaceData(name="Gi0/1", if_index=1, status="up",
                                         in_bytes=0, out_bytes=0)]

        mock_collector = MagicMock()
        mock_collector.collect.return_value = data
        mocker.patch("apps.collectors.factory.CollectorFactory.create", return_value=mock_collector)
        mock_save = mocker.patch("apps.metrics.writer.save_metrics")
        mocker.patch("apps.collectors.ping_util.icmp_ping", return_value=(True, 1.0))

        poll_device.run(device.pk)

        device.refresh_from_db()
        assert device.last_seen is not None
        mock_save.assert_called_once()

    def test_poll_device_updates_os_family(self, mocker):
        device = CiscoSNMPDeviceFactory(os_family="cisco_ios")
        data = _make_normalized_data("cisco_iosxe")

        mock_collector = MagicMock()
        mock_collector.collect.return_value = data
        mocker.patch("apps.collectors.factory.CollectorFactory.create", return_value=mock_collector)
        mocker.patch("apps.metrics.writer.save_metrics")
        # Switch mạng cần ICMP thông mới chạy collect (offline ⇒ short-circuit, bỏ collect).
        mocker.patch("apps.collectors.ping_util.icmp_ping", return_value=(True, 1.0))

        poll_device.run(device.pk)

        device.refresh_from_db()
        assert device.os_family == "cisco_iosxe"

    def test_poll_device_skips_collect_when_icmp_down(self, mocker):
        # Thiết bị mạng ICMP fail ⇒ chắc chắn offline ⇒ bỏ qua collect SNMP đắt đỏ
        # (tránh treo worker ~240s/thiết bị chết khi chu kỳ poll 120s).
        device = CiscoSNMPDeviceFactory(os_family="cisco_ios")

        mock_collector = MagicMock()
        mocker.patch("apps.collectors.factory.CollectorFactory.create", return_value=mock_collector)
        mock_save = mocker.patch("apps.metrics.writer.save_metrics")
        mocker.patch("apps.collectors.ping_util.icmp_ping", return_value=(False, None))

        poll_device.run(device.pk)

        mock_collector.collect.assert_not_called()
        mock_save.assert_not_called()
        device.refresh_from_db()
        assert device.last_seen is None

    def test_poll_device_does_not_retry_on_device_not_found(self, mocker):
        mock_retry = mocker.patch.object(poll_device, "retry")
        mock_save = mocker.patch("apps.metrics.writer.save_metrics")

        poll_device.run(99999)  # device không tồn tại

        mock_retry.assert_not_called()
        mock_save.assert_not_called()

    def test_poll_device_retries_on_collector_exception(self, mocker):
        device = CiscoSNMPDeviceFactory()
        mocker.patch(
            "apps.collectors.factory.CollectorFactory.create",
            side_effect=ConnectionError("timeout"),
        )
        mocker.patch.object(
            poll_device, "retry",
            side_effect=Exception("retry-sentinel"),
        )

        with pytest.raises(Exception, match="retry-sentinel"):
            poll_device.run(device.pk)

    def test_poll_device_hyperv_saves_vm_data(self, mocker):
        device = HyperVDeviceFactory()
        vms = [{"name": "VM1", "state": "Running", "cpu_percent": 5, "mem_mb": 2048, "repl_health": "Normal"}]
        data = NormalizedData(
            device_name=device.name,
            ip_address=device.ip_address,
            timestamp=datetime.now(tz=timezone.utc),
            os_family="hyperv_winrm",
            cpu_percent=20.0,
            mem_percent=50.0,
            uptime_secs=3600,
            interfaces=[],
            extra={"vms": vms},
        )

        mock_collector = MagicMock()
        mock_collector.collect.return_value = data
        mocker.patch("apps.collectors.factory.CollectorFactory.create", return_value=mock_collector)
        mock_save = mocker.patch("apps.metrics.writer.save_metrics")

        poll_device.run(device.pk)

        mock_save.assert_called_once_with(device, data)


# ---------------------------------------------------------------------------
# TestPollAllSwitches
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPollAllSwitches:
    def test_delegates_to_poll_all_network_devices(self, mocker):
        # poll_all_switches là backward-compat wrapper, delegate tới poll_all_network_devices
        mock_nd = mocker.patch("apps.collectors.tasks.poll_all_network_devices.delay")
        poll_all_switches.run()
        mock_nd.assert_called_once()


@pytest.mark.django_db
class TestPollAllNetworkDevices:
    def test_dispatches_for_switch_router_firewall_enabled(self, mocker):
        from tests.conftest import MikroTikSNMPDeviceFactory, FortinetSNMPDeviceFactory
        CiscoSNMPDeviceFactory(device_type="switch",   enabled=True)
        CiscoSNMPDeviceFactory(device_type="switch",   enabled=False)  # skip disabled
        MikroTikSNMPDeviceFactory(device_type="router",   enabled=True)
        FortinetSNMPDeviceFactory(device_type="firewall", enabled=True)

        mock_delay = mocker.patch("apps.collectors.tasks.poll_device.delay")
        from apps.collectors.tasks import poll_all_network_devices
        poll_all_network_devices.run()

        assert mock_delay.call_count == 3  # 1 switch + 1 router + 1 firewall

    def test_excludes_ping_protocol_devices(self, mocker):
        CiscoSNMPDeviceFactory(device_type="switch", enabled=True, protocol="snmp")
        CiscoSNMPDeviceFactory(device_type="switch", enabled=True, protocol="ping")
        mock_delay = mocker.patch("apps.collectors.tasks.poll_device.delay")
        from apps.collectors.tasks import poll_all_network_devices
        poll_all_network_devices.run()
        assert mock_delay.call_count == 1

    def test_dispatches_zero_when_no_network_devices(self, mocker):
        mock_delay = mocker.patch("apps.collectors.tasks.poll_device.delay")
        from apps.collectors.tasks import poll_all_network_devices
        poll_all_network_devices.run()
        mock_delay.assert_not_called()


# ---------------------------------------------------------------------------
# TestPollAllHyperV
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPollAllHyperV:
    def test_polls_inline_for_each_enabled_hyperv(self, mocker):
        HyperVDeviceFactory(enabled=True)
        HyperVDeviceFactory(enabled=True)
        HyperVDeviceFactory(enabled=False)  # không dispatch

        mock_once = mocker.patch("apps.collectors.tasks._poll_device_once")
        poll_all_hyperv.run()

        assert mock_once.call_count == 2

    def test_polls_zero_when_no_hyperv_hosts(self, mocker):
        mock_once = mocker.patch("apps.collectors.tasks._poll_device_once")
        poll_all_hyperv.run()
        mock_once.assert_not_called()

    def test_inline_poll_continues_when_one_host_fails(self, mocker):
        HyperVDeviceFactory(enabled=True)
        HyperVDeviceFactory(enabled=True)
        mock_once = mocker.patch(
            "apps.collectors.tasks._poll_device_once",
            side_effect=[None, RuntimeError("timeout")],
        )
        poll_all_hyperv.run()
        assert mock_once.call_count == 2


@pytest.mark.django_db
class TestPollAllPingDevices:
    def test_dispatches_only_enabled_ping_devices(self, mocker):
        CiscoSNMPDeviceFactory(device_type="switch", enabled=True, protocol="ping")
        CiscoSNMPDeviceFactory(device_type="switch", enabled=True, protocol="snmp")
        CiscoSNMPDeviceFactory(device_type="switch", enabled=False, protocol="ping")
        mock_delay = mocker.patch("apps.collectors.tasks.poll_device.delay")
        poll_all_ping_devices.run()
        assert mock_delay.call_count == 1
