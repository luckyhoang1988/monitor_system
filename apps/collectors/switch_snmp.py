"""SNMP Collector cho Switch — tự động detect vendor/OS-family."""
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .base import BaseCollector, NormalizedData, InterfaceData
from .snmp_client import (
    create_snmp_session,
    resolve_snmp_backend,
    snmp_get_value,
    snmp_walk_pairs,
)

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

OID_DIR = Path(__file__).resolve().parent.parent.parent / "oids"

# OIDs dùng để auto-detect
OID_SYS_DESCR    = "1.3.6.1.2.1.1.1.0"
OID_SYS_OBJECT_ID= "1.3.6.1.2.1.1.2.0"
OID_SYS_UPTIME   = "1.3.6.1.2.1.1.3.0"
OID_SYNO_MODEL   = "1.3.6.1.4.1.6574.1.5.1.0"  # SYNOLOGY-SYSTEM-MIB modelName (probe Synology)

# Interface table OIDs (MIB-II standard)
OID_IF_INDEX   = "1.3.6.1.2.1.2.2.1.1"
OID_IF_DESCR   = "1.3.6.1.2.1.2.2.1.2"
OID_IF_OPER    = "1.3.6.1.2.1.2.2.1.8"
OID_IF_ALIAS   = "1.3.6.1.2.1.31.1.1.1.18"
OID_HC_IN      = "1.3.6.1.2.1.31.1.1.1.6"   # 64-bit in octets
OID_HC_OUT     = "1.3.6.1.2.1.31.1.1.1.10"  # 64-bit out octets
OID_IF_IN_ERR  = "1.3.6.1.2.1.2.2.1.14"
OID_IF_OUT_ERR = "1.3.6.1.2.1.2.2.1.20"
OID_IF_SPEED   = "1.3.6.1.2.1.31.1.1.1.15"  # ifHighSpeed (Mbps)

IF_STATUS_MAP = {1: "up", 2: "down", 3: "testing", 4: "unknown",
                 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}

_CISCO_BUSINESS_MARKERS = (
    "catalyst 1200",
    "catalyst 1300",
    "cbs250",
    "cbs350",
    "cbs220",
    "cbs150",
)


def _is_cisco_business(sys_desc: str) -> bool:
    lowered = sys_desc.lower()
    return any(marker in lowered for marker in _CISCO_BUSINESS_MARKERS)


def _parse_portlist(raw: object) -> set[int]:
    """Giải mã Q-BRIDGE PortList (OCTET STRING bitmap) → tập dot1dBasePort.

    Bit MSB của byte 0 (0x80) = port 1, bit kế (0x40) = port 2, … (RFC 4363).
    Giá trị từ SNMP backend có nhiều dạng:
      - bytes (một số easysnmp build)
      - hex string "0x800010" hoặc "80 00 10" / "0x80 00 10" (pysnmp prettyPrint, net-snmp Hex-STRING)
      - raw latin-1 string (easysnmp octet string có thể chứa byte 0..255)
    ⚠️ easysnmp có thể cắt chuỗi tại null byte → port ở octet sau vị trí null không thấy.
    Tool verify_vlan_oids in ra để đối chiếu thực tế trên fleet.
    """
    if raw is None:
        return set()

    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    else:
        s = str(raw).strip()
        if not s:
            return set()
        hexpart = s[2:] if s[:2].lower() == "0x" else s
        compact = hexpart.replace(" ", "")
        is_hex = bool(compact) and len(compact) % 2 == 0 and all(
            c in "0123456789abcdefABCDEF" for c in compact
        )
        if s[:2].lower() == "0x" or (is_hex and " " in s):
            # Có tiền tố 0x, hoặc trông như "80 00 10" (Hex-STRING) → giải mã hex.
            try:
                data = bytes.fromhex(compact)
            except ValueError:
                return set()
        elif is_hex and len(compact) >= 2:
            # Chuỗi hex liền không khoảng trắng (vd "800010").
            try:
                data = bytes.fromhex(compact)
            except ValueError:
                data = s.encode("latin-1", "ignore")
        else:
            # Octet string thô — mỗi ký tự = 1 byte.
            data = s.encode("latin-1", "ignore")

    ports: set[int] = set()
    for byte_idx, byte in enumerate(data):
        for bit in range(8):
            if byte & (0x80 >> bit):
                ports.add(byte_idx * 8 + bit + 1)  # base-1 dot1dBasePort
    return ports


