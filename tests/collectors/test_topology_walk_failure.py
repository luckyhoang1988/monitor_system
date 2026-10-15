"""Topology discovery (1.9): walk SNMP thất bại ≠ bảng rỗng, soft limit không bị nuốt, không walk lặp.

Bằng chứng prod 2026-10-15: cả 7/7 lượt discovery trong cửa sổ log (03:41–06:43) bị soft limit
120s đúng lúc +120,0s và bị nuốt, chạy 128–150s (lượt 03:41 xong 0,2s trước hard limit 150s); FDB bị
walk 2 lần/switch; và 6 TopologyLink mang tên cổng giả `port-N` (2 dòng tạo lúc 03:13:41, lượt 03:11
nằm ngoài cửa sổ log) vì `snmp_walk_pairs` đổi mọi lỗi thành bảng rỗng.

Dùng session SNMP giả (không mạng). Đồng hồ giả cho test deadline: patch `_monotonic()` của module
topology_writer (không patch `time.monotonic` toàn cục).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

from apps.collectors import tasks, topology_fdb, topology_lldp, topology_switch_fdb, topology_writer
from apps.collectors.topology_fdb import (
    OID_DOT1D_BASEPORT_IFINDEX,
    OID_DOT1D_FDB_PORT,
    OID_DOT1Q_FDB_PORT,
    OID_IF_DESCR,
    collect_switch_mac_table,
)
from apps.collectors.topology_lldp import _load_lldp_oids, collect_lldp_neighbors
from apps.collectors.topology_switch_fdb import (
    OID_DOT1D_BASE_BRIDGE_ADDRESS,
    OID_IF_PHYS_ADDRESS,
)
from apps.collectors.topology_writer import discover_all_switches
from apps.devices.models import TopologyLink
from apps.metrics.models import WifiApStats
from tests.conftest import HuaweiACDeviceFactory, HuaweiSNMPDeviceFactory

AP_MAC = "aa:bb:cc:dd:ee:01"
LLDP_OIDS = _load_lldp_oids("huawei_vrp")


# --------------------------------------------------------------------------- hạ tầng giả
class FakeNetwork:
    """Bảng SNMP theo từng host. Giá trị bảng: list[(oid, value)] hoặc 1 exception để ném."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, object]] = {}
        self.walks: list[tuple[str, str]] = []
        self.on_walk = None  # callback(host, prefix) — cho đồng hồ giả

    def session_for(self, kwargs, backend=None):
        return _Session(self, str(kwargs["hostname"]))

    def count(self, host: str, prefix: str) -> int:
        return sum(1 for h, p in self.walks if h == host and p == prefix)

    def walked_hosts_in_order(self) -> list[str]:
        return list(dict.fromkeys(h for h, _p in self.walks))


class _Session:
    def __init__(self, net: FakeNetwork, host: str) -> None:
        self.net = net
        self.host = host

    def walk(self, prefix: str):
        self.net.walks.append((self.host, prefix))
        if self.net.on_walk:
            self.net.on_walk(self.host, prefix)
        value = self.net.tables.get(self.host, {}).get(prefix, [])
        if isinstance(value, BaseException):
            raise value
        return [SimpleNamespace(oid=o, value=v) for o, v in value]


def _mac_to_oid_suffix(mac: str) -> str:
    return ".".join(str(int(o, 16)) for o in mac.split(":"))


def _fdb_row(mac: str, port: int, vlan: int = 1) -> tuple[str, str]:
    return f"{OID_DOT1Q_FDB_PORT}.{vlan}.{_mac_to_oid_suffix(mac)}", str(port)


def _port_tables(n_ports: int = 5) -> dict[str, object]:
    return {
        OID_IF_DESCR: [(f"{OID_IF_DESCR}.{i}", f"GE1/0/{i}") for i in range(1, n_ports + 1)],
        OID_DOT1D_BASEPORT_IFINDEX: [
            (f"{OID_DOT1D_BASEPORT_IFINDEX}.{i}", str(i)) for i in range(1, n_ports + 1)
        ],
    }


