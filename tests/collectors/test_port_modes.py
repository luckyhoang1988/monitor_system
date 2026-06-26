"""Tests cho phân loại trunk/access qua Q-BRIDGE (_parse_portlist + _collect_port_modes)."""
import pytest

from apps.collectors.switch_snmp import SwitchSNMPCollector, _parse_portlist
from tests.conftest import HuaweiSNMPDeviceFactory


# ---------------------------------------------------------------------------
# _parse_portlist — bitmap PortList → set dot1dBasePort (bit 0x80 byte0 = port 1)
# ---------------------------------------------------------------------------

class TestParsePortlist:
    def test_empty_string(self):
        assert _parse_portlist("") == set()

    def test_none(self):
        assert _parse_portlist(None) == set()

    def test_first_bit_is_port_1(self):
        # 0x80 = 1000 0000 → chỉ port 1.
        assert _parse_portlist("0x80") == {1}

    def test_bytes_input(self):
        # byte 0x40 = 0100 0000 → port 2.
        assert _parse_portlist(b"\x40") == {2}

    def test_hex_with_0x_prefix_multibyte(self):
        # 0x80 00 10: byte0 bit0 = port1; byte2 0x10=0001 0000 → bit3 → port (2*8+4)=20.
        assert _parse_portlist("0x800010") == {1, 20}

    def test_hex_string_spaces_no_prefix(self):
        # "80 00 10" dạng net-snmp Hex-STRING.
        assert _parse_portlist("80 00 10") == {1, 20}

    def test_hex_prefix_with_spaces(self):
        assert _parse_portlist("0x80 00 10") == {1, 20}

    def test_all_bits_first_byte(self):
        # 0xFF → ports 1..8.
        assert _parse_portlist("0xFF") == {1, 2, 3, 4, 5, 6, 7, 8}

    def test_raw_latin1_octet_string(self):
        # Một byte 0x80 dưới dạng latin-1 str → port 1.
        assert _parse_portlist("\x80") == {1}


# ---------------------------------------------------------------------------
# _collect_port_modes — gom egress/untagged bitmap theo VLAN → mode/ifIndex
# ---------------------------------------------------------------------------

EGRESS   = "1.3.6.1.2.1.17.7.1.4.3.1.2"
UNTAGGED = "1.3.6.1.2.1.17.7.1.4.3.1.4"
BASEPORT = "1.3.6.1.2.1.17.1.4.1.2"

OID_PROFILE = {"vlan": {
    "dot1q_static_egress": EGRESS,
    "dot1q_static_untagged": UNTAGGED,
    "dot1d_baseport_ifindex": BASEPORT,
}}


def _walk(mocker, collector, mapping: dict[str, list[tuple[str, str]]]):
    def side_effect(prefix):
        return [(f"{prefix}.{s}", v) for s, v in mapping.get(prefix, [])]
    mocker.patch.object(collector, "_snmp_walk", side_effect=side_effect)


class TestCollectPortModes:
    @pytest.fixture
    def collector(self):
        return SwitchSNMPCollector(HuaweiSNMPDeviceFactory.build())

    def test_access_port_single_untagged_vlan(self, mocker, collector):
        # Port 1 (=ifIndex 10): untagged trong đúng VLAN 1 → access.
        _walk(mocker, collector, {
            BASEPORT: [("1", "10")],
            EGRESS:   [("1", "0x80")],   # VLAN 1: egress port 1
            UNTAGGED: [("1", "0x80")],   # VLAN 1: untagged port 1
        })
        assert collector._collect_port_modes(OID_PROFILE) == {10: "access"}

    def test_trunk_port_tagged_in_vlan(self, mocker, collector):
        # Port 1 (=ifIndex 10): egress ở VLAN 10 & 20 nhưng KHÔNG untagged → trunk.
        _walk(mocker, collector, {
            BASEPORT: [("1", "10")],
            EGRESS:   [("10", "0x80"), ("20", "0x80")],
            UNTAGGED: [("10", "0x00"), ("20", "0x00")],
        })
        assert collector._collect_port_modes(OID_PROFILE) == {10: "trunk"}

    def test_hybrid_untagged_in_two_vlans(self, mocker, collector):
        _walk(mocker, collector, {
            BASEPORT: [("1", "10")],
            EGRESS:   [("5", "0x80"), ("6", "0x80")],
            UNTAGGED: [("5", "0x80"), ("6", "0x80")],
        })
        assert collector._collect_port_modes(OID_PROFILE) == {10: "hybrid"}

    def test_port_not_member_skipped(self, mocker, collector):
        # Port 2 không xuất hiện trong bitmap → không có entry (fallback heuristic).
        _walk(mocker, collector, {
            BASEPORT: [("1", "10"), ("2", "11")],
            EGRESS:   [("1", "0x80")],
            UNTAGGED: [("1", "0x80")],
        })
        result = collector._collect_port_modes(OID_PROFILE)
        assert result == {10: "access"}
        assert 11 not in result

    def test_mixed_access_and_trunk_via_baseport_map(self, mocker, collector):
        # Port1→if10 access (VLAN1 untagged), Port2→if11 trunk (VLAN10 tagged).
        # 0xC0 = 1100 0000 → ports 1 & 2.
        _walk(mocker, collector, {
            BASEPORT: [("1", "10"), ("2", "11")],
            EGRESS:   [("1", "0x80"), ("10", "0x40")],
            UNTAGGED: [("1", "0x80"), ("10", "0x00")],
        })
        assert collector._collect_port_modes(OID_PROFILE) == {10: "access", 11: "trunk"}

    def test_returns_empty_when_oids_missing(self, collector):
        assert collector._collect_port_modes({"vlan": {}}) == {}

    def test_cisco_vtp_status_preferred(self, mocker, collector):
        # CISCO-VTP-MIB index = ifIndex trực tiếp; 1=trunk, 2=access.
        vtp = "1.3.6.1.4.1.9.9.46.1.6.1.1.14"
        _walk(mocker, collector, {vtp: [("10101", "2"), ("10110", "1")]})
        profile = {"vlan": {"vlan_trunk_status": vtp}}
        assert collector._collect_port_modes(profile) == {10101: "access", 10110: "trunk"}

    def test_falls_back_to_qbridge_when_vtp_empty(self, mocker, collector):
        # VTP rỗng (Huawei) → dùng Q-BRIDGE bitmap.
        vtp = "1.3.6.1.4.1.9.9.46.1.6.1.1.14"
        _walk(mocker, collector, {
            vtp: [],
            BASEPORT: [("1", "10")],
            EGRESS:   [("1", "0x80")],
            UNTAGGED: [("1", "0x80")],
        })
        profile = {"vlan": {
            "vlan_trunk_status": vtp,
            "dot1q_static_egress": EGRESS,
            "dot1q_static_untagged": UNTAGGED,
            "dot1d_baseport_ifindex": BASEPORT,
        }}
        assert collector._collect_port_modes(profile) == {10: "access"}

    def test_returns_empty_when_baseport_walk_empty(self, mocker, collector):
        _walk(mocker, collector, {EGRESS: [("1", "0x80")], UNTAGGED: [("1", "0x80")]})
        assert collector._collect_port_modes(OID_PROFILE) == {}
