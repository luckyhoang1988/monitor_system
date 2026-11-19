"""Review ngoài lần 4 (2026-11-19) — P1: cảnh báo giả từ mẫu DB cũ KHI CHƯA TỪNG có alert nào hồi phục.

Lần 3 chặn ca "alert đã hồi phục nổ lại" bằng `Alert.resolved_at`. Còn biến thể không có mốc đó: CPU 99% ghi vào DB (Redis lỗi)
nhưng chưa đủ cửa sổ để nổ → Redis phục hồi, CPU 10% (chỉ ở Redis) → Redis mất dữ liệu → engine đọc lại chuỗi 99% cũ và nổ.
Engine cần một dấu BỀN cho biết "đã có mẫu mới hơn mẫu DB này": `Device.metrics_seen_at` — writer ghi mốc mẫu mới nhất đã
đưa vào Redis (cùng nơi duy nhất biết Redis vừa nhận mẫu), engine bỏ mẫu DB đường lui cũ hơn mốc đó.
"""
from datetime import UTC, datetime, timedelta

import pytest

from apps.alerts.engine import (
    _count_vms_running,
    _latest_mem,
    _port_down_throughout,
    _recovery_evidence_for,
    _sustained_cpu_mem,
    _sustained_host_perf,
    _sustained_uplink_traffic_max,
    _sustained_vm_metric,
    _sustained_wifi_ap_offline_count,
    _sustained_wifi_client_count,
    _uplink_statuses,
    check_device_alerts,
)
from apps.alerts.models import Alert, AlertRule
from apps.collectors.base import NormalizedData
from apps.devices.models import Device, Interface
from apps.metrics import cache as metrics_cache
from apps.metrics.models import InterfaceStats, SystemHealth, VMStats, WifiApStats
from apps.metrics.writer import save_metrics
from tests.conftest import CiscoSNMPDeviceFactory, HyperVDeviceFactory

pytestmark = pytest.mark.django_db


def _now(offset_secs: float = 0.0) -> datetime:
    return datetime.now(tz=UTC) - timedelta(seconds=offset_secs)


def _since(minutes: int = 10) -> datetime:
    return _now() - timedelta(minutes=minutes)


def _rule(**overrides) -> AlertRule:
    defaults = dict(
        name="Sustained CPU", device_type="all", metric="cpu_percent", condition="gt", threshold=90.0,
        severity="WARNING", channels=[], enabled=True, duration_min=4,
    )
    defaults.update(overrides)
    return AlertRule.objects.create(**defaults)


def _active(device, rule=None) -> bool:
    qs = Alert.objects.filter(device=device, is_active=True)
    return (qs.filter(rule=rule) if rule else qs).exists()


@pytest.fixture
def cache_mode(settings):
    settings.METRICS_WRITE_MODE = "cache"
    client = metrics_cache._get_client()
    if client is None:
        pytest.skip("Redis /1 không sẵn sàng")
    yield client
    for pat in ("m:latest:*", "m:series:sys:*", "m:series:if:*"):
        keys = client.keys(pat)
        if keys:
            client.delete(*keys)


def _redis_writes_fail(monkeypatch):
    monkeypatch.setattr(metrics_cache, "set_latest", lambda *a, **k: False)
    monkeypatch.setattr(metrics_cache, "push_series", lambda *a, **k: False)


def _nd(device, cpu=50.0, ts=None) -> NormalizedData:
    return NormalizedData(
        device_name=device.name, ip_address=device.ip_address, timestamp=ts or _now(), os_family="cisco_ios",
        cpu_percent=cpu, mem_percent=40.0, interfaces=[], extra={},
    )


def _seen_at(device):
    return Device.objects.get(pk=device.pk).metrics_seen_at


# ─── P1 · mẫu DB cũ khi chưa từng có alert hồi phục ──────────────────────────────────────────────────────────