def device_stub(pk: int = 1, name: str = "SW-STUB") -> SimpleNamespace:
    return SimpleNamespace(
        pk=pk, id=pk, name=name, ip_address="10.9.9.9", snmp_version="v2c",
        snmp_community="x", os_family="huawei_vrp",
    )


@pytest.fixture
def net(monkeypatch) -> FakeNetwork:
    network = FakeNetwork()
    for module in (topology_lldp, topology_fdb, topology_switch_fdb):
        monkeypatch.setattr(module, "create_snmp_session", network.session_for)
    return network


# --------------------------------------------------------------------------- walk từng bảng
class TestFdbWalkFailure:
    """`collect_switch_mac_table`: lỗi đường truyền không được thành "FDB rỗng / tên cổng giả"."""

    def _host(self, net: FakeNetwork, **overrides) -> SimpleNamespace:
        dev = device_stub()
        tables = {
            **_port_tables(),
            OID_DOT1Q_FDB_PORT: [_fdb_row("02:00:00:00:00:aa", 2)],
        }
        tables.update(overrides)
        net.tables[dev.ip_address] = tables
        return dev

    def test_normal_walk_resolves_port_names(self, net):
        dev = self._host(net)
        entries = collect_switch_mac_table(dev)
        assert [(e.mac, e.local_port) for e in entries] == [("02:00:00:00:00:aa", "GE1/0/2")]

    def test_ifdescr_timeout_raises_instead_of_ghost_port_names(self, net):
        dev = self._host(net, **{OID_IF_DESCR: ConnectionError("No SNMP response received before timeout")})
        with pytest.raises(ConnectionError):
            collect_switch_mac_table(dev)

    def test_soft_limit_during_walk_propagates(self, net):
        dev = self._host(net, **{OID_IF_DESCR: SoftTimeLimitExceeded()})
        with pytest.raises(SoftTimeLimitExceeded):
            collect_switch_mac_table(dev)

    def test_qbridge_timeout_falls_back_to_dot1d(self, net):
        mac_oid = f"{OID_DOT1D_FDB_PORT}.{_mac_to_oid_suffix('02:00:00:00:00:bb')}"
        dev = self._host(net, **{
            OID_DOT1Q_FDB_PORT: ConnectionError("timeout"),
            OID_DOT1D_FDB_PORT: [(mac_oid, "3")],
        })
        entries = collect_switch_mac_table(dev)
        assert [(e.mac, e.local_port) for e in entries] == [("02:00:00:00:00:bb", "GE1/0/3")]

    def test_soft_limit_in_fdb_table_propagates_not_taken_for_table_failure(self, net):
        """Soft limit rơi đúng vào bảng Q-BRIDGE không được bị coi là "bảng này hỏng, thử bảng kia"."""
        mac_oid = f"{OID_DOT1D_FDB_PORT}.{_mac_to_oid_suffix('02:00:00:00:00:dd')}"
        dev = self._host(net, **{
            OID_DOT1Q_FDB_PORT: SoftTimeLimitExceeded(),
            OID_DOT1D_FDB_PORT: [(mac_oid, "2")],
        })
        with pytest.raises(SoftTimeLimitExceeded):
            collect_switch_mac_table(dev)
        assert net.count(dev.ip_address, OID_DOT1D_FDB_PORT) == 0

    def test_both_fdb_tables_failing_raises(self, net):
        dev = self._host(net, **{
            OID_DOT1Q_FDB_PORT: ConnectionError("timeout"),
            OID_DOT1D_FDB_PORT: ConnectionError("timeout"),
        })
        with pytest.raises(ConnectionError):
            collect_switch_mac_table(dev)

    def test_genuinely_empty_fdb_is_empty_not_error(self, net):
        dev = self._host(net, **{OID_DOT1Q_FDB_PORT: [], OID_DOT1D_FDB_PORT: []})
        assert collect_switch_mac_table(dev) == []

    def test_error_status_on_both_fdb_tables_is_empty_not_failure(self, net):
        """Thiết bị TRẢ LỜI "bảng không có" (error-status) ≠ không hỏi được: rỗng hợp lệ, không ném."""
        from apps.collectors.snmp_client import SnmpDeviceError

        dev = self._host(net, **{
            OID_DOT1Q_FDB_PORT: SnmpDeviceError("noSuchName at 1"),
            OID_DOT1D_FDB_PORT: SnmpDeviceError("noSuchName at 1"),
        })
        assert collect_switch_mac_table(dev) == []

    def test_error_status_on_first_fdb_table_falls_back_to_second(self, net):
        """Giữ hành vi cũ: Q-BRIDGE báo error-status thì dùng BRIDGE-MIB."""
        from apps.collectors.snmp_client import SnmpDeviceError

        mac_oid = f"{OID_DOT1D_FDB_PORT}.{_mac_to_oid_suffix('02:00:00:00:00:cc')}"
        dev = self._host(net, **{
            OID_DOT1Q_FDB_PORT: SnmpDeviceError("noSuchName at 1"),
            OID_DOT1D_FDB_PORT: [(mac_oid, "4")],
        })
        entries = collect_switch_mac_table(dev)
        assert [e.mac for e in entries] == ["02:00:00:00:00:cc"]


