"""Tests for Dashboard Views."""
import pytest
from datetime import timedelta
from django.contrib.auth.models import User
from django.urls import reverse
from tests.conftest import (
    CiscoSNMPDeviceFactory,
    HyperVDeviceFactory,
    MikroTikSNMPDeviceFactory,
    FortinetSNMPDeviceFactory,
)
from apps.devices.models import Device, Interface
from apps.metrics.models import SystemHealth, InterfaceStats, VMStats, WifiApStats
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

    def test_dashboard_index_shows_per_type_stats(self, logged_in_client):
        CiscoSNMPDeviceFactory.create_batch(3, device_type="switch")
        MikroTikSNMPDeviceFactory.create_batch(2, device_type="router")
        FortinetSNMPDeviceFactory(name="fw-only", device_type="firewall")

        response = logged_in_client.get(reverse("dashboard:index"))
        assert response.status_code == 200
        stats = {s["type"]: s for s in response.context["device_type_stats"]}
        assert stats["switch"]["total"] == 3
        assert stats["router"]["total"] == 2
        assert stats["firewall"]["total"] == 1
        assert stats["hyperv"]["total"] == 0

    def test_dashboard_index_shows_all_device_panels(self, logged_in_client):
        response = logged_in_client.get(reverse("dashboard:index"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Switch" in content
        assert "Router" in content
        assert "Firewall" in content
        assert "NAS" in content
        assert "HyperV" in content
        assert "panel-count" in content
        assert "dashboard-panels" in content
        assert "grid-template-columns: repeat(2" in content

    def test_dashboard_index_shows_offline_notice_with_group_per_line(self, logged_in_client):
        online_sw = CiscoSNMPDeviceFactory(name="sw-online", device_type="switch")
        online_sw.last_seen = timezone.now() - timedelta(seconds=30)
        online_sw.save(update_fields=["last_seen"])
        CiscoSNMPDeviceFactory(name="sw-offline", device_type="switch")
        HyperVDeviceFactory(name="hv-offline", device_type="hyperv")

        response = logged_in_client.get(reverse("dashboard:index"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Thiết bị đang Offline" in content
        assert "sw-offline" in content
        assert "hv-offline" in content
        assert "Switch" in content
        assert "HyperV" in content

    def test_alerts_summary_includes_ap_offline_stats_and_offline_notice_html(self, logged_in_client):
        from tests.conftest import HuaweiACDeviceFactory
        from django.urls import reverse

        ac = HuaweiACDeviceFactory(name="ac-1")
        ts = timezone.now()
        WifiApStats.objects.create(
            device=ac, timestamp=ts,
            ap_name="AP-ONLINE", ap_mac="aa:bb:cc:dd:ee:01", ap_ip="10.0.0.11",
            ap_group="G1", is_online=True, run_state="8", client_count=1,
        )
        WifiApStats.objects.create(
            device=ac, timestamp=ts,
            ap_name="AP-OFFLINE", ap_mac="aa:bb:cc:dd:ee:02", ap_ip="10.0.0.12",
            ap_group="G1", is_online=False, run_state="2", client_count=0,
        )

        response = logged_in_client.get(reverse("dashboard:alerts_summary"))
        assert response.status_code == 200
        data = response.json()

        ap_stat = next(s for s in data["stats"] if s["type"] == "ap")
        assert ap_stat["total"] == 2
        assert ap_stat["online"] == 1
        assert ap_stat["offline"] == 1
        # Factory mặc định last_seen=None → bản thân AC được tính offline như 1 Device.
        assert data["offline_count"] == 2

        assert "Thiết bị đang Offline" in data["offline_notice_html"]
        assert "AP-OFFLINE" in data["offline_notice_html"]
        assert "Access Point" in data["offline_notice_html"]
        assert "ac-1" in data["offline_notice_html"]

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
