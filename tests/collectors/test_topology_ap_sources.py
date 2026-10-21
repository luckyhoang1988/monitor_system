"""upsert_switch_topology: LLDP + FDB chạy cùng nhau, stale theo nguồn ĐÃ chạy.

Lỗi cũ: FDB chỉ chạy khi LLDP không thấy AP nào, và pha stale lọc theo `protocol_used` — nên
 (a) link LLDP cũ không bao giờ stale khi switch rơi sang FDB (AP rút/đổi switch vẫn hiện ở switch cũ),
 (b) khi 1 AP bắt đầu gửi LLDP, các link FDB còn lại đứng yên: không làm mới, không stale,
 (c) switch có LLDP với 1 phần AP thì các AP còn lại không bao giờ được map.
"""
from unittest.mock import patch

import pytest

from apps.collectors.topology_fdb import FdbMacEntry
from apps.collectors.topology_lldp import NeighborRecord
from apps.collectors.topology_writer import (
    STALE_MISS_THRESHOLD,
    ApFdbWalkError,
    upsert_switch_topology,
)
from apps.devices.models import TopologyLink
from tests.conftest import CiscoSNMPDeviceFactory, DeviceFactory

pytestmark = pytest.mark.django_db

AP1 = "aa:bb:cc:dd:ee:01"
AP2 = "aa:bb:cc:dd:ee:02"
SNAPSHOT = {AP1: {"ap_name": "AP-1"}, AP2: {"ap_name": "AP-2"}}


def _lldp(port, mac, name="AP"):
    return NeighborRecord(
        local_port=port, local_port_num=1, remote_sys_name=name, remote_mac=mac, is_ap_candidate=True,
    )


def _fdb(port, mac):
    return FdbMacEntry(local_port=port, local_port_num=1, mac=mac, is_ap_match=True)


@pytest.fixture
def env():
    """Patch 3 nguồn dữ liệu của upsert_switch_topology; trả mock để test chỉnh theo kịch bản."""
    with patch("apps.collectors.topology_writer.load_ac_ap_snapshot", return_value=dict(SNAPSHOT)) as snap, \
         patch("apps.collectors.topology_writer.collect_lldp_neighbors", return_value=[]) as lldp, \
         patch("apps.collectors.topology_writer.collect_fdb_ap_mappings", return_value=[]) as fdb:
        yield type("Env", (), {
            "ac": DeviceFactory(device_type="wlan_controller"),
            "sw": CiscoSNMPDeviceFactory(),
            "snap": snap, "lldp": lldp, "fdb": fdb,
        })()


def _link(env, port):
    return TopologyLink.objects.get(local_device=env.sw, local_port=port)


def _run(env, times=1):
    for _ in range(times):
        upsert_switch_topology(env.sw, env.ac)


class TestStaleFollowsEverySourceThatRan:
    def test_lldp_link_goes_stale_when_ap_is_gone_and_switch_has_no_lldp_neighbor(self, env):
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        _run(env)
        assert _link(env, "Gi0/0/1").protocol == "lldp"

        env.lldp.return_value = []          # AP rút khỏi switch; FDB cũng không thấy
        _run(env, STALE_MISS_THRESHOLD)
        link = _link(env, "Gi0/0/1")
        assert link.is_stale is True
        assert link.miss_count == STALE_MISS_THRESHOLD

    def test_fdb_link_goes_stale_when_ap_is_gone_even_if_another_ap_speaks_lldp(self, env):
        TopologyLink.objects.create(
            local_device=env.sw, local_port="Gi0/0/5", link_kind="ap", protocol="fdb",
            remote_ap_mac=AP2, match_method="mac", is_confirmed=True,
        )
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]   # FDB không còn thấy AP2
        _run(env, STALE_MISS_THRESHOLD)
        assert _link(env, "Gi0/0/5").is_stale is True

    def test_fdb_link_is_refreshed_not_frozen_when_another_ap_speaks_lldp(self, env):
        TopologyLink.objects.create(
            local_device=env.sw, local_port="Gi0/0/5", link_kind="ap", protocol="fdb",
            remote_ap_mac=AP2, match_method="mac", is_confirmed=True, miss_count=2,
        )
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        env.fdb.return_value = [_fdb("Gi0/0/5", AP2)]
        _run(env)
        link = _link(env, "Gi0/0/5")
        assert link.miss_count == 0 and link.is_stale is False

    def test_link_found_again_after_going_stale_recovers(self, env):
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        _run(env)
        env.lldp.return_value = []
        _run(env, STALE_MISS_THRESHOLD)
        assert _link(env, "Gi0/0/1").is_stale is True
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        _run(env)
        link = _link(env, "Gi0/0/1")
        assert link.is_stale is False and link.miss_count == 0

    def test_fdb_links_untouched_when_ac_has_no_ap_list(self, env):
        """FDB không chạy được (không có MAC AP để đối chiếu) → 'không hỏi được' ≠ 'không còn'."""
        TopologyLink.objects.create(
            local_device=env.sw, local_port="Gi0/0/5", link_kind="ap", protocol="fdb",
            remote_ap_mac=AP2, match_method="mac", is_confirmed=True,
        )
        env.snap.return_value = {}
        _run(env, STALE_MISS_THRESHOLD + 2)
        link = _link(env, "Gi0/0/5")
        assert link.is_stale is False and link.miss_count == 0
        env.fdb.assert_not_called()

    def test_manual_link_is_never_staled(self, env):
        TopologyLink.objects.create(
            local_device=env.sw, local_port="Gi0/0/7", link_kind="ap", protocol="lldp",
            remote_ap_mac=AP1, match_method="manual", is_confirmed=True,
        )
        _run(env, STALE_MISS_THRESHOLD + 2)
        assert _link(env, "Gi0/0/7").is_stale is False


