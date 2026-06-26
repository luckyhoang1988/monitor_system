"""Tests cho topology dashboard views + API."""
import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from apps.devices.models import TopologyLink
from apps.metrics.models import WifiApStats
from tests.conftest import CiscoSNMPDeviceFactory, DeviceFactory


@pytest.fixture
def logged_in_client(client, db):
    user = User.objects.create_user(username="topo_user", password="password123")
    client.login(username="topo_user", password="password123")
    return client


@pytest.mark.django_db
class TestTopologyViews:
    def test_topology_requires_login(self, client):
        response = client.get(reverse("dashboard:topology"))
        assert response.status_code == 302

    def test_topology_page_renders(self, logged_in_client):
        response = logged_in_client.get(reverse("dashboard:topology"))
        assert response.status_code == 200
        assert "topology-cy" in response.content.decode("utf-8")

    def test_topology_data_requires_login(self, client):
        response = client.get(reverse("dashboard:topology_data"))
        assert response.status_code == 302

    def test_topology_data_json_schema(self, logged_in_client):
        ac = DeviceFactory(device_type="wlan_controller", name="ACL_Wlan")
        sw = CiscoSNMPDeviceFactory(name="SW-ACCESS")
        ts = timezone.now()
        WifiApStats.objects.create(
            device=ac, timestamp=ts, ap_name="AP-OFFLINE",
            ap_mac="0c:84:08:59:80:c0", ap_ip="10.0.198.103",
            is_online=False, client_count=0,
        )
        TopologyLink.objects.create(
            local_device=sw,
            local_port="GigabitEthernet0/0/12",
            remote_ap_mac="0c:84:08:59:80:c0",
            remote_ap_name="AP-OFFLINE",
            match_method="mac",
            is_confirmed=True,
        )

        response = logged_in_client.get(
            reverse("dashboard:topology_data") + f"?ac={ac.pk}"
        )
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert "edges" in data
        assert "meta" in data
        assert data["meta"]["ap_total"] == 1
        assert data["meta"]["ap_offline"] == 1
        assert data["meta"]["ap_mapped"] == 1

        ap_nodes = [n for n in data["nodes"] if n["data"].get("type") == "ap"]
        assert len(ap_nodes) == 1
        assert ap_nodes[0]["data"]["online"] is False
        assert ap_nodes[0]["data"]["switch_name"] == "SW-ACCESS"

        sw_nodes = [n for n in data["nodes"] if n["data"].get("type") == "switch"]
        assert len(sw_nodes) == 1
        assert len(data["edges"]) == 1

    def test_topology_orphan_ap(self, logged_in_client):
        ac = DeviceFactory(device_type="wlan_controller", name="AC-ORPHAN")
        ts = timezone.now()
        WifiApStats.objects.create(
            device=ac, timestamp=ts, ap_name="AP-NO-LINK",
            ap_mac="11:22:33:44:55:66", is_online=True, client_count=2,
        )
        response = logged_in_client.get(reverse("dashboard:topology_data"))
        data = response.json()
        assert data["meta"]["ap_unmapped"] == 1
        orphan = [n for n in data["nodes"] if n["data"].get("orphan")]
        assert len(orphan) == 1
