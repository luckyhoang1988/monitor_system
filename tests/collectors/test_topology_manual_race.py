"""Link thủ công không bị discovery ghi đè KỂ CẢ khi admin lưu đúng lúc discovery đang chờ mạng.

Review ngoài 2026-10-21: `manual_ports` đọc TRƯỚC khi walk FDB (có thể chờ hàng chục giây); admin lưu link
trong khoảng đó thì tập này chưa chứa cổng vừa ghim và `update_or_create` của discovery vẫn đè
`match_method="manual"` thành "mac". Kiểm tra "có phải link thủ công không" phải nằm TRONG transaction ghi,
dưới khoá chung với API (khoá dòng `(device, port)` + unique index khi dòng chưa tồn tại).

Hai nhóm test:
 - đơn connection, tất định: mô phỏng "admin lưu giữa lúc chờ" bằng cách cho chính hàm walk/ghép (patch) gọi
   hàm ghi của API trước khi discovery ghi — phủ từng đường ghi của discovery;
 - 2 connection Postgres THẬT + 2 thread: chứng minh khoá có tác dụng ở cả hai thứ tự, cả khi dòng đã có lẫn chưa có.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest
from django.db import connection, transaction

from apps.collectors import topology_writer
from apps.collectors.topology_fdb import FdbMacEntry
from apps.collectors.topology_lldp import NeighborRecord
from apps.collectors.topology_writer import (
    _count_miss,
    _upsert_ap_link,
    _upsert_switch_link,
    discover_switch_links_fdb,
    discover_switch_uplinks,
    upsert_switch_topology,
)
from apps.devices.models import TopologyLink
from apps.devices.topology_switch_match import build_switch_device_index
from tests.conftest import DeviceFactory, HuaweiSNMPDeviceFactory

AP1 = "aa:bb:cc:dd:ee:01"
AP2 = "aa:bb:cc:dd:ee:02"
PORT = "Gi0/0/5"


def _lldp(port, mac, name="AP"):
    return NeighborRecord(
        local_port=port, local_port_num=1, remote_sys_name=name, remote_mac=mac, is_ap_candidate=True,
    )


def _fdb(port, mac):
    return FdbMacEntry(local_port=port, local_port_num=1, mac=mac, is_ap_match=True)


def admin_pins_ap(device, port=PORT, mac=AP2, name="AP-MANUAL"):
    """Đúng thứ API `_create_link(kind=ap)` gọi."""
    _upsert_ap_link(
        device, port, mac=mac, ap_name=name, protocol="lldp", match_method="manual", is_confirmed=True,
    )


def auto_ap_link(device, port=PORT, mac=AP2):
    return TopologyLink.objects.create(
        local_device=device, local_port=port, link_kind="ap", protocol="fdb",
        remote_ap_mac=mac, match_method="mac", is_confirmed=True,
    )


def assert_still_manual(device, port=PORT, mac=AP2, name="AP-MANUAL"):
    link = TopologyLink.objects.get(local_device=device, local_port=port)
    assert link.match_method == "manual", f"bị đè thành {link.match_method}/{link.protocol}"
    assert (link.protocol, link.remote_ap_mac, link.remote_ap_name, link.is_stale) == ("lldp", mac, name, False)


@pytest.fixture
def env(db):
    with patch("apps.collectors.topology_writer.load_ac_ap_snapshot",
               return_value={AP1: {"ap_name": "AP-1"}, AP2: {"ap_name": "AP-2"}}), \
         patch("apps.collectors.topology_writer.collect_lldp_neighbors", return_value=[]) as lldp, \
         patch("apps.collectors.topology_writer.collect_fdb_ap_mappings", return_value=[]) as fdb:
        yield type("Env", (), {
            "ac": DeviceFactory(device_type="wlan_controller"),
            "sw": HuaweiSNMPDeviceFactory(name="T-SW1"),
            "lldp": lldp, "fdb": fdb,
        })()


# --------------------------------------------------------------------------- admin lưu giữa lúc chờ
class TestAdminSavesWhileDiscoveryIsWaiting:
    def test_fdb_walk_existing_auto_link_gets_pinned(self, env):
        """Đúng kịch bản review: manual_ports đã đọc, rồi admin ghim cổng trong lúc chờ walk FDB."""
        auto_ap_link(env.sw)

        def fdb_walk(device, macs):
            admin_pins_ap(device)
            return [_fdb(PORT, AP2)]

        env.fdb.side_effect = fdb_walk
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        upsert_switch_topology(env.sw, env.ac)
        assert_still_manual(env.sw)

    def test_fdb_walk_no_row_yet_admin_creates_it(self, env):
        def fdb_walk(device, macs):
            admin_pins_ap(device)
            return [_fdb(PORT, AP2)]

        env.fdb.side_effect = fdb_walk
        upsert_switch_topology(env.sw, env.ac)
        assert_still_manual(env.sw)

    def test_lldp_ap_path_existing_row(self, env):
        auto_ap_link(env.sw, port="Gi0/0/1", mac=AP1)
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1)]
        real_match = topology_writer.match_lldp_to_ap

        def pin_then_match(neighbor, ac):
            admin_pins_ap(env.sw, port="Gi0/0/1")
            return real_match(neighbor, ac)

        with patch("apps.collectors.topology_writer.match_lldp_to_ap", side_effect=pin_then_match):
            upsert_switch_topology(env.sw, env.ac)
        assert_still_manual(env.sw, port="Gi0/0/1")

    def test_lldp_uplink_path(self, env):
        remote = HuaweiSNMPDeviceFactory(name="T-SW2")
        other = HuaweiSNMPDeviceFactory(name="T-SW3")
        env.lldp.return_value = [NeighborRecord(
            local_port="Gi0/0/24", local_port_num=24, remote_sys_name=remote.name,
        )]
        real_match = topology_writer.match_neighbor_to_switch

        def pin_then_match(*args, **kwargs):
            _upsert_switch_link(
                env.sw, "Gi0/0/24", other, is_confirmed=True, protocol="lldp", match_method="manual",
            )
            return real_match(*args, **kwargs)

        with patch("apps.collectors.topology_writer.match_neighbor_to_switch", side_effect=pin_then_match):
            written = discover_switch_uplinks(env.sw, build_switch_device_index())
        link = TopologyLink.objects.get(local_device=env.sw, local_port="Gi0/0/24")
        assert (link.match_method, link.remote_device_id) == ("manual", other.pk)
        assert written == 0                       # lượt này không ghi gì, không được đếm như đã ghi

    def test_switch_fdb_phase(self, env):
        b = HuaweiSNMPDeviceFactory(name="T-SW2")
        c = HuaweiSNMPDeviceFactory(name="T-SW3")

        def pin_during_core_lookup():
            _upsert_switch_link(
                env.sw, "Gi0/0/7", c, is_confirmed=True, protocol="lldp", match_method="manual",
            )
            return None

        with patch("apps.collectors.topology_switch_fdb.build_switch_mac_registry", return_value={}), \
             patch("apps.collectors.topology_switch_fdb.discover_switch_adjacency",
                   return_value=[(env.sw.pk, "Gi0/0/7", b.pk, "Gi0/0/1")]), \
             patch("apps.devices.topology_hierarchy.find_core_device", side_effect=pin_during_core_lookup):
            written = discover_switch_links_fdb([env.sw, b, c])
        link = TopologyLink.objects.get(local_device=env.sw, local_port="Gi0/0/7")
        assert (link.match_method, link.remote_device_id) == ("manual", c.pk)
        assert written == 0

    def test_missing_link_loaded_before_admin_pins_it_is_not_staled(self, env):
        """`_count_miss` ghi trên bản nạp từ trước: chỉ UPDATE khi dòng hiện tại vẫn không phải thủ công."""
        link = auto_ap_link(env.sw)
        TopologyLink.objects.filter(pk=link.pk).update(miss_count=2)
        loaded_by_worker = TopologyLink.objects.get(pk=link.pk)
        assert loaded_by_worker.match_method == "mac"

        TopologyLink.objects.filter(pk=link.pk).update(match_method="manual")   # admin ghim
        _count_miss(loaded_by_worker)

        link.refresh_from_db()
        assert (link.match_method, link.is_stale, link.miss_count) == ("manual", False, 2)

    def test_discovery_still_refreshes_ordinary_auto_links(self, env):
        """Đối chứng: bảo vệ không làm discovery ngừng cập nhật link tự động."""
        auto_ap_link(env.sw, mac=AP1)
        env.fdb.return_value = [_fdb(PORT, AP2)]
        upsert_switch_topology(env.sw, env.ac)
        link = TopologyLink.objects.get(local_device=env.sw, local_port=PORT)
        assert (link.match_method, link.remote_ap_mac, link.remote_ap_name) == ("mac", AP2, "AP-2")

    def test_api_write_still_overwrites_auto_link_and_manual_link(self, env):
        auto_ap_link(env.sw)
        admin_pins_ap(env.sw, mac=AP1, name="AP-FIRST")
        admin_pins_ap(env.sw, mac=AP2, name="AP-MANUAL")      # sửa lại link thủ công bằng API
        assert_still_manual(env.sw)


# --------------------------------------------------------------------------- lọc sớm cổng đã ghim
class TestPinnedPortsAreNotEvenWritten:
    """Khoá trong transaction ghi (`_write_link`) là nơi quyết định cuối cùng; việc lọc sớm theo ảnh chụp
    `manual_ports`/`manual_keys` chỉ để khỏi tốn 1 lượt tra interface + SELECT FOR UPDATE cho cổng đã ghim.
    Spy `_write_link` để hành vi lọc sớm này không thành mã không ai canh (kết quả cuối giống nhau dù có hay không)."""

    @staticmethod
    def _written_ports(spy) -> list[str]:
        return [call.args[1] for call in spy.call_args_list]

    def _spy(self):
        return patch("apps.collectors.topology_writer._write_link", wraps=topology_writer._write_link)

    def test_lldp_ap_loop(self, env):
        admin_pins_ap(env.sw, port="Gi0/0/1")
        env.lldp.return_value = [_lldp("Gi0/0/1", AP1), _lldp("Gi0/0/2", AP2)]
        with self._spy() as spy:
            upsert_switch_topology(env.sw, env.ac)
        assert "Gi0/0/1" not in self._written_ports(spy)
        assert "Gi0/0/2" in self._written_ports(spy)

    def test_fdb_complement_loop(self, env):
        admin_pins_ap(env.sw)
        env.fdb.return_value = [_fdb(PORT, AP2), _fdb("Gi0/0/6", AP1)]
        with self._spy() as spy:
            upsert_switch_topology(env.sw, env.ac)
        assert PORT not in self._written_ports(spy)
        assert "Gi0/0/6" in self._written_ports(spy)

    def test_lldp_uplink_loop(self, env):
        remote = HuaweiSNMPDeviceFactory(name="T-SW2")
        other = HuaweiSNMPDeviceFactory(name="T-SW3")
        _upsert_switch_link(env.sw, "Gi0/0/24", other, protocol="lldp", match_method="manual")
        env.lldp.return_value = [
            NeighborRecord(local_port="Gi0/0/24", local_port_num=24, remote_sys_name=remote.name),
            NeighborRecord(local_port="Gi0/0/23", local_port_num=23, remote_sys_name=remote.name),
        ]
        with self._spy() as spy:
            discover_switch_uplinks(env.sw, build_switch_device_index())
        assert "Gi0/0/24" not in self._written_ports(spy)
        assert "Gi0/0/23" in self._written_ports(spy)

    def test_switch_fdb_loop(self, env):
        b = HuaweiSNMPDeviceFactory(name="T-SW2")
        c = HuaweiSNMPDeviceFactory(name="T-SW3")
        _upsert_switch_link(env.sw, "Gi0/0/7", c, protocol="lldp", match_method="manual")
        with patch("apps.collectors.topology_switch_fdb.build_switch_mac_registry", return_value={}), \
             patch("apps.collectors.topology_switch_fdb.discover_switch_adjacency",
                   return_value=[(env.sw.pk, "Gi0/0/7", b.pk, "Gi0/0/1"), (env.sw.pk, "Gi0/0/8", c.pk, "Gi0/0/1")]), \
             self._spy() as spy:
            discover_switch_links_fdb([env.sw, b, c])
        assert "Gi0/0/7" not in self._written_ports(spy)
        assert "Gi0/0/8" in self._written_ports(spy)


# --------------------------------------------------------------------------- 2 connection Postgres thật
def _wait_until_a_backend_is_waiting_for_a_lock(give_up: threading.Event, deadline_secs: float = 10.0) -> bool:
    """True khi có backend Postgres khác đang chờ khoá (quan sát qua pg_stat_activity, không dùng đồng hồ)."""
    deadline = time.monotonic() + deadline_secs
    while time.monotonic() < deadline and not give_up.is_set():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND wait_event_type = 'Lock'"
            )
            if cursor.fetchone()[0]:
                return True
        time.sleep(0.02)
    return False


def _race(holder, contender):
    """`holder` chạy trong transaction mở (giữ khoá, chưa commit); `contender` chạy ở connection khác và PHẢI
    bị chặn tới khi holder commit. Trả (blocked, contender_finished_early, errors)."""
    holding, release, contender_done = threading.Event(), threading.Event(), threading.Event()
    errors: list = []

    def run_holder():
        try:
            with transaction.atomic():
                holder()
                holding.set()
                if not release.wait(timeout=15):
                    errors.append("release timeout")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            holding.set()
            connection.close()

    def run_contender():
        try:
            if not holding.wait(timeout=15):
                errors.append("holder không vào transaction kịp")
                return
            contender()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            contender_done.set()
            connection.close()

    t_holder, t_contender = threading.Thread(target=run_holder), threading.Thread(target=run_contender)
    t_holder.start()
    t_contender.start()
    blocked = _wait_until_a_backend_is_waiting_for_a_lock(give_up=contender_done)
    finished_early = contender_done.is_set()
    release.set()
    t_holder.join(timeout=20)
    t_contender.join(timeout=20)
    return blocked, finished_early, errors


def _discovery_write(device):
    return _upsert_ap_link(
        device, PORT, mac=AP1, ap_name="AP-1", protocol="fdb", match_method="mac", is_confirmed=True,
    )


@pytest.mark.django_db(transaction=True)
class TestTwoRealConnections:
    @pytest.fixture
    def sw(self):
        return HuaweiSNMPDeviceFactory(name="T-SW1")

    @pytest.mark.parametrize("row_exists", [True, False], ids=["row-exists", "no-row-yet"])
    def test_admin_holds_lock_discovery_waits_then_manual_survives(self, sw, row_exists):
        if row_exists:
            auto_ap_link(sw, mac=AP1)
        blocked, early, errors = _race(holder=lambda: admin_pins_ap(sw), contender=lambda: _discovery_write(sw))
        assert not errors, errors
        assert blocked and not early, "discovery không bị chặn bởi giao dịch ghim của admin"
        assert_still_manual(sw)

    @pytest.mark.parametrize("row_exists", [True, False], ids=["row-exists", "no-row-yet"])
    def test_discovery_holds_lock_admin_waits_then_manual_wins(self, sw, row_exists):
        if row_exists:
            auto_ap_link(sw, mac=AP1)
        blocked, early, errors = _race(holder=lambda: _discovery_write(sw), contender=lambda: admin_pins_ap(sw))
        assert not errors, errors
        assert blocked and not early, "API không bị chặn bởi giao dịch ghi của discovery"
        assert_still_manual(sw)
