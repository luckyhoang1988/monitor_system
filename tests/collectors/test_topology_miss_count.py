"""TopologyLink.miss_count không được tăng vô hạn (tồn đọng từ 1.9 (d)).

`miss_count` là PositiveSmallIntegerField (Postgres smallint, tối đa 32767). Cả 3 vòng "đánh dấu
không còn thấy" trong topology_writer vẫn cộng 1 cho link ĐÃ stale mỗi lượt discovery, nên số đếm
tăng mãi: đo prod 2026-10-15, 20/59 link đã stale, lớn nhất 4304 sau 111 ngày (~39/ngày). Chạm 32768
thì mỗi lượt `save()` ném DataError (smallint out of range) và bỏ qua phần còn lại của switch đó.

Cũng là 1 câu UPDATE vô ích/link stale/lượt: 20 câu mỗi 30 phút trên prod.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.collectors import topology_switch_fdb, topology_writer
from apps.collectors.topology_writer import STALE_MISS_THRESHOLD
from apps.devices.models import TopologyLink
from tests.conftest import HuaweiSNMPDeviceFactory

pytestmark = pytest.mark.django_db

SMALLINT_MAX = 32767  # PositiveSmallIntegerField trên Postgres

# Ba vòng "không còn thấy" — mỗi vòng lọc link theo (loại, giao thức) riêng.
CASES = {
    "switch_fdb": {"link_kind": "switch", "protocol": "fdb"},
    "switch_lldp": {"link_kind": "switch", "protocol": "lldp"},
    "ap_lldp": {"link_kind": "ap", "protocol": "lldp"},
}


@pytest.fixture(params=list(CASES))
def case(request, monkeypatch) -> SimpleNamespace:
    """Chạy đúng 1 vòng discovery với dữ liệu mạng RỖNG: mọi link của loại đó đều "không còn thấy"."""
    monkeypatch.setattr(topology_writer, "collect_lldp_neighbors", lambda *a, **k: [])
    monkeypatch.setattr(topology_writer, "load_ac_ap_snapshot", lambda ac: {})
    monkeypatch.setattr(
        topology_switch_fdb, "build_switch_mac_registry", lambda switches, missing=None: {},
    )
    monkeypatch.setattr(
        topology_switch_fdb, "discover_switch_adjacency",
        lambda switches, registry, missing=None: [],
    )
    runners = {
        "switch_fdb": lambda dev: topology_writer.discover_switch_links_fdb([dev]),
        "switch_lldp": lambda dev: topology_writer.discover_switch_uplinks(dev, ({}, {})),
        "ap_lldp": lambda dev: topology_writer.upsert_switch_topology(dev, ac_device=object()),
    }
    name = request.param
    return SimpleNamespace(name=name, link=CASES[name], run=runners[name])


def make_link(device, case, **fields) -> TopologyLink:
    return TopologyLink.objects.create(
        local_device=device, local_port="GE1/0/1", match_method="mac", is_confirmed=True,
        **case.link, **fields,
    )


class TestMissingLinkIsCountedThenMarkedStale:
    """Hành vi sẵn có phải giữ nguyên: đếm từng lượt, chạm ngưỡng thì stale."""

    def test_counts_each_run_and_goes_stale_at_threshold(self, case):
        device = HuaweiSNMPDeviceFactory()
        link = make_link(device, case)

        seen = []
        for _ in range(STALE_MISS_THRESHOLD):
            case.run(device)
            link.refresh_from_db()
            seen.append((link.miss_count, link.is_stale))

        assert seen == [(1, False), (2, False), (3, True)]

    def test_link_below_threshold_is_not_stale_yet(self, case):
        device = HuaweiSNMPDeviceFactory()
        link = make_link(device, case, miss_count=1)

        case.run(device)

        link.refresh_from_db()
        assert link.miss_count == 2 and link.is_stale is False


class TestStaleLinkStopsCounting:
    @pytest.mark.parametrize(
        "start",
        [
            pytest.param(STALE_MISS_THRESHOLD, id="exactly_at_threshold"),
            pytest.param(4304, id="largest_value_seen_on_prod"),
            pytest.param(SMALLINT_MAX, id="column_limit"),
        ],
    )
    def test_stale_link_keeps_its_value_and_never_raises(self, case, start):
        """Ở mã cũ: start=32767 ném DataError; các giá trị khác bị cộng thêm 1 mỗi lượt."""
        device = HuaweiSNMPDeviceFactory()
        link = make_link(device, case, miss_count=start, is_stale=True)

        case.run(device)
        case.run(device)

        link.refresh_from_db()
        assert link.miss_count == start
        assert link.is_stale is True

    def test_link_unstaled_by_hand_with_large_counter_goes_stale_again(self, case):
        """Admin bỏ cờ stale nhưng quên đặt lại bộ đếm: lượt kế không được tràn cột."""
        device = HuaweiSNMPDeviceFactory()
        link = make_link(device, case, miss_count=SMALLINT_MAX, is_stale=False)

        case.run(device)

        link.refresh_from_db()
        assert link.is_stale is True
        assert link.miss_count == STALE_MISS_THRESHOLD

    def test_stale_link_is_not_rewritten(self, case):
        device = HuaweiSNMPDeviceFactory()
        make_link(device, case, miss_count=STALE_MISS_THRESHOLD, is_stale=True)

        with CaptureQueriesContext(connection) as ctx:
            case.run(device)

        updates = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith("UPDATE") and "devices_topologylink" in q["sql"]
        ]
        assert updates == []

    def test_other_links_are_still_counted_next_to_a_stale_one(self, case):
        device = HuaweiSNMPDeviceFactory()
        make_link(device, case, miss_count=4304, is_stale=True)
        fresh = TopologyLink.objects.create(
            local_device=device, local_port="GE1/0/2", match_method="mac", is_confirmed=True,
            **case.link,
        )

        case.run(device)

        fresh.refresh_from_db()
        assert fresh.miss_count == 1 and fresh.is_stale is False
