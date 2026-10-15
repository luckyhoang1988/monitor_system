"""Bộ nhớ đệm kết quả walk SNMP cho MỘT lượt topology discovery.

Một lượt discovery từng walk cùng một bảng nhiều lần trên cùng switch: FDB 2 lần (lần 1 để dò AP
cho switch không có LLDP, lần 2 để dựng cạnh switch↔switch) và LLDP 2 lần (AP rồi uplink). Đo prod
2026-10-15 (17 switch): ~45% của 149,8s là walk lặp. Bộ nhớ đệm này chỉ sống trong
`with walk_memo():` — ngoài khối đó (lệnh verify, test) mọi lời gọi vẫn walk thật.

Chỉ lưu dữ liệu THÔ đã walk (chưa lọc theo AP/uplink) nên các nơi gọi với tham số lọc khác nhau
vẫn dùng chung được. Lần walk lỗi KHÔNG được lưu: lỗi nổi lên cho nơi gọi, lần sau thử lại.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_memo: ContextVar[dict[Any, Any] | None] = ContextVar("topology_walk_memo", default=None)


@contextmanager
def walk_memo() -> Iterator[None]:
    """Bật bộ nhớ đệm cho các lời gọi `memoized` bên trong khối."""
    token = _memo.set({})
    try:
        yield
    finally:
        _memo.reset(token)


def memoized[T](key: Any, walk: Callable[[], T]) -> T:
    """Trả kết quả đã walk cho `key` trong lượt hiện tại, chưa có thì gọi `walk()` rồi lưu."""
    memo = _memo.get()
    if memo is None:
        return walk()
    if key not in memo:
        memo[key] = walk()
    return memo[key]
