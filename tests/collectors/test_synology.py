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


_MEMORY_OIDS = {
    "mem_total":  "1.3.6.1.4.1.2021.4.5.0",
    "mem_avail":  "1.3.6.1.4.1.2021.4.6.0",
    "mem_buffer": "1.3.6.1.4.1.2021.4.14.0",
    "mem_cached": "1.3.6.1.4.1.2021.4.15.0",
}

# Profile thật (khớp oids/synology_dsm.yaml) — có RAW counter, cpu_idle chỉ để fallback.
SYN_PROFILE = {
    "cpu": {
        "cpu_idle":       "1.3.6.1.4.1.2021.11.11.0",
        "cpu_raw_user":   "1.3.6.1.4.1.2021.11.50.0",
        "cpu_raw_nice":   "1.3.6.1.4.1.2021.11.51.0",
        "cpu_raw_system": "1.3.6.1.4.1.2021.11.52.0",
        "cpu_raw_idle":   "1.3.6.1.4.1.2021.11.53.0",
    },
    "memory": _MEMORY_OIDS,
}

# Profile cũ (trước fix) — không có RAW counter, dùng để test nhánh fallback.
SYN_PROFILE_NO_RAW = {
    "cpu": {"cpu_idle": "1.3.6.1.4.1.2021.11.11.0"},
    "memory": _MEMORY_OIDS,
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

    def test_empty_string_probe_not_synology(self, mocker):
        """Regression: backend trả "" cho OID không tồn tại → KHÔNG nhận nhầm Synology."""
        c = _collector()
        def fake_get(oid):
            if oid == OID_SYS_OBJECT_ID:
                return "1.3.6.1.4.1.9.1.1"
            if oid == OID_SYS_DESCR:
                return "Cisco IOS Software, C2960"
            return ""  # OID_SYNO_MODEL → chuỗi rỗng (không phải None!)
        mocker.patch.object(c, "_snmp_get", side_effect=fake_get)
        assert c.detect_os_family() == "cisco_ios"


class TestSynologyCpuMem:
    """⚠️ ssCpuIdle KHÔNG dùng trực tiếp nữa — verify runtime NAS-Pfvn 2026-07-11:
    DSM báo User(0)+System(1)+Idle(47)=48 (phải ≈100), 100-idle cho CPU giả 53-54%
    trong khi Resource Monitor thật ~1%. CPU nay tính từ delta RAW counter 2 lần poll.
    """

    def test_cpu_from_raw_delta_with_baseline(self, mocker):
        c = _collector()
        prev = {"cpu_raw_user": 1000.0, "cpu_raw_nice": 0.0, "cpu_raw_system": 500.0, "cpu_raw_idle": 8500.0}
        mocker.patch("apps.collectors.cpu_state.get_last_raw", return_value=prev)
        set_mock = mocker.patch("apps.collectors.cpu_state.set_last_raw")
        # now - prev: user+50, system+50, idle+400 → total delta 500, busy 100 → cpu 20%
        # mem: total=1000 avail=400 buffer=100 cached=200 → used=300 → 30%
        mocker.patch.object(c, "_snmp_get", side_effect=[
            "1050", "0", "550", "8900",   # raw user/nice/system/idle hiện tại
            "1000", "400", "100", "200",  # mem total/avail/buffer/cached
        ])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE)
        assert cpu == 20.0
        assert mem == 30.0
        set_mock.assert_called_once()

    def test_cpu_first_poll_no_baseline_returns_zero(self, mocker):
        """Chưa có mẫu trước (poll đầu / Redis hết TTL) → cpu=0.0, KHÔNG suy đoán bừa."""
        c = _collector()
        mocker.patch("apps.collectors.cpu_state.get_last_raw", return_value=None)
        set_mock = mocker.patch("apps.collectors.cpu_state.set_last_raw")
        mocker.patch.object(c, "_snmp_get", side_effect=[
            "1050", "0", "550", "8900",
            "1000", "400", "100", "200",
        ])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE)
        assert cpu == 0.0
        assert mem == 30.0
        set_mock.assert_called_once()  # vẫn lưu baseline cho poll kế tiếp

    def test_cpu_counter_reset_on_reboot_returns_zero(self, mocker):
        """Raw counter nhỏ hơn mẫu trước (NAS reboot, jiffies reset) → bỏ mẫu, cpu=0.0."""
        c = _collector()
        prev = {"cpu_raw_user": 5000.0, "cpu_raw_nice": 0.0, "cpu_raw_system": 3000.0, "cpu_raw_idle": 90000.0}
        mocker.patch("apps.collectors.cpu_state.get_last_raw", return_value=prev)
        mocker.patch("apps.collectors.cpu_state.set_last_raw")
        mocker.patch.object(c, "_snmp_get", side_effect=[
            "10", "0", "5", "50",         # counter vừa reset, nhỏ hơn prev nhiều
            "1000", "400", "100", "200",
        ])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE)
        assert cpu == 0.0

    def test_fallback_to_idle_formula_when_profile_missing_raw_oids(self, mocker):
        """Profile cũ (chưa có OID raw) → giữ tương thích ngược bằng 100-idle."""
        c = _collector()
        mocker.patch.object(c, "_snmp_get", side_effect=["95", "1000", "400", "100", "200"])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE_NO_RAW)
        assert cpu == 5.0
        assert mem == 30.0

    def test_fallback_to_idle_formula_when_raw_snmp_incomplete(self, mocker):
        """Đủ OID raw trong profile nhưng SNMP trả thiếu (None giữa chừng) → fallback idle."""
        c = _collector()
        mocker.patch.object(c, "_snmp_get", side_effect=[
            "1050", "0", "550", None,     # raw idle GET fail → bỏ nhánh raw
            "95",                          # fallback: cpu_idle
            "1000", "400", "100", "200",   # mem
        ])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE)
        assert cpu == 5.0
        assert mem == 30.0

    def test_fallback_when_no_buffer_cached(self, mocker):
        c = _collector()
        # buffer+cached lớn bất thường → used âm → fallback total-avail = 600 → 60%
        mocker.patch.object(c, "_snmp_get", side_effect=["95", "1000", "400", "900", "900"])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE_NO_RAW)
        assert mem == 60.0

    def test_handles_missing_values(self, mocker):
        c = _collector()
        mocker.patch.object(c, "_snmp_get", side_effect=[None, None, None, None, None])
        cpu, mem = c._collect_cpu_mem_synology(SYN_PROFILE_NO_RAW)
        assert cpu == 0.0 and mem == 0.0

    def test_handles_missing_values_with_raw_profile(self, mocker):
        """Profile có OID raw nhưng SNMP trả None ngay từ get đầu tiên → fallback idle → cpu=0.

        Call order: 1 GET raw (fail→break) + 1 GET cpu_idle fallback (fail) + 4 GET mem (fail) = 6.
        """
        c = _collector()
        mocker.patch.object(c, "_snmp_get", side_effect=[None, None, None, None, None, None])
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
