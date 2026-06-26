"""Tests cho discover_topology_links task."""
import pytest
from unittest.mock import patch, MagicMock

from apps.collectors.topology_lldp import NeighborRecord
from apps.collectors.tasks import discover_topology_links
from apps.devices.models import TopologyLink
from tests.conftest import CiscoSNMPDeviceFactory, DeviceFactory


@pytest.mark.django_db
class TestDiscoverTopologyLinks:
    @patch("apps.collectors.topology_writer.discover_all_switches")
    def test_task_calls_discovery(self, mock_discover):
        mock_discover.return_value = {
            "switches": 3, "links": 10, "confirmed": 8, "errors": 0,
        }
        discover_topology_links.run()
        mock_discover.assert_called_once()

    @patch("apps.collectors.topology_writer.collect_lldp_neighbors")
    def test_upsert_creates_link(self, mock_collect):
        from apps.collectors.topology_writer import upsert_switch_topology

        ac = DeviceFactory(device_type="wlan_controller")
        sw = CiscoSNMPDeviceFactory()
        mock_collect.return_value = [
            NeighborRecord(
                local_port="Gi0/0/1", local_port_num=1,
                remote_sys_name="AP-TEST",
                remote_mac="aa:bb:cc:dd:ee:01",
                is_ap_candidate=True,
            ),
        ]
        n, c = upsert_switch_topology(sw, ac)
        assert n == 1
        assert TopologyLink.objects.filter(local_device=sw).count() == 1
        link = TopologyLink.objects.get(local_device=sw, local_port="Gi0/0/1")
        assert link.remote_ap_mac == "aa:bb:cc:dd:ee:01"
