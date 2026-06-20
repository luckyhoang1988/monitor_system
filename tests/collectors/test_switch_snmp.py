"""Tests cho SwitchSNMPCollector — mock _snmp_get/_snmp_walk để tránh easysnmp."""
import pytest
from apps.collectors.switch_snmp import SwitchSNMPCollector, OID_SYS_OBJECT_ID, OID_SYS_DESCR
from tests.conftest import CiscoSNMPDeviceFactory, HuaweiSNMPDeviceFactory


# ---------------------------------------------------------------------------
# detect_os_family
# ---------------------------------------------------------------------------

class TestDetectOsFamily:
    @pytest.fixture
    def cisco_collector(self):
        return SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())

    @pytest.fixture
    def huawei_collector(self):
        return SwitchSNMPCollector(HuaweiSNMPDeviceFactory.build())

    def test_detects_huawei_by_sys_oid(self, mocker, cisco_collector):
        mocker.patch.object(cisco_collector, "_snmp_get", side_effect=[
            "1.3.6.1.4.1.2011.1.1",  # OID_SYS_OBJECT_ID — chứa "2011"
            "S5700",                  # OID_SYS_DESCR
        ])
        assert cisco_collector.detect_os_family() == "huawei_vrp"

    def test_detects_huawei_by_vrp_descr(self, mocker, cisco_collector):
        mocker.patch.object(cisco_collector, "_snmp_get", side_effect=[
            "1.3.6.1.4.1.9.1.1",   # OID không chứa "2011"
            "Huawei VRP Software",  # descr chứa "VRP"
        ])
        assert cisco_collector.detect_os_family() == "huawei_vrp"

    def test_detects_cisco_iosxe(self, mocker, cisco_collector):
        mocker.patch.object(cisco_collector, "_snmp_get", side_effect=[
            "1.3.6.1.4.1.9.1.2227",        # Cisco OID
            "Cisco IOS XE Software, 16.12",  # descr chứa "IOS XE"
        ])
        assert cisco_collector.detect_os_family() == "cisco_iosxe"

    def test_detects_cisco_business_c1200(self, mocker, cisco_collector):
        mocker.patch.object(cisco_collector, "_snmp_get", side_effect=[
            "1.3.6.1.4.1.9.1.3212",
            "Catalyst 1200 Series Smart Switch, 8-port GE",
        ])
        assert cisco_collector.detect_os_family() == "cisco_business"

    def test_detects_cisco_iosxe_hyphen(self, mocker, cisco_collector):
        mocker.patch.object(cisco_collector, "_snmp_get", side_effect=[
            "1.3.6.1.4.1.9.1.2227",
            "Cisco IOS-XE Software",  # descr chứa "IOS-XE"
        ])
        assert cisco_collector.detect_os_family() == "cisco_iosxe"

    def test_detects_cisco_ios_default(self, mocker, cisco_collector):
        mocker.patch.object(cisco_collector, "_snmp_get", side_effect=[
            "1.3.6.1.4.1.9.1.1",
            "Cisco IOS Software, Version 15.2",
        ])
        assert cisco_collector.detect_os_family() == "cisco_ios"

    def test_returns_none_snmp_get_returns_none(self, mocker, cisco_collector):
        mocker.patch.object(cisco_collector, "_snmp_get", return_value=None)
        # Both OIDs return None → empty strings → default cisco_ios
        result = cisco_collector.detect_os_family()
        assert result == "cisco_ios"


# ---------------------------------------------------------------------------
# _collect_interfaces
# ---------------------------------------------------------------------------

# Contants mô phỏng kết quả SNMP walk cho 2 interface
IF_IDX_1  = "1.3.6.1.2.1.2.2.1.1.1"
IF_DESCR_1 = "1.3.6.1.2.1.2.2.1.2.1"
IF_DESCR_2 = "1.3.6.1.2.1.2.2.1.2.2"

def _make_walk_results(oid_prefix: str, entries: list[tuple[str, str]]):
    """Tạo (oid_suffix, value) pairs như easysnmp trả về."""
    return [(f"{oid_prefix}.{suffix}", value) for suffix, value in entries]


