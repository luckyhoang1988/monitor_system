"""Tests cho REST API (Devices, Alerts, Export)."""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient
from django.contrib.auth.models import User
from apps.devices.models import Device
from apps.alerts.models import Alert, AlertRule
from tests.conftest import CiscoSNMPDeviceFactory

@pytest.fixture
def api_client(db):
    user = User.objects.create_user(username="apiuser", password="apipass")
    client = APIClient()
    client.force_authenticate(user=user)
    return client

@pytest.mark.django_db
class TestDeviceAPI:
    def test_list_devices(self, api_client):
        CiscoSNMPDeviceFactory(name="Switch-1")
        CiscoSNMPDeviceFactory(name="Switch-2")
        
        response = api_client.get("/api/v1/devices/")
        assert response.status_code == 200
        data = response.json()
        
        # Test pagination
        assert "count" in data
        assert "results" in data
        assert len(data["results"]) == 2
        assert data["results"][0]["name"] == "Switch-1"

    def test_filter_devices_by_vendor(self, api_client):
        CiscoSNMPDeviceFactory(name="Cisco-SW", vendor="cisco")
        CiscoSNMPDeviceFactory(name="Juniper-SW", vendor="juniper")
        
        response = api_client.get("/api/v1/devices/?vendor=cisco")
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["name"] == "Cisco-SW"

@pytest.mark.django_db
class TestAlertAPI:
    def test_list_alerts(self, api_client):
        device = CiscoSNMPDeviceFactory()
        rule = AlertRule.objects.create(name="High CPU", metric="cpu", severity="critical", threshold=90)
        Alert.objects.create(device=device, rule=rule, severity="critical", message="CPU too high", metric_value=95)
        
        response = api_client.get("/api/v1/alerts/")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["severity"] == "critical"
        assert data["results"][0]["device_name"] == device.name

@pytest.mark.django_db
class TestExportAPI:
    def test_export_system_csv(self, api_client):
        device = CiscoSNMPDeviceFactory()
        response = api_client.get("/api/metrics/export/", {
            "device_id": device.pk,
            "type": "system",
            "range": "1h",
            "export_format": "csv"
        })
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv; charset=utf-8-sig"
        assert "attachment; filename=" in response["Content-Disposition"]
        
    def test_export_interface_excel(self, api_client):
        device = CiscoSNMPDeviceFactory()
        response = api_client.get("/api/metrics/export/", {
            "device_id": device.pk,
            "type": "interface",
            "range": "1h",
            "export_format": "excel"
        })
        assert response.status_code == 200
        assert "spreadsheetml.sheet" in response["Content-Type"]
        
    def test_export_missing_device(self, api_client):
        response = api_client.get("/api/metrics/export/?type=system")
        assert response.status_code == 400
