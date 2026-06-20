"""Tests cho SwitchSSHCollector — dùng sample CLI output strings, không cần thiết bị thật."""
import pytest
from apps.collectors.switch_ssh import SwitchSSHCollector
from tests.conftest import CiscoSSHDeviceFactory, CiscoXESSHDeviceFactory, HuaweiSSHDeviceFactory

# ---------------------------------------------------------------------------
# Sample CLI output constants
# ---------------------------------------------------------------------------

CISCO_VERSION_OUTPUT = """\
Cisco IOS Software, Version 15.2(7)E3
ROM: Bootstrap program is C2960 boot loader
SW-Core uptime is 2 weeks, 3 days, 14 hours, 22 minutes
System returned to ROM by power-on
"""

CISCO_XE_VERSION_OUTPUT = """\
Cisco IOS XE Software, Version 16.12.4
SW-Edge uptime is 1 week, 0 days, 2 hours, 5 minutes
"""

CISCO_CPU_OUTPUT = """\
CPU utilization for five seconds: 5%/0%; one minute: 3%; five minutes: 4%
"""

CISCO_MEMORY_OUTPUT = """\
Processor  123456789  98765432  1234 124 Free (b): 98765432
"""

CISCO_INTERFACES_OUTPUT = """\
GigabitEthernet0/1 is up, line protocol is up (connected)
  Description: Uplink to Core
  Hardware is Gigabit Ethernet, address is 1c6a.7a2b.3c4d
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
     5 minute input rate 1234000 bits/sec, 123 packets/sec
     5 minute output rate 567000 bits/sec, 45 packets/sec
        99999 packets input, 9876543 bytes, 0 no buffer
        0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored
        55555 packets output, 5432100 bytes, 0 underruns
        0 output errors, 0 collisions, 3 interface resets
GigabitEthernet0/2 is down, line protocol is down (notconnected)
  Description: Access port
  Hardware is Gigabit Ethernet, address is 1c6a.7a2b.3c4e
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
        0 packets input, 0 bytes, 0 no buffer
        0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored
        0 packets output, 0 bytes, 0 underruns
        0 output errors, 0 collisions, 0 interface resets
"""

HUAWEI_VERSION_OUTPUT = """\
Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.170 (S5700 V200R011C10SPC600)
HUAWEI S5700-28X-LI-AC
Uptime is 0 week(s) 3 day(s) 2 hour(s) 41 minute(s)
"""

HUAWEI_CPU_OUTPUT = """\
CPU Usage     : 15%
"""

HUAWEI_MEMORY_OUTPUT = """\
Memory Using Percentage Is: 45%
"""

HUAWEI_INTERFACES_OUTPUT = """\
GigabitEthernet0/0/1 current state : UP
Line protocol current state : UP
Description:Uplink to Core
Speed : 1000, Loopback: NONE
     Input:  10000 packets, 1280000 bytes
     Output: 8000 packets, 640000 bytes
     Input error: 2 packets
     Output error: 0 packets
GigabitEthernet0/0/2 current state : DOWN
Line protocol current state : DOWN
Description:Access port
Speed : 1000, Loopback: NONE
     Input:  0 packets, 0 bytes
     Output: 0 packets, 0 bytes
     Input error: 0 packets
     Output error: 0 packets
"""


# ---------------------------------------------------------------------------
# Cisco parsing — không cần DB hay network
# ---------------------------------------------------------------------------

