"""Adapter cho Cisco IOS-XE — format giống IOS, chỉ khác nhãn os_family."""
from .cisco_ios import CiscoIOSAdapter


class CiscoIOSXEAdapter(CiscoIOSAdapter):
    os_family = "cisco_iosxe"
