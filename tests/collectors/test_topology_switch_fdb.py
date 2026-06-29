"""Tests cho thuật toán phát hiện switch↔switch qua FDB (giao-rỗng trên cây)."""
from unittest.mock import patch

from apps.collectors.topology_switch_fdb import discover_switch_adjacency


class _FakeSw:
    def __init__(self, sid: int, name: str):
        self.id = sid
        self.name = name


# Cây 3 tầng:  CORE(1) ── D1(2) ── A1(4), A2(5)
#                    └─── D2(3)
# Mỗi switch 1 MAC: 00:..:0<id>. FDB mỗi switch = MAC switch khác học qua từng cổng.
MAC = {i: f"00:00:00:00:00:0{i}" for i in range(1, 6)}
REGISTRY = {mac: sid for sid, mac in MAC.items()}

FDB_BY_ID = {
    # CORE: hướng D1 thấy cả subtree D1 (D1,A1,A2); hướng D2 thấy D2.
    1: {"c2d1": {MAC[2], MAC[4], MAC[5]}, "c2d2": {MAC[3]}},
    # D1: hướng lên core thấy CORE+D2; xuống A1/A2 mỗi cổng 1 AP-switch.
    2: {"d1up": {MAC[1], MAC[3]}, "d1a1": {MAC[4]}, "d1a2": {MAC[5]}},
    # D2: chỉ 1 cổng lên core, thấy mọi switch còn lại.
    3: {"d2up": {MAC[1], MAC[2], MAC[4], MAC[5]}},
    # A1: 1 cổng lên D1, thấy mọi switch còn lại.
    4: {"a1up": {MAC[1], MAC[2], MAC[3], MAC[5]}},
    # A2: 1 cổng lên D1, thấy mọi switch còn lại.
    5: {"a2up": {MAC[1], MAC[2], MAC[3], MAC[4]}},
}

SWITCHES = [_FakeSw(i, f"SW{i}") for i in range(1, 6)]


def _fake_fdb(device):
    return FDB_BY_ID[device.id]


def _pairs_as_sets(results):
    return {frozenset((a, b)) for a, _pa, b, _pb in results}


@patch("apps.collectors.topology_switch_fdb.collect_switch_fdb_by_port", _fake_fdb)
def test_detects_only_direct_tree_links():
    results = discover_switch_adjacency(SWITCHES, REGISTRY)
    pairs = _pairs_as_sets(results)
    # Chỉ các cạnh trực tiếp: CORE-D1, CORE-D2, D1-A1, D1-A2.
    assert pairs == {
        frozenset((1, 2)),
        frozenset((1, 3)),
        frozenset((2, 4)),
        frozenset((2, 5)),
    }


@patch("apps.collectors.topology_switch_fdb.collect_switch_fdb_by_port", _fake_fdb)
def test_intermediate_switch_excluded():
    """CORE-A1 (có D1 xen giữa) và D1-D2 (qua core) KHÔNG được coi là trực tiếp."""
    pairs = _pairs_as_sets(discover_switch_adjacency(SWITCHES, REGISTRY))
    assert frozenset((1, 4)) not in pairs  # CORE ↔ A1 gián tiếp
    assert frozenset((1, 5)) not in pairs  # CORE ↔ A2 gián tiếp
    assert frozenset((2, 3)) not in pairs  # D1 ↔ D2 qua core
    assert frozenset((4, 5)) not in pairs  # A1 ↔ A2 qua D1


@patch("apps.collectors.topology_switch_fdb.collect_switch_fdb_by_port", _fake_fdb)
def test_reports_correct_ports():
    """Cặp CORE-D1 phải trả đúng cổng mỗi đầu."""
    results = discover_switch_adjacency(SWITCHES, REGISTRY)
    by_pair = {frozenset((a, b)): (a, pa, b, pb) for a, pa, b, pb in results}
    a, pa, b, pb = by_pair[frozenset((1, 2))]
    ports = {a: pa, b: pb}
    assert ports[1] == "c2d1"
    assert ports[2] == "d1up"


@patch("apps.collectors.topology_switch_fdb.collect_switch_fdb_by_port",
       lambda device: {} if device.id == 3 else FDB_BY_ID[device.id])
def test_empty_fdb_switch_skipped():
    """Switch có FDB rỗng (D2) bị bỏ qua — không sinh cạnh tới nó vòng này."""
    pairs = _pairs_as_sets(discover_switch_adjacency(SWITCHES, REGISTRY))
    assert all(3 not in p for p in pairs)
    # Các cạnh không liên quan D2 vẫn còn.
    assert frozenset((1, 2)) in pairs
    assert frozenset((2, 4)) in pairs
