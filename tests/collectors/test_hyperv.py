"""Tests cho HyperVCollector — mock WinRM, không cần kết nối thật."""
import json
import pytest
from datetime import datetime, timedelta, timezone

winrm = pytest.importorskip("winrm")

from apps.collectors.hyperv import HyperVCollector
from apps.collectors.base import NormalizedData
from tests.conftest import HyperVDeviceFactory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _boot_time_str(hours_ago: float = 2.0) -> str:
    """Trả về ISO 8601 string giống PowerShell .ToString("o")."""
    boot_dt = datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)
    # PowerShell thường sinh 7 fractional digits + Z
    return boot_dt.strftime("%Y-%m-%dT%H:%M:%S.%f0Z")


def _make_raw(
    cpu: float = 50.0,
    mem: float = 60.0,
    boot_time: str | None = None,
    vms: list | None = None,
) -> dict:
    raw: dict = {"host_cpu_percent": cpu, "host_mem_percent": mem}
    if boot_time is not None:
        raw["host_boot_time"] = boot_time
    raw["vms"] = vms or []
    return raw


def _make_winrm_mock(mocker, raw_dict: dict, status_code: int = 0, stderr: bytes = b""):
    mock_result = mocker.MagicMock()
    mock_result.status_code = status_code
    mock_result.std_out = json.dumps(raw_dict).encode()
    mock_result.std_err = stderr

    mock_session = mocker.MagicMock()
    mock_session.run_ps.return_value = mock_result

    mocker.patch("winrm.Session", return_value=mock_session)
    return mock_session


# ---------------------------------------------------------------------------
# TestHyperVCollectorAdapt — pure unit, không cần DB
# ---------------------------------------------------------------------------

class TestHyperVCollectorAdapt:
    @pytest.fixture
    def collector(self, db):
        device = HyperVDeviceFactory()
        return HyperVCollector(device)

    def test_adapt_returns_normalized_data(self, collector):
        raw = _make_raw(cpu=75.0, mem=60.0, boot_time=_boot_time_str(2))
        result = collector.adapt(raw)
        assert isinstance(result, NormalizedData)
        assert result.cpu_percent == 75.0
        assert result.mem_percent == 60.0

    def test_adapt_os_family(self, collector):
        result = collector.adapt(_make_raw())
        assert result.os_family == "hyperv_winrm"

    def test_adapt_uptime_computed_from_boot_time(self, collector):
        raw = _make_raw(boot_time=_boot_time_str(2.0))
        result = collector.adapt(raw)
        # ~7200 giây, cho phép sai số ±10s
        assert abs(result.uptime_secs - 7200) < 10

    def test_adapt_uptime_zero_when_boot_time_missing(self, collector):
        raw = _make_raw()  # không có host_boot_time
        result = collector.adapt(raw)
        assert result.uptime_secs == 0

    def test_adapt_uptime_zero_on_invalid_boot_time(self, collector):
        raw = _make_raw(boot_time="not-a-valid-date")
        result = collector.adapt(raw)
        assert result.uptime_secs == 0  # không raise exception

    def test_adapt_vms_in_extra(self, collector):
        vms = [
            {"name": "VM-Web", "state": "Running", "cpu_percent": 10, "mem_mb": 4096, "repl_health": "Normal"},
            {"name": "VM-DB",  "state": "Stopped", "cpu_percent": 0,  "mem_mb": 8192, "repl_health": "NotConfigured"},
        ]
        raw = _make_raw(vms=vms)
        result = collector.adapt(raw)
        assert result.extra["vms"] == vms

    def test_adapt_empty_vms(self, collector):
        result = collector.adapt(_make_raw(vms=[]))
        assert result.extra["vms"] == []

    def test_adapt_interfaces_always_empty(self, collector):
        """HyperV collector không thu thập interface stats."""
        result = collector.adapt(_make_raw())
        assert result.interfaces == []

    def test_adapt_missing_cpu_mem_defaults_to_zero(self, collector):
        result = collector.adapt({"vms": []})
        assert result.cpu_percent == 0.0
        assert result.mem_percent == 0.0


# ---------------------------------------------------------------------------
# TestHyperVCollectorCollectRaw — mock winrm.Session
# ---------------------------------------------------------------------------

class TestHyperVCollectorCollectRaw:
    @pytest.fixture
    def collector(self, db):
        device = HyperVDeviceFactory()
        return HyperVCollector(device)

    def test_collect_raw_calls_winrm_with_ntlm(self, collector, mocker):
        mock_session = _make_winrm_mock(mocker, _make_raw())
        collector.collect_raw()
        import winrm as _winrm
        _winrm.Session.assert_called_once()
        call_kwargs = _winrm.Session.call_args
        assert call_kwargs.kwargs.get("transport") == "ntlm" or "ntlm" in str(call_kwargs)

    def test_collect_raw_returns_parsed_dict(self, collector, mocker):
        raw = _make_raw(cpu=80.0, mem=55.0, boot_time=_boot_time_str(1))
        _make_winrm_mock(mocker, raw)
        result = collector.collect_raw()
        assert result["host_cpu_percent"] == 80.0
        assert result["host_mem_percent"] == 55.0

    def test_collect_raw_raises_on_nonzero_exit_code(self, collector, mocker):
        _make_winrm_mock(mocker, {}, status_code=1, stderr=b"Access Denied")
        with pytest.raises(RuntimeError, match="PowerShell error"):
            collector.collect_raw()

    def test_collect_returns_normalized_data_end_to_end(self, collector, mocker):
        raw = _make_raw(cpu=40.0, mem=70.0, boot_time=_boot_time_str(3))
        _make_winrm_mock(mocker, raw)
        result = collector.collect()
        assert isinstance(result, NormalizedData)
        assert result.cpu_percent == 40.0
        assert result.os_family == "hyperv_winrm"

    def test_collect_raw_fallbacks_to_5986_when_5985_connect_timeout(self, collector, mocker):
        from requests import exceptions as req_exc

        mock_result = mocker.MagicMock()
        mock_result.status_code = 0
        mock_result.std_out = json.dumps(_make_raw()).encode()
        mock_result.std_err = b""

        session_5985 = mocker.MagicMock()
        session_5985.run_ps.side_effect = req_exc.ConnectTimeout("5985 timeout")
        session_5986 = mocker.MagicMock()
        session_5986.run_ps.return_value = mock_result

        session_factory = mocker.patch("winrm.Session", side_effect=[session_5985, session_5986])

        result = collector.collect_raw()

        assert result["host_cpu_percent"] == 50.0
        assert session_factory.call_count == 2
        first_target = session_factory.call_args_list[0].kwargs["target"]
        second_target = session_factory.call_args_list[1].kwargs["target"]
        assert first_target.startswith(f"http://{collector.device.ip_address}:5985")
        assert second_target.startswith(f"https://{collector.device.ip_address}:5986")