class TestFdbComplementsLldp:
    def test_fdb_maps_ap_that_lldp_does_not_see(self, env):
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        env.fdb.return_value = [_fdb("Gi0/0/5", AP2)]
        n, confirmed = upsert_switch_topology(env.sw, env.ac)
        assert (_link(env, "Gi0/0/1").protocol, _link(env, "Gi0/0/5").protocol) == ("lldp", "fdb")
        assert _link(env, "Gi0/0/5").remote_ap_mac == AP2
        assert _link(env, "Gi0/0/5").remote_ap_name == "AP-2"
        assert n == 2
        assert confirmed >= 1

    def test_fdb_does_not_duplicate_ap_already_found_by_lldp(self, env):
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        env.fdb.return_value = [_fdb("Gi0/0/9", AP1)]      # cùng AP học thêm ở cổng khác
        _run(env)
        assert not TopologyLink.objects.filter(local_device=env.sw, local_port="Gi0/0/9").exists()
        assert TopologyLink.objects.filter(local_device=env.sw).count() == 1

    def test_lldp_wins_when_both_claim_the_same_port(self, env):
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        env.fdb.return_value = [_fdb("Gi0/0/1", AP2)]
        _run(env)
        link = _link(env, "Gi0/0/1")
        assert (link.protocol, link.remote_ap_mac) == ("lldp", AP1)

    def test_fdb_failure_does_not_stale_or_write_anything_half_done(self, env):
        """Walk FDB hỏng → ném lỗi: không đánh stale (không biết gì), link cũ giữ nguyên."""
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        _run(env)
        TopologyLink.objects.create(
            local_device=env.sw, local_port="Gi0/0/5", link_kind="ap", protocol="fdb",
            remote_ap_mac=AP2, match_method="mac", is_confirmed=True,
        )
        env.lldp.return_value = []
        env.fdb.side_effect = ConnectionError("snmp timeout")
        for _ in range(STALE_MISS_THRESHOLD + 1):
            with pytest.raises(ApFdbWalkError) as excinfo:
                upsert_switch_topology(env.sw, env.ac)
            assert isinstance(excinfo.value.__cause__, ConnectionError)
        assert not _link(env, "Gi0/0/1").is_stale
        assert not _link(env, "Gi0/0/5").is_stale


# --------------------------------------------------------------------------- review ngoài 2026-10-21
# Hai lỗi P2 của chính bản sửa trên: (1) walk FDB lỗi làm discover_all_switches bỏ cả pha uplink LLDP
# dù LLDP đã đọc xong; (2) FDB bổ sung ghi đè link thủ công (match_method="manual") ở cổng trùng.
from apps.collectors import topology_writer  # noqa: E402
from apps.collectors.topology_writer import discover_all_switches  # noqa: E402
from tests.conftest import HuaweiSNMPDeviceFactory  # noqa: E402


def _switch_neighbor(port, remote_name):
    return NeighborRecord(
        local_port=port, local_port_num=1, remote_sys_name=remote_name, remote_port_id="Gi0/0/24",
    )


