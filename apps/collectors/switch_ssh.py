"""SSH/CLI Collector cho Switch — dùng Netmiko, hỗ trợ Cisco và Huawei."""
import logging
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .base import BaseCollector, NormalizedData, InterfaceData

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)
DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "debug-f05be0.log"

NETMIKO_DRIVER = {
    "cisco":    "cisco_ios",    # Netmiko tự detect IOS vs IOS-XE
    "huawei":   "huawei_vrp",
    "hp":       "hp_comware",
    "mikrotik": "mikrotik_routeros",
    "fortinet": "fortinet",
}

# SSH commands theo vendor
COMMANDS = {
    "cisco": {
        "version":   "show version",
        "cpu":       "show processes cpu | include CPU",
        "memory":    "show processes memory | include Processor",
        "interface": "show interfaces",
    },
    "huawei": {
        "version":   "display version",
        "cpu":       "display cpu-usage",
        "memory":    "display memory-usage",
        "interface": "display interface",
    },
    "hp": {
        "version":   "display version",
        "cpu":       "display cpu-usage",
        "memory":    "display memory-usage",
        "interface": "display interface",
    },
    "mikrotik": {
        "resource":  "/system resource print",
        "interface": "/interface print detail",
    },
    "fortinet": {
        "perf":      "get system performance status",
        "version":   "get system status",
        "interface": "get system interface physical",
    },
}


def _debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": "f05be0",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        }
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        pass