class TestSwitchFdbPhaseSoftLimit:
    """Pha switch↔switch gọi walk riêng: soft limit không được bị `except Exception` của vòng lặp nuốt."""

    def test_registry_build_propagates_soft_limit(self, net):
        dev = device_stub()
        net.tables[dev.ip_address] = {OID_DOT1D_BASE_BRIDGE_ADDRESS: SoftTimeLimitExceeded()}
        with pytest.raises(SoftTimeLimitExceeded):
            topology_switch_fdb.build_switch_mac_registry([dev])

    def test_registry_build_skips_switch_on_ordinary_failure(self, net, caplog):
        bad, good = device_stub(1, "BAD"), device_stub(2, "GOOD")
        good.ip_address = "10.9.9.10"
        net.tables[bad.ip_address] = {OID_DOT1D_BASE_BRIDGE_ADDRESS: ConnectionError("timeout")}
        net.tables[good.ip_address] = {
            OID_DOT1D_BASE_BRIDGE_ADDRESS: [(f"{OID_DOT1D_BASE_BRIDGE_ADDRESS}.0", "02:00:00:00:00:09")],
        }
        registry = topology_switch_fdb.build_switch_mac_registry([bad, good])
        assert registry == {"02:00:00:00:00:09": 2}
        assert "BAD" in caplog.text

    def test_adjacency_propagates_soft_limit(self, net):
        dev = device_stub()
        net.tables[dev.ip_address] = {**_port_tables(), OID_DOT1Q_FDB_PORT: SoftTimeLimitExceeded()}
        with pytest.raises(SoftTimeLimitExceeded):
            topology_switch_fdb.discover_switch_adjacency([dev], registry={})


