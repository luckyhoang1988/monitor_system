"""Tests for Dashboard Views."""
import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from tests.conftest import (
    CiscoSNMPDeviceFactory,
    HyperVDeviceFactory,
    MikroTikSNMPDeviceFactory,
    FortinetSNMPDeviceFactory,
)
from apps.devices.models import Device, Interface
from apps.metrics.models import SystemHealth, InterfaceStats, VMStats
from django.utils import timezone


@pytest.fixture
def logged_in_client(client, db):
    user = User.objects.create_user(username="admin", password="password123")
    client.login(username="admin", password="password123")
    return client


@pytest.mark.django_db
class TestDashboardViews:
    def test_dashboard_index_requires_login(self, client):
        response = client.get(reverse("dashboard:index"))
        assert response.status_code == 302

    def test_dashboard_index_view(self, logged_in_client):
        switch = CiscoSNMPDeviceFactory(name="sw-1", device_type="switch")
        router = MikroTikSNMPDeviceFactory(name="rt-1", device_type="router")
        firewall = FortinetSNMPDeviceFactory(name="fw-1", device_type="firewall")
        hyperv = HyperVDeviceFactory(name="hv-1", device_type="hyperv")

        response = logged_in_client.get(reverse("dashboard:index"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "sw-1" in content
        assert "rt-1" in content
        assert "fw-1" in content
        assert "hv-1" in content

    def test_switch_detail_view(self, logged_in_client):
        switch = CiscoSNMPDeviceFactory(name="sw-1", device_type="switch")
        iface = Interface.objects.create(device=switch, if_index=1, name="Gi0/1")
        
        # Add system health
        SystemHealth.objects.create(
            device=switch, timestamp=timezone.now(), cpu_percent=12.5, mem_percent=45.0
        )
        # Add interface stats
        InterfaceStats.objects.create(
            interface=iface, timestamp=timezone.now(), status="up", in_bytes=1000, out_bytes=2000,
            in_mbps=0.5, out_mbps=1.2
        )

        response = logged_in_client.get(reverse("dashboard:switch_detail", args=[switch.pk]))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "sw-1" in content
        assert "Gi0/1" in content

    def test_router_detail_view(self, logged_in_client):
        router = MikroTikSNMPDeviceFactory(name="rt-1", device_type="router")
        response = logged_in_client.get(reverse("dashboard:router_detail", args=[router.pk]))
        assert response.status_code == 200
        assert "rt-1" in response.content.decode("utf-8")

    def test_firewall_detail_view(self, logged_in_client):
        fw = FortinetSNMPDeviceFactory(name="fw-1", device_type="firewall")
        response = logged_in_client.get(reverse("dashboard:firewall_detail", args=[fw.pk]))
        assert response.status_code == 200
        assert "fw-1" in response.content.decode("utf-8")

    def test_hyperv_detail_view(self, logged_in_client):
        hv = HyperVDeviceFactory(name="hv-1", device_type="hyperv")
        
        # Add system health
        SystemHealth.objects.create(
            device=hv, timestamp=timezone.now(), cpu_percent=25.0, mem_percent=60.0
        )
        # Add VM stats
        VMStats.objects.create(
            device=hv, timestamp=timezone.now(), vm_name="VM-01", state="Running",
            cpu_percent=5.0, mem_assigned_mb=2048, repl_health="Normal"
        )

        response = logged_in_client.get(reverse("dashboard:hyperv_detail", args=[hv.pk]))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "hv-1" in content
        assert "VM-01" in content
