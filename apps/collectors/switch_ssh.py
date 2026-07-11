"""SSH/CLI Collector cho Switch — dùng Netmiko, hỗ trợ Cisco và Huawei."""
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .base import BaseCollector, NormalizedData, InterfaceData

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

NETMIKO_DRIVER = {
    "cisco":    "cisco_ios",    # Netmiko tự detect IOS vs IOS-XE
    "huawei":   "huawei_vrp",
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
}


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
        cmd_set = COMMANDS.get(self.device.vendor, {})
        version_cmd = cmd_set.get("version")
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

    def _parse_interfaces(self, output: str, vendor: str) -> list[InterfaceData]:
        if vendor == "huawei":
            return self._parse_huawei_interfaces(output)
        return self._parse_cisco_interfaces(output)

    def collect_raw(self) -> dict:
        vendor   = self.device.vendor
        commands = COMMANDS.get(vendor, COMMANDS["cisco"])

        with self._get_connection() as conn:
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