class TestCollectInterfaces:
    @pytest.fixture
    def collector(self):
        return SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())

    def _mock_walk(self, mocker, collector, data: dict[str, list]):
        """Mock _snmp_walk với dict {oid_prefix: [(suffix, val)...]}."""
        from apps.collectors.switch_snmp import (
            OID_IF_DESCR, OID_IF_OPER, OID_IF_ALIAS,
            OID_HC_IN, OID_HC_OUT, OID_IF_IN_ERR, OID_IF_OUT_ERR, OID_IF_SPEED,
        )
        def side_effect(prefix):
            raw = data.get(prefix, [])
            return [(f"{prefix}.{s}", v) for s, v in raw]
        mocker.patch.object(collector, "_snmp_walk", side_effect=side_effect)

    def test_returns_correct_count(self, mocker, collector):
        self._mock_walk(mocker, collector, {
            "1.3.6.1.2.1.2.2.1.2": [("1", "Gi0/1"), ("2", "Gi0/2")],
            "1.3.6.1.2.1.2.2.1.8": [("1", "1"), ("2", "2")],
            "1.3.6.1.2.1.31.1.1.1.18": [],
            "1.3.6.1.2.1.31.1.1.1.6":  [],
            "1.3.6.1.2.1.31.1.1.1.10": [],
            "1.3.6.1.2.1.2.2.1.14": [],
            "1.3.6.1.2.1.2.2.1.20": [],
            "1.3.6.1.2.1.31.1.1.1.15": [],
        })
        ifaces = collector._collect_interfaces()
        assert len(ifaces) == 2

    def test_interface_status_up(self, mocker, collector):
        self._mock_walk(mocker, collector, {
            "1.3.6.1.2.1.2.2.1.2": [("1", "Gi0/1")],
            "1.3.6.1.2.1.2.2.1.8": [("1", "1")],  # 1 = up
            "1.3.6.1.2.1.31.1.1.1.18": [], "1.3.6.1.2.1.31.1.1.1.6": [],
            "1.3.6.1.2.1.31.1.1.1.10": [], "1.3.6.1.2.1.2.2.1.14": [],
            "1.3.6.1.2.1.2.2.1.20": [], "1.3.6.1.2.1.31.1.1.1.15": [],
        })
        ifaces = collector._collect_interfaces()
        assert ifaces[0].status == "up"

    def test_interface_status_down(self, mocker, collector):
        self._mock_walk(mocker, collector, {
            "1.3.6.1.2.1.2.2.1.2": [("1", "Gi0/2")],
            "1.3.6.1.2.1.2.2.1.8": [("1", "2")],  # 2 = down
            "1.3.6.1.2.1.31.1.1.1.18": [], "1.3.6.1.2.1.31.1.1.1.6": [],
            "1.3.6.1.2.1.31.1.1.1.10": [], "1.3.6.1.2.1.2.2.1.14": [],
            "1.3.6.1.2.1.2.2.1.20": [], "1.3.6.1.2.1.31.1.1.1.15": [],
        })
        ifaces = collector._collect_interfaces()
        assert ifaces[0].status == "down"

    def test_interface_bytes(self, mocker, collector):
        self._mock_walk(mocker, collector, {
            "1.3.6.1.2.1.2.2.1.2": [("1", "Gi0/1")],
            "1.3.6.1.2.1.2.2.1.8": [("1", "1")],
            "1.3.6.1.2.1.31.1.1.1.18": [],
            "1.3.6.1.2.1.31.1.1.1.6":  [("1", "1234567890")],
            "1.3.6.1.2.1.31.1.1.1.10": [("1", "987654321")],
            "1.3.6.1.2.1.2.2.1.14": [], "1.3.6.1.2.1.2.2.1.20": [],
            "1.3.6.1.2.1.31.1.1.1.15": [],
        })
        ifaces = collector._collect_interfaces()
        assert ifaces[0].in_bytes == 1234567890
        assert ifaces[0].out_bytes == 987654321

    def test_interface_errors(self, mocker, collector):
        self._mock_walk(mocker, collector, {
            "1.3.6.1.2.1.2.2.1.2": [("1", "Gi0/1")],
            "1.3.6.1.2.1.2.2.1.8": [("1", "1")],
            "1.3.6.1.2.1.31.1.1.1.18": [], "1.3.6.1.2.1.31.1.1.1.6": [],
            "1.3.6.1.2.1.31.1.1.1.10": [],
            "1.3.6.1.2.1.2.2.1.14": [("1", "5")],
            "1.3.6.1.2.1.2.2.1.20": [("1", "2")],
            "1.3.6.1.2.1.31.1.1.1.15": [],
        })
        ifaces = collector._collect_interfaces()
        assert ifaces[0].in_errors == 5
        assert ifaces[0].out_errors == 2

    def test_interface_speed_mbps(self, mocker, collector):
        self._mock_walk(mocker, collector, {
            "1.3.6.1.2.1.2.2.1.2": [("1", "Gi0/1")],
            "1.3.6.1.2.1.2.2.1.8": [("1", "1")],
            "1.3.6.1.2.1.31.1.1.1.18": [], "1.3.6.1.2.1.31.1.1.1.6": [],
            "1.3.6.1.2.1.31.1.1.1.10": [], "1.3.6.1.2.1.2.2.1.14": [],
            "1.3.6.1.2.1.2.2.1.20": [],
            "1.3.6.1.2.1.31.1.1.1.15": [("1", "1000")],  # ifHighSpeed Mbps
        })
        ifaces = collector._collect_interfaces()
        assert ifaces[0].speed_mbps == 1000.0

    def test_snmp_walk_failure_returns_empty(self, mocker, collector):
        mocker.patch.object(collector, "_snmp_walk", return_value=[])
        ifaces = collector._collect_interfaces()
        assert ifaces == []


