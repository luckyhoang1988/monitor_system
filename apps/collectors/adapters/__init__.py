from .cisco_ios import CiscoIOSAdapter
from .cisco_iosxe import CiscoIOSXEAdapter
from .huawei_vrp import HuaweiVRPAdapter
from .mikrotik_routeros import MikroTikRouterOSAdapter
from .fortinet_fortios import FortinetFortiOSAdapter
from apps.collectors.base import BaseAdapter

_ADAPTERS: dict[str, BaseAdapter] = {
    "cisco_ios":          CiscoIOSAdapter(),
    "cisco_iosxe":        CiscoIOSXEAdapter(),
    "huawei_vrp":         HuaweiVRPAdapter(),
    "mikrotik_routeros":  MikroTikRouterOSAdapter(),
    "fortinet_fortios":   FortinetFortiOSAdapter(),
}


def get_adapter(os_family: str) -> BaseAdapter:
    adapter = _ADAPTERS.get(os_family)
    if adapter is None:
        raise ValueError(f"Không có adapter cho os_family: {os_family!r}")
    return adapter


__all__ = [
    "get_adapter",
    "CiscoIOSAdapter", "CiscoIOSXEAdapter", "HuaweiVRPAdapter",
    "MikroTikRouterOSAdapter", "FortinetFortiOSAdapter",
]