class TestCiscoSSHParsing:
    """Test các _parse_* methods trực tiếp với string input."""

    @pytest.fixture
    def collector(self):
        device = CiscoSSHDeviceFactory.build()
        return SwitchSSHCollector(device)

    def test_parse_cpu_five_minutes(self, collector):
        assert collector._parse_cisco_cpu(CISCO_CPU_OUTPUT) == 4.0

    def test_parse_cpu_missing(self, collector):
        assert collector._parse_cisco_cpu("no match here") == 0.0

    def test_parse_mem_basic(self, collector):
        result = collector._parse_cisco_mem(CISCO_MEMORY_OUTPUT)
        # 123456789 / (123456789 + 98765432) ≈ 55.6%
        assert 55.0 < result < 56.5

    def test_parse_mem_zero_total(self, collector):
        assert collector._parse_cisco_mem("Processor  0  0") == 0.0

    def test_parse_mem_missing(self, collector):
        assert collector._parse_cisco_mem("no match") == 0.0

    def test_parse_uptime_full(self, collector):
        # 2 weeks + 3 days + 14 hours + 22 minutes
        expected = 2 * 7 * 86400 + 3 * 86400 + 14 * 3600 + 22 * 60
        assert collector._parse_cisco_uptime(CISCO_VERSION_OUTPUT) == expected

    def test_parse_uptime_hours_minutes_only(self, collector):
        output = "SW uptime is 4 hours, 30 minutes"
        assert collector._parse_cisco_uptime(output) == 4 * 3600 + 30 * 60

    def test_parse_uptime_no_match(self, collector):
        assert collector._parse_cisco_uptime("no time info") == 0

    def test_parse_interfaces_count(self, collector):
        ifaces = collector._parse_cisco_interfaces(CISCO_INTERFACES_OUTPUT)
        assert len(ifaces) == 2

    def test_parse_interfaces_status_up(self, collector):
        ifaces = collector._parse_cisco_interfaces(CISCO_INTERFACES_OUTPUT)
        assert ifaces[0].name == "GigabitEthernet0/1"
        assert ifaces[0].status == "up"

    def test_parse_interfaces_status_down(self, collector):
        ifaces = collector._parse_cisco_interfaces(CISCO_INTERFACES_OUTPUT)
        assert ifaces[1].name == "GigabitEthernet0/2"
        assert ifaces[1].status == "down"

    def test_parse_interfaces_bytes(self, collector):
        ifaces = collector._parse_cisco_interfaces(CISCO_INTERFACES_OUTPUT)
        assert ifaces[0].in_bytes == 9876543
        assert ifaces[0].out_bytes == 5432100

    def test_parse_interfaces_errors(self, collector):
        ifaces = collector._parse_cisco_interfaces(CISCO_INTERFACES_OUTPUT)
        assert ifaces[0].in_errors == 0
        assert ifaces[0].out_errors == 0

    def test_parse_interfaces_speed_mbps(self, collector):
        ifaces = collector._parse_cisco_interfaces(CISCO_INTERFACES_OUTPUT)
        assert ifaces[0].speed_mbps == 1000.0

    def test_parse_interfaces_description(self, collector):
        ifaces = collector._parse_cisco_interfaces(CISCO_INTERFACES_OUTPUT)
        assert ifaces[0].description == "Uplink to Core"
        assert ifaces[1].description == "Access port"

    def test_parse_interfaces_empty_string(self, collector):
        assert collector._parse_cisco_interfaces("") == []

    def test_parse_interfaces_if_index_sequential(self, collector):
        ifaces = collector._parse_cisco_interfaces(CISCO_INTERFACES_OUTPUT)
        assert ifaces[0].if_index == 1
        assert ifaces[1].if_index == 2


# ---------------------------------------------------------------------------
# Huawei parsing
# ---------------------------------------------------------------------------

class TestHuaweiSSHParsing:
    @pytest.fixture
    def collector(self):
        device = HuaweiSSHDeviceFactory.build()
        return SwitchSSHCollector(device)

    def test_parse_cpu_basic(self, collector):
        assert collector._parse_huawei_cpu(HUAWEI_CPU_OUTPUT) == 15.0

    def test_parse_cpu_missing(self, collector):
        assert collector._parse_huawei_cpu("no match") == 0.0

    def test_parse_mem_basic(self, collector):
        assert collector._parse_huawei_mem(HUAWEI_MEMORY_OUTPUT) == 45.0

    def test_parse_mem_missing(self, collector):
        assert collector._parse_huawei_mem("no match") == 0.0

    def test_parse_uptime_full(self, collector):
        # 0 weeks, 3 days, 2 hours, 41 minutes
        expected = 3 * 86400 + 2 * 3600 + 41 * 60
        assert collector._parse_huawei_uptime(HUAWEI_VERSION_OUTPUT) == expected

    def test_parse_uptime_no_match(self, collector):
        assert collector._parse_huawei_uptime("no time info") == 0

    def test_parse_interfaces_count(self, collector):
        ifaces = collector._parse_huawei_interfaces(HUAWEI_INTERFACES_OUTPUT)
        assert len(ifaces) == 2

    def test_parse_interfaces_status_up(self, collector):
        ifaces = collector._parse_huawei_interfaces(HUAWEI_INTERFACES_OUTPUT)
        assert ifaces[0].name == "GigabitEthernet0/0/1"
        assert ifaces[0].status == "up"

    def test_parse_interfaces_status_down(self, collector):
        ifaces = collector._parse_huawei_interfaces(HUAWEI_INTERFACES_OUTPUT)
        assert ifaces[1].status == "down"

    def test_parse_interfaces_bytes(self, collector):
        ifaces = collector._parse_huawei_interfaces(HUAWEI_INTERFACES_OUTPUT)
        assert ifaces[0].in_bytes == 1280000
        assert ifaces[0].out_bytes == 640000

    def test_parse_interfaces_errors(self, collector):
        ifaces = collector._parse_huawei_interfaces(HUAWEI_INTERFACES_OUTPUT)
        assert ifaces[0].in_errors == 2
        assert ifaces[0].out_errors == 0

    def test_parse_interfaces_speed_mbps(self, collector):
        ifaces = collector._parse_huawei_interfaces(HUAWEI_INTERFACES_OUTPUT)
        assert ifaces[0].speed_mbps == 1000.0

    def test_parse_interfaces_description(self, collector):
        ifaces = collector._parse_huawei_interfaces(HUAWEI_INTERFACES_OUTPUT)
        assert ifaces[0].description == "Uplink to Core"

    def test_parse_interfaces_empty_string(self, collector):
        assert collector._parse_huawei_interfaces("") == []