@pytest.fixture
def fleet_env():
    """2 switch + AC; LLDP trả AP ở pha AP (ap_only=True) và switch láng giềng ở pha uplink."""
    sw = HuaweiSNMPDeviceFactory(name="T-SW1")
    remote = HuaweiSNMPDeviceFactory(name="T-SW2")
    ac = DeviceFactory(device_type="wlan_controller")

    def fake_lldp(device, *, ap_only=True, ap_macs=None, **kw):
        if device.pk != sw.pk:
            return []
        if ap_only:
            return [_lldp("Gi0/0/1", AP1)]
        return [_switch_neighbor("Gi0/0/24", remote.name)]

    with patch("apps.collectors.topology_writer.load_ac_ap_snapshot", return_value=dict(SNAPSHOT)), \
         patch("apps.collectors.topology_writer.get_default_ac_device", return_value=ac), \
         patch("apps.collectors.topology_writer.collect_lldp_neighbors", side_effect=fake_lldp) as lldp, \
         patch("apps.collectors.topology_writer.collect_fdb_ap_mappings", return_value=[]) as fdb, \
         patch("apps.collectors.topology_writer.discover_switch_links_fdb", return_value=0):
        yield type("FleetEnv", (), {"sw": sw, "remote": remote, "ac": ac, "lldp": lldp, "fdb": fdb})()


class TestFdbErrorDoesNotSkipLldpUplinks:
    def test_uplink_link_is_written_when_ap_fdb_walk_fails(self, fleet_env):
        def fdb_fails_only_on_sw1(device, macs):
            if device.pk == fleet_env.sw.pk:
                raise ConnectionError("snmp timeout")
            return []

        fleet_env.fdb.side_effect = fdb_fails_only_on_sw1
        stats = discover_all_switches()
        up = TopologyLink.objects.get(local_device=fleet_env.sw, local_port="Gi0/0/24")
        assert (up.link_kind, up.remote_device_id) == ("switch", fleet_env.remote.pk)
        assert stats["errors"] == 1                      # lỗi FDB vẫn được đếm, không bị nuốt

    def test_missing_uplink_is_still_aged_when_ap_fdb_walk_fails(self, fleet_env):
        TopologyLink.objects.create(
            local_device=fleet_env.sw, local_port="Gi0/0/30", link_kind="switch", protocol="lldp",
            remote_device=fleet_env.remote, match_method="name", is_confirmed=True,
        )
        fleet_env.fdb.side_effect = ConnectionError("snmp timeout")
        for _ in range(STALE_MISS_THRESHOLD):
            discover_all_switches()
        assert TopologyLink.objects.get(local_device=fleet_env.sw, local_port="Gi0/0/30").is_stale

    def test_ap_links_are_not_aged_when_ap_fdb_walk_fails(self, fleet_env):
        """Giữ nguyên all-or-nothing của pha AP: FDB không hỏi được ⇒ không kết luận AP biến mất."""
        TopologyLink.objects.create(
            local_device=fleet_env.sw, local_port="Gi0/0/5", link_kind="ap", protocol="fdb",
            remote_ap_mac=AP2, match_method="mac", is_confirmed=True,
        )
        fleet_env.fdb.side_effect = ConnectionError("snmp timeout")
        for _ in range(STALE_MISS_THRESHOLD + 1):
            discover_all_switches()
        assert not TopologyLink.objects.get(local_device=fleet_env.sw, local_port="Gi0/0/5").is_stale

    def test_soft_limit_in_fdb_walk_propagates_unwrapped(self, fleet_env):
        """Hết giờ task không phải 'FDB hỏng': phải nổi lên để task dừng, không bị bọc rồi nuốt."""
        from celery.exceptions import SoftTimeLimitExceeded

        fleet_env.fdb.side_effect = SoftTimeLimitExceeded()
        with pytest.raises(SoftTimeLimitExceeded):
            discover_all_switches()

    def test_lldp_walk_failure_still_skips_uplinks_without_a_second_walk(self, fleet_env):
        """LLDP không đọc được ở pha AP thì pha uplink (cùng bảng LLDP) không thử lại: tránh nhân đôi
        timeout trên switch chết — margin deadline của discovery chỉ tính cho 1 walk hỏng."""
        fleet_env.lldp.side_effect = ConnectionError("snmp timeout")
        stats = discover_all_switches()
        assert fleet_env.lldp.call_count == 2             # đúng 1 lần/switch (2 switch), không gọi lần 2
        assert stats["errors"] == 2


