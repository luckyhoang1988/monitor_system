"""Tests cho icmp_ping util và logic online kết hợp ICMP+SNMP trong _poll_device_once."""
import pytest
from datetime import datetime, timezone

from apps.collectors import ping_util
from apps.collectors.ping_util import icmp_ping
from apps.collectors.base import NormalizedData, InterfaceData
from apps.collectors import tasks
from tests.conftest import CiscoSNMPDeviceFactory, HyperVDeviceFactory


class _FakeProc:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# ---------------------------------------------------------------------------
# icmp_ping
# ---------------------------------------------------------------------------

class TestIcmpPing:
    def test_success_parses_rtt(self, mocker):
        mocker.patch("subprocess.run", return_value=_FakeProc(0, "time=1.23 ms"))
        ok, rtt = icmp_ping("10.0.0.1")
        assert ok is True
        assert rtt == 1.23

    def test_failure_returns_false_none(self, mocker):
        mocker.patch("subprocess.run", return_value=_FakeProc(1, ""))
        ok, rtt = icmp_ping("10.0.0.1", attempts=2)
        assert ok is False
        assert rtt is None

    def test_missing_ping_binary(self, mocker):
        mocker.patch("subprocess.run", side_effect=FileNotFoundError())
        ok, rtt = icmp_ping("10.0.0.1")
        assert ok is False and rtt is None

    def test_retries_until_success(self, mocker):
        run = mocker.patch("subprocess.run",
                           side_effect=[_FakeProc(1, ""), _FakeProc(0, "time=2 ms")])
        ok, rtt = icmp_ping("10.0.0.1", attempts=2)
        assert ok is True and rtt == 2.0
        assert run.call_count == 2


# ---------------------------------------------------------------------------
# _poll_device_once — kết hợp ICMP + SNMP (AND cho thiết bị mạng)
# ---------------------------------------------------------------------------

def _nd(device, n_ifaces: int) -> NormalizedData:
    ifaces = [InterfaceData(name=f"Gi0/{i}", if_index=i, status="up",
                            in_bytes=0, out_bytes=0) for i in range(1, n_ifaces + 1)]
    return NormalizedData(
        device_name=device.name, ip_address=device.ip_address,
        timestamp=datetime.now(tz=timezone.utc), os_family="cisco_ios",
        cpu_percent=10.0, mem_percent=20.0, interfaces=ifaces,
    )


@pytest.mark.django_db
class TestPollOnlineCombination:
    def _patch(self, mocker, device, data, icmp_result):
        fake = mocker.Mock()
        if isinstance(data, Exception):
            fake.collect.side_effect = data
        else:
            fake.collect.return_value = data
        mocker.patch("apps.collectors.factory.CollectorFactory.create", return_value=fake)
        mocker.patch("apps.metrics.writer.save_metrics")
        mocker.patch("apps.collectors.ping_util.icmp_ping", return_value=icmp_result)

    def test_online_when_snmp_and_ping_ok(self, mocker, db):
        device = CiscoSNMPDeviceFactory(last_seen=None)
        self._patch(mocker, device, _nd(device, 3), (True, 1.0))
        tasks._poll_device_once(device.pk)
        device.refresh_from_db()
        assert device.last_seen is not None

    def test_offline_when_ping_fails(self, mocker, db):
        device = CiscoSNMPDeviceFactory(last_seen=None)
        self._patch(mocker, device, _nd(device, 3), (False, None))
        tasks._poll_device_once(device.pk)
        device.refresh_from_db()
        assert device.last_seen is None  # SNMP ok nhưng ping fail → offline

    def test_offline_when_snmp_empty(self, mocker, db):
        device = CiscoSNMPDeviceFactory(last_seen=None)
        self._patch(mocker, device, _nd(device, 0), (True, 1.0))
        tasks._poll_device_once(device.pk)
        device.refresh_from_db()
        assert device.last_seen is None  # 0 interface → SNMP rỗng → offline

    def test_offline_when_snmp_raises(self, mocker, db):
        device = CiscoSNMPDeviceFactory(last_seen=None)
        self._patch(mocker, device, RuntimeError("SNMP timeout"), (True, 1.0))
        tasks._poll_device_once(device.pk)
        device.refresh_from_db()
        assert device.last_seen is None

    def test_offline_clears_last_seen_when_was_online(self, mocker, db):
        """Chuyển online→offline: last_seen phải clear để is_online khớp SSE badge."""
        from django.utils import timezone

        device = CiscoSNMPDeviceFactory(last_seen=timezone.now())
        assert device.is_online is True
        self._patch(mocker, device, _nd(device, 0), (True, 1.0))  # SNMP rỗng → offline
        tasks._poll_device_once(device.pk)
        device.refresh_from_db()
        assert device.last_seen is None
        assert device.is_online is False

    def test_hyperv_does_not_require_icmp(self, mocker, db):
        device = HyperVDeviceFactory(last_seen=None)
        data = NormalizedData(
            device_name=device.name, ip_address=device.ip_address,
            timestamp=datetime.now(tz=timezone.utc), os_family="hyperv_winrm",
            cpu_percent=5.0, mem_percent=10.0, interfaces=[],
        )
        # icmp_ping không nên được gọi cho hyperv; SNMP/WinRM hợp lệ → online
        ping_spy = mocker.patch("apps.collectors.ping_util.icmp_ping", return_value=(False, None))
        fake = mocker.Mock()
        fake.collect.return_value = data
        mocker.patch("apps.collectors.factory.CollectorFactory.create", return_value=fake)
        mocker.patch("apps.metrics.writer.save_metrics")
        tasks._poll_device_once(device.pk)
        device.refresh_from_db()
        assert device.last_seen is not None
        ping_spy.assert_not_called()
