"""Shared factories and fixtures for all tests."""
import pytest
import factory
from factory.django import DjangoModelFactory
from apps.devices.models import Device


class DeviceFactory(DjangoModelFactory):
    class Meta:
        model = Device

    name             = factory.Sequence(lambda n: f"sw-test-{n:03d}")
    device_type      = "switch"
    ip_address       = factory.Sequence(lambda n: f"10.0.{n // 254}.{(n % 254) + 1}")
    vendor           = "cisco"
    os_family        = "cisco_ios"
    protocol         = "snmp"
    snmp_version     = "v2c"
    snmp_community   = "public"
    ssh_username     = "admin"
    ssh_password     = "testpass"
    collect_interval = 300
    uplink_ports     = []
    enabled          = True


class CiscoSSHDeviceFactory(DeviceFactory):
    vendor    = "cisco"
    protocol  = "ssh"
    os_family = "cisco_ios"


class CiscoXESSHDeviceFactory(DeviceFactory):
    vendor    = "cisco"
    protocol  = "ssh"
    os_family = "cisco_iosxe"


class HuaweiSSHDeviceFactory(DeviceFactory):
    vendor    = "huawei"
    protocol  = "ssh"
    os_family = "huawei_vrp"


class CiscoSNMPDeviceFactory(DeviceFactory):
    vendor    = "cisco"
    protocol  = "snmp"
    os_family = "cisco_ios"


class HuaweiSNMPDeviceFactory(DeviceFactory):
    vendor         = "huawei"
    protocol       = "snmp"
    os_family      = "huawei_vrp"
    snmp_community = "huawei_ro"


class MikroTikSNMPDeviceFactory(DeviceFactory):
    name             = factory.Sequence(lambda n: f"mt-test-{n:03d}")
    device_type      = "router"
    vendor           = "mikrotik"
    protocol         = "snmp"
    os_family        = "mikrotik_routeros"
    snmp_community   = "public"


class MikroTikSSHDeviceFactory(DeviceFactory):
    name        = factory.Sequence(lambda n: f"mt-ssh-{n:03d}")
    device_type = "router"
    vendor      = "mikrotik"
    protocol    = "ssh"
    os_family   = "mikrotik_routeros"


class MikroTikSwitchSNMPDeviceFactory(DeviceFactory):
    name        = factory.Sequence(lambda n: f"mt-sw-{n:03d}")
    device_type = "switch"
    vendor      = "mikrotik"
    protocol    = "snmp"
    os_family   = "mikrotik_routeros"


class FortinetSNMPDeviceFactory(DeviceFactory):
    name             = factory.Sequence(lambda n: f"fw-test-{n:03d}")
    device_type      = "firewall"
    vendor           = "fortinet"
    protocol         = "snmp"
    os_family        = "fortinet_fortios"
    snmp_community   = "public"


class FortinetSSHDeviceFactory(DeviceFactory):
    name        = factory.Sequence(lambda n: f"fw-ssh-{n:03d}")
    device_type = "firewall"
    vendor      = "fortinet"
    protocol    = "ssh"
    os_family   = "fortinet_fortios"


class HyperVDeviceFactory(DeviceFactory):
    name             = factory.Sequence(lambda n: f"hyperv-test-{n:03d}")
    device_type      = "hyperv"
    vendor           = "microsoft"
    protocol         = "winrm"
    os_family        = "hyperv_winrm"
    ssh_username     = "Administrator"
    ssh_password     = "testpass"
    collect_interval = 120


@pytest.fixture
def hyperv_device(db):
    return HyperVDeviceFactory()


@pytest.fixture
def cisco_ssh_device(db):
    return CiscoSSHDeviceFactory()


@pytest.fixture
def cisco_xe_ssh_device(db):
    return CiscoXESSHDeviceFactory()


@pytest.fixture
def huawei_ssh_device(db):
    return HuaweiSSHDeviceFactory()


@pytest.fixture
def cisco_snmp_device(db):
    return CiscoSNMPDeviceFactory()


@pytest.fixture
def huawei_snmp_device(db):
    return HuaweiSNMPDeviceFactory()


@pytest.fixture
def mikrotik_snmp_device(db):
    return MikroTikSNMPDeviceFactory()


@pytest.fixture
def mikrotik_ssh_device(db):
    return MikroTikSSHDeviceFactory()


@pytest.fixture
def fortinet_snmp_device(db):
    return FortinetSNMPDeviceFactory()


@pytest.fixture
def fortinet_ssh_device(db):
    return FortinetSSHDeviceFactory()
