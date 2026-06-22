"""Tests cho ghi metrics WiFi — save_metrics lưu WifiApStats/WifiClientStats."""
import pytest
from datetime import datetime, timezone

from apps.collectors.base import NormalizedData
from apps.metrics.models import SystemHealth, WifiApStats, WifiClientStats
from apps.metrics.writer import save_metrics
from tests.conftest import HuaweiACDeviceFactory


def _data(extra):
    return NormalizedData(
        device_name="ac",
        ip_address="10.0.198.199",
        timestamp=datetime.now(tz=timezone.utc),
        os_family="huawei_vrp",
        cpu_percent=10.0,
        mem_percent=20.0,
        uptime_secs=100,
        interfaces=[],
        extra=extra,
    )


@pytest.mark.django_db
class TestSaveWifiMetrics:
    @pytest.fixture
    def device(self, db):
        return HuaweiACDeviceFactory()

    def test_saves_ap_stats(self, device):
        save_metrics(device, _data({
            "wifi_aps": [
                {"name": "AP1", "mac": "00:11:22:33:44:55", "ip": "10.0.50.11",
                 "group": "G1", "is_online": True, "run_state": "8", "client_count": 5},
                {"name": "AP2", "is_online": False, "run_state": "4", "client_count": 0},
            ],
        }))
        assert WifiApStats.objects.filter(device=device).count() == 2
        ap1 = WifiApStats.objects.get(device=device, ap_name="AP1")
        assert ap1.is_online is True
        assert ap1.client_count == 5

    def test_saves_client_stats(self, device):
        save_metrics(device, _data({
            "wifi_clients": [
                {"mac": "aa:bb:cc:dd:ee:01", "ip": "10.0.60.10", "ssid": "Corp",
                 "ap_name": "AP1", "radio": "5G", "rssi": -60, "online_secs": 1200},
            ],
        }))
        assert WifiClientStats.objects.filter(device=device).count() == 1
        c = WifiClientStats.objects.get(device=device)
        assert c.ssid == "Corp"
        assert c.rssi == -60

    def test_wifi_lists_not_stored_in_system_health_extra(self, device):
        save_metrics(device, _data({
            "wifi_aps": [{"name": "AP1", "is_online": True}],
            "wifi_clients": [{"mac": "aa:bb:cc:dd:ee:01"}],
            "ping_ok": True,
        }))
        sh = SystemHealth.objects.get(device=device)
        assert "wifi_aps" not in sh.extra
        assert "wifi_clients" not in sh.extra
        assert sh.extra.get("ping_ok") is True

    def test_client_with_null_rssi(self, device):
        save_metrics(device, _data({
            "wifi_clients": [{"mac": "aa:bb:cc:dd:ee:02", "rssi": None}],
        }))
        c = WifiClientStats.objects.get(device=device)
        assert c.rssi is None
