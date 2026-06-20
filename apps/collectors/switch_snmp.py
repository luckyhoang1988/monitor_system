"""SNMP Collector cho Switch — tự động detect vendor/OS-family."""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .base import BaseCollector, NormalizedData, InterfaceData
from .adapters import get_adapter
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
        version_map = {"v1": 1, "v2c": 2, "v3": 3}
        effective_version = version_map.get(self.device.snmp_version)
        if effective_version is None:
            raise ValueError(f"SNMP version '{self.device.snmp_version}' không hợp lệ.")

        kwargs = {
            "hostname": self.device.ip_address,
            "version": effective_version,
            "timeout": 10,
            "retries": 2,
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
        """Detect vendor và OS-family từ sysObjectID + sysDescr."""
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

        # Cisco — enterprise prefix 9
        if "IOS-XE" in sys_desc or "IOS XE" in sys_desc:
            return "cisco_iosxe"

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

    def collect_raw(self) -> dict:
        os_family   = self.detect_os_family()
        oid_profile = _load_oid_profile(os_family)
        uptime_raw  = self._snmp_get(OID_SYS_UPTIME) or 0
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
            cpu_val = float(self._snmp_get(oid_profile["cpu"]["cpu_usage"]) or 0)
            mem_val = float(self._snmp_get(oid_profile["memory"]["mem_usage"]) or 0)

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

        return {
            "os_family":   os_family,
            "cpu_percent": round(cpu_val, 1),
            "mem_percent": round(mem_val, 1),
            "uptime_secs": int(uptime_raw) // 100,  # TimeTicks → seconds
            "interfaces":  self._collect_interfaces(),
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