class TestManualLinksSurviveDiscovery:
    def _manual_ap(self, env, port, mac=AP2, name="AP-MANUAL"):
        return TopologyLink.objects.create(
            local_device=env.sw, local_port=port, link_kind="ap", protocol="lldp",
            remote_ap_mac=mac, remote_ap_name=name, match_method="manual", is_confirmed=True,
        )

    def _assert_untouched(self, link, **expected):
        link.refresh_from_db()
        assert link.match_method == "manual"
        assert link.is_stale is False
        for field, value in expected.items():
            assert getattr(link, field) == value, field

    def test_fdb_complement_does_not_overwrite_manual_link(self, env):
        manual = self._manual_ap(env, "Gi0/0/5")
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        env.fdb.return_value = [_fdb("Gi0/0/5", AP2)]      # đúng kịch bản review ngoài
        upsert_switch_topology(env.sw, env.ac)
        self._assert_untouched(manual, protocol="lldp", remote_ap_name="AP-MANUAL", remote_ap_mac=AP2)
        assert _link(env, "Gi0/0/1").protocol == "lldp"

    def test_fdb_only_switch_does_not_overwrite_manual_link_for_another_ap(self, env):
        manual = self._manual_ap(env, "Gi0/0/5", mac=AP2, name="AP-MANUAL")
        env.fdb.return_value = [_fdb("Gi0/0/5", AP1)]
        upsert_switch_topology(env.sw, env.ac)
        self._assert_untouched(manual, remote_ap_mac=AP2, remote_ap_name="AP-MANUAL")

    def test_lldp_ap_neighbor_does_not_overwrite_manual_link(self, env):
        manual = self._manual_ap(env, "Gi0/0/1", mac=AP2, name="AP-MANUAL")
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        upsert_switch_topology(env.sw, env.ac)
        self._assert_untouched(manual, remote_ap_mac=AP2, remote_ap_name="AP-MANUAL")

    def test_fdb_does_not_place_an_ap_elsewhere_when_lldp_saw_it_on_a_manual_port(self, env):
        """LLDP đã cho biết AP1 ở cổng admin ghim (dù không ghi đè) — FDB không được đặt AP1 sang cổng khác."""
        self._manual_ap(env, "Gi0/0/1", mac=AP2, name="AP-MANUAL")
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        env.fdb.return_value = [_fdb("Gi0/0/9", AP1)]
        upsert_switch_topology(env.sw, env.ac)
        assert not TopologyLink.objects.filter(local_device=env.sw, local_port="Gi0/0/9").exists()

    def test_lldp_switch_neighbor_does_not_overwrite_manual_link(self, fleet_env):
        other = HuaweiSNMPDeviceFactory(name="T-SW3")
        manual = TopologyLink.objects.create(
            local_device=fleet_env.sw, local_port="Gi0/0/24", link_kind="switch", protocol="lldp",
            remote_device=other, match_method="manual", is_confirmed=True,
        )
        discover_all_switches()                          # LLDP thấy T-SW2 ở đúng cổng đó
        manual.refresh_from_db()
        assert (manual.match_method, manual.remote_device_id) == ("manual", other.pk)

    def test_switch_fdb_phase_does_not_overwrite_manual_link(self, db):
        a = HuaweiSNMPDeviceFactory(name="T-SW1")
        b = HuaweiSNMPDeviceFactory(name="T-SW2")
        c = HuaweiSNMPDeviceFactory(name="T-SW3")
        manual = TopologyLink.objects.create(
            local_device=a, local_port="Gi0/0/7", link_kind="switch", protocol="lldp",
            remote_device=c, match_method="manual", is_confirmed=True,
        )
        with patch("apps.collectors.topology_switch_fdb.build_switch_mac_registry", return_value={}), \
             patch("apps.collectors.topology_switch_fdb.discover_switch_adjacency",
                   return_value=[(a.pk, "Gi0/0/7", b.pk, "Gi0/0/1")]):
            topology_writer.discover_switch_links_fdb([a, b, c])
        manual.refresh_from_db()
        assert (manual.match_method, manual.remote_device_id, manual.protocol) == ("manual", c.pk, "lldp")

    def test_api_created_manual_link_still_overwrites_auto_link(self, env):
        """Bảo vệ chỉ ở đường discovery — admin ghi tay vẫn đè được link tự động."""
        from apps.collectors.topology_writer import _upsert_ap_link

        TopologyLink.objects.create(
            local_device=env.sw, local_port="Gi0/0/5", link_kind="ap", protocol="fdb",
            remote_ap_mac=AP1, match_method="mac", is_confirmed=True,
        )
        _upsert_ap_link(env.sw, "Gi0/0/5", mac=AP2, ap_name="AP-2", protocol="lldp",
                        match_method="manual", is_confirmed=True)
        link = _link(env, "Gi0/0/5")
        assert (link.match_method, link.remote_ap_mac) == ("manual", AP2)
