"""Tests cho CollectorFactory — dùng factory.build() để tránh DB."""
from tests.conftest import (
    CiscoSSHDeviceFactory, CiscoSNMPDeviceFactory,
    HuaweiSSHDeviceFactory, HuaweiSNMPDeviceFactory,
)
from apps.collectors.factory import CollectorFactory
from apps.collectors.switch_snmp import SwitchSNMPCollector
from apps.collectors.switch_ssh import SwitchSSHCollector
from apps.collectors.hyperv import HyperVCollector


class TestCollectorFactory:
    def test_snmp_protocol_returns_snmp_collector(self):
        device = CiscoSNMPDeviceFactory.build()
        collector = CollectorFactory.create(device)
        assert isinstance(collector, SwitchSNMPCollector)

    def test_ssh_protocol_returns_ssh_collector(self):
        device = CiscoSSHDeviceFactory.build()
        collector = CollectorFactory.create(device)
        assert isinstance(collector, SwitchSSHCollector)

    def test_huawei_ssh_returns_ssh_collector(self):
        device = HuaweiSSHDeviceFactory.build()
        collector = CollectorFactory.create(device)
        assert isinstance(collector, SwitchSSHCollector)

    def test_hyperv_device_type_returns_hyperv_collector(self):
        device = CiscoSNMPDeviceFactory.build(device_type="hyperv")
        collector = CollectorFactory.create(device)
        assert isinstance(collector, HyperVCollector)

    def test_hyperv_takes_precedence_over_ssh_protocol(self):
        device = CiscoSSHDeviceFactory.build(device_type="hyperv")
        collector = CollectorFactory.create(device)
        assert isinstance(collector, HyperVCollector)

    def test_default_is_snmp_for_switch(self):
        # protocol != "ssh" và device_type != "hyperv" → SNMP
        device = HuaweiSNMPDeviceFactory.build()
        collector = CollectorFactory.create(device)
        assert isinstance(collector, SwitchSNMPCollector)

    def test_snmp_router_returns_snmp_collector(self):
        # device_type không ảnh hưởng dispatch — chỉ protocol/hyperv mới ảnh hưởng.
        device = CiscoSNMPDeviceFactory.build(device_type="router")
        collector = CollectorFactory.create(device)
        assert isinstance(collector, SwitchSNMPCollector)

    def test_snmp_firewall_returns_snmp_collector(self):
        # Firewall thật trong fleet (USG6525E) là Huawei SNMP.
        device = HuaweiSNMPDeviceFactory.build(device_type="firewall")
        collector = CollectorFactory.create(device)
        assert isinstance(collector, SwitchSNMPCollector)

    def test_ssh_firewall_returns_ssh_collector(self):
        device = CiscoSSHDeviceFactory.build(device_type="firewall")
        collector = CollectorFactory.create(device)
        assert isinstance(collector, SwitchSSHCollector)
