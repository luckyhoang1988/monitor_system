"""Factory — chọn collector phù hợp dựa trên device.protocol."""
from typing import TYPE_CHECKING
from .base import BaseCollector

if TYPE_CHECKING:
    from apps.devices.models import Device


class CollectorFactory:
    @staticmethod
    def create(device: "Device") -> BaseCollector:
        if device.device_type == "hyperv":
            from .hyperv import HyperVCollector
            return HyperVCollector(device)

        if device.protocol == "ping":
            from .ping import PingCollector
            return PingCollector(device)

        if device.protocol == "ssh":
            from .switch_ssh import SwitchSSHCollector
            return SwitchSSHCollector(device)

        # default: SNMP — áp dụng cho switch, router, firewall
        from .switch_snmp import SwitchSNMPCollector
        return SwitchSNMPCollector(device)
