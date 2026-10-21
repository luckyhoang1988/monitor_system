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
        assert "parent" not in ap_nodes[0]["data"]
        ap_nid = ap_nodes[0]["data"]["id"]

        sw_nodes = [n for n in data["nodes"] if n["data"].get("type") == "switch"]
        assert len(sw_nodes) == 1

        # AP nối switch bằng edge (thay cho compound parent)
        ap_edges = [e for e in data["edges"] if e["data"].get("type") == "ap"]
        assert len(ap_edges) == 1
        assert ap_edges[0]["data"]["source"] == f"sw-{sw.pk}"
        assert ap_edges[0]["data"]["target"] == ap_nid

    def test_topology_hierarchy_core_to_access(self, logged_in_client):
        ac = DeviceFactory(device_type="wlan_controller", name="ACL_Wlan")
        core = CiscoSNMPDeviceFactory(name="PFVN-CORE-SW")
        sw = CiscoSNMPDeviceFactory(name="X1_SW2_Access")
        ts = timezone.now()
        WifiApStats.objects.create(
            device=ac, timestamp=ts, ap_name="AP-01",
            ap_mac="0c:84:08:59:80:c1", is_online=True, client_count=1,
        )
        TopologyLink.objects.create(
            local_device=sw,
            local_port="GE0/0/12",
            link_kind="ap",
            remote_ap_mac="0c:84:08:59:80:c1",
            remote_ap_name="AP-01",
            match_method="mac",
            is_confirmed=True,
        )

        response = logged_in_client.get(reverse("dashboard:topology_data"))
        data = response.json()
        core_nodes = [n for n in data["nodes"] if n["data"].get("type") == "core"]
        assert len(core_nodes) == 1
        assert core_nodes[0]["data"]["id"] == f"sw-{core.pk}"

        uplinks = [e for e in data["edges"] if e["data"].get("type") == "uplink"]
        assert len(uplinks) == 1
        assert uplinks[0]["data"]["source"] == f"sw-{core.pk}"
        assert uplinks[0]["data"]["target"] == f"sw-{sw.pk}"
        assert uplinks[0]["data"]["inferred"] is True

    def test_ap_link_on_core_does_not_steal_ap_from_access_switch(self, logged_in_client):
        """Link AP trên CORE không vẽ; nó cũng không được chiếm quyền sở hữu AP.

        CORE xếp đầu theo tên nên nếu link của nó vào ap_owner thì AP bị gán cho CORE rồi không được
        vẽ → AP biến mất khỏi switch access thật và rơi vào "Chưa map".
        """
        ac = DeviceFactory(device_type="wlan_controller", name="AC")
        core = CiscoSNMPDeviceFactory(name="CORE")
        access = CiscoSNMPDeviceFactory(name="X1_SW1_Access")
        mac = "aa:bb:cc:dd:ee:01"
        WifiApStats.objects.create(
            device=ac, timestamp=timezone.now(), ap_name="AP-1", ap_mac=mac,
            is_online=True, client_count=3,
        )
        for dev, port in ((core, "Gi0/0/9"), (access, "Gi0/0/3")):
            TopologyLink.objects.create(
                local_device=dev, local_port=port, link_kind="ap", remote_ap_mac=mac,
                remote_ap_name="AP-1", match_method="mac", is_confirmed=True,
            )

        data = logged_in_client.get(reverse("dashboard:topology_data") + f"?ac={ac.pk}").json()

        ap_nodes = [n["data"] for n in data["nodes"] if n["data"].get("type") == "ap"]
        assert [(n["switch_name"], n.get("orphan")) for n in ap_nodes] == [("X1_SW1_Access", None)]
        assert data["meta"]["ap_mapped"] == 1
        assert data["meta"]["ap_unmapped"] == 0
        core_node = next(n["data"] for n in data["nodes"] if n["data"]["type"] == "core")
        assert core_node["ap_count"] == 0

    def test_ap_linked_only_to_core_stays_unmapped(self, logged_in_client):
        """Hành vi giữ nguyên: AP chỉ có link trên CORE vẫn không vẽ dưới CORE (hiện ở Chưa map)."""
        ac = DeviceFactory(device_type="wlan_controller", name="AC")
        core = CiscoSNMPDeviceFactory(name="CORE")
        mac = "aa:bb:cc:dd:ee:02"
        WifiApStats.objects.create(
            device=ac, timestamp=timezone.now(), ap_name="AP-2", ap_mac=mac, is_online=True,
        )
        TopologyLink.objects.create(
            local_device=core, local_port="Gi0/0/9", link_kind="ap", remote_ap_mac=mac,
            remote_ap_name="AP-2", match_method="mac", is_confirmed=True,
        )
        data = logged_in_client.get(reverse("dashboard:topology_data") + f"?ac={ac.pk}").json()
        assert data["meta"]["ap_unmapped"] == 1
        assert not [e for e in data["edges"] if e["data"].get("source") == f"sw-{core.pk}"
                    and e["data"].get("type") == "ap"]

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
        assert "parent" not in orphan[0]["data"]
        orphan_edges = [
            e for e in data["edges"] if e["data"].get("source") == "group-orphan"
        ]
        assert len(orphan_edges) == 1
        assert orphan_edges[0]["data"]["target"] == orphan[0]["data"]["id"]
