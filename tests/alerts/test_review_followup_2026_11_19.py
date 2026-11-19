"""Review ngoài lần 2 (2026-11-19) soi lại CHÍNH bản sửa F01/F02 — 4 điểm, tái hiện bằng test đỏ trước khi sửa.

P1-1  Redis mất dữ liệu: getter đọc DB và nhặt DÒNG BẰNG CHỨNG (ghi lúc alert nổ / đổi trạng thái — bản sao của mẫu
      cũ) làm "mẫu mới nhất" → alert đã hồi phục nổ lại.
P1-2  Redis ghi lỗi nhưng đọc được: writer rơi về ghi DB nhưng getter vẫn chọn snapshot Redis cũ (còn "tươi" theo
      cửa sổ) → bỏ qua mẫu DB mới hơn, F01 chưa khép kín.
P2-3  `start_slack = 1,5 × nhịp poll` có thể LỚN HƠN cả cửa sổ rule (HyperV 300s → 450s > 5 phút; rule mạng 2 phút →
      135s > 120s) nên 1 mẫu vừa xuất hiện vẫn đủ nổ.
P2-4  `_expected_poll_secs` lấy max(network, ping) cho mọi thiết bị thay vì theo nguồn poll của chính thiết bị.
"""
from datetime import UTC, datetime, timedelta

import pytest

from apps.alerts.engine import (
    _count_vms_running,
    _expected_poll_secs,
    _port_down_throughout,
    _uplink_statuses,
    _uplink_traffic_max,
    check_device_alerts,
)
from apps.alerts.models import Alert, AlertRule
from apps.collectors.base import InterfaceData, NormalizedData
from apps.devices.models import Interface
from apps.metrics import cache as metrics_cache
from apps.metrics.models import InterfaceStats, SystemHealth
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
        severity="WARNING", channels=[], enabled=True, duration_min=10,
    )
    defaults.update(overrides)
    return AlertRule.objects.create(**defaults)


def _active(device) -> bool:
    return Alert.objects.filter(device=device, is_active=True).exists()


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


def _nd(device, cpu=50.0, ts=None, extra=None) -> NormalizedData:
    return NormalizedData(
        device_name=device.name, ip_address=device.ip_address, timestamp=ts or _now(),
        os_family="cisco_ios", cpu_percent=cpu, mem_percent=40.0, interfaces=[], extra=extra or {},
    )


def _hv_nd(device, latency, ts) -> NormalizedData:
    return NormalizedData(
        device_name=device.name, ip_address=device.ip_address, timestamp=ts, os_family="hyperv_winrm",
        cpu_percent=1.0, mem_percent=1.0, disk_read_latency_ms=latency, interfaces=[], extra={},
    )


# ─── P1-1 · dòng bằng chứng không phải là mẫu ────────────────────────────────

