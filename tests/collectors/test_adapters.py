"""Tests cho các adapter — pure Python, không cần DB hay mock."""
from datetime import timezone
import pytest
from apps.collectors.adapters import (
    get_adapter,
    CiscoIOSAdapter, CiscoIOSXEAdapter, HuaweiVRPAdapter,
)
from apps.collectors.base import InterfaceData


SAMPLE_RAW = {
    "cpu_percent": 45,
    "mem_percent": 60,
    "uptime_secs": 86400,
    "interfaces": [],
}

SAMPLE_IFACE = InterfaceData(
    name="Gi0/1", if_index=1, status="up",
    in_bytes=1000, out_bytes=2000,
)


class TestCiscoIOSAdapter:
    def test_os_family(self):
        data = CiscoIOSAdapter().normalize(SAMPLE_RAW, "sw-01", "10.0.0.1")
        assert data.os_family == "cisco_ios"

    def test_cpu_float_cast(self):
        raw = {**SAMPLE_RAW, "cpu_percent": "55"}
        data = CiscoIOSAdapter().normalize(raw, "sw-01", "10.0.0.1")
        assert data.cpu_percent == 55.0
        assert isinstance(data.cpu_percent, float)

    def test_mem_float_cast(self):
        raw = {**SAMPLE_RAW, "mem_percent": "70"}
        data = CiscoIOSAdapter().normalize(raw, "sw-01", "10.0.0.1")
        assert data.mem_percent == 70.0

    def test_uptime_int_cast(self):
        raw = {**SAMPLE_RAW, "uptime_secs": "3600"}
        data = CiscoIOSAdapter().normalize(raw, "sw-01", "10.0.0.1")
        assert data.uptime_secs == 3600
        assert isinstance(data.uptime_secs, int)

    def test_interfaces_passthrough(self):
        raw = {**SAMPLE_RAW, "interfaces": [SAMPLE_IFACE]}
        data = CiscoIOSAdapter().normalize(raw, "sw-01", "10.0.0.1")
        assert len(data.interfaces) == 1
        assert data.interfaces[0].name == "Gi0/1"

    def test_missing_keys_default_zero(self):
        data = CiscoIOSAdapter().normalize({}, "sw-01", "10.0.0.1")
        assert data.cpu_percent == 0.0
        assert data.mem_percent == 0.0
        assert data.uptime_secs == 0
        assert data.interfaces == []

    def test_device_name_and_ip(self):
        data = CiscoIOSAdapter().normalize(SAMPLE_RAW, "sw-core", "192.168.1.1")
        assert data.device_name == "sw-core"
        assert data.ip_address == "192.168.1.1"

    def test_timestamp_is_utc(self):
        data = CiscoIOSAdapter().normalize(SAMPLE_RAW, "sw-01", "10.0.0.1")
        assert data.timestamp.tzinfo is not None
        assert data.timestamp.tzinfo == timezone.utc


class TestCiscoIOSXEAdapter:
    def test_os_family(self):
        data = CiscoIOSXEAdapter().normalize(SAMPLE_RAW, "sw-01", "10.0.0.1")
        assert data.os_family == "cisco_iosxe"

    def test_missing_keys_default_zero(self):
        data = CiscoIOSXEAdapter().normalize({}, "sw-01", "10.0.0.1")
        assert data.cpu_percent == 0.0
        assert data.mem_percent == 0.0

    def test_timestamp_is_utc(self):
        data = CiscoIOSXEAdapter().normalize(SAMPLE_RAW, "sw-01", "10.0.0.1")
        assert data.timestamp.tzinfo == timezone.utc


class TestHuaweiVRPAdapter:
    def test_os_family(self):
        data = HuaweiVRPAdapter().normalize(SAMPLE_RAW, "huawei-01", "10.0.0.2")
        assert data.os_family == "huawei_vrp"

    def test_mem_percent_direct(self):
        # Huawei trả về % trực tiếp, không cần tính
        raw = {**SAMPLE_RAW, "mem_percent": 45}
        data = HuaweiVRPAdapter().normalize(raw, "huawei-01", "10.0.0.2")
        assert data.mem_percent == 45.0

    def test_missing_keys_default_zero(self):
        data = HuaweiVRPAdapter().normalize({}, "huawei-01", "10.0.0.2")
        assert data.cpu_percent == 0.0
        assert data.mem_percent == 0.0


class TestGetAdapter:
    def test_get_cisco_ios(self):
        adapter = get_adapter("cisco_ios")
        assert isinstance(adapter, CiscoIOSAdapter)

    def test_get_cisco_iosxe(self):
        adapter = get_adapter("cisco_iosxe")
        assert isinstance(adapter, CiscoIOSXEAdapter)

    def test_get_huawei_vrp(self):
        adapter = get_adapter("huawei_vrp")
        assert isinstance(adapter, HuaweiVRPAdapter)

    def test_unknown_os_raises_value_error(self):
        with pytest.raises(ValueError, match="Không có adapter"):
            get_adapter("unknown_os")
