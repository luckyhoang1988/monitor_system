from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.devices.models import Device


@dataclass
class InterfaceData:
    name:       str
    if_index:   int
    status:     str          # "up" | "down" | "testing" | "unknown"
    in_bytes:   int          # raw counter (octets)
    out_bytes:  int
    in_errors:  int = 0
    out_errors: int = 0
    description: str = ""
    speed_mbps: float = 0.0
    access_vlan: int | None = None  # PVID / access VLAN (SNMP), None nếu trunk/không lấy được


@dataclass
class NormalizedData:
    """Format chuẩn — mọi collector/adapter đều trả về kiểu này."""
    device_name:  str
    ip_address:   str
    timestamp:    datetime
    os_family:    str              # cisco_ios | cisco_iosxe | huawei_vrp
    cpu_percent:  float
    mem_percent:  float
    uptime_secs:  int = 0
    interfaces:   list[InterfaceData] = field(default_factory=list)
    extra:        dict = field(default_factory=dict)  # dữ liệu bổ sung tuỳ vendor


class BaseCollector(ABC):
    def __init__(self, device: "Device") -> None:
        self.device = device

    @abstractmethod
    def test_connection(self) -> str:
        """Kiểm tra kết nối, trả về os_family nếu thành công."""

    @abstractmethod
    def collect_raw(self) -> dict:
        """Kết nối thiết bị, thu thập dữ liệu thô."""

    @abstractmethod
    def adapt(self, raw: dict) -> NormalizedData:
        """Chuẩn hóa raw data sang NormalizedData."""

    def collect(self) -> NormalizedData:
        raw = self.collect_raw()
        return self.adapt(raw)


class BaseAdapter(ABC):
    @abstractmethod
    def normalize(self, raw: dict, device_name: str, ip_address: str) -> NormalizedData:
        """Chuyển raw data của từng vendor/OS sang NormalizedData chuẩn."""
