"""Tests cho lọc uplink/trunk trong topology FDB."""
import pytest

from apps.collectors.topology_fdb import (
    FdbMacEntry,
    PortMeta,
    _mac_from_fdb_index,
    filter_fdb_ap_entries,
    is_uplink_port,
)
from tests.conftest import CiscoSNMPDeviceFactory


class TestMacFromFdbIndex:
    def test_vlan_plus_mac(self):
        vlan, mac = _mac_from_fdb_index("8.0.12.132.8.89.128")
        assert vlan == 8
        assert mac == "00:0c:84:08:59:80"

    def test_mac_only(self):
        vlan, mac = _mac_from_fdb_index("0.12.132.8.89.128")
        assert vlan is None
        assert mac == "00:0c:84:08:59:80"


def _entry(port: str, mac: str) -> FdbMacEntry:
    return FdbMacEntry(
        local_port=port, local_port_num=1, mac=mac, is_ap_match=True,
    )


class TestIsUplinkPort:
    def test_trunk_port_mode(self):
        meta = {"Gi0/1": PortMeta(port_mode="trunk")}
        assert is_uplink_port("Gi0/1", meta) is True

    def test_access_port_mode(self):
        meta = {"Gi0/1": PortMeta(port_mode="access", ap_mac_count=1)}
        assert is_uplink_port("Gi0/1", meta) is False

    def test_ap_flood_threshold(self):
        meta = {"GE1/0/25": PortMeta(port_mode="access", ap_mac_count=5)}
        assert is_uplink_port("GE1/0/25", meta) is True

    def test_eth_trunk_name(self):
        assert is_uplink_port("Eth-Trunk1", {}) is True

    def test_total_mac_flood_threshold(self):
        # Uplink chỉ mang 1 MAC-AP nhưng hàng trăm MAC tổng → vẫn là uplink.
        meta = {"Gi9": PortMeta(ap_mac_count=1, total_mac_count=582)}
        assert is_uplink_port("Gi9", meta) is True

    def test_low_total_mac_is_access(self):
        meta = {"GE1/0/3": PortMeta(ap_mac_count=1, total_mac_count=1)}
        assert is_uplink_port("GE1/0/3", meta) is False


@pytest.mark.django_db
class TestFilterFdbApEntries:
    def test_excludes_uplink_and_picks_access(self):
        device = CiscoSNMPDeviceFactory(uplink_ports=[])
        from apps.devices.models import Interface

        Interface.objects.create(
            device=device, if_index=25, name="GE1/0/25",
            port_mode="trunk", is_uplink=True,
        )
        Interface.objects.create(
            device=device, if_index=12, name="GE1/0/12",
            port_mode="access", is_uplink=False,
        )
        mac = "44:e9:68:bc:4e:c0"
        raw = [
            _entry("GE1/0/25", mac),
            _entry("GE1/0/12", mac),
        ]
        filtered = filter_fdb_ap_entries(device, raw)
        assert len(filtered) == 1
        assert filtered[0].local_port == "GE1/0/12"

    def test_excludes_port_with_many_ap_macs(self):
        device = CiscoSNMPDeviceFactory()
        raw = [_entry("GE1/0/25", f"aa:bb:cc:dd:ee:{i:02x}") for i in range(4)]
        filtered = filter_fdb_ap_entries(device, raw)
        assert filtered == []

    def test_excludes_uplink_by_total_mac_no_portmode(self):
        # Như Cisco Business: không có port_mode/is_uplink. Uplink chỉ phân biệt
        # được qua tổng MAC (582 vs 1) — đúng case AP flood qua uplink switch nối tầng.
        device = CiscoSNMPDeviceFactory(uplink_ports=[])
        mac = "ac:99:29:e4:89:70"
        raw = [_entry("Gi9", mac), _entry("GE1/0/3", mac)]
        filtered = filter_fdb_ap_entries(
            device, raw, port_total_counts={"Gi9": 582, "GE1/0/3": 1},
        )
        assert len(filtered) == 1
        assert filtered[0].local_port == "GE1/0/3"
