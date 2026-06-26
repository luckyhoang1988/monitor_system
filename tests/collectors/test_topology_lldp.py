"""Tests cho topology_lldp — parse MAC, phân loại AP, gom LLDP index."""
import pytest

from apps.collectors.topology_lldp import (
    DEFAULT_AP_NAME_PATTERN,
    NeighborRecord,
    _group_rem_rows,
    _parse_rem_index,
    is_ap_neighbor,
    normalize_mac,
)


class TestNormalizeMac:
    def test_colon_format(self):
        assert normalize_mac("0C:84:08:59:80:C0") == "0c:84:08:59:80:c0"

    def test_hex_no_separator(self):
        assert normalize_mac("0c84085980c0") == "0c:84:08:59:80:c0"

    def test_empty(self):
        assert normalize_mac("") == ""
        assert normalize_mac(None) == ""


class TestParseRemIndex:
    def test_valid(self):
        assert _parse_rem_index("0.12.3") == (0, 12)

    def test_invalid(self):
        assert _parse_rem_index("12") is None
        assert _parse_rem_index("a.b.c") is None


class TestGroupRemRows:
    def test_keeps_newest_time_mark(self):
        columns = {
            "sys_name": {
                "0.5.1": "AP-OLD",
                "1.5.1": "AP-NEW",
            },
            "chassis_id": {
                "0.5.1": "aa",
                "1.5.1": "bb",
            },
        }
        grouped = _group_rem_rows(columns)
        assert grouped[5]["sys_name"] == "AP-NEW"
        assert grouped[5]["chassis_id"] == "bb"


class TestIsApNeighbor:
    def test_mac_chassis(self):
        assert is_ap_neighbor("", "0c:84:08:59:80:c0") is True

    def test_ap_name_pattern(self):
        assert is_ap_neighbor("AP-XUONG1_MAY/IE", "") is True
        assert is_ap_neighbor("BPVN-XUONG4-AP01", "") is True

    def test_not_ap(self):
        assert is_ap_neighbor("CORE-SW", "") is False
        assert is_ap_neighbor("", "") is False

    def test_custom_pattern(self):
        import re
        pat = re.compile(r"^MYAP", re.I)
        assert is_ap_neighbor("MYAP-01", "", pat) is True


class TestNeighborRecord:
    def test_dataclass_defaults(self):
        n = NeighborRecord(local_port="Gi0/0/1", local_port_num=1)
        assert n.protocol == "lldp"
        assert n.is_ap_candidate is False
