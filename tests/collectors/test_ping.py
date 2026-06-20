"""Tests for PingCollector."""
import pytest
from unittest.mock import patch, MagicMock
from apps.collectors.factory import CollectorFactory
from apps.collectors.ping import PingCollector
from tests.conftest import DeviceFactory


class PingDeviceFactory(DeviceFactory):
    protocol = "ping"
    vendor = "cisco"
    device_type = "switch"


@pytest.fixture
def ping_device(db):
    return PingDeviceFactory()


@pytest.mark.django_db
class TestPingCollector:
    def test_factory_creates_ping_collector(self, ping_device):
        collector = CollectorFactory.create(ping_device)
        assert isinstance(collector, PingCollector)

    @patch("apps.collectors.ping.PingCollector._ping")
    def test_connection_success(self, mock_ping, ping_device):
        mock_ping.return_value = (True, 4.5)
        collector = PingCollector(ping_device)
        os_family = collector.test_connection()
        assert os_family == "ping_only"

    @patch("apps.collectors.ping.PingCollector._ping")
    def test_connection_failure(self, mock_ping, ping_device):
        mock_ping.return_value = (False, 0.0)
        collector = PingCollector(ping_device)
        with pytest.raises(Exception) as excinfo:
            collector.test_connection()
        assert "Không thể kết nối Ping" in str(excinfo.value)

    @patch("apps.collectors.ping.PingCollector._ping")
    def test_collect_raw(self, mock_ping, ping_device):
        mock_ping.return_value = (True, 12.3)
        collector = PingCollector(ping_device)
        raw = collector.collect_raw()
        assert raw["success"] is True
        assert raw["rtt_ms"] == 12.3

    @patch("apps.collectors.ping.PingCollector._ping")
    def test_adapt_success(self, mock_ping, ping_device):
        mock_ping.return_value = (True, 15.0)
        collector = PingCollector(ping_device)
        data = collector.collect()
        assert data.os_family == "ping_only"
        assert data.cpu_percent == 0.0
        assert data.mem_percent == 0.0
        assert data.uptime_secs == 86400
        assert data.extra["ping_rtt_ms"] == 15.0

    @patch("apps.collectors.ping.PingCollector._ping")
    def test_adapt_failure(self, mock_ping, ping_device):
        mock_ping.return_value = (False, 0.0)
        collector = PingCollector(ping_device)
        data = collector.collect()
        assert data.cpu_percent == -1.0
        assert data.mem_percent == -1.0
        assert data.uptime_secs == 0
        assert "ping_rtt_ms" not in data.extra

    def test_parse_rtt_windows(self):
        collector = PingCollector(MagicMock())
        output = "Reply from 127.0.0.1: bytes=32 time=4ms TTL=128\nMinimum = 4ms, Maximum = 4ms, Average = 4ms"
        assert collector._parse_rtt(output) == 4.0

    def test_parse_rtt_linux(self):
        collector = PingCollector(MagicMock())
        output = "64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.045 ms"
        assert collector._parse_rtt(output) == 0.045
