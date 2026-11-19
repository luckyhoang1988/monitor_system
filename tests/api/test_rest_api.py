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

@pytest.fixture
def admin_client(db):
    from django.contrib.auth.models import Group

    group, _ = Group.objects.get_or_create(name="Network Admins")
    user = User.objects.create_user(username="apiadmin", password="apipass")
    user.groups.add(group)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _device_payload(**overrides):
    payload = {
        "name": "api-created", "ip_address": "10.9.9.9", "device_type": "switch",
        "vendor": "cisco", "os_family": "cisco_ios", "snmp_version": "v2c", "enabled": True,
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
class TestDeviceAPIWrites:
    """Review ngoài 2026-11-19 (F03): `DeviceSerializer` không có `protocol` nên POST thiếu protocol vẫn thành
    công và lưu thiết bị `protocol=""` — thiết bị mà CollectorFactory không biết poll thế nào."""

    def test_create_without_protocol_is_rejected(self, admin_client):
        response = admin_client.post("/api/v1/devices/", _device_payload(), format="json")

        assert response.status_code == 400
        assert "protocol" in response.json()
        assert not Device.objects.filter(name="api-created").exists()

    def test_create_with_protocol_stores_it(self, admin_client):
        response = admin_client.post("/api/v1/devices/", _device_payload(protocol="snmp"), format="json")

        assert response.status_code == 201, response.content
        assert Device.objects.get(name="api-created").protocol == "snmp"
        assert response.json()["protocol"] == "snmp"

    def test_create_with_unknown_protocol_is_rejected(self, admin_client):
        response = admin_client.post("/api/v1/devices/", _device_payload(protocol="carrier-pigeon"), format="json")

        assert response.status_code == 400
        assert "protocol" in response.json()

    def test_snmpv3_is_rejected_when_backend_cannot_do_v3(self, admin_client):
        """F06: pysnmp backend của repo chỉ hỗ trợ v1/v2c — thiết bị v3 lưu được nhưng poll nào cũng lỗi."""
        from unittest.mock import patch

        with patch("apps.collectors.snmp_client.resolve_snmp_backend", return_value="pysnmp"):
            response = admin_client.post(
                "/api/v1/devices/", _device_payload(protocol="snmp", snmp_version="v3"), format="json")

        assert response.status_code == 400
        assert "snmp_version" in response.json()

    def test_snmpv3_is_accepted_when_backend_supports_it(self, admin_client):
        from unittest.mock import patch

        with patch("apps.collectors.snmp_client.resolve_snmp_backend", return_value="easysnmp"):
            response = admin_client.post(
                "/api/v1/devices/", _device_payload(protocol="snmp", snmp_version="v3"), format="json")

        assert response.status_code == 201, response.content


@pytest.mark.django_db
class TestAlertAPIIsReadOnly:
    """F03: `AlertViewSet` là ModelViewSet và `is_active`/`resolved_at`/`message` ghi được — admin PATCH
    `is_active=false` đóng alert mà KHÔNG qua `_resolve_alert` (không RECOVERED, không outbox, không khoá
    Device), hoặc DELETE kéo theo xoá cả lịch sử thông báo. Vòng đời alert chỉ do engine/UI điều khiển."""

    def _alert(self):
        device = CiscoSNMPDeviceFactory()
        rule = AlertRule.objects.create(name="API rule", metric="cpu_percent", severity="critical", threshold=90)
        return Alert.objects.create(device=device, rule=rule, severity="critical", message="CPU", metric_value=95)

    def test_patch_cannot_close_an_alert(self, admin_client):
        alert = self._alert()

        response = admin_client.patch(f"/api/v1/alerts/{alert.pk}/", {"is_active": False}, format="json")

        assert response.status_code == 405
        alert.refresh_from_db()
        assert alert.is_active is True
        assert alert.resolved_at is None

    def test_put_post_delete_are_not_allowed(self, admin_client):
        alert = self._alert()

        assert admin_client.put(f"/api/v1/alerts/{alert.pk}/", {"message": "x"}, format="json").status_code == 405
        assert admin_client.post("/api/v1/alerts/", {"message": "x"}, format="json").status_code == 405
        assert admin_client.delete(f"/api/v1/alerts/{alert.pk}/").status_code == 405
        assert Alert.objects.filter(pk=alert.pk, is_active=True).exists()

    def test_read_still_works_for_every_authenticated_user(self, api_client):
        alert = self._alert()

        listing = api_client.get("/api/v1/alerts/")
        detail = api_client.get(f"/api/v1/alerts/{alert.pk}/")

        assert listing.status_code == 200 and listing.json()["count"] == 1
        assert detail.status_code == 200 and detail.json()["id"] == alert.pk


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
        
    def test_csv_has_exactly_one_bom_at_the_start(self, api_client):
        """`csv.writer` gọi `HttpResponse.write` từng dòng và Django mã hoá MỖI lần bằng `utf-8-sig`, nên BOM bị
        chèn vào đầu từng dòng (4 dòng → 4 BOM): Excel thấy ký tự rác ở ô đầu mỗi dòng, kể cả ô thời gian."""
        from datetime import datetime, timedelta, timezone as tz
        from apps.metrics.models import SystemHealth

        device = CiscoSNMPDeviceFactory()
        for i in range(3):
            SystemHealth.objects.create(device=device, timestamp=datetime.now(tz=tz.utc) - timedelta(minutes=i),
                                        cpu_percent=1, mem_percent=2)

        response = api_client.get("/api/metrics/export/", {
            "device_id": device.pk, "type": "system", "range": "1h", "export_format": "csv"})

        raw = response.content
        assert raw.count(b"\n") == 4
        assert raw.count(b"\xef\xbb\xbf") == 1
        assert raw.startswith(b"\xef\xbb\xbf")

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