class TestIncompleteFleetData:
    """Chế độ all-or-nothing của pha switch↔switch: thiếu dữ liệu 1 switch thì dừng ngay, nêu tên."""

    @staticmethod
    def _macs(net, dev, mac):
        net.tables[dev.ip_address] = {
            OID_DOT1D_BASE_BRIDGE_ADDRESS: [(f"{OID_DOT1D_BASE_BRIDGE_ADDRESS}.0", mac)],
            OID_IF_PHYS_ADDRESS: [],
        }

    def test_registry_reports_failed_switch_and_stops_walking_the_rest(self, net):
        first, second = device_stub(1, "FIRST"), device_stub(2, "SECOND")
        second.ip_address = "10.9.9.10"
        net.tables[first.ip_address] = {OID_DOT1D_BASE_BRIDGE_ADDRESS: ConnectionError("timeout")}
        self._macs(net, second, "02:00:00:00:00:02")
        missing: list[str] = []
        topology_switch_fdb.build_switch_mac_registry([first, second], missing=missing)
        assert missing == ["FIRST"]
        # Fail-fast: mỗi walk hỏng tốn tới 30s, pha sẽ bị bỏ nên không walk tiếp switch còn lại
        assert second.ip_address not in {h for h, _p in net.walks}

    def test_registry_treats_switch_without_any_mac_as_missing(self, net):
        dev = device_stub(1, "NOMAC")
        net.tables[dev.ip_address] = {OID_DOT1D_BASE_BRIDGE_ADDRESS: [], OID_IF_PHYS_ADDRESS: []}
        missing: list[str] = []
        topology_switch_fdb.build_switch_mac_registry([dev], missing=missing)
        assert missing == ["NOMAC"]

    def test_default_mode_still_skips_and_continues(self, net):
        """Không truyền `missing` (lệnh chẩn đoán/tests cũ): giữ hành vi cũ, bỏ qua switch hỏng."""
        bad, good = device_stub(1, "BAD"), device_stub(2, "GOOD")
        good.ip_address = "10.9.9.10"
        net.tables[bad.ip_address] = {OID_DOT1D_BASE_BRIDGE_ADDRESS: ConnectionError("timeout")}
        self._macs(net, good, "02:00:00:00:00:09")
        assert topology_switch_fdb.build_switch_mac_registry([bad, good]) == {"02:00:00:00:00:09": 2}

    def test_adjacency_reports_empty_fdb_switch_and_discards_nothing_silently(self, net):
        dev = device_stub(1, "EMPTYFDB")
        net.tables[dev.ip_address] = {**_port_tables(), OID_DOT1Q_FDB_PORT: [], OID_DOT1D_FDB_PORT: []}
        missing: list[str] = []
        pairs = topology_switch_fdb.discover_switch_adjacency([dev], {}, missing=missing)
        assert pairs == [] and missing == ["EMPTYFDB"]


class TestWalkMemo:
    def test_without_scope_every_call_walks(self):
        from apps.collectors.topology_walk import memoized

        calls = []
        assert memoized("k", lambda: calls.append(1) or "v") == "v"
        assert memoized("k", lambda: calls.append(1) or "v") == "v"
        assert len(calls) == 2

    def test_inside_scope_same_key_walks_once(self):
        from apps.collectors.topology_walk import memoized, walk_memo

        calls = []
        with walk_memo():
            memoized("k", lambda: calls.append(1) or "v")
            memoized("k", lambda: calls.append(1) or "v")
            memoized("other", lambda: calls.append(1) or "v")
        assert len(calls) == 2

    def test_scope_does_not_leak_between_runs(self):
        from apps.collectors.topology_walk import memoized, walk_memo

        calls = []
        with walk_memo():
            memoized("k", lambda: calls.append(1) or "v")
        with walk_memo():
            memoized("k", lambda: calls.append(1) or "v")
        assert len(calls) == 2

    def test_failed_walk_is_not_stored_and_is_retried(self):
        from apps.collectors.topology_walk import memoized, walk_memo

        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("timeout")
            return "ok"

        with walk_memo():
            with pytest.raises(ConnectionError):
                memoized("k", flaky)
            assert memoized("k", flaky) == "ok"
        assert len(attempts) == 2