class SwitchSSHCollector(BaseCollector):
    def __init__(self, device: "Device") -> None:
        super().__init__(device)
        self._driver = NETMIKO_DRIVER.get(device.vendor, "cisco_ios")
        self._connect_params = {
            "device_type": self._driver,
            "host":        device.ip_address,
            "username":    device.ssh_username,
            "password":    device.ssh_password,
            "timeout":     30,
            "session_timeout": 60,
        }

    def _get_connection(self):
        from netmiko import ConnectHandler
        from netmiko.exceptions import (
            NetmikoAuthenticationException,
            NetmikoTimeoutException,
        )
        try:
            return ConnectHandler(**self._connect_params)
        except NetmikoAuthenticationException:
            raise ConnectionError(f"SSH auth failed for device {self.device.name} ({self.device.ip_address})")
        except NetmikoTimeoutException:
            raise TimeoutError(f"SSH timeout connecting to device {self.device.name} ({self.device.ip_address})")

    def detect_os_family(self) -> str:
        # region agent log
        _debug_log(
            run_id="pre-fix",
            hypothesis_id="H2",
            location="apps/collectors/switch_ssh.py:98",
            message="SSH OS detection started",
            data={"device": self.device.name, "vendor": self.device.vendor},
        )
        # endregion
        if self.device.vendor == "mikrotik":
            # region agent log
            _debug_log(
                run_id="post-fix",
                hypothesis_id="H2",
                location="apps/collectors/switch_ssh.py:106",
                message="SSH vendor shortcut selected",
                data={"vendor": self.device.vendor, "os_family": "mikrotik_routeros"},
            )
            # endregion
            return "mikrotik_routeros"
        if self.device.vendor == "fortinet":
            # region agent log
            _debug_log(
                run_id="post-fix",
                hypothesis_id="H2",
                location="apps/collectors/switch_ssh.py:115",
                message="SSH vendor shortcut selected",
                data={"vendor": self.device.vendor, "os_family": "fortinet_fortios"},
            )
            # endregion
            return "fortinet_fortios"
        if self.device.vendor == "hp":
            # region agent log
            _debug_log(
                run_id="post-fix",
                hypothesis_id="H2",
                location="apps/collectors/switch_ssh.py:124",
                message="SSH vendor shortcut selected",
                data={"vendor": self.device.vendor, "os_family": "hp_comware"},
            )
            # endregion
            return "hp_comware"

        cmd_set = COMMANDS.get(self.device.vendor, {})
        version_cmd = cmd_set.get("version")
        # region agent log
        _debug_log(
            run_id="pre-fix",
            hypothesis_id="H2",
            location="apps/collectors/switch_ssh.py:109",
            message="SSH version command resolved",
            data={"vendor": self.device.vendor, "has_version_cmd": bool(version_cmd)},
        )
        # endregion
        if not version_cmd:
            raise KeyError(f"Missing version command for vendor={self.device.vendor}")
        with self._get_connection() as conn:
            output = conn.send_command(version_cmd)
        if self.device.vendor == "huawei":
            return "huawei_vrp"
        if "IOS-XE" in output or "IOS XE" in output:
            return "cisco_iosxe"
        return "cisco_ios"

    def test_connection(self) -> str:
        return self.detect_os_family()

    def _parse_cisco_cpu(self, output: str) -> float:
        # "CPU utilization for five seconds: 5%/0%; one minute: 3%; five minutes: 4%"
        match = re.search(r"five minutes:\s*(\d+)%", output)
        return float(match.group(1)) if match else 0.0

    def _parse_huawei_cpu(self, output: str) -> float:
        # "CPU Usage     : 15%"
        match = re.search(r"CPU Usage\s*:\s*(\d+)%", output)
        return float(match.group(1)) if match else 0.0

    def _parse_cisco_mem(self, output: str) -> float:
        # "Processor  123456789  98765432  ..."  (used, free)
        match = re.search(r"Processor\s+(\d+)\s+(\d+)", output)
        if match:
            used, free = int(match.group(1)), int(match.group(2))
            return used / (used + free) * 100 if (used + free) else 0.0
        return 0.0

    def _parse_huawei_mem(self, output: str) -> float:
        # "Memory Using Percentage Is: 45%"
        match = re.search(r"Memory Using Percentage Is:\s*(\d+)%", output)
        return float(match.group(1)) if match else 0.0

    def _parse_cisco_uptime(self, output: str) -> int:
        """Parse uptime từ 'show version' output của Cisco."""
        weeks   = re.search(r"(\d+)\s+week",   output)
        days    = re.search(r"(\d+)\s+day",    output)
        hours   = re.search(r"(\d+)\s+hour",   output)
        minutes = re.search(r"(\d+)\s+minute", output)
        total = 0
        if weeks:   total += int(weeks.group(1))   * 7 * 86400
        if days:    total += int(days.group(1))    * 86400
        if hours:   total += int(hours.group(1))   * 3600
        if minutes: total += int(minutes.group(1)) * 60
        return total

    def _parse_huawei_uptime(self, output: str) -> int:
        """Parse uptime từ 'display version' output của Huawei."""
        match = re.search(
            r"Uptime is\s+(\d+)\s+week.*?(\d+)\s+day.*?(\d+)\s+hour.*?(\d+)\s+minute",
            output, re.IGNORECASE,
        )
        if match:
            w, d, h, m = (int(x) for x in match.groups())
            return w * 7 * 86400 + d * 86400 + h * 3600 + m * 60
        return 0

    def _parse_cisco_interfaces(self, output: str) -> list[InterfaceData]:
        """Parse 'show interfaces' output của Cisco IOS/IOS-XE."""
        interfaces = []
        blocks = re.split(r"\n(?=[A-Za-z])", output)
        for idx, block in enumerate(blocks, start=1):
            header = re.match(
                r"^(\S+)\s+is\s+(up|down|administratively down)",
                block, re.IGNORECASE,
            )
            if not header:
                continue

            name   = header.group(1)
            status = "up" if header.group(2).lower() == "up" else "down"

            desc_m = re.search(r"Description:\s*(.+)", block)
            in_m   = re.search(r"(\d+)\s+packets input,\s+(\d+)\s+bytes", block)
            out_m  = re.search(r"(\d+)\s+packets output,\s+(\d+)\s+bytes", block)
            ierr_m = re.search(r"(\d+)\s+input errors", block)
            oerr_m = re.search(r"(\d+)\s+output errors", block)
            spd_m  = re.search(r"BW\s+(\d+)\s+Kbit", block)

            interfaces.append(InterfaceData(
                name=name,
                if_index=idx,
                status=status,
                in_bytes=int(in_m.group(2)) if in_m else 0,
                out_bytes=int(out_m.group(2)) if out_m else 0,
                in_errors=int(ierr_m.group(1)) if ierr_m else 0,
                out_errors=int(oerr_m.group(1)) if oerr_m else 0,
                description=desc_m.group(1).strip() if desc_m else "",
                speed_mbps=float(spd_m.group(1)) / 1000 if spd_m else 0.0,
            ))
        return interfaces

    def _parse_huawei_interfaces(self, output: str) -> list[InterfaceData]:
        """Parse 'display interface' output của Huawei VRP."""
        interfaces = []
        # Chỉ split tại header interface "XxxYyy current state", không split các dòng khác
        blocks = re.split(r"\n(?=\S+\s+current state)", output, flags=re.IGNORECASE)
        for idx, block in enumerate(blocks, start=1):
            header = re.match(
                r"^(\S+)\s+current state\s*:\s*(UP|DOWN|Administratively DOWN)",
                block, re.IGNORECASE,
            )
            if not header:
                continue

            name   = header.group(1)
            status = "up" if header.group(2).upper() == "UP" else "down"

            desc_m = re.search(r"Description[:\s]+(.+)", block)
            in_m   = re.search(r"Input:\s+\d+\s+packets,\s+(\d+)\s+bytes", block)
            out_m  = re.search(r"Output:\s+\d+\s+packets,\s+(\d+)\s+bytes", block)
            ierr_m = re.search(r"Input error:\s+(\d+)", block)
            oerr_m = re.search(r"Output error:\s+(\d+)", block)
            spd_m  = re.search(r"Speed\s*:\s*(\d+)", block)

            interfaces.append(InterfaceData(
                name=name,
                if_index=idx,
                status=status,
                in_bytes=int(in_m.group(1)) if in_m else 0,
                out_bytes=int(out_m.group(1)) if out_m else 0,
                in_errors=int(ierr_m.group(1)) if ierr_m else 0,
                out_errors=int(oerr_m.group(1)) if oerr_m else 0,
                description=desc_m.group(1).strip() if desc_m else "",
                speed_mbps=float(spd_m.group(1)) if spd_m else 0.0,
            ))
        return interfaces

    # ─── MikroTik RouterOS parsers ─────────────────────────────────────────────

    def _parse_mikrotik_resource(self, output: str) -> tuple[float, float, int]:
        """Parse '/system resource print' → (cpu%, mem%, uptime_secs)."""
        cpu_m   = re.search(r"cpu-load:\s*(\d+)", output)
        free_m  = re.search(r"free-memory:\s*([\d.]+)([MKG]iB)", output)
        total_m = re.search(r"total-memory:\s*([\d.]+)([MKG]iB)", output)
        up_m    = re.search(r"uptime:\s*(\S+)", output)

        cpu_val = float(cpu_m.group(1)) if cpu_m else 0.0

        def to_mb(val: float, unit: str) -> float:
            return {"GiB": val * 1024, "MiB": val, "KiB": val / 1024}.get(unit, val)

        mem_val = 0.0
        if free_m and total_m:
            free  = to_mb(float(free_m.group(1)),  free_m.group(2))
            total = to_mb(float(total_m.group(1)), total_m.group(2))
            mem_val = (total - free) / total * 100 if total else 0.0

        uptime = 0
        if up_m:
            # Format: 10w4d19h2m29s
            raw = up_m.group(1)
            for pat, mult in [(r"(\d+)w", 604800), (r"(\d+)d", 86400),
                              (r"(\d+)h", 3600),   (r"(\d+)m", 60), (r"(\d+)s", 1)]:
                m = re.search(pat, raw)
                if m:
                    uptime += int(m.group(1)) * mult

        return cpu_val, round(mem_val, 1), uptime

    def _parse_mikrotik_interfaces(self, output: str) -> list[InterfaceData]:
        """Parse '/interface print detail' output của MikroTik RouterOS."""
        interfaces = []
        # Mỗi interface bắt đầu bằng số thứ tự đầu dòng
        blocks = re.split(r"\n(?=\s*\d+\s+)", output)
        for idx, block in enumerate(blocks, start=1):
            name_m    = re.search(r'name="([^"]+)"', block)
            running_m = re.search(r"running=(yes|no)", block)
            disabled_m= re.search(r"disabled=(yes|no)", block)
            rx_m      = re.search(r"rx-byte=(\d+)", block)
            tx_m      = re.search(r"tx-byte=(\d+)", block)
            rx_err_m  = re.search(r"rx-error=(\d+)", block)
            tx_err_m  = re.search(r"tx-error=(\d+)", block)
            comment_m = re.search(r'comment="([^"]*)"', block)

            if not name_m:
                continue

            if disabled_m and disabled_m.group(1) == "yes":
                status = "down"
            elif running_m:
                status = "up" if running_m.group(1) == "yes" else "down"
            else:
                status = "unknown"

            interfaces.append(InterfaceData(
                name=name_m.group(1),
                if_index=idx,
                status=status,
                in_bytes=int(rx_m.group(1)) if rx_m else 0,
                out_bytes=int(tx_m.group(1)) if tx_m else 0,
                in_errors=int(rx_err_m.group(1)) if rx_err_m else 0,
                out_errors=int(tx_err_m.group(1)) if tx_err_m else 0,
                description=comment_m.group(1) if comment_m else "",
            ))
        return interfaces

    # ─── Fortinet FortiOS parsers ───────────────────────────────────────────────

    def _parse_fortinet_perf(self, output: str) -> tuple[float, float]:
        """Parse 'get system performance status' → (cpu%, mem%)."""
        # "CPU states: 5% user 1% system 0% nice 94% idle"
        cpu_m = re.search(r"CPU states?:\s*(\d+)%\s*user", output)
        # "Memory: total=1000000 KB used=500000 KB free=500000 KB"
        used_m  = re.search(r"Memory:.*?used=(\d+)\s*KB", output)
        total_m = re.search(r"Memory:.*?total=(\d+)\s*KB", output)

        cpu_val = float(cpu_m.group(1)) if cpu_m else 0.0
        mem_val = 0.0
        if used_m and total_m:
            used, total = int(used_m.group(1)), int(total_m.group(1))
            mem_val = used / total * 100 if total else 0.0

        return cpu_val, round(mem_val, 1)

    def _parse_fortinet_uptime(self, output: str) -> int:
        """Parse 'get system status' → uptime_secs."""
        # "System time: Mon Jan  1 00:00:00 2024\nUptime: 10 days,  2 hours,  15 minutes"
        m = re.search(r"Uptime:\s*(?:(\d+)\s*day)?.*?(?:(\d+)\s*hour)?.*?(?:(\d+)\s*minute)?", output)
        if not m:
            return 0
        d = int(m.group(1) or 0)
        h = int(m.group(2) or 0)
        mi = int(m.group(3) or 0)
        return d * 86400 + h * 3600 + mi * 60

    def _parse_fortinet_interfaces(self, output: str) -> list[InterfaceData]:
        """Parse 'get system interface physical' output của Fortinet."""
        interfaces = []
        # Mỗi interface bắt đầu bằng "== [interface_name] =="
        blocks = re.split(r"==\s+\[", output)
        for idx, block in enumerate(blocks[1:], start=1):
            header_m = re.match(r"([^\]]+)\]", block)
            if not header_m:
                continue

            name = header_m.group(1).strip()
            status = "up" if re.search(r"Link.*?up", block, re.IGNORECASE) else "down"
            desc_m  = re.search(r"alias\s*:\s*(.+)", block)
            rx_m    = re.search(r"rx-bytes\s*:\s*(\d+)", block)
            tx_m    = re.search(r"tx-bytes\s*:\s*(\d+)", block)
            spd_m   = re.search(r"speed\s*=\s*(\d+)", block)

            interfaces.append(InterfaceData(
                name=name,
                if_index=idx,
                status=status,
                in_bytes=int(rx_m.group(1)) if rx_m else 0,
                out_bytes=int(tx_m.group(1)) if tx_m else 0,
                description=desc_m.group(1).strip() if desc_m else "",
                speed_mbps=float(spd_m.group(1)) if spd_m else 0.0,
            ))
        return interfaces

    def _parse_interfaces(self, output: str, vendor: str) -> list[InterfaceData]:
        if vendor == "huawei":
            return self._parse_huawei_interfaces(output)
        if vendor == "mikrotik":
            return self._parse_mikrotik_interfaces(output)
        if vendor == "fortinet":
            return self._parse_fortinet_interfaces(output)
        return self._parse_cisco_interfaces(output)

    def collect_raw(self) -> dict:
        vendor   = self.device.vendor
        commands = COMMANDS.get(vendor, COMMANDS["cisco"])

        with self._get_connection() as conn:
            if vendor == "mikrotik":
                res_out = conn.send_command(commands["resource"])
                if_out  = conn.send_command(commands["interface"])
                cpu_percent, mem_percent, uptime_secs = self._parse_mikrotik_resource(res_out)
                os_family = "mikrotik_routeros"
                return {
                    "os_family":   os_family,
                    "cpu_percent": cpu_percent,
                    "mem_percent": mem_percent,
                    "uptime_secs": uptime_secs,
                    "interfaces":  self._parse_mikrotik_interfaces(if_out),
                    "extra":       {},
                }

            if vendor == "fortinet":
                perf_out = conn.send_command(commands["perf"])
                ver_out  = conn.send_command(commands["version"])
                if_out   = conn.send_command(commands["interface"])
                cpu_percent, mem_percent = self._parse_fortinet_perf(perf_out)
                uptime_secs = self._parse_fortinet_uptime(ver_out)
                return {
                    "os_family":   "fortinet_fortios",
                    "cpu_percent": cpu_percent,
                    "mem_percent": mem_percent,
                    "uptime_secs": uptime_secs,
                    "interfaces":  self._parse_fortinet_interfaces(if_out),
                    "extra":       {},
                }

            # Cisco / Huawei
            ver_out = conn.send_command(commands["version"])
            cpu_out = conn.send_command(commands["cpu"])
            mem_out = conn.send_command(commands["memory"])
            if_out  = conn.send_command(commands["interface"])

        if vendor == "huawei":
            os_family   = "huawei_vrp"
            cpu_percent = self._parse_huawei_cpu(cpu_out)
            mem_percent = self._parse_huawei_mem(mem_out)
            uptime_secs = self._parse_huawei_uptime(ver_out)
        elif "IOS-XE" in ver_out or "IOS XE" in ver_out:
            os_family   = "cisco_iosxe"
            cpu_percent = self._parse_cisco_cpu(cpu_out)
            mem_percent = self._parse_cisco_mem(mem_out)
            uptime_secs = self._parse_cisco_uptime(ver_out)
        else:
            os_family   = "cisco_ios"
            cpu_percent = self._parse_cisco_cpu(cpu_out)
            mem_percent = self._parse_cisco_mem(mem_out)
            uptime_secs = self._parse_cisco_uptime(ver_out)

        return {
            "os_family":   os_family,
            "cpu_percent": cpu_percent,
            "mem_percent": round(mem_percent, 1),
            "uptime_secs": uptime_secs,
            "interfaces":  self._parse_interfaces(if_out, vendor),
            "extra":       {},
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
