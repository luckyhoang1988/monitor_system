"""Tests cho topology_match — ghép LLDP neighbor với WifiApStats."""
import pytest
from django.utils import timezone

from apps.collectors.topology_lldp import NeighborRecord
from apps.devices.topology_match import match_lldp_to_ap
from apps.metrics.models import WifiApStats
from tests.conftest import DeviceFactory


@pytest.mark.django_db
class TestMatchLldpToAp:
    def test_match_by_mac(self):
        ac = DeviceFactory(device_type="wlan_controller", name="ACL_Wlan")
        ts = timezone.now()
        WifiApStats.objects.create(
            device=ac, timestamp=ts, ap_name="AP-TEST",
            ap_mac="0c:84:08:59:80:c0", ap_ip="10.0.198.103",
            is_online=True, client_count=5,
        )
        neighbor = NeighborRecord(
            local_port="Gi0/0/12", local_port_num=12,
            remote_sys_name="AP-TEST",
            remote_mac="0c:84:08:59:80:c0",
        )
        result = match_lldp_to_ap(neighbor, ac)
        assert result.is_confirmed is True
        assert result.match_method == "mac"
        assert result.ap_name == "AP-TEST"

    def test_match_by_name_fallback(self):
        ac = DeviceFactory(device_type="wlan_controller", name="AC2")
        ts = timezone.now()
        WifiApStats.objects.create(
            device=ac, timestamp=ts, ap_name="AP_QC_X1",
            ap_mac="", ap_ip="", is_online=False, client_count=0,
        )
        neighbor = NeighborRecord(
            local_port="Gi0/0/5", local_port_num=5,
            remote_sys_name="AP_QC_X1", remote_mac="",
        )
        result = match_lldp_to_ap(neighbor, ac)
        assert result.is_confirmed is True
        assert result.match_method == "name"

    def test_no_ac_returns_unconfirmed(self):
        neighbor = NeighborRecord(
            local_port="Gi0/0/1", local_port_num=1,
            remote_sys_name="AP-X", remote_mac="aa:bb:cc:dd:ee:ff",
        )
        result = match_lldp_to_ap(neighbor, None)
        assert result.is_confirmed is False
        assert result.match_method == "lldp"
