"""Tests for Device Views."""
import pytest
from django.urls import reverse
from unittest.mock import patch
from tests.conftest import CiscoSNMPDeviceFactory
from apps.devices.models import Device


@pytest.mark.django_db
class TestDeviceViews:
    def test_device_list_requires_login(self, client):
        response = client.get(reverse("devices:list"))
        assert response.status_code == 302  # redirects to login

    def test_device_list_view(self, logged_in_client):
        device = CiscoSNMPDeviceFactory()
        response = logged_in_client.get(reverse("devices:list"))
        assert response.status_code == 200
        assert device.name in response.content.decode("utf-8")

    def test_device_list_sort_by_name(self, logged_in_client):
        CiscoSNMPDeviceFactory(name="Z-Switch", ip_address="10.0.0.2")
        CiscoSNMPDeviceFactory(name="A-Switch", ip_address="10.0.0.1")
        response = logged_in_client.get(reverse("devices:list"), {"sort": "name", "dir": "asc"})
        content = response.content.decode("utf-8")
        assert content.index("A-Switch") < content.index("Z-Switch")

    def test_device_list_sort_by_ip(self, logged_in_client):
        CiscoSNMPDeviceFactory(name="Device-B", ip_address="10.0.0.10")
        CiscoSNMPDeviceFactory(name="Device-A", ip_address="10.0.0.2")
        response = logged_in_client.get(reverse("devices:list"), {"sort": "ip", "dir": "asc"})
        content = response.content.decode("utf-8")
        assert content.index("10.0.0.2") < content.index("10.0.0.10")

    def test_device_list_filter_by_type(self, logged_in_client):
        from tests.conftest import HyperVDeviceFactory

        CiscoSNMPDeviceFactory(name="SW-Only", device_type="switch")
        HyperVDeviceFactory(name="HV-Only", device_type="hyperv")
        response = logged_in_client.get(reverse("devices:list"), {"type": "switch"})
        content = response.content.decode("utf-8")
        assert "SW-Only" in content
        assert "HV-Only" not in content

    def test_device_list_invalid_type_filter_ignored(self, logged_in_client):
        device = CiscoSNMPDeviceFactory(name="Keep-Me")
        response = logged_in_client.get(reverse("devices:list"), {"type": "invalid"})
        content = response.content.decode("utf-8")
        assert response.status_code == 200
        assert device.name in content

    def test_device_add_view_get(self, logged_in_client):
        response = logged_in_client.get(reverse("devices:add"))
        assert response.status_code == 200
        assert "Thêm thiết bị" in response.content.decode("utf-8")

    def test_device_add_view_post(self, logged_in_client):
        data = {
            "name": "New-Switch",
            "device_type": "switch",
            "ip_address": "192.168.1.10",
            "vendor": "cisco",
            "protocol": "snmp",
            "snmp_version": "v2c",
            "snmp_community": "public",
            "collect_interval": 300,
            "uplink_ports": "Gi0/1",
            "enabled": True,
        }
        response = logged_in_client.post(reverse("devices:add"), data=data)
        assert response.status_code == 302  # redirects to list
        assert Device.objects.filter(name="New-Switch").exists()

    def test_device_edit_view(self, logged_in_client):
        device = CiscoSNMPDeviceFactory()
        response = logged_in_client.get(reverse("devices:edit", args=[device.pk]))
        assert response.status_code == 200
        assert f"Sửa: {device.name}" in response.content.decode("utf-8")

        # Edit post
        data = {
            "name": "Updated-Name",
            "device_type": device.device_type,
            "ip_address": device.ip_address,
            "vendor": device.vendor,
            "protocol": device.protocol,
            "snmp_version": device.snmp_version,
            "snmp_community": device.snmp_community,
            "collect_interval": 120,
            "uplink_ports": "Gi0/1",
            "enabled": True,
        }
        response = logged_in_client.post(reverse("devices:edit", args=[device.pk]), data=data)
        assert response.status_code == 302
        device.refresh_from_db()
        assert device.name == "Updated-Name"
        assert device.collect_interval == 120

    def test_device_delete_view(self, logged_in_client):
        device = CiscoSNMPDeviceFactory()
        response = logged_in_client.get(reverse("devices:delete", args=[device.pk]))
        assert response.status_code == 200
        assert device.name in response.content.decode("utf-8")

        # Confirm delete
        response = logged_in_client.post(reverse("devices:delete", args=[device.pk]))
        assert response.status_code == 302
        assert not Device.objects.filter(pk=device.pk).exists()

    @patch("apps.collectors.factory.CollectorFactory.create")
    def test_device_test_connection_rejects_get(self, mock_create, logged_in_client):
        device = CiscoSNMPDeviceFactory()
        response = logged_in_client.get(reverse("devices:test", args=[device.pk]))
        assert response.status_code == 405
        mock_create.assert_not_called()

    @patch("apps.collectors.factory.CollectorFactory.create")
    def test_device_test_connection_success(self, mock_create, logged_in_client):
        device = CiscoSNMPDeviceFactory()
        mock_collector = mock_create.return_value
        mock_collector.test_connection.return_value = "cisco_ios"

        response = logged_in_client.post(reverse("devices:test", args=[device.pk]))
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["os_family"] == "cisco_ios"
        assert "Kết nối OK" in data["message"]

    @patch("apps.collectors.factory.CollectorFactory.create")
    def test_device_test_connection_fail(self, mock_create, logged_in_client):
        device = CiscoSNMPDeviceFactory()
        mock_collector = mock_create.return_value
        mock_collector.test_connection.side_effect = Exception("SNMP Timeout")

        response = logged_in_client.post(reverse("devices:test", args=[device.pk]))
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "SNMP Timeout" in data["message"]
