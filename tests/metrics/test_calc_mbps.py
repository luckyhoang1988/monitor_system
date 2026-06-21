"""Tests cho _calc_mbps / _counter_delta — xử lý counter wrap, reset, sanity cap."""
from datetime import datetime, timedelta, timezone

from apps.collectors.base import InterfaceData
from apps.metrics.models import InterfaceStats
from apps.metrics.writer import _calc_mbps, _counter_delta

MAX64 = 2**64


def _prev(in_bytes: int, out_bytes: int, ts: datetime) -> InterfaceStats:
    """InterfaceStats chưa lưu DB — chỉ cần các field _calc_mbps đọc."""
    return InterfaceStats(timestamp=ts, in_bytes=in_bytes, out_bytes=out_bytes)


def _new(in_bytes: int, out_bytes: int, speed_mbps: float = 0.0) -> InterfaceData:
    return InterfaceData(
        name="Gi0/1", if_index=1, status="up",
        in_bytes=in_bytes, out_bytes=out_bytes, speed_mbps=speed_mbps,
    )


class TestCounterDelta:
    def test_normal_positive_delta(self):
        assert _counter_delta(1000, 3000, MAX64) == 2000

    def test_wrap_when_prev_near_ceiling(self):
        # prev gần trần → coi là tràn vòng, cộng max_counter
        prev = MAX64 - 1000
        assert _counter_delta(prev, 2000, MAX64) == 3000

    def test_reset_when_prev_small(self):
        # prev nhỏ mà delta âm → reset (reboot) → trả 0
        assert _counter_delta(1_000_000, 0, MAX64) == 0


class TestCalcMbps:
    def test_returns_zero_without_prev(self):
        assert _calc_mbps(None, _new(0, 0), datetime.now(tz=timezone.utc), 300) == (0.0, 0.0)

    def test_normal_throughput(self):
        ts = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
        prev = _prev(0, 0, ts)
        # 1_250_000 bytes trong 1s = 10 Mbps
        new = _new(1_250_000, 1_250_000)
        in_mbps, out_mbps = _calc_mbps(prev, new, ts + timedelta(seconds=1), 300)
        assert in_mbps == 10.0
        assert out_mbps == 10.0

    def test_counter_wrap_keeps_spike(self):
        ts = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
        prev = _prev(MAX64 - 1000, MAX64 - 1000, ts)
        new = _new(2000, 2000)  # wrap: delta thực = 3000 bytes
        in_mbps, out_mbps = _calc_mbps(prev, new, ts + timedelta(seconds=1), 300)
        assert in_mbps > 0
        assert out_mbps > 0

    def test_counter_reset_returns_zero(self):
        ts = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
        prev = _prev(1_000_000, 1_000_000, ts)
        new = _new(0, 0)  # reset → delta âm, prev nhỏ → 0
        assert _calc_mbps(prev, new, ts + timedelta(seconds=1), 300) == (0.0, 0.0)

    def test_sanity_cap_drops_garbage_spike(self):
        ts = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
        prev = _prev(0, 0, ts)
        # 100 Mbps port nhưng counter nhảy ~1000 Mbps trong 1s → vượt cap 1.5× → bỏ mẫu
        new = _new(125_000_000, 125_000_000, speed_mbps=100.0)
        assert _calc_mbps(prev, new, ts + timedelta(seconds=1), 300) == (0.0, 0.0)