class TestStaleDbSamplesAreNotEvidenceOfNow:
    def test_sustained_rule_does_not_fire_from_old_fallback_rows_after_redis_recovered_then_lost(
            self, cache_mode, monkeypatch):
        """Đúng kịch bản review: 99% vào DB (chưa đủ cửa sổ nên chưa nổ) → Redis lại, 10% → Redis mất dữ liệu."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        _redis_writes_fail(monkeypatch)
        for off in (250, 160):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))
        assert not Alert.objects.filter(device=device).exists()                 # chưa có alert nào, chưa có mốc hồi phục

        monkeypatch.undo()                                                      # Redis phục hồi
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(10)))
        cache_mode.flushdb()                                                    # rồi mất dữ liệu
        check_device_alerts(device, _since())

        assert not _active(device, rule)

    def test_instant_rule_does_not_fire_from_an_old_fallback_row_either(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Instant CPU", duration_min=0)
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(100)))               # chưa kịp đánh giá lần nào
        monkeypatch.undo()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(10)))
        cache_mode.flushdb()

        check_device_alerts(device, _since())

        assert not _active(device, rule)

    # ── đối chứng: đường lui vẫn dùng được khi KHÔNG có mẫu nào mới hơn ───────────────────────────────────────

    def test_steady_fallback_still_fires(self, cache_mode, monkeypatch):
        """Redis lỗi suốt: mọi mẫu nằm ở DB, mẫu mới nhất chính là mẫu cuối cùng → vẫn phải nổ."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        _redis_writes_fail(monkeypatch)
        for off in (250, 160, 70):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_fallback_after_a_period_on_redis_still_fires(self, cache_mode, monkeypatch):
        """Redis chạy bình thường rồi lỗi ghi: các mẫu DB sau đó MỚI HƠN mốc Redis cuối → vẫn nổ."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(400)))               # Redis còn ghi được
        _redis_writes_fail(monkeypatch)
        for off in (250, 160, 70):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_redis_holding_the_newest_samples_is_authoritative(self, cache_mode, monkeypatch):
        """Mẫu DB cũ hơn mốc nhưng Redis còn mẫu mới nhất: chuỗi ghép vẫn hợp lệ (Redis là nguồn chủ)."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        _redis_writes_fail(monkeypatch)
        for off in (400, 310):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))           # nửa đầu ở DB
        monkeypatch.undo()
        for off in (220, 130, 40):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))           # nửa sau ở Redis

        check_device_alerts(device, _since())

        assert _active(device, rule)


# ─── dấu bền `metrics_seen_at` do writer ghi ─────────────────────────────────────────────────────────────────