# ---------------------------------------------------------------------------
# collect_raw — mock netmiko connection
# ---------------------------------------------------------------------------

def _make_mock_conn(mocker, side_effects: list[str]):
    """Helper tạo mock ConnectHandler context manager."""
    mock_conn = mocker.MagicMock()
    mock_conn.send_command.side_effect = side_effects
    mock_cm = mocker.MagicMock()
    mock_cm.__enter__ = mocker.MagicMock(return_value=mock_conn)
    mock_cm.__exit__ = mocker.MagicMock(return_value=False)
    return mock_cm


class TestCollectRaw:
    @pytest.mark.django_db
    def test_cisco_ios(self, mocker, cisco_ssh_device):
        mock_cm = _make_mock_conn(mocker, [
            CISCO_VERSION_OUTPUT,
            CISCO_CPU_OUTPUT,
            CISCO_MEMORY_OUTPUT,
            CISCO_INTERFACES_OUTPUT,
        ])
        mocker.patch.object(SwitchSSHCollector, "_get_connection", return_value=mock_cm)

        raw = SwitchSSHCollector(cisco_ssh_device).collect_raw()

        assert raw["os_family"] == "cisco_ios"
        assert raw["cpu_percent"] == 4.0
        assert 55.0 < raw["mem_percent"] < 56.5
        expected_uptime = 2 * 7 * 86400 + 3 * 86400 + 14 * 3600 + 22 * 60
        assert raw["uptime_secs"] == expected_uptime
        assert len(raw["interfaces"]) == 2

    @pytest.mark.django_db
    def test_cisco_iosxe_detected_from_version(self, mocker, cisco_ssh_device):
        mock_cm = _make_mock_conn(mocker, [
            CISCO_XE_VERSION_OUTPUT,
            CISCO_CPU_OUTPUT,
            CISCO_MEMORY_OUTPUT,
            CISCO_INTERFACES_OUTPUT,
        ])
        mocker.patch.object(SwitchSSHCollector, "_get_connection", return_value=mock_cm)

        raw = SwitchSSHCollector(cisco_ssh_device).collect_raw()
        assert raw["os_family"] == "cisco_iosxe"

    @pytest.mark.django_db
    def test_huawei(self, mocker, huawei_ssh_device):
        mock_cm = _make_mock_conn(mocker, [
            HUAWEI_VERSION_OUTPUT,
            HUAWEI_CPU_OUTPUT,
            HUAWEI_MEMORY_OUTPUT,
            HUAWEI_INTERFACES_OUTPUT,
        ])
        mocker.patch.object(SwitchSSHCollector, "_get_connection", return_value=mock_cm)

        raw = SwitchSSHCollector(huawei_ssh_device).collect_raw()

        assert raw["os_family"] == "huawei_vrp"
        assert raw["cpu_percent"] == 15.0
        assert raw["mem_percent"] == 45.0
        expected_uptime = 3 * 86400 + 2 * 3600 + 41 * 60
        assert raw["uptime_secs"] == expected_uptime
        assert len(raw["interfaces"]) == 2


class TestAdapt:
    @pytest.mark.django_db
    def test_adapt_returns_normalized_data(self, cisco_ssh_device):
        from apps.collectors.base import NormalizedData
        raw = {
            "os_family": "cisco_ios",
            "cpu_percent": 30.0,
            "mem_percent": 50.0,
            "uptime_secs": 7200,
            "interfaces": [],
        }
        result = SwitchSSHCollector(cisco_ssh_device).adapt(raw)
        assert isinstance(result, NormalizedData)
        assert result.os_family == "cisco_ios"
        assert result.cpu_percent == 30.0
        assert result.uptime_secs == 7200

    @pytest.mark.django_db
    def test_adapt_timestamp_is_utc(self, cisco_ssh_device):
        from datetime import timezone
        raw = {"os_family": "cisco_ios", "cpu_percent": 0.0,
               "mem_percent": 0.0, "uptime_secs": 0, "interfaces": []}
        result = SwitchSSHCollector(cisco_ssh_device).adapt(raw)
        assert result.timestamp.tzinfo == timezone.utc