class TestLldpWalkFailure:
    def _host(self, net: FakeNetwork, **overrides) -> SimpleNamespace:
        dev = device_stub()
        rem = LLDP_OIDS
        tables = {
            **_port_tables(),
            rem["loc_port_id"]: [(f"{rem['loc_port_id']}.5", "GE1/0/5")],
            rem["rem_sys_name"]: [(f"{rem['rem_sys_name']}.0.5.1", "AP-LOBBY-01")],
            rem["rem_chassis_id"]: [(f"{rem['rem_chassis_id']}.0.5.1", AP_MAC)],
            rem["rem_port_id"]: [(f"{rem['rem_port_id']}.0.5.1", "eth0")],
        }
        tables.update(overrides)
        net.tables[dev.ip_address] = tables
        return dev

    def test_normal_walk_returns_neighbor(self, net):
        dev = self._host(net)
        neighbors = collect_lldp_neighbors(dev, ap_only=False, ap_macs=set())
        assert [(n.local_port, n.remote_sys_name) for n in neighbors] == [("GE1/0/5", "AP-LOBBY-01")]

    def test_device_error_status_on_optional_table_keeps_neighbors(self, net):
        from apps.collectors.snmp_client import SnmpDeviceError

        dev = self._host(net, **{LLDP_OIDS["loc_port_id"]: SnmpDeviceError("noSuchName at 1")})
        neighbors = collect_lldp_neighbors(dev, ap_only=False, ap_macs=set())
        assert [(n.local_port, n.remote_sys_name) for n in neighbors] == [("GE1/0/5", "AP-LOBBY-01")]

    def test_remote_column_timeout_raises_instead_of_zero_neighbors(self, net):
        dev = self._host(net, **{LLDP_OIDS["rem_sys_name"]: ConnectionError("timeout")})
        with pytest.raises(ConnectionError):
            collect_lldp_neighbors(dev, ap_only=False, ap_macs=set())

    def test_ifdescr_timeout_raises_instead_of_ghost_port_names(self, net):
        dev = self._host(net, **{OID_IF_DESCR: ConnectionError("timeout")})
        with pytest.raises(ConnectionError):
            collect_lldp_neighbors(dev, ap_only=False, ap_macs=set())

    def test_soft_limit_during_walk_propagates(self, net):
        dev = self._host(net, **{LLDP_OIDS["rem_chassis_id"]: SoftTimeLimitExceeded()})
        with pytest.raises(SoftTimeLimitExceeded):
            collect_lldp_neighbors(dev, ap_only=False, ap_macs=set())


# --------------------------------------------------------------------------- cả lượt discovery
def build_chain(net: FakeNetwork, switches, *, ap_on: str | None = None) -> dict[str, str]:
    """Chuỗi SW1 — SW2 — SW3: cổng 1 hướng về switch trước, cổng 2 hướng về switch sau.

    AP (nếu có) cắm ở cổng 5 của `ap_on`. LLDP rỗng nên FDB là đường dò AP. Trả {tên: MAC switch}.
    """
    macs = {sw.name: f"02:00:00:00:00:{i:02x}" for i, sw in enumerate(switches, 1)}
    for idx, sw in enumerate(switches):
        rows = []
        for j, other in enumerate(switches):
            if j < idx:
                rows.append(_fdb_row(macs[other.name], 1))
            elif j > idx:
                rows.append(_fdb_row(macs[other.name], 2))
        if ap_on == sw.name:
            rows.append(_fdb_row(AP_MAC, 5))
        net.tables[str(sw.ip_address)] = {
            **_port_tables(),
            OID_DOT1Q_FDB_PORT: rows,
            OID_DOT1D_BASE_BRIDGE_ADDRESS: [(f"{OID_DOT1D_BASE_BRIDGE_ADDRESS}.0", macs[sw.name])],
            OID_IF_PHYS_ADDRESS: [],
        }
    return macs


@pytest.fixture
def fleet(db):
    """AC có 1 AP + 3 switch SNMP, tên sắp theo thứ tự quét mặc định (device_type, name)."""
    ac = HuaweiACDeviceFactory()
    WifiApStats.objects.create(
        device=ac, timestamp=timezone.now(), ap_name="AP-LOBBY-01", ap_mac=AP_MAC, is_online=True,
    )
    return [HuaweiSNMPDeviceFactory(name=f"T-SW{i}") for i in (1, 2, 3)]


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(topology_writer, "_monotonic", c, raising=False)
    return c