# ---------------------------------------------------------------------------
# _snmp_get failure
# ---------------------------------------------------------------------------

class TestSnmpGetFailure:
    # Session được import bên trong function nên patch tại easysnmp module.
    # Dùng pytest.importorskip để skip nếu easysnmp chưa cài.

    def test_returns_none_on_exception(self, mocker):
        pytest.importorskip("easysnmp")
        collector = SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())
        mocker.patch("easysnmp.Session", side_effect=Exception("connection refused"))
        result = collector._snmp_get("1.3.6.1.2.1.1.1.0")
        assert result is None

    def test_walk_returns_empty_list_on_exception(self, mocker):
        pytest.importorskip("easysnmp")
        collector = SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())
        mocker.patch("easysnmp.Session", side_effect=Exception("timeout"))
        result = collector._snmp_walk("1.3.6.1.2.1.2.2.1.2")
        assert result == []


class TestSnmpV3SessionArgs:
    def test_builds_v3_auth_priv_kwargs(self):
        device = CiscoSNMPDeviceFactory.build(
            snmp_version="v3",
            snmp_community="",
            snmpv3_username="snmpv3user",
            snmpv3_auth_protocol="sha",
            snmpv3_auth_password="authpass",
            snmpv3_priv_protocol="aes",
            snmpv3_priv_password="privpass",
        )
        collector = SwitchSNMPCollector(device)
        assert collector._snmp_kwargs["version"] == 3
        assert collector._snmp_kwargs["security_username"] == "snmpv3user"
        assert collector._snmp_kwargs["security_level"] == "auth_with_privacy"
        assert collector._snmp_kwargs["auth_protocol"] == "sha"
        assert collector._snmp_kwargs["privacy_protocol"] == "aes"

    def test_v3_requires_username(self):
        device = CiscoSNMPDeviceFactory.build(
            snmp_version="v3",
            snmp_community="",
            snmpv3_username="",
        )
        with pytest.raises(ValueError, match="username"):
            SwitchSNMPCollector(device)

    def test_v3_legacy_device_without_v3_fields_raises_value_error_instead_of_attribute_error(self):
        class LegacyDevice:
            ip_address = "10.0.0.1"
            snmp_version = "v3"
            snmp_community = ""
            name = "legacy-switch"

        device = LegacyDevice()
        with pytest.raises(ValueError, match="username"):
            SwitchSNMPCollector(device)


