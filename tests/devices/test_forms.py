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

    def test_snmpv3_form_is_rejected_when_backend_cannot_do_v3(self):
        """Review ngoài 2026-11-19 (F06): backend pysnmp của repo chỉ hỗ trợ v1/v2c
        (`create_snmp_session` ném RuntimeError với v3, Docker không cài easysnmp). Form để lưu thiết bị v3 thì
        mọi lần poll đều lỗi — chặn ngay lúc lưu và nói rõ lý do."""
        from unittest.mock import patch

        data = {
            "name": "SW-V3-NOBACKEND", "device_type": "switch", "ip_address": "192.168.1.12",
            "vendor": "cisco", "protocol": "snmp", "snmp_version": "v3", "snmpv3_username": "snmpuser",
            "snmpv3_auth_protocol": "sha", "snmpv3_auth_password": "auth-secret",
            "collect_interval": 300, "enabled": True,
        }
        with patch("apps.collectors.snmp_client.resolve_snmp_backend", return_value="pysnmp"):
            form = DeviceForm(data=data)
            assert not form.is_valid()

        assert "snmp_version" in form.errors
        assert "easysnmp" in form.errors["snmp_version"][0]

    def test_snmp_v2c_form_is_unaffected_by_backend_choice(self):
        from unittest.mock import patch

        data = {
            "name": "SW-V2C", "device_type": "switch", "ip_address": "192.168.1.13", "vendor": "cisco",
            "protocol": "snmp", "snmp_version": "v2c", "snmp_community": "public",
            "collect_interval": 300, "enabled": True,
        }
        with patch("apps.collectors.snmp_client.resolve_snmp_backend", return_value="pysnmp"):
            assert DeviceForm(data=data).is_valid()

    def test_valid_snmpv3_device_form(self, mocker):
        # v3 chỉ hợp lệ khi backend thật sự làm được v3 (easysnmp).
        mocker.patch("apps.collectors.snmp_client.resolve_snmp_backend", return_value="easysnmp")
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