@pytest.mark.django_db
class TestDiscoverAllSwitches:
    def test_each_table_walked_once_per_run(self, net, fleet):
        build_chain(net, fleet, ap_on="T-SW3")
        discover_all_switches()
        for sw in fleet:
            host = str(sw.ip_address)
            # FDB: lượt dò AP + lượt dựng cạnh switch↔switch dùng chung 1 lần walk
            assert net.count(host, OID_DOT1Q_FDB_PORT) == 1, sw.name
            # LLDP: lượt AP + lượt uplink dùng chung 1 lần walk
            assert net.count(host, LLDP_OIDS["rem_sys_name"]) == 1, sw.name
            # ifDescr: 1 lần cho LLDP + 1 lần cho FDB
            assert net.count(host, OID_IF_DESCR) == 2, sw.name

    def test_results_same_as_before_walk_sharing(self, net, fleet):
        build_chain(net, fleet, ap_on="T-SW3")
        stats = discover_all_switches()

        ap_links = TopologyLink.objects.filter(link_kind="ap")
        assert [(link.local_device.name, link.local_port, link.remote_ap_mac) for link in ap_links] == [
            ("T-SW3", "GE1/0/5", AP_MAC),
        ]
        sw_links = {
            (link.local_device.name, link.local_port, link.remote_device.name)
            for link in TopologyLink.objects.filter(link_kind="switch")
        }
        assert sw_links == {("T-SW1", "GE1/0/2", "T-SW2"), ("T-SW2", "GE1/0/2", "T-SW3")}
        assert stats["switch_links"] == 2
        assert stats["errors"] == 0

    def test_unreachable_ifdescr_does_not_create_ghost_port_links(self, net, fleet):
        build_chain(net, fleet, ap_on="T-SW2")
        net.tables[str(fleet[1].ip_address)][OID_IF_DESCR] = ConnectionError(
            "No SNMP response received before timeout",
        )
        stats = discover_all_switches()

        ghosts = TopologyLink.objects.filter(local_port__startswith="port-")
        assert not ghosts.exists(), list(ghosts.values_list("local_device__name", "local_port"))
        assert stats["errors"] >= 1
        # SW2 là switch trung gian không đọc được FDB → không suy cạnh SW1–SW3 (cạnh ma)
        assert stats["switch_fdb_skipped"] == 1
        assert not TopologyLink.objects.filter(link_kind="switch").exists()
        # Switch khác vẫn được quét bình thường (pha riêng từng switch)
        walked = set(net.walked_hosts_in_order())
        assert {str(fleet[0].ip_address), str(fleet[2].ip_address)} <= walked

    def _switch_links(self) -> set[tuple[str, str]]:
        return {
            (link.local_device.name, link.remote_device.name)
            for link in TopologyLink.objects.filter(link_kind="switch").select_related(
                "local_device", "remote_device",
            )
        }

    def test_partial_lldp_failure_still_uses_intermediate_switch_in_fdb_phase(self, net, fleet):
        """Review ngoài 2026-10-15: chỉ cột LLDP của SW2 timeout, FDB/MAC tự thân của SW2 vẫn đọc được.

        SW2 là switch TRUNG GIAN: loại nó khỏi registry thì SW1 và SW3 "thấy nhau" không có gì xen
        giữa → suy ra cạnh ma SW1–SW3. Phải ra đúng SW1–SW2 và SW2–SW3 như khi không có lỗi.
        """
        build_chain(net, fleet)
        net.tables[str(fleet[1].ip_address)][LLDP_OIDS["rem_port_id"]] = ConnectionError(
            "No SNMP response received before timeout",
        )
        stats = discover_all_switches()

        assert self._switch_links() == {("T-SW1", "T-SW2"), ("T-SW2", "T-SW3")}
        assert stats["errors"] == 1  # LLDP của SW2 vẫn được báo là lỗi của riêng switch đó
        assert stats["switch_fdb_skipped"] == 0

    def test_unreachable_intermediate_switch_skips_inference_and_keeps_links(self, net, fleet):
        """SW2 chết hẳn: thiếu MAC tự thân/FDB của nó → KHÔNG đủ cơ sở suy ra cạnh trực tiếp.

        Suy luận "X nối trực tiếp Y khi không switch nào xen giữa" chỉ đúng khi biết MAC của MỌI
        switch; thiếu 1 switch thì tạo cạnh ma SW1–SW3 và đếm trượt 2 cạnh thật. Giữ link cũ.
        """
        build_chain(net, fleet)
        dead = str(fleet[1].ip_address)
        net.tables[dead] = {
            prefix: ConnectionError("No SNMP response received before timeout")
            for prefix in (
                OID_IF_DESCR, OID_DOT1D_BASEPORT_IFINDEX, OID_DOT1Q_FDB_PORT, OID_DOT1D_FDB_PORT,
                OID_DOT1D_BASE_BRIDGE_ADDRESS, OID_IF_PHYS_ADDRESS, *LLDP_OIDS.values(),
            )
        }
        old = [
            TopologyLink.objects.create(
                local_device=fleet[0], local_port="GE1/0/2", link_kind="switch",
                remote_device=fleet[1], protocol="fdb", match_method="mac", is_confirmed=True,
            ),
            TopologyLink.objects.create(
                local_device=fleet[1], local_port="GE1/0/2", link_kind="switch",
                remote_device=fleet[2], protocol="fdb", match_method="mac", is_confirmed=True,
            ),
        ]
        stats = discover_all_switches()

        assert self._switch_links() == {("T-SW1", "T-SW2"), ("T-SW2", "T-SW3")}  # không có SW1–SW3
        for link in old:
            link.refresh_from_db()
            assert link.miss_count == 0 and link.is_stale is False
        assert stats["switch_fdb_skipped"] == 1

    def test_incomplete_fleet_data_names_the_missing_switch(self, net, fleet, caplog):
        build_chain(net, fleet)
        dead = str(fleet[1].ip_address)
        net.tables[dead][OID_DOT1D_BASE_BRIDGE_ADDRESS] = ConnectionError("timeout")
        with caplog.at_level(logging.WARNING, logger="apps.collectors.topology_writer"):
            discover_all_switches()
        msg = next(r.getMessage() for r in caplog.records if "switch-FDB bỏ qua" in r.getMessage())
        assert "T-SW2" in msg

    def test_switch_with_empty_fdb_also_skips_inference(self, net, fleet):
        """FDB rỗng bất thường cũng là thiếu dữ liệu: cạnh tới switch đó sẽ biến mất rồi bị đếm trượt."""
        build_chain(net, fleet)
        net.tables[str(fleet[1].ip_address)][OID_DOT1Q_FDB_PORT] = []
        old = TopologyLink.objects.create(
            local_device=fleet[0], local_port="GE1/0/2", link_kind="switch",
            remote_device=fleet[1], protocol="fdb", match_method="mac", is_confirmed=True,
        )
        stats = discover_all_switches()
        old.refresh_from_db()
        assert old.miss_count == 0 and old.is_stale is False
        assert stats["switch_fdb_skipped"] == 1

    def test_soft_limit_inside_walk_stops_the_run(self, net, fleet):
        build_chain(net, fleet)
        net.tables[str(fleet[1].ip_address)][OID_DOT1Q_FDB_PORT] = SoftTimeLimitExceeded()
        with pytest.raises(SoftTimeLimitExceeded):
            discover_all_switches()
        # Không quét tiếp switch sau khi hết giờ
        assert str(fleet[2].ip_address) not in net.walked_hosts_in_order()

    def test_deadline_stops_before_next_switch_and_names_skipped(self, net, fleet, clock, caplog):
        build_chain(net, fleet)
        net.on_walk = lambda host, prefix: clock.advance(10)  # 10s mỗi walk
        with caplog.at_level(logging.WARNING, logger="apps.collectors.topology_writer"):
            stats = discover_all_switches(deadline_secs=50)

        assert net.walked_hosts_in_order() == [str(fleet[0].ip_address)]
        assert stats["skipped"] == 2
        assert stats["switch_fdb_skipped"] == 1
        msg = next(r.getMessage() for r in caplog.records if "dừng sớm" in r.getMessage())
        skipped_part = msg.split("bỏ qua:")[1]
        assert "T-SW2" in skipped_part and "T-SW3" in skipped_part
        assert "T-SW1" not in skipped_part

    def test_full_run_marks_unseen_fdb_link_missing(self, net, fleet):
        """Tiền đề của 2 test "không đánh stale": chạy đủ thì link không còn thấy bị đếm trượt."""
        build_chain(net, fleet)
        old = TopologyLink.objects.create(
            local_device=fleet[1], local_port="GE1/0/4", link_kind="switch",
            remote_device=fleet[2], protocol="fdb", match_method="mac", is_confirmed=True,
        )
        discover_all_switches()
        old.refresh_from_db()
        assert old.miss_count == 1

    def test_cut_run_does_not_mark_existing_links_missing(self, net, fleet, clock):
        build_chain(net, fleet)
        old = TopologyLink.objects.create(
            local_device=fleet[1], local_port="GE1/0/4", link_kind="switch",
            remote_device=fleet[2], protocol="fdb", match_method="mac", is_confirmed=True,
        )
        net.on_walk = lambda host, prefix: clock.advance(10)
        discover_all_switches(deadline_secs=50)
        old.refresh_from_db()
        assert old.miss_count == 0 and old.is_stale is False

    def test_deadline_between_phases_skips_switch_fdb_phase_whole(self, net, fleet, clock):
        build_chain(net, fleet)
        old = TopologyLink.objects.create(
            local_device=fleet[0], local_port="GE1/0/4", link_kind="switch",
            remote_device=fleet[1], protocol="fdb", match_method="mac", is_confirmed=True,
        )
        net.on_walk = lambda host, prefix: clock.advance(1)  # mỗi switch ~9 walk
        stats = discover_all_switches(deadline_secs=20)

        assert stats["skipped"] == 0  # cả 3 switch bắt đầu trước 20s
        assert stats["switch_fdb_skipped"] == 1  # nhưng hết giờ trước pha switch↔switch
        assert all(p != OID_DOT1D_BASE_BRIDGE_ADDRESS for _h, p in net.walks)
        old.refresh_from_db()
        assert old.miss_count == 0 and old.is_stale is False

    def test_within_deadline_everything_is_scanned(self, net, fleet, clock):
        build_chain(net, fleet)
        net.on_walk = lambda host, prefix: clock.advance(0.1)
        stats = discover_all_switches(deadline_secs=50)
        assert stats["skipped"] == 0 and stats["switch_fdb_skipped"] == 0
        assert stats["switch_links"] == 2

    def test_switch_with_failed_last_poll_is_scanned_last(self, net, fleet):
        build_chain(net, fleet)
        # T-SW1 theo tên đứng đầu, nhưng poll lần trước lỗi (last_seen=None) → quét sau cùng, để
        # nếu lượt bị cắt thì cắt đúng switch đang hỏng chứ không phải switch khoẻ.
        Device = type(fleet[0])
        Device.objects.filter(pk=fleet[0].pk).update(last_seen=None)
        Device.objects.filter(pk__in=[fleet[1].pk, fleet[2].pk]).update(last_seen=timezone.now())
        discover_all_switches()
        assert net.walked_hosts_in_order()[-1] == str(fleet[0].ip_address)


