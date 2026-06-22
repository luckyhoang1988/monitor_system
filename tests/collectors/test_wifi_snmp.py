"""Tests cho thu thập WLAN (AP + client) của SwitchSNMPCollector — mock SNMP walk."""
import pytest

from apps.collectors.switch_snmp import SwitchSNMPCollector
from tests.conftest import HuaweiACDeviceFactory


AP = "1.3.6.1.4.1.2011.6.139.13.3.3.1"
STA = "1.3.6.1.4.1.2011.6.139.13.3.5.1"

WLAN_PROFILE = {
    "wlan": {
        "enterprise_parent": "1.3.6.1.4.1.2011.6.139",
        "ap_run_state_online": [8],
        "ap": {
            "name":      f"{AP}.4",
            "mac":       f"{AP}.2",
            "ip":        f"{AP}.30",
            "group":     f"{AP}.6",
            "run_state": f"{AP}.13",
            "sta_count": f"{AP}.40",
        },
        "station": {
            "ap_name":     f"{STA}.3",
            "ssid":        f"{STA}.4",
            "ip":          f"{STA}.7",
            "radio_type":  f"{STA}.5",
            "rssi":        f"{STA}.9",
            "online_time": f"{STA}.12",
        },
    }
}


def _walk_mock(data: dict[str, list[tuple[str, str]]]):
    """data: {column_oid: [(index_suffix, value), ...]} -> side_effect cho _snmp_walk."""
    def side_effect(prefix):
        rows = data.get(prefix, [])
        return [(f"{prefix}.{suffix}", value) for suffix, value in rows]
    return side_effect


class TestMacHelpers:
    def test_mac_from_index_decodes_trailing_six_octets(self):
        assert SwitchSNMPCollector._mac_from_index("0.17.34.51.68.85") == "00:11:22:33:44:55"

    def test_mac_from_index_short_index_returns_empty(self):
        assert SwitchSNMPCollector._mac_from_index("1.2.3") == ""

    def test_decode_mac_value_hex_string(self):
        assert SwitchSNMPCollector._decode_mac_value("0x001122334455") == "00:11:22:33:44:55"

    def test_decode_mac_value_passthrough_formatted(self):
        assert SwitchSNMPCollector._decode_mac_value("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"


class TestCollectWifi:
    @pytest.fixture
    def collector(self):
        return SwitchSNMPCollector(HuaweiACDeviceFactory.build())

    def test_parses_aps_and_online_state(self, mocker, collector):
        mocker.patch.object(collector, "_snmp_walk", side_effect=_walk_mock({
            f"{AP}.4": [("1", "AP-Floor1"), ("2", "AP-Floor2")],
            f"{AP}.2": [("1", "0x00112233aa01"), ("2", "0x00112233aa02")],
            f"{AP}.30": [("1", "10.0.50.11"), ("2", "10.0.50.12")],
            f"{AP}.6": [("1", "GroupA"), ("2", "GroupA")],
            f"{AP}.13": [("1", "8"), ("2", "4")],   # 8=online, 4=offline
            f"{AP}.40": [("1", "12"), ("2", "0")],
        }))
        result = collector._collect_wifi(WLAN_PROFILE)
        aps = result["wifi_aps"]
        assert len(aps) == 2
        ap1 = next(a for a in aps if a["name"] == "AP-Floor1")
        assert ap1["is_online"] is True
        assert ap1["client_count"] == 12
        assert ap1["mac"] == "00:11:22:33:aa:01"
        ap2 = next(a for a in aps if a["name"] == "AP-Floor2")
        assert ap2["is_online"] is False

    def test_parses_clients_with_mac_from_index(self, mocker, collector):
        idx = "0.17.34.51.68.85"  # MAC 00:11:22:33:44:55
        mocker.patch.object(collector, "_snmp_walk", side_effect=_walk_mock({
            f"{STA}.3": [(idx, "AP-Floor1")],
            f"{STA}.4": [(idx, "Corp-WiFi")],
            f"{STA}.7": [(idx, "10.0.60.20")],
            f"{STA}.5": [(idx, "5G")],
            f"{STA}.9": [(idx, "-55")],
            f"{STA}.12": [(idx, "3600")],
        }))
        result = collector._collect_wifi(WLAN_PROFILE)
        clients = result["wifi_clients"]
        assert len(clients) == 1
        c = clients[0]
        assert c["mac"] == "00:11:22:33:44:55"
        assert c["ssid"] == "Corp-WiFi"
        assert c["ap_name"] == "AP-Floor1"
        assert c["rssi"] == -55
        assert c["online_secs"] == 3600

    def test_empty_profile_returns_empty(self, mocker, collector):
        mocker.patch.object(collector, "_snmp_walk", return_value=[])
        result = collector._collect_wifi({"wlan": {}})
        assert result == {"wifi_aps": [], "wifi_clients": []}

    def test_collect_raw_includes_wifi_for_controller(self, mocker, collector):
        mocker.patch.object(collector, "detect_os_family", return_value="huawei_vrp")
        mocker.patch("apps.collectors.switch_snmp._load_oid_profile", return_value={
            "cpu": {"cpu_usage": "1.1", "cpu_table": "1"},
            "memory": {"mem_usage": "2.1", "mem_table": "2"},
            **WLAN_PROFILE,
        })
        mocker.patch.object(collector, "_collect_cpu_mem_huawei", return_value=(10.0, 20.0))
        mocker.patch.object(collector, "_collect_interfaces", return_value=[])
        mocker.patch.object(collector, "_snmp_get", return_value=0)
        mocker.patch.object(collector, "_collect_wifi", return_value={
            "wifi_aps": [{"name": "AP1"}],
            "wifi_clients": [{"mac": "aa:bb:cc:dd:ee:ff"}],
        })
        raw = collector.collect_raw()
        assert raw["extra"]["wifi_aps"] == [{"name": "AP1"}]
        assert raw["extra"]["wifi_clients"][0]["mac"] == "aa:bb:cc:dd:ee:ff"
