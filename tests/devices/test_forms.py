"""Tests for DeviceForm."""
import pytest
from apps.devices.forms import DeviceForm


@pytest.mark.django_db
class TestDeviceForm:
    def test_valid_device_form(self):
        data = {
            "name": "Switch-Core",
            "device_type": "switch",
            "ip_address": "192.168.1.1",
            "vendor": "cisco",
            "protocol": "snmp",
            "snmp_version": "v2c",
            "snmp_community": "public",
            "collect_interval": 300,
            "uplink_ports": "Gi0/1, Gi0/2",
            "enabled": True,
        }
        form = DeviceForm(data=data)
        assert form.is_valid()
        device = form.save()
        assert device.name == "Switch-Core"
        assert device.uplink_ports == ["Gi0/1", "Gi0/2"]

    def test_clean_uplink_ports_empty(self):
        data = {
            "name": "Switch-Core",
            "device_type": "switch",
            "ip_address": "192.168.1.1",
            "vendor": "cisco",
            "protocol": "snmp",
            "snmp_version": "v2c",
            "snmp_community": "public",
            "collect_interval": 300,
            "uplink_ports": "",
            "enabled": True,
        }
        form = DeviceForm(data=data)
        assert form.is_valid()
        device = form.save()
        assert device.uplink_ports == []

    def test_invalid_device_form_missing_required(self):
        data = {
            "name": "",  # missing required name
            "device_type": "switch",
            "ip_address": "192.168.1.1",
            "vendor": "cisco",
            "protocol": "snmp",
        }
        form = DeviceForm(data=data)
        assert not form.is_valid()
        assert "name" in form.errors

    def test_valid_snmpv3_device_form(self):
        data = {
            "name": "SW-SNMPV3",
            "device_type": "switch",
            "ip_address": "192.168.1.10",
            "vendor": "cisco",
            "protocol": "snmp",
            "snmp_version": "v3",
            "snmpv3_username": "snmpuser",
            "snmpv3_auth_protocol": "sha",
            "snmpv3_auth_password": "auth-secret",
            "snmpv3_priv_protocol": "aes",
            "snmpv3_priv_password": "priv-secret",
            "collect_interval": 300,
            "enabled": True,
        }
        form = DeviceForm(data=data)
        assert form.is_valid()
        device = form.save()
        assert device.snmp_version == "v3"
        assert device.snmpv3_username == "snmpuser"

    def test_invalid_snmpv3_requires_username(self):
        data = {
            "name": "SW-SNMPV3-INVALID",
            "device_type": "switch",
            "ip_address": "192.168.1.11",
            "vendor": "cisco",
            "protocol": "snmp",
            "snmp_version": "v3",
            "snmpv3_auth_protocol": "sha",
            "snmpv3_auth_password": "auth-secret",
            "collect_interval": 300,
            "enabled": True,
        }
        form = DeviceForm(data=data)
        assert not form.is_valid()
        assert "snmpv3_username" in form.errors
