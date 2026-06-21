"""Tests cho hỗ trợ Synology NAS qua SNMP (detect + CPU/mem + validity)."""
from apps.collectors.switch_snmp import SwitchSNMPCollector
from apps.collectors.base import NormalizedData, InterfaceData
from apps.collectors.tasks import _has_valid_data
from apps.devices.models import Device
from tests.conftest import CiscoSNMPDeviceFactory


def _collector():
    return SwitchSNMPCollector(CiscoSNMPDeviceFactory.build())


SYN_PROFILE = {
    "cpu": {"cpu_idle": "1.3.6.1.4.1.2021.11.11.0"},
    "memory": {
        "mem_total": "1.3.6.1.4.1.2021.4.5.0",
        "mem_avail": "1.3.6.1.4.1.2021.4.6.0",
    },
}


class TestDetectSynology:
    def test_detect_by_sys_oid(self, mocker):
        c = _collector()
        mocker.patch.object(c, "_snmp_get", side_effect=[
            "1.3.6.1.4.1.6574.1",       # sysObjectID chứa "6574"
            "Linux DiskStation 4.4",     # sysDescr
        ])
        assert c.detect_os_family() == "synology_dsm"

    def test_detect_by_descr(self, mocker):
        c = _collector()
        mocker.patch.object(c, "_snmp_get", side_effect=[
            "1.3.6.1.4.1.8072.3.2.10",   # OID generic (net-snmp), không có 6574
            "Synology DSM DS920+",        # descr chứa "synology"
        ])
        assert c.detect_os_family() == "synology_dsm"


class TestSynologyCpuMem:
    def test_cpu_from_idle_and_mem_from_total_avail(self, mocker):
        c = _collector()
        # ssCpuIdle=95 → cpu=5; total=1000, avail=400 → mem=60%
        mocker.patch.object(c, "_snmp_get", side_effect=["95", "1000", "400"])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE)
        assert cpu == 5.0
        assert mem == 60.0

    def test_handles_missing_values(self, mocker):
        c = _collector()
        mocker.patch.object(c, "_snmp_get", side_effect=[None, None, None])
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
