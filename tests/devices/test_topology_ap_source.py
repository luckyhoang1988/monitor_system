"""Nguồn dữ liệu AP của topology phải theo METRICS_WRITE_MODE.

Cache-mode không còn ghi WifiApStats xuống DB nên đọc thẳng bảng đó trả snapshot đóng băng (prod
2026-10-21: lần ghi cuối 2026-07-02, DB 22 AP trong khi AC thật 23 → AP mới không bao giờ được map,
AP offline vẫn hiện online). Mọi chỗ của topology đi qua `load_ac_ap_rows`.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from apps.collectors.topology_lldp import NeighborRecord
from apps.devices.models import TopologyLink
from apps.devices.topology_match import (
    load_ac_ap_rows,
    load_ac_ap_snapshot,
    match_lldp_to_ap,
)
from apps.metrics.models import WifiApStats
from tests.conftest import CiscoSNMPDeviceFactory, DeviceFactory

pytestmark = pytest.mark.django_db

MAC_OLD = "0c:84:08:59:80:c0"   # AP có trong DB đóng băng lẫn Redis
MAC_NEW = "0c:84:08:59:82:c0"   # AP thêm sau khi bật cache-mode: chỉ có trong Redis

CACHE_GET = "apps.metrics.cache.get_latest"


def _redis_ap(name, mac, online=True, clients=0, ip=""):
    return {
        "name": name, "mac": mac, "ip": ip, "group": "g", "is_online": online,
        "run_state": "8" if online else "1", "client_count": clients,
    }


@pytest.fixture
def ac():
    return DeviceFactory(device_type="wlan_controller", name="ACL_Wlan")


@pytest.fixture
def frozen_db(ac):
    """Snapshot WifiApStats đóng băng 111 ngày trước: chỉ AP-OLD, đang online."""
    WifiApStats.objects.create(
        device=ac, timestamp=timezone.now() - timedelta(days=111), ap_name="AP-OLD",
        ap_mac=MAC_OLD, ap_ip="10.0.0.1", is_online=True, client_count=9,
    )


@pytest.fixture
def cache_mode(settings):
    settings.METRICS_WRITE_MODE = "cache"


class TestLoadRowsFollowsWriteMode:
    def test_cache_mode_reads_redis_snapshot_not_frozen_db(self, ac, frozen_db, cache_mode):
        snap = {"wifi_aps": [
            _redis_ap("AP-OLD", MAC_OLD, online=False, clients=0),
            _redis_ap("AP-NEW", MAC_NEW, online=True, clients=4),
        ]}
        with patch(CACHE_GET, return_value=snap):
            rows = {r["ap_mac"]: r for r in load_ac_ap_rows(ac)}
        assert set(rows) == {MAC_OLD, MAC_NEW}
        assert rows[MAC_OLD]["is_online"] is False        # DB đóng băng nói online
        assert rows[MAC_NEW]["client_count"] == 4

    def test_cache_mode_without_snapshot_falls_back_to_db(self, ac, frozen_db, cache_mode, caplog):
        with patch(CACHE_GET, return_value=None), caplog.at_level("WARNING"):
            rows = load_ac_ap_rows(ac)
        assert [r["ap_mac"] for r in rows] == [MAC_OLD]
        assert "chưa có snapshot AP" in caplog.text

    def test_cache_snapshot_with_zero_aps_is_real_not_a_miss(self, ac, frozen_db, cache_mode):
        """AC báo 0 AP là dữ liệu thật — không được 'vá' bằng DB đóng băng."""
        with patch(CACHE_GET, return_value={"wifi_aps": []}):
            assert load_ac_ap_rows(ac) == []

    def test_db_mode_reads_db_and_never_touches_redis(self, ac, frozen_db, settings):
        settings.METRICS_WRITE_MODE = "db"
        with patch(CACHE_GET, side_effect=AssertionError("db-mode không được đọc Redis")):
            rows = load_ac_ap_rows(ac)
        assert [r["ap_mac"] for r in rows] == [MAC_OLD]

    def test_no_ac_returns_empty(self, cache_mode):
        assert load_ac_ap_rows(None) == []

    def test_mac_is_normalized_and_bad_counts_are_zero(self, ac, cache_mode):
        snap = {"wifi_aps": [
            {"name": "AP-X", "mac": "0C-84-08-59-82-C0", "is_online": 1, "client_count": "n/a"},
        ]}
        with patch(CACHE_GET, return_value=snap):
            (row,) = load_ac_ap_rows(ac)
        assert row["ap_mac"] == MAC_NEW
        assert row["client_count"] == 0
        assert row["is_online"] is True

    def test_snapshot_helper_uses_cache(self, ac, frozen_db, cache_mode):
        snap = {"wifi_aps": [_redis_ap("AP-NEW", MAC_NEW)]}
        with patch(CACHE_GET, return_value=snap):
            assert set(load_ac_ap_snapshot(ac)) == {MAC_NEW}


class TestLldpMatchUsesCurrentAps:
    def test_ap_known_only_in_redis_is_confirmed_by_mac(self, ac, frozen_db, cache_mode):
        snap = {"wifi_aps": [_redis_ap("AP-NEW", MAC_NEW, ip="10.0.0.9")]}
        neighbor = NeighborRecord(
            local_port="Gi0/0/3", local_port_num=3, remote_sys_name="", remote_mac=MAC_NEW,
        )
        with patch(CACHE_GET, return_value=snap):
            result = match_lldp_to_ap(neighbor, ac)
        assert result.is_confirmed is True
        assert result.match_method == "mac"
        assert result.ap_name == "AP-NEW"
        assert result.ap_ip == "10.0.0.9"


class TestGraphAndApisInCacheMode:
    @pytest.fixture
    def client_in(self, client):
        User.objects.create_user(username="u", password="p")
        client.login(username="u", password="p")
        return client

    def test_graph_shows_new_ap_and_current_offline_status(self, ac, frozen_db, cache_mode, client_in):
        sw = CiscoSNMPDeviceFactory(name="X1_SW1_Access")
        TopologyLink.objects.create(
            local_device=sw, local_port="Gi0/0/3", link_kind="ap", remote_ap_mac=MAC_OLD,
            remote_ap_name="AP-OLD", match_method="mac", is_confirmed=True,
        )
        snap = {"wifi_aps": [
            _redis_ap("AP-OLD", MAC_OLD, online=False),
            _redis_ap("AP-NEW", MAC_NEW, online=True),
        ]}
        with patch(CACHE_GET, return_value=snap):
            data = client_in.get(reverse("dashboard:topology_data") + f"?ac={ac.pk}").json()

        meta = data["meta"]
        assert meta["ap_total"] == 2                      # DB đóng băng nói 1
        assert meta["ap_offline"] == 1                    # DB đóng băng nói 0
        assert meta["ap_unmapped"] == 1
        by_mac = {n["data"].get("mac"): n["data"] for n in data["nodes"] if n["data"].get("type") == "ap"}
        assert by_mac[MAC_OLD]["online"] is False
        assert by_mac[MAC_OLD]["switch_name"] == "X1_SW1_Access"
        assert by_mac[MAC_NEW]["orphan"] is True          # AP mới hiện trong "Chưa map"

    def test_manual_link_ap_picker_lists_new_ap_as_unmapped(self, ac, frozen_db, cache_mode, client_in):
        snap = {"wifi_aps": [_redis_ap("AP-OLD", MAC_OLD), _redis_ap("AP-NEW", MAC_NEW)]}
        with patch(CACHE_GET, return_value=snap):
            data = client_in.get(reverse("dashboard:topology_aps") + f"?ac={ac.pk}").json()
        by_mac = {a["mac"]: a for a in data["aps"]}
        assert set(by_mac) == {MAC_OLD, MAC_NEW}
        assert by_mac[MAC_NEW]["mapped"] is False