# ---------------------------------------------------------------------------
# adapt
# ---------------------------------------------------------------------------

class TestAdapt:
    def test_builds_normalized_data(self):
        from apps.collectors.base import NormalizedData
        collector = SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())
        raw = {
            "os_family": "cisco_ios",
            "cpu_percent": 20.0,
            "mem_percent": 40.0,
            "uptime_secs": 3600,
            "interfaces": [],
        }
        result = collector.adapt(raw)
        assert isinstance(result, NormalizedData)
        assert result.os_family == "cisco_ios"
        assert result.cpu_percent == 20.0
        assert result.uptime_secs == 3600

    def test_timestamp_is_utc(self):
        from datetime import timezone
        collector = SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())
        raw = {"os_family": "cisco_ios", "cpu_percent": 0.0,
               "mem_percent": 0.0, "uptime_secs": 0, "interfaces": []}
        result = collector.adapt(raw)
        assert result.timestamp.tzinfo == timezone.utc


class TestCollectRawResilience:
    def test_collect_raw_cisco_missing_memory_oids_does_not_raise(self, mocker):
        collector = SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())
        mocker.patch.object(collector, "detect_os_family", return_value="cisco_ios")
        mocker.patch("apps.collectors.switch_snmp._load_oid_profile", return_value={
            "cpu": {"cpu_5min": "1.2.3"},
            "memory": {},
        })
        mocker.patch.object(collector, "_snmp_get", return_value=0)
        mocker.patch.object(collector, "_collect_interfaces", return_value=[])

        raw = collector.collect_raw()
        assert raw["mem_percent"] == 0.0

    def test_collect_raw_mikrotik_missing_memory_oids_does_not_raise(self, mocker):
        collector = SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())
        mocker.patch.object(collector, "detect_os_family", return_value="mikrotik_routeros")
        mocker.patch("apps.collectors.switch_snmp._load_oid_profile", return_value={
            "cpu": {"processor_table": "1.2.3"},
            "memory": {},
        })
        mocker.patch.object(collector, "_snmp_walk", return_value=[("1.2.3.1", "10")])
        mocker.patch.object(collector, "_snmp_get", return_value=0)
        mocker.patch.object(collector, "_collect_interfaces", return_value=[])

        raw = collector.collect_raw()
        assert raw["mem_percent"] == 0.0

    def test_collect_cpu_mem_huawei_walks_entity_table_when_scalar_empty(self, mocker):
        collector = SwitchSNMPCollector(HuaweiSNMPDeviceFactory.build())
        mocker.patch.object(collector, "_snmp_get", return_value="")
        mocker.patch.object(collector, "_snmp_walk", side_effect=[
            [
                ("1.3.6.1.4.1.2011.5.25.31.1.1.1.1.6.67108867", "0"),
                ("1.3.6.1.4.1.2011.5.25.31.1.1.1.1.6.67108873", "95"),
            ],
            [
                ("1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5.67108867", "0"),
                ("1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5.67108873", "6"),
            ],
        ])

        cpu_val, mem_val = collector._collect_cpu_mem_huawei({
            "cpu": {
                "cpu_usage": "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.6.0",
                "cpu_table": "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.6",
            },
            "memory": {
                "mem_usage": "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5.0",
                "mem_table": "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5",
            },
        })

        assert cpu_val == 95.0
        assert mem_val == 6.0

    def test_collect_cpu_mem_cisco_business(self, mocker):
        collector = SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())
        mocker.patch.object(collector, "_snmp_get", return_value="12")
        cpu_val, mem_val = collector._collect_cpu_mem_cisco_business({
            "cpu": {"cpu_5min": "1.3.6.1.4.1.9.6.1.101.1.9.0"},
        })
        assert cpu_val == 12.0
        assert mem_val == 0.0