class TestEvidenceRowsAreNotSamples:
    def test_resolved_alert_does_not_refire_from_its_own_evidence_row_after_a_redis_flush(self, cache_mode):
        """CPU 99% → ALERT (ghi 1 dòng bằng chứng) → CPU 10% → hồi phục → Redis mất dữ liệu: dòng bằng chứng 99%
        trong DB KHÔNG được đọc như 'CPU hiện tại' để nổ lại."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Instant CPU", duration_min=0)

        save_metrics(device, _nd(device, cpu=99.0, ts=_now(100)))
        check_device_alerts(device, _since())
        assert Alert.objects.filter(device=device, rule=rule, is_active=True).count() == 1
        assert SystemHealth.objects.filter(device=device).count() == 1        # dòng bằng chứng lúc nổ

        save_metrics(device, _nd(device, cpu=10.0, ts=_now(10)))
        check_device_alerts(device, _since())
        assert not _active(device)                                            # đã hồi phục

        cache_mode.flushdb()                                                  # Redis mất snapshot
        check_device_alerts(device, _since())

        assert not _active(device)
        assert Alert.objects.filter(device=device, rule=rule).count() == 1    # không có ALERT thứ hai

    def test_change_event_context_row_is_not_a_sample_either(self, cache_mode):
        """Dòng SystemHealth 'ngữ cảnh' ghi cùng lúc VM đổi trạng thái cũng là bằng chứng, không phải mẫu poll."""
        device = CiscoSNMPDeviceFactory()
        _rule(name="Instant CPU", duration_min=0)
        running = {"vms": [{"name": "vm1", "state": "Running", "repl_health": "Normal"}]}
        stopped = {"vms": [{"name": "vm1", "state": "Off", "repl_health": "Normal"}]}

        save_metrics(device, _nd(device, cpu=10.0, ts=_now(200), extra=running))
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(100), extra=stopped))   # VM đổi → dòng ngữ cảnh
        assert SystemHealth.objects.filter(device=device).count() == 1

        cache_mode.flushdb()
        check_device_alerts(device, _since())

        assert not _active(device)

    def test_evidence_rows_carry_an_explicit_marker(self, cache_mode):
        """Dấu hiệu rõ ràng trong dữ liệu để phân biệt dòng bằng chứng với mẫu poll thường."""
        device = CiscoSNMPDeviceFactory()
        _rule(name="Instant CPU", duration_min=0)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(30)))
        check_device_alerts(device, _since())

        row = SystemHealth.objects.get(device=device)

        assert row.extra.get("_evidence") is True


# ─── P1-2 · mẫu DB mới hơn Redis thì DB thắng ────────────────────────────────

class TestNewerDbSampleBeatsOlderRedis:
    def test_instant_rule_uses_the_newer_db_sample_when_redis_writes_fail_but_reads_work(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()
        _rule(name="Instant CPU", duration_min=0)
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(100)))                     # Redis còn ghi được
        monkeypatch.setattr(metrics_cache, "set_latest", lambda *a, **k: False)       # từ đây ghi lỗi, đọc vẫn được
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(0)))                       # writer rơi về ghi DB
        assert SystemHealth.objects.filter(device=device, cpu_percent=99.0).count() == 1

        check_device_alerts(device, _since())

        assert _active(device)

    def test_a_stale_redis_snapshot_does_not_hide_the_newer_db_sample(self, cache_mode, monkeypatch):
        """Snapshot Redis đã cũ hơn cửa sổ `since` (ghi lỗi từ lâu) — trước đây bị coi là 'thiết bị ngừng poll'
        nên getter im lặng dù DB có mẫu poll thật vài chục giây trước."""
        device = CiscoSNMPDeviceFactory()
        _rule(name="Instant CPU", duration_min=0)
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(1500)))                    # snapshot Redis 25 phút trước
        monkeypatch.setattr(metrics_cache, "set_latest", lambda *a, **k: False)
        monkeypatch.setattr(metrics_cache, "push_series", lambda *a, **k: False)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(30)))                      # mẫu thật, ghi vào DB

        check_device_alerts(device, _since())

        assert _active(device)

    def test_sustained_rule_merges_redis_and_db_samples_across_a_write_failure(self, cache_mode, monkeypatch):
        """Cửa sổ 10 phút nằm vắt qua lúc Redis bắt đầu ghi lỗi: nửa đầu ở Redis, nửa sau ở DB — phải ghép lại."""
        device = CiscoSNMPDeviceFactory()
        _rule()
        for off in (660, 570, 480, 390):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))
        monkeypatch.setattr(metrics_cache, "set_latest", lambda *a, **k: False)
        monkeypatch.setattr(metrics_cache, "push_series", lambda *a, **k: False)
        for off in (300, 210, 120, 30):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))

        check_device_alerts(device, _since())

        assert _active(device)

    def test_merge_does_not_ignore_a_good_redis_sample_that_breaks_the_streak(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()
        _rule()
        for off in (660, 570):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))
        save_metrics(device, _nd(device, cpu=20.0, ts=_now(480)))                     # mẫu tốt trong Redis
        monkeypatch.setattr(metrics_cache, "set_latest", lambda *a, **k: False)
        monkeypatch.setattr(metrics_cache, "push_series", lambda *a, **k: False)
        for off in (390, 300, 210, 120, 30):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))

        check_device_alerts(device, _since())

        assert not _active(device)


class TestOtherFamiliesFollowTheSameSourceRule:
    """Cùng hợp đồng cho VM / trạng thái cổng / uplink traffic: DB có mẫu poll mới hơn snapshot Redis → đọc DB."""

    @staticmethod
    def _redis_write_fails(monkeypatch):
        monkeypatch.setattr(metrics_cache, "set_latest", lambda *a, **k: False)
        monkeypatch.setattr(metrics_cache, "push_series", lambda *a, **k: False)

    def test_vm_count_reads_the_newer_db_snapshot(self, cache_mode, monkeypatch):
        device = HyperVDeviceFactory()
        three = {"vms": [{"name": f"vm{i}", "state": "Running", "repl_health": "Normal"} for i in range(3)]}
        one = {"vms": [{"name": "vm1", "state": "Running", "repl_health": "Normal"}]}
        save_metrics(device, _nd(device, ts=_now(100), extra=three))
        self._redis_write_fails(monkeypatch)
        save_metrics(device, _nd(device, ts=_now(0), extra=one))

        assert _count_vms_running(device, _since()) == 1.0

    def test_port_status_reads_the_newer_db_sample(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()

        def nd(status, ts):
            data = _nd(device, ts=ts)
            data.interfaces = [InterfaceData(name="Gi0/1", if_index=1, status=status, in_bytes=0, out_bytes=0)]
            return data

        save_metrics(device, nd("up", _now(100)))
        iface = Interface.objects.get(device=device, name="Gi0/1")
        self._redis_write_fails(monkeypatch)
        save_metrics(device, nd("down", _now(0)))
        assert InterfaceStats.objects.filter(interface=iface, status="down").count() == 1

        assert _uplink_statuses(device, [iface.pk], _since()) == {iface.pk: "down"}

    def test_uplink_traffic_peak_includes_the_newer_db_sample(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1", is_uplink=True)
        metrics_cache.push_series(device.id, {"ts": _now(100).timestamp()},
                                  {iface.id: {"ts": _now(100).timestamp(), "in_mbps": 5.0, "out_mbps": 1.0,
                                              "status": "up"}})
        InterfaceStats.objects.create(interface=iface, timestamp=_now(10), status="up", in_mbps=50.0, out_mbps=1.0)

        assert _uplink_traffic_max(device, _since(), "in") == 50.0

    def test_uplink_traffic_sustained_merges_redis_and_db(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1", is_uplink=True)
        _rule(name="Uplink in", metric="uplink_in_mbps_max", threshold=10.0, duration_min=10)
        for off in (660, 570, 480, 390):                      # nửa đầu cửa sổ nằm ở Redis
            metrics_cache.push_series(device.id, {"ts": _now(off).timestamp()},
                                      {iface.id: {"ts": _now(off).timestamp(), "in_mbps": 50.0, "out_mbps": 1.0,
                                                  "status": "up"}})
        for off in (300, 210, 120, 30):                       # nửa sau ở DB (Redis đã ghi lỗi)
            InterfaceStats.objects.create(interface=iface, timestamp=_now(off), status="up", in_mbps=50.0,
                                          out_mbps=1.0)

        check_device_alerts(device, _since())

        assert _active(device)


    def test_port_down_streak_merges_redis_and_db_samples(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1")
        for off in (380, 290):                                # nửa đầu ở Redis
            metrics_cache.push_series(device.id, {"ts": _now(off).timestamp()},
                                      {iface.id: {"ts": _now(off).timestamp(), "status": "down"}})
        for off in (200, 110, 20):                            # nửa sau ở DB (Redis đã ghi lỗi)
            InterfaceStats.objects.create(interface=iface, timestamp=_now(off), status="down")

        assert _port_down_throughout(device, iface.pk, _now() - timedelta(minutes=5)) is True


# ─── P2-3 · mẫu đơn lẻ không đủ cho cửa sổ ngắn ──────────────────────────────

class TestShortWindowsNeedARealBoundarySample:
    def test_hyperv_5_minute_rule_does_not_fire_on_one_fresh_sample(self):
        """poll 300s → start_slack cũ 450s > cửa sổ 300s nên mẫu vừa xuất hiện vẫn nổ."""
        device = HyperVDeviceFactory()
        _rule(name="HV latency", device_type="hyperv", metric="disk_read_latency_ms", threshold=20.0, duration_min=5)
        SystemHealth.objects.create(device=device, timestamp=_now(), cpu_percent=1.0, mem_percent=1.0,
                                    disk_read_latency_ms=55.0)

        check_device_alerts(device, _since(5))

        assert not _active(device)

    def test_hyperv_5_minute_rule_fires_in_steady_state(self):
        """Nhịp 300s thật: 1 mẫu NGAY TRƯỚC đầu cửa sổ + 1 mẫu mới — đủ bằng chứng, không bị bỏ đói."""
        device = HyperVDeviceFactory()
        _rule(name="HV latency", device_type="hyperv", metric="disk_read_latency_ms", threshold=20.0, duration_min=5)
        for off in (330, 30):
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=1.0, mem_percent=1.0,
                                        disk_read_latency_ms=55.0)

        check_device_alerts(device, _since(5))

        assert _active(device)

    def test_network_2_minute_rule_does_not_fire_on_one_fresh_sample(self):
        device = CiscoSNMPDeviceFactory()
        _rule(name="CPU 2m", duration_min=2)
        SystemHealth.objects.create(device=device, timestamp=_now(), cpu_percent=99.0, mem_percent=40.0)

        check_device_alerts(device, _since(2))

        assert not _active(device)

    def test_network_2_minute_rule_fires_in_steady_state(self):
        device = CiscoSNMPDeviceFactory()
        _rule(name="CPU 2m", duration_min=2)
        for off in (200, 110, 20):
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=99.0, mem_percent=40.0)

        check_device_alerts(device, _since(2))

        assert _active(device)

    def test_a_good_sample_just_before_the_window_start_holds_the_rule_back(self):
        """Mẫu ngay trước đầu cửa sổ là trạng thái ĐANG GIỮ khi cửa sổ bắt đầu — nếu nó bình thường thì CPU chưa
        kéo dài đủ 2 phút."""
        device = CiscoSNMPDeviceFactory()
        _rule(name="CPU 2m", duration_min=2)
        SystemHealth.objects.create(device=device, timestamp=_now(200), cpu_percent=20.0, mem_percent=40.0)
        for off in (110, 20):
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=99.0, mem_percent=40.0)

        check_device_alerts(device, _since(2))

        assert not _active(device)

    def test_samples_older_than_the_boundary_sample_do_not_count(self):
        """Mẫu biên THAY THẾ các mẫu cũ hơn nó: CPU bình thường ở 740s trước, rồi 99% từ 650s (trước đầu cửa sổ
        10 phút) tới nay — điều kiện đã kéo dài đủ, mẫu 740s không được phá chuỗi."""
        device = CiscoSNMPDeviceFactory()
        _rule()
        SystemHealth.objects.create(device=device, timestamp=_now(740), cpu_percent=20.0, mem_percent=40.0)
        for off in (650, 560, 470, 380, 290, 200, 110, 20):
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=99.0, mem_percent=40.0)

        check_device_alerts(device, _since())

        assert _active(device)

    def test_cache_mode_hyperv_5_minute_rule_does_not_fire_on_one_fresh_sample(self, cache_mode):
        device = HyperVDeviceFactory()
        _rule(name="HV latency", device_type="hyperv", metric="disk_read_latency_ms", threshold=20.0, duration_min=5)
        save_metrics(device, _hv_nd(device, 55.0, _now()))

        check_device_alerts(device, _since(5))

        assert not _active(device)

    def test_cache_mode_hyperv_5_minute_rule_fires_in_steady_state(self, cache_mode):
        device = HyperVDeviceFactory()
        _rule(name="HV latency", device_type="hyperv", metric="disk_read_latency_ms", threshold=20.0, duration_min=5)
        for off in (330, 30):
            save_metrics(device, _hv_nd(device, 55.0, _now(off)))

        check_device_alerts(device, _since(5))

        assert _active(device)

    def test_cache_mode_network_2_minute_rule_does_not_fire_on_one_fresh_sample(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        _rule(name="CPU 2m", duration_min=2)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now()))

        check_device_alerts(device, _since(2))

        assert not _active(device)


# ─── P2-4 · nhịp poll theo nguồn của chính thiết bị ──────────────────────────

class TestPollCadenceFollowsTheDevicesOwnSource:
    def test_snmp_device_ignores_the_ping_cadence(self, settings):
        settings.POLL_NETWORK_INTERVAL_SECS = 90
        settings.POLL_PING_INTERVAL_SECS = 600
        device = CiscoSNMPDeviceFactory(protocol="snmp")

        assert _expected_poll_secs(device) == 90

    def test_ping_device_uses_the_ping_cadence(self, settings):
        settings.POLL_NETWORK_INTERVAL_SECS = 600
        settings.POLL_PING_INTERVAL_SECS = 90
        device = CiscoSNMPDeviceFactory(protocol="ping")

        assert _expected_poll_secs(device) == 90

    def test_ssh_device_uses_the_network_cadence(self, settings):
        settings.POLL_NETWORK_INTERVAL_SECS = 120
        settings.POLL_PING_INTERVAL_SECS = 600
        device = CiscoSNMPDeviceFactory(protocol="ssh")

        assert _expected_poll_secs(device) == 120

    def test_slow_ping_cadence_does_not_loosen_an_snmp_rule(self, settings):
        """network=90, ping=600: rule SNMP 10 phút KHÔNG được chấp nhận 1 mẫu mới (dung sai phải theo 90s)."""
        settings.POLL_NETWORK_INTERVAL_SECS = 90
        settings.POLL_PING_INTERVAL_SECS = 600
        device = CiscoSNMPDeviceFactory(protocol="snmp")
        _rule()
        SystemHealth.objects.create(device=device, timestamp=_now(), cpu_percent=99.0, mem_percent=40.0)

        check_device_alerts(device, _since())

        assert not _active(device)
