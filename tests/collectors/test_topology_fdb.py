"""Tests cho topology_fdb — parse MAC index FDB."""
from apps.collectors.topology_fdb import _mac_from_fdb_index


class TestMacFromFdbIndex:
    def test_vlan_plus_mac(self):
        vlan, mac = _mac_from_fdb_index("8.0.12.132.8.89.128")
        assert vlan == 8
        assert mac == "00:0c:84:08:59:80"

    def test_mac_only(self):
        vlan, mac = _mac_from_fdb_index("0.12.132.8.89.128")
        assert vlan is None
        assert mac == "00:0c:84:08:59:80"

    def test_invalid(self):
        vlan, mac = _mac_from_fdb_index("1.2.3")
        assert mac == ""