class TestWriterRecordsTheNewestSampleItHandedToRedis:
    def test_marker_follows_the_sample_written_to_redis(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        ts = _now(30)

        save_metrics(device, _nd(device, ts=ts))

        assert abs((_seen_at(device) - ts).total_seconds()) < 0.001

    def test_marker_never_moves_backwards(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        newer = _now(10)
        save_metrics(device, _nd(device, ts=newer))

        save_metrics(device, _nd(device, ts=_now(200)))                         # mẫu cũ đến muộn

        assert abs((_seen_at(device) - newer).total_seconds()) < 0.001

    def test_marker_is_not_written_when_the_sample_goes_to_the_db(self, cache_mode, monkeypatch):
        """Ghi đường lui: chính dòng DB là bằng chứng, không cần thêm 1 lần UPDATE Device mỗi poll."""
        device = CiscoSNMPDeviceFactory()
        _redis_writes_fail(monkeypatch)

        save_metrics(device, _nd(device, ts=_now(10)))

        assert _seen_at(device) is None
        assert SystemHealth.objects.filter(device=device).count() == 1

    def test_marker_is_not_written_in_db_mode(self, settings):
        settings.METRICS_WRITE_MODE = "db"
        device = CiscoSNMPDeviceFactory()

        save_metrics(device, _nd(device, ts=_now(10)))

        assert _seen_at(device) is None

    def test_marker_is_on_the_in_memory_device_too(self, cache_mode):
        """Nơi gọi tiếp theo (engine inline sau poll) dùng đúng đối tượng Device này."""
        device = CiscoSNMPDeviceFactory()
        ts = _now(20)

        save_metrics(device, _nd(device, ts=ts))

        assert device.metrics_seen_at == ts


# ─── mọi họ getter / chuỗi sustained đều bỏ đuôi DB cũ hơn mốc ───────────────────────────────────────────────

_WINDOW_OFFSETS = (700, 610, 520, 430, 340, 250, 160, 70)


def _set_marker(device, off_secs):
    Device.objects.filter(pk=device.pk).update(metrics_seen_at=_now(off_secs))


class TestEveryFamilyIgnoresAStaleDbTail:
    """Dựng chuỗi DB (Redis trống) rồi hỏi hàm đọc: trước khi có mốc phải đọc được (đối chứng), sau khi writer báo đã có mẫu
    mới hơn mọi dòng DB thì không còn bằng chứng nào về hiện tại."""

    @staticmethod
    def _check(device, rule, read):
        with _recovery_evidence_for(device, rule):
            assert read() is not None, "đối chứng: chưa có mốc thì chuỗi DB phải đọc được"
        _set_marker(device, 5)
        with _recovery_evidence_for(device, rule):
            assert read() is None, "đuôi DB cũ hơn mẫu mới nhất đã vào Redis không được là bằng chứng về hiện tại"

    def test_mem_instant(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Mem", metric="mem_percent", duration_min=0)
        SystemHealth.objects.create(device=device, timestamp=_now(100), cpu_percent=1.0, mem_percent=95.0)

        self._check(device, rule, lambda: _latest_mem(device, _since()))

    def test_mem_sustained(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Mem", metric="mem_percent", duration_min=10)
        for off in _WINDOW_OFFSETS:
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=1.0, mem_percent=95.0)

        self._check(device, rule, lambda: _sustained_cpu_mem(device, rule, _now() - timedelta(minutes=10)))

    def test_host_perf_sustained(self, cache_mode):
        device = HyperVDeviceFactory()
        rule = _rule(name="Latency", metric="disk_read_latency_ms", threshold=20.0, duration_min=20)
        for off in (1500, 1200, 900, 600, 300, 100):
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=1.0, mem_percent=1.0,
                                        disk_read_latency_ms=50.0)

        self._check(device, rule, lambda: _sustained_host_perf(device, rule, _now() - timedelta(minutes=20)))

    def test_vm_sustained_and_instant(self, cache_mode):
        device = HyperVDeviceFactory()
        sustained = _rule(name="VM repl", metric="vm_repl_unhealthy", threshold=0.0, duration_min=10)
        instant = _rule(name="VM run", metric="vm_count_running", condition="lt", threshold=5.0, duration_min=0)
        for off in _WINDOW_OFFSETS:
            VMStats.objects.create(device=device, timestamp=_now(off), vm_name="vm1", state="Running",
                                   repl_health="Critical")

        self._check(device, sustained, lambda: _sustained_vm_metric(device, sustained, _now() - timedelta(minutes=10)))
        Device.objects.filter(pk=device.pk).update(metrics_seen_at=None)
        self._check(device, instant, lambda: _count_vms_running(device, _since()))

    def test_wifi_sustained(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        clients = _rule(name="Wifi clients", metric="wifi_client_count", threshold=0.0, duration_min=10)
        offline = _rule(name="AP off", metric="wifi_ap_offline", threshold=0.0, duration_min=10)
        for off in _WINDOW_OFFSETS:
            WifiApStats.objects.create(device=device, timestamp=_now(off), ap_name="ap1", ap_mac="aa:bb:cc:00:00:01",
                                       ap_ip="10.0.0.9", ap_group="g", is_online=False, run_state="offline",
                                       client_count=3)
        window = _now() - timedelta(minutes=10)

        self._check(device, clients, lambda: _sustained_wifi_client_count(device, clients, window))
        Device.objects.filter(pk=device.pk).update(metrics_seen_at=None)
        self._check(device, offline, lambda: _sustained_wifi_ap_offline_count(device, offline, window))

    def test_uplink_traffic_sustained(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1", is_uplink=True)
        rule = _rule(name="Up long", metric="uplink_in_mbps_max", threshold=40.0, duration_min=10)
        for off in _WINDOW_OFFSETS:
            InterfaceStats.objects.create(interface=iface, timestamp=_now(off), status="up", in_mbps=50.0,
                                          out_mbps=0.0)

        self._check(device, rule,
                    lambda: _sustained_uplink_traffic_max(device, rule, _now() - timedelta(minutes=10)))

    def test_port_down_streak(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1")
        rule = AlertRule.objects.create(
            name="Uplink Down", device_type="switch", metric="if_status", condition="eq", threshold=0.0,
            severity="CRITICAL", channels=[], enabled=True, duration_min=5,
        )
        for off in (400, 310, 220, 130, 40):
            InterfaceStats.objects.create(interface=iface, timestamp=_now(off), status="down")
        window = _now() - timedelta(minutes=5)

        self._check(device, rule, lambda: True if _port_down_throughout(device, iface.pk, window) else None)

    def test_a_port_status_stale_tail_is_not_the_current_state(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1")
        rule = AlertRule.objects.create(
            name="Uplink Down", device_type="switch", metric="if_status", condition="eq", threshold=0.0,
            severity="CRITICAL", channels=[], enabled=True,
        )
        InterfaceStats.objects.create(interface=iface, timestamp=_now(100), status="down")

        with _recovery_evidence_for(device, rule):
            assert _uplink_statuses(device, [iface.pk], _since()) == {iface.pk: "down"}
        _set_marker(device, 5)
        with _recovery_evidence_for(device, rule):
            assert _uplink_statuses(device, [iface.pk], _since()) == {iface.pk: None}


class TestMarkerWriteFailureNeverBreaksThePoll:
    def test_save_metrics_still_stores_the_sample_when_the_marker_update_fails(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()

        def boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(Device.objects, "filter", boom)

        save_metrics(device, _nd(device, cpu=42.0, ts=_now(10)))             # không được ném

        assert metrics_cache.get_latest(device.id)["cpu"] == 42.0
