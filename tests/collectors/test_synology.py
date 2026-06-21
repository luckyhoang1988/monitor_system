"""Tests cho hỗ trợ Synology NAS qua SNMP (detect + CPU/mem + validity)."""
from apps.collectors.switch_snmp import (
    SwitchSNMPCollector, OID_SYS_OBJECT_ID, OID_SYS_DESCR,
)
from apps.collectors.base import NormalizedData, InterfaceData
from apps.collectors.tasks import _has_valid_data
from apps.devices.models import Device
from tests.conftest import CiscoSNMPDeviceFactory


def _collector(**overrides):
    return SwitchSNMPCollector(CiscoSNMPDeviceFactory.build(**overrides))


SYN_PROFILE = {
    "cpu": {"cpu_idle": "1.3.6.1.4.1.2021.11.11.0"},
    "memory": {
        "mem_total":  "1.3.6.1.4.1.2021.4.5.0",
        "mem_avail":  "1.3.6.1.4.1.2021.4.6.0",
        "mem_buffer": "1.3.6.1.4.1.2021.4.14.0",
        "mem_cached": "1.3.6.1.4.1.2021.4.15.0",
    },
}


class TestDetectSynology:
    def test_detect_by_vendor(self, mocker):
        """Vendor=synology → synology_dsm dù SNMP báo net-snmp (8072 / Linux ...)."""
        c = _collector(vendor="synology", os_family="synology_dsm")
        # Không cần SNMP — vendor quyết định; nếu có gọi cũng trả net-snmp.
        mocker.patch.object(c, "_snmp_get", return_value="1.3.6.1.4.1.8072.3.2.10")
        assert c.detect_os_family() == "synology_dsm"

    def test_detect_by_model_probe_when_vendor_unset(self, mocker):
        """Auto-discovery (vendor=cisco mặc định): probe OID model 6574 → synology_dsm."""
        c = _collector()  # vendor=cisco
        def fake_get(oid):
            if oid == "1.3.6.1.4.1.6574.1.5.1.0":
                return "DS920+"        # probe Synology trả model
            if oid == OID_SYS_OBJECT_ID:
                return "1.3.6.1.4.1.8072.3.2.10"
            if oid == OID_SYS_DESCR:
                return "Linux DiskStation 4.4 x86_64"
            return None
        mocker.patch.object(c, "_snmp_get", side_effect=fake_get)
        assert c.detect_os_family() == "synology_dsm"

    def test_cisco_not_misdetected_as_synology(self, mocker):
        """Cisco IOS thật: probe 6574 trả None → vẫn cisco_ios."""
        c = _collector()
        def fake_get(oid):
            if oid == OID_SYS_OBJECT_ID:
                return "1.3.6.1.4.1.9.1.1"
            if oid == OID_SYS_DESCR:
                return "Cisco IOS Software, C2960"
            return None  # OID_SYNO_MODEL → None
        mocker.patch.object(c, "_snmp_get", side_effect=fake_get)
        assert c.detect_os_family() == "cisco_ios"


class TestSynologyCpuMem:
    def test_cpu_idle_and_mem_excludes_cache(self, mocker):
        c = _collector()
        # idle=95 → cpu=5; total=1000 avail=400 buffer=100 cached=200
        # used = 1000-400-100-200 = 300 → mem=30%
        mocker.patch.object(c, "_snmp_get", side_effect=["95", "1000", "400", "100", "200"])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE)
        assert cpu == 5.0
        assert mem == 30.0

    def test_fallback_when_no_buffer_cached(self, mocker):
        c = _collector()
        # buffer+cached lớn bất thường → used âm → fallback total-avail = 600 → 60%
        mocker.patch.object(c, "_snmp_get", side_effect=["95", "1000", "400", "900", "900"])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE)
        assert mem == 60.0

    def test_handles_missing_values(self, mocker):
        c = _collector()
        mocker.patch.object(c, "_snmp_get", side_effect=[None, None, None, None, None])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE)
        assert cpu == 0.0 and mem == 0.0


class TestNasValidity:
    def _nd(self, ifaces=0, mem=0.0):
        return NormalizedData(
            device_name="nas", ip_address="10.0.0.9", timestamp=None,
            os_family="synology_dsm", cpu_percent=0.0, mem_percent=mem,
            interfaces=[InterfaceData(name=f"eth{i}", if_index=i, status="up",
                                      in_bytes=0, out_bytes=0) for i in range(ifaces)],
        )

    def test_valid_when_has_interfaces(self):
        dev = Device(device_type="nas")
        assert _has_valid_data(dev, self._nd(ifaces=1, mem=0.0)) is True

    def test_valid_when_mem_present(self):
        dev = Device(device_type="nas")
        assert _has_valid_data(dev, self._nd(ifaces=0, mem=42.0)) is True

    def test_invalid_when_empty(self):
        dev = Device(device_type="nas")
        assert _has_valid_data(dev, self._nd(ifaces=0, mem=0.0)) is False