def _load_oid_profile(os_family: str) -> dict:
    path = OID_DIR / f"{os_family}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"OID profile không tồn tại: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class SwitchSNMPCollector(BaseCollector):
    def __init__(self, device: "Device") -> None:
        super().__init__(device)
        self._snmp_kwargs = self._build_snmp_kwargs()
        self._snmp_backend = resolve_snmp_backend()
        self.__session = None

    def _build_snmp_kwargs(self) -> dict:
        from django.conf import settings

        version_map = {"v1": 1, "v2c": 2, "v3": 3}
        effective_version = version_map.get(self.device.snmp_version)
        if effective_version is None:
            raise ValueError(f"SNMP version '{self.device.snmp_version}' không hợp lệ.")

        kwargs = {
            "hostname": self.device.ip_address,
            "version": effective_version,
            "timeout": int(getattr(settings, "SNMP_TIMEOUT_SECS", 5)),
            "retries": int(getattr(settings, "SNMP_RETRIES", 1)),
        }
        if effective_version in (1, 2):
            kwargs["community"] = self.device.snmp_community
            return kwargs

        # SNMPv3
        # Dùng getattr để tương thích dữ liệu/model cũ chưa có đủ field SNMPv3.
        username = (getattr(self.device, "snmpv3_username", "") or "").strip()
        auth_protocol = (getattr(self.device, "snmpv3_auth_protocol", "") or "").strip()
        auth_password = (getattr(self.device, "snmpv3_auth_password", "") or "").strip()
        priv_protocol = (getattr(self.device, "snmpv3_priv_protocol", "") or "").strip()
        priv_password = (getattr(self.device, "snmpv3_priv_password", "") or "").strip()

        if not username:
            raise ValueError("SNMPv3 yêu cầu username.")
        if priv_protocol and not auth_protocol:
            raise ValueError("SNMPv3 privacy yêu cầu auth protocol.")
        if auth_protocol and not auth_password:
            raise ValueError("SNMPv3 auth protocol yêu cầu auth password.")
        if priv_protocol and not priv_password:
            raise ValueError("SNMPv3 privacy protocol yêu cầu privacy password.")

        kwargs["security_username"] = username
        if priv_protocol and priv_password:
            kwargs["security_level"] = "auth_with_privacy"
            kwargs["auth_protocol"] = auth_protocol
            kwargs["auth_password"] = auth_password
            kwargs["privacy_protocol"] = priv_protocol
            kwargs["privacy_password"] = priv_password
        elif auth_protocol and auth_password:
            kwargs["security_level"] = "auth_without_privacy"
            kwargs["auth_protocol"] = auth_protocol
            kwargs["auth_password"] = auth_password
        else:
            kwargs["security_level"] = "no_auth_or_privacy"
        return kwargs

    @property
    def _session(self):
        """Lazy-init SNMP Session để tái sử dụng kết nối (giảm chi phí setup UDP/SNMP)."""
        if self.__session is None:
            self.__session = create_snmp_session(self._snmp_kwargs, backend=self._snmp_backend)
        return self.__session

    def _snmp_get(self, oid: str) -> str | int | None:
        value = snmp_get_value(self._session, oid)
        if value is None:
            logger.debug("SNMP GET %s on device %s failed", oid, self.device.name)
        return value

    def _snmp_walk(self, oid_prefix: str) -> list[tuple[str, str]]:
        """Walk một OID table, trả về list (oid, value)."""
        results = snmp_walk_pairs(self._session, oid_prefix)
        if not results:
            logger.warning("SNMP WALK %s failed on %s", oid_prefix, self.device.name)
        return results

    def detect_os_family(self) -> str:
        """Detect OS-family từ vendor đã chọn + sysObjectID/sysDescr.

        Synology DSM chạy net-snmp → sysObjectID trả 8072 (không phải 6574) và
        sysDescr là "Linux ..." nên KHÔNG nhận diện được qua sysObjectID. Vì vậy
        ưu tiên vendor người dùng chọn, và có probe OID đặc trưng Synology làm fallback.
        """
        if (self.device.vendor or "").lower() == "synology":
            return "synology_dsm"

        sys_oid  = self._snmp_get(OID_SYS_OBJECT_ID) or ""
        sys_desc = self._snmp_get(OID_SYS_DESCR) or ""

        # MikroTik — enterprise prefix 14988
        if "14988" in sys_oid or "RouterOS" in sys_desc:
            return "mikrotik_routeros"

        # Fortinet — enterprise prefix 12356
        if "12356" in sys_oid or "FortiGate" in sys_desc or "FortiOS" in sys_desc:
            return "fortinet_fortios"

        # Huawei — enterprise prefix 2011
        if "2011" in sys_oid or "VRP" in sys_desc:
            return "huawei_vrp"

        # HP/H3C Comware — enterprise prefix 25506; dùng chung profile Huawei entity table
        if "25506" in sys_oid or "Comware" in sys_desc or "H3C" in sys_desc:
            logger.info("Device %s: detect HP/H3C Comware — dùng profile huawei_vrp", self.device.name)
            return "huawei_vrp"

        # Synology NAS (DSM) — enterprise prefix 6574 (hiếm khi xuất hiện ở sysObjectID
        # vì DSM dùng net-snmp; giữ lại cho trường hợp firmware báo 6574/descr có "synology")
        if "6574" in sys_oid or "synology" in str(sys_desc).lower():
            return "synology_dsm"

        # Cisco Business (Catalyst 1200/1300, CBS) — OID khác enterprise IOS
        if _is_cisco_business(sys_desc):
            return "cisco_business"

        # Cisco — enterprise prefix 9
        if "IOS-XE" in sys_desc or "IOS XE" in sys_desc:
            return "cisco_iosxe"

        # Fallback (auto-discovery chưa set vendor): probe OID model Synology
        # trước khi mặc định Cisco IOS. Lưu ý: backend SNMP có thể trả "" (chuỗi
        # rỗng) cho OID không tồn tại — phải kiểm tra TRUTHY, không dùng `is not None`.
        if self._snmp_get(OID_SYNO_MODEL):
            return "synology_dsm"

        return "cisco_ios"

    def test_connection(self) -> str:
        uptime = self._snmp_get(OID_SYS_UPTIME)
        if uptime is None:
            if self.device.snmp_version == "v3":
                sec = f"user: {(self.device.snmpv3_username or '')[:3]}***"
            else:
                sec = f"community: {self.device.snmp_community[:3]}***"
            raise ConnectionError(f"SNMP không phản hồi từ {self.device.ip_address} ({sec})")
        return self.detect_os_family()

    def _collect_interfaces(self) -> list[InterfaceData]:
        """Thu thập toàn bộ interface từ MIB-II (standard, mọi vendor)."""
        descrs  = dict(self._snmp_walk(OID_IF_DESCR))
        opers   = dict(self._snmp_walk(OID_IF_OPER))
        aliases = dict(self._snmp_walk(OID_IF_ALIAS))
        hc_in   = dict(self._snmp_walk(OID_HC_IN))
        hc_out  = dict(self._snmp_walk(OID_HC_OUT))
        in_err  = dict(self._snmp_walk(OID_IF_IN_ERR))
        out_err = dict(self._snmp_walk(OID_IF_OUT_ERR))
        speeds  = dict(self._snmp_walk(OID_IF_SPEED))

        interfaces = []
        for oid_key, if_name in descrs.items():
            idx = oid_key.split(".")[-1]
            oper_val = int(opers.get(f"{OID_IF_OPER}.{idx}", 4) or 4)
            interfaces.append(InterfaceData(
                name=if_name,
                if_index=int(idx),
                status=IF_STATUS_MAP.get(oper_val, "unknown"),
                in_bytes=int(hc_in.get(f"{OID_HC_IN}.{idx}", 0) or 0),
                out_bytes=int(hc_out.get(f"{OID_HC_OUT}.{idx}", 0) or 0),
                in_errors=int(in_err.get(f"{OID_IF_IN_ERR}.{idx}", 0) or 0),
                out_errors=int(out_err.get(f"{OID_IF_OUT_ERR}.{idx}", 0) or 0),
                description=aliases.get(f"{OID_IF_ALIAS}.{idx}", ""),
                speed_mbps=float(speeds.get(f"{OID_IF_SPEED}.{idx}", 0) or 0),
            ))
        return interfaces

    def _collect_access_vlans(self, oid_profile: dict) -> dict[int, int]:
        """Trả về {ifIndex: access_vlan} (PVID).

        Cisco: vmVlan (CISCO-VLAN-MEMBERSHIP-MIB) index TRỰC TIẾP theo ifIndex,
        chỉ access port có entry (trunk không xuất hiện → đúng ý).
        Fallback/Huawei: dot1qPvid (Q-BRIDGE-MIB) index theo dot1dBasePort →
        cần map qua dot1dBasePortIfIndex để ra ifIndex.
        """
        vlan_oids = oid_profile.get("vlan", {})
        if not vlan_oids:
            return {}

        # 0. Cisco Business (CBS/Catalyst 1200/1300) — CISCOSB vlanAccessPortModeVlanId,
        #    index = ifIndex trực tiếp. Nguồn THẬT: dot1qPvid trên CBS trả 1 cho mọi cổng (sai).
        cisco_sb_vlan_oid = vlan_oids.get("ciscosb_access_vlan")
        if cisco_sb_vlan_oid:
            result = {}
            for oid, val in self._snmp_walk(cisco_sb_vlan_oid):
                try:
                    result[int(oid.split(".")[-1])] = int(val)
                except (ValueError, TypeError):
                    continue
            if result:
                return result

        # 1. Cisco vmVlan — index = ifIndex
        vm_vlan_oid = vlan_oids.get("vm_vlan")
        if vm_vlan_oid:
            result: dict[int, int] = {}
            for oid, val in self._snmp_walk(vm_vlan_oid):
                try:
                    result[int(oid.split(".")[-1])] = int(val)
                except (ValueError, TypeError):
                    continue
            if result:
                return result

        # 2. Fallback chuẩn — dot1qPvid (index = dot1dBasePort) + map ra ifIndex
        pvid_oid = vlan_oids.get("dot1q_pvid")
        baseport_oid = vlan_oids.get("dot1d_baseport_ifindex")
        if not pvid_oid or not baseport_oid:
            return {}

        baseport_to_ifindex: dict[str, int] = {}
        for oid, val in self._snmp_walk(baseport_oid):
            try:
                baseport_to_ifindex[oid.split(".")[-1]] = int(val)
            except (ValueError, TypeError):
                continue

        result = {}
        for oid, val in self._snmp_walk(pvid_oid):
            base_port = oid.split(".")[-1]
            if_index = baseport_to_ifindex.get(base_port)
            if if_index is None:
                continue
            try:
                result[if_index] = int(val)
            except (ValueError, TypeError):
                continue
        return result

    def _collect_port_modes(self, oid_profile: dict) -> dict[int, str]:
        """Trả về {ifIndex: "access"|"trunk"|"hybrid"} từ Q-BRIDGE dot1qVlanStaticTable.

        Mỗi VLAN có 2 PortList bitmap (index = VLAN id):
          egress  = tất cả cổng thành viên (tagged ∪ untagged)
          untagged = cổng untagged → tagged(VLAN) = egress \\ untagged.
        Gom theo dot1dBasePort:
          - tagged ở ≥1 VLAN              → trunk
          - chỉ untagged & đúng 1 VLAN    → access
          - untagged ở ≥2 VLAN, không tag → hybrid
        Map dot1dBasePort → ifIndex qua dot1dBasePortIfIndex. Bitmap KHÔNG có entry
        (cổng routed/L3, member Eth-Trunk) → không xuất hiện → để fallback heuristic.

        Cisco IOS/IOS-XE ưu tiên CISCO-VTP-MIB vlanTrunkPortDynamicStatus (index =
        ifIndex trực tiếp; trunking(1)/notTrunking(2)) — Cisco không expose Q-BRIDGE
        static table theo cách chuẩn. Cisco Business expose cả hai đều rỗng → heuristic.
        """
        vlan_oids = oid_profile.get("vlan", {})

        # 0. Cisco Business (CBS/Catalyst 1200/1300) — CISCOSB vlanPortModeState, index = ifIndex.
        #    11=access, 12=trunk (verify runtime 2026-07-02 CBS250: gi9 trunk-link=12, access=11).
        #    CBS KHÔNG expose CISCO-VTP-MIB lẫn dot1qVlanStaticTable (bitmap toàn 0) → phải đọc MIB này.
        #    Giá trị khác 11/12 (general/customer…) → để rỗng, rơi về heuristic (không khẳng định sai).
        cisco_sb_mode_oid = vlan_oids.get("ciscosb_port_mode")
        if cisco_sb_mode_oid:
            modes: dict[int, str] = {}
            for oid, val in self._snmp_walk(cisco_sb_mode_oid):
                try:
                    ifidx, mode_code = int(oid.split(".")[-1]), int(val)
                except (ValueError, TypeError):
                    continue
                if mode_code == 11:
                    modes[ifidx] = "access"
                elif mode_code == 12:
                    modes[ifidx] = "trunk"
            if modes:
                return modes

        # 1. Cisco — CISCO-VTP-MIB vlanTrunkPortDynamicStatus, index = ifIndex.
        trunk_status_oid = vlan_oids.get("vlan_trunk_status")
        if trunk_status_oid:
            vtp: dict[int, str] = {}
            for oid, val in self._snmp_walk(trunk_status_oid):
                try:
                    ifidx, status = int(oid.split(".")[-1]), int(val)
                except (ValueError, TypeError):
                    continue
                if status == 1:
                    vtp[ifidx] = "trunk"
                elif status == 2:
                    vtp[ifidx] = "access"
            if vtp:
                return vtp

        # 2. Huawei + chuẩn — Q-BRIDGE dot1qVlanStaticTable bitmap.
        egress_oid = vlan_oids.get("dot1q_static_egress")
        untagged_oid = vlan_oids.get("dot1q_static_untagged")
        baseport_oid = vlan_oids.get("dot1d_baseport_ifindex")
        if not egress_oid or not untagged_oid or not baseport_oid:
            return {}

        baseport_to_ifindex: dict[int, int] = {}
        for oid, val in self._snmp_walk(baseport_oid):
            try:
                baseport_to_ifindex[int(oid.split(".")[-1])] = int(val)
            except (ValueError, TypeError):
                continue
        if not baseport_to_ifindex:
            return {}

        egress_by_vlan: dict[str, set[int]] = {}
        for oid, val in self._snmp_walk(egress_oid):
            egress_by_vlan[oid.split(".")[-1]] = _parse_portlist(val)
        if not egress_by_vlan:
            return {}

        untagged_by_vlan: dict[str, set[int]] = {}
        for oid, val in self._snmp_walk(untagged_oid):
            untagged_by_vlan[oid.split(".")[-1]] = _parse_portlist(val)

        # Đếm số VLAN tagged / untagged cho từng dot1dBasePort.
        tagged_count: dict[int, int] = {}
        untagged_count: dict[int, int] = {}
        for vlan, egress_ports in egress_by_vlan.items():
            untagged_ports = untagged_by_vlan.get(vlan, set())
            for port in egress_ports:
                if port in untagged_ports:
                    untagged_count[port] = untagged_count.get(port, 0) + 1
                else:
                    tagged_count[port] = tagged_count.get(port, 0) + 1

        result: dict[int, str] = {}
        for base_port, if_index in baseport_to_ifindex.items():
            tagged = tagged_count.get(base_port, 0)
            untagged = untagged_count.get(base_port, 0)
            if tagged >= 1:
                mode = "trunk"
            elif untagged >= 2:
                mode = "hybrid"
            elif untagged == 1:
                mode = "access"
            else:
                continue  # không thành viên VLAN nào → bỏ qua, dùng fallback
            result[if_index] = mode
        return result

    def _collect_cpu_mem_mikrotik(self, oid_profile: dict) -> tuple[float, float]:
        """CPU và Memory cho MikroTik RouterOS."""
        cpu_table_oid = oid_profile.get("cpu", {}).get("processor_table")
        cpu_rows = self._snmp_walk(cpu_table_oid) if cpu_table_oid else []
        cpu_val  = float(cpu_rows[0][1]) if cpu_rows else 0.0

        mem_profile = oid_profile.get("memory", {})
        mem_used_oid = mem_profile.get("mem_used")
        mem_total_oid = mem_profile.get("mem_total")
        mem_used = int(self._snmp_get(mem_used_oid) or 0) if mem_used_oid else 0
        mem_total = int(self._snmp_get(mem_total_oid) or 1) if mem_total_oid else 1
        mem_val   = mem_used / mem_total * 100 if mem_total else 0.0
        return cpu_val, round(mem_val, 1)

    def _collect_cpu_mem_fortinet(self, oid_profile: dict) -> tuple[float, float]:
        """CPU và Memory cho Fortinet FortiOS — cả hai là % trực tiếp."""
        cpu_val = float(self._snmp_get(oid_profile["cpu"]["cpu_usage"]) or 0)
        mem_val = float(self._snmp_get(oid_profile["memory"]["mem_usage"]) or 0)
        return cpu_val, mem_val

    def _collect_cpu_mem_huawei(self, oid_profile: dict) -> tuple[float, float]:
        """Huawei VRP — scalar .0 thường trống, cần walk entity table."""
        cpu_scalar = oid_profile["cpu"]["cpu_usage"]
        mem_scalar = oid_profile["memory"]["mem_usage"]
        cpu_table = oid_profile["cpu"].get("cpu_table", cpu_scalar.rsplit(".", 1)[0])
        mem_table = oid_profile["memory"].get("mem_table", mem_scalar.rsplit(".", 1)[0])

        cpu_val = float(self._snmp_get(cpu_scalar) or 0)
        mem_val = float(self._snmp_get(mem_scalar) or 0)
        if cpu_val and mem_val:
            return cpu_val, mem_val

        cpu_rows = self._snmp_walk(cpu_table)
        best_idx = ""
        best_cpu = 0.0
        mem_by_idx: dict[str, float] = {}

        for oid, val in self._snmp_walk(mem_table):
            idx = oid.split(".")[-1]
            mem_by_idx[idx] = float(val or 0)

        for oid, val in cpu_rows:
            idx = oid.split(".")[-1]
            cpu = float(val or 0)
            if cpu >= best_cpu:
                best_cpu = cpu
                best_idx = idx

        if not cpu_val and best_cpu:
            cpu_val = best_cpu
        if not mem_val and best_idx:
            mem_val = mem_by_idx.get(best_idx, 0.0)
        if not mem_val and mem_by_idx:
            mem_val = max(mem_by_idx.values())

        if not cpu_val and not mem_val:
            logger.warning(
                "Huawei CPU/Memory SNMP trả rỗng trên %s — kiểm tra SNMP view cho 1.3.6.1.4.1.2011.*",
                self.device.name,
            )
        return cpu_val, mem_val

    def _collect_cpu_mem_cisco_business(self, oid_profile: dict) -> tuple[float, float]:
        """Catalyst 1200/1300, CBS — rlCpuUtil OID, không có memory % qua SNMP."""
        cpu_val = float(self._snmp_get(oid_profile["cpu"]["cpu_5min"]) or 0)
        return cpu_val, 0.0

    def _collect_cpu_mem_synology(self, oid_profile: dict) -> tuple[float, float]:
        """Synology DSM — UCD-SNMP-MIB. CPU = 100 - ssCpuIdle.

        Mem 'thực dùng' = total - free - buffer - cached (loại cache/buffer để khớp
        DSM Resource Monitor). Nếu thiếu buffer/cached → quay về total - free.
        """
        def _to_float(val) -> float:
            try:
                return float(val)
            except (TypeError, ValueError):
                return 0.0

        idle_raw = self._snmp_get(oid_profile["cpu"]["cpu_idle"])
        cpu_val = max(0.0, 100.0 - _to_float(idle_raw)) if idle_raw not in (None, "") else 0.0

        mem = oid_profile["memory"]

        def _kb(oid: str | None) -> float:
            return _to_float(self._snmp_get(oid)) if oid else 0.0

        total  = _kb(mem.get("mem_total"))
        avail  = _kb(mem.get("mem_avail"))
        buffer = _kb(mem.get("mem_buffer"))
        cached = _kb(mem.get("mem_cached"))

        used = total - avail - buffer - cached
        if used < 0:  # thiết bị không trả buffer/cached hợp lý → fallback
            used = total - avail
        mem_val = (used / total * 100.0) if total else 0.0
        return cpu_val, mem_val

    @staticmethod
    def _index_after(oid: str, prefix: str) -> str:
        """Phần index phía sau prefix cột (vd '.5.1.0.0.94.x' -> '5.1.0.0.94.x')."""
        full_prefix = prefix if prefix.endswith(".") else prefix + "."
        return oid[len(full_prefix):] if oid.startswith(full_prefix) else ""

    @staticmethod
    def _mac_from_index(index: str) -> str:
        """Suy MAC từ 6 octet cuối của index (Huawei encode MAC client trong index)."""
        parts = index.split(".")
        if len(parts) < 6:
            return ""
        tail = parts[-6:]
        try:
            octets = [int(p) for p in tail]
        except ValueError:
            return ""
        if any(o < 0 or o > 255 for o in octets):
            return ""
        return ":".join(f"{o:02x}" for o in octets)

    @staticmethod
    def _decode_mac_value(raw: str) -> str:
        """Chuẩn hoá MAC từ giá trị SNMP (hex-string '0x...' hoặc dạng đã format)."""
        if not raw:
            return ""
        val = raw.strip()
        if val.lower().startswith("0x"):
            val = val[2:]
        hex_only = val.replace(":", "").replace("-", "").replace(" ", "")
        if len(hex_only) == 12:
            try:
                int(hex_only, 16)
                return ":".join(hex_only[i:i + 2].lower() for i in range(0, 12, 2))
            except ValueError:
                pass
        return raw.strip()

    @staticmethod
    def _decode_inet_ipv4(raw: str | None) -> str:
        """Decode InetAddress OctetString → dotted IPv4. '' nếu rỗng/0.0.0.0/IPv6.

        Huawei AC trả IP AP (cột .13) là OctetString 4 byte nhị phân; str(var_bind)
        cho ra chuỗi 4 ký tự latin-1. KHÔNG .strip() vì byte đầu IP 10.x là '\\n'.
        """
        if not raw:
            return ""
        val = raw.strip()
        # vài backend tự format sẵn thành dotted IPv4 → giữ nguyên
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", val):
            return "" if val == "0.0.0.0" else val
        try:
            b = raw.encode("latin-1", "ignore")
        except (UnicodeEncodeError, AttributeError):
            return ""
        if len(b) == 4 and any(b):           # 4 byte, không phải 0.0.0.0
            return ".".join(str(x) for x in b)
        return ""                             # 16-byte IPv6 all-zero / khác → bỏ qua

    def _walk_column(self, oid: str | None) -> dict[str, str]:
        """Walk 1 cột bảng, trả về {index_suffix: value}."""
        if not oid:
            return {}
        result: dict[str, str] = {}
        for full_oid, value in self._snmp_walk(oid):
            idx = self._index_after(full_oid, oid)
            if idx:
                result[idx] = value
        return result

    def _collect_wifi(self, oid_profile: dict) -> dict:
        """Thu thập danh sách AP + client từ Huawei AC qua HUAWEI-WLAN MIB.

        Trả về {"wifi_aps": [...], "wifi_clients": [...]}; rỗng nếu profile thiếu
        section wlan hoặc thiết bị không trả dữ liệu.
        """
        wlan = oid_profile.get("wlan") or {}
        ap_cols = wlan.get("ap") or {}
        sta_cols = wlan.get("station") or {}
        online_states = {str(s) for s in (wlan.get("ap_run_state_online") or [])}

        aps: list[dict] = []
        ap_name_by_index: dict[str, str] = {}
        if ap_cols:
            names = self._walk_column(ap_cols.get("name"))
            macs = self._walk_column(ap_cols.get("mac"))
            ips = self._walk_column(ap_cols.get("ip"))
            groups = self._walk_column(ap_cols.get("group"))
            states = self._walk_column(ap_cols.get("run_state"))
            sta_counts = self._walk_column(ap_cols.get("sta_count"))
            indexes = set().union(names, macs, ips, groups, states, sta_counts)
            for idx in sorted(indexes):
                name = (names.get(idx) or "").strip()
                state_raw = (states.get(idx) or "").strip()
                if online_states:
                    is_online = state_raw in online_states
                else:
                    is_online = bool(state_raw)
                decoded_mac = self._decode_mac_value(macs.get(idx, ""))
                index_mac = self._mac_from_index(idx)
                # Huawei AC6508 returns AP serial in the documented "mac" column;
                # the stable AP MAC is encoded in the table index.
                mac = decoded_mac if ":" in decoded_mac else (index_mac or decoded_mac)
                try:
                    sta_count = int(sta_counts.get(idx, 0) or 0)
                except ValueError:
                    sta_count = 0
                ap = {
                    "index": idx,
                    "name": name or mac or idx,
                    "mac": mac,
                    "ip": self._decode_inet_ipv4(ips.get(idx)),
                    "group": (groups.get(idx) or "").strip(),
                    "run_state": state_raw,
                    "is_online": is_online,
                    "client_count": sta_count,
                }
                aps.append(ap)
                if name:
                    ap_name_by_index[idx] = name

        clients: list[dict] = []
        if sta_cols:
            ap_names = self._walk_column(sta_cols.get("ap_name"))
            ssids = self._walk_column(sta_cols.get("ssid"))
            ips = self._walk_column(sta_cols.get("ip"))
            radios = self._walk_column(sta_cols.get("radio_type"))
            rssis = self._walk_column(sta_cols.get("rssi"))
            online_times = self._walk_column(sta_cols.get("online_time"))
            indexes = set().union(ap_names, ssids, ips, radios, rssis, online_times)
            for idx in sorted(indexes):
                mac = self._mac_from_index(idx)
                try:
                    rssi = int(rssis[idx]) if rssis.get(idx) not in (None, "") else None
                except ValueError:
                    rssi = None
                try:
                    online_secs = int(online_times.get(idx, 0) or 0)
                except ValueError:
                    online_secs = 0
                clients.append({
                    "mac": mac or idx,
                    "ip": (ips.get(idx) or "").strip(),
                    "ssid": (ssids.get(idx) or "").strip(),
                    "ap_name": (ap_names.get(idx) or "").strip(),
                    "radio": (radios.get(idx) or "").strip(),
                    "rssi": rssi,
                    "online_secs": online_secs,
                })

        if not aps and not clients:
            logger.warning(
                "WLAN poll trả rỗng trên %s — kiểm tra OID wlan (chạy verify_wlan_oids) "
                "và SNMP view cho 1.3.6.1.4.1.2011.6.139.*",
                self.device.name,
            )
        return {"wifi_aps": aps, "wifi_clients": clients}

    def collect_raw(self) -> dict:
        # Short-circuit SNMP: 1 GET rẻ (sysUpTime) làm probe reachability.
        # SNMP chết (icmp-up/snmp-down như firewall qua WAN) -> dừng NGAY,
        # khỏi detect + walk 8 cột bảng interface đắt đỏ (từng tốn ~150s).
        # Analog của ICMP short-circuit trong tasks._poll_device_once.
        uptime_raw = self._snmp_get(OID_SYS_UPTIME)
        if uptime_raw is None:
            raise ConnectionError(
                f"SNMP không phản hồi từ {self.device.ip_address} "
                f"(sysUpTime) — bỏ qua collect"
            )

        os_family   = self.detect_os_family()
        oid_profile = _load_oid_profile(os_family)
        extra: dict = {}

        if os_family == "mikrotik_routeros":
            cpu_val, mem_val = self._collect_cpu_mem_mikrotik(oid_profile)

        elif os_family == "fortinet_fortios":
            cpu_val, mem_val = self._collect_cpu_mem_fortinet(oid_profile)
            # Lưu session count vào extra nếu có
            ses_oid = oid_profile.get("extra", {}).get("session_count")
            if ses_oid:
                ses = self._snmp_get(ses_oid)
                if ses is not None:
                    extra["session_count"] = int(ses)

        elif os_family == "huawei_vrp":
            cpu_val, mem_val = self._collect_cpu_mem_huawei(oid_profile)

        elif os_family == "cisco_business":
            cpu_val, mem_val = self._collect_cpu_mem_cisco_business(oid_profile)

        elif os_family == "synology_dsm":
            cpu_val, mem_val = self._collect_cpu_mem_synology(oid_profile)

        else:
            # Cisco IOS / IOS-XE: CPU từ OID 5-min, Memory cần tính
            cpu_val  = float(self._snmp_get(oid_profile["cpu"]["cpu_5min"]) or 0)
            mem_profile = oid_profile.get("memory", {})
            mem_used_oid = mem_profile.get("mem_used") or mem_profile.get("mem_processor_used")
            mem_free_oid = mem_profile.get("mem_free") or mem_profile.get("mem_processor_free")
            if not mem_used_oid or not mem_free_oid:
                logger.warning(
                    "OID profile memory thiếu key dùng được cho %s (%s).",
                    self.device.name,
                    os_family,
                )
                mem_used = 0
                mem_free = 1
            else:
                mem_used = int(self._snmp_get(mem_used_oid) or 0)
                mem_free = int(self._snmp_get(mem_free_oid) or 1)
            mem_val  = mem_used / (mem_used + mem_free) * 100 if (mem_used + mem_free) else 0

        # WLAN controller (Huawei AC/ACL): bổ sung danh sách AP + client.
        if self.device.device_type == "wlan_controller":
            extra.update(self._collect_wifi(oid_profile))

        # Gán access VLAN (PVID) + chế độ cổng (trunk/access) cho từng interface theo ifIndex.
        interfaces = self._collect_interfaces()
        vlan_map = self._collect_access_vlans(oid_profile)
        mode_map = self._collect_port_modes(oid_profile)
        if vlan_map or mode_map:
            for iface in interfaces:
                if vlan_map:
                    iface.access_vlan = vlan_map.get(iface.if_index)
                if mode_map:
                    iface.port_mode = mode_map.get(iface.if_index)

        return {
            "os_family":   os_family,
            "cpu_percent": round(cpu_val, 1),
            "mem_percent": round(mem_val, 1),
            "uptime_secs": int(uptime_raw or 0) // 100,  # TimeTicks → seconds
            "interfaces":  interfaces,
            "extra":       extra,
        }

    def adapt(self, raw: dict) -> NormalizedData:
        return NormalizedData(
            device_name=self.device.name,
            ip_address=self.device.ip_address,
            timestamp=datetime.now(tz=timezone.utc),
            os_family=raw["os_family"],
            cpu_percent=raw["cpu_percent"],
            mem_percent=raw["mem_percent"],
            uptime_secs=raw["uptime_secs"],
            interfaces=raw["interfaces"],
            extra=raw.get("extra", {}),
        )