# --------------------------------------------------------------------------- lệnh verify
@pytest.mark.django_db
class TestVerifySwitchLinksCommand:
    def test_write_refuses_when_a_switch_has_no_data(self, net, fleet):
        from io import StringIO

        from django.core.management import call_command

        build_chain(net, fleet)
        net.tables[str(fleet[1].ip_address)][OID_DOT1D_BASE_BRIDGE_ADDRESS] = ConnectionError("timeout")
        out = StringIO()
        call_command("verify_switch_links", "--write", stdout=out)
        assert "KHÔNG ghi link" in out.getvalue()
        assert not TopologyLink.objects.filter(link_kind="switch").exists()


# --------------------------------------------------------------------------- task
class TestDiscoverTopologyLinksTask:
    def test_soft_limit_is_logged_not_raised(self, mocker, caplog):
        mocker.patch(
            "apps.collectors.topology_writer.discover_all_switches",
            side_effect=SoftTimeLimitExceeded(),
        )
        with caplog.at_level(logging.WARNING, logger="apps.collectors.tasks"):
            assert tasks.discover_topology_links.run() is None
        assert "Topology discovery" in caplog.text

    def test_task_gives_discovery_a_deadline_inside_the_soft_limit(self, mocker):
        mock_discover = mocker.patch(
            "apps.collectors.topology_writer.discover_all_switches",
            return_value={"switches": 0, "links": 0, "confirmed": 0, "errors": 0},
        )
        tasks.discover_topology_links.run()
        (_args, kwargs) = mock_discover.call_args
        deadline = kwargs["deadline_secs"]
        assert 0 < deadline < tasks.TOPOLOGY_DISCOVER_SOFT_LIMIT
