"""Review ngoài lần 3 (2026-11-19) soi lại bản sửa vòng 2 — 2 điểm, tái hiện bằng test đỏ trước khi sửa.

P1  Alert đã hồi phục vẫn nổ lại từ MẪU DB THƯỜNG (không phải dòng bằng chứng, nên dấu `_evidence` không chặn được):
    Redis ghi lỗi → CPU 99% vào DB → ALERT → Redis phục hồi, CPU 10% → hồi phục → Redis mất snapshot lần nữa →
    getter rơi về DB và nhặt lại mẫu 99% cũ. Phải giữ BẰNG CHỨNG HỒI PHỤC (`Alert.resolved_at` của chính alert đó)
    khi chọn dữ liệu đường lui: mẫu DB có thời điểm ≤ lúc alert hồi phục thật là mẫu đã bị vượt qua.
P2  Ghép nguồn SAU khi tính max che mất uplink vượt ngưỡng: if-series Redis được đẩy theo TỪNG cổng (ghi dở dang là có
    thể), nên 1 thời điểm có thể có cổng A ở Redis và cổng B ở DB. `_sustained_uplink_traffic_max` tính max theo
    thời điểm riêng cho Redis và DB rồi ghép "nguồn trước thắng" → 50 Mbps của DB bị bỏ. Phải ghép THEO TỪNG CỔNG
    trước khi lấy max.
"""
from datetime import UTC, datetime, timedelta

import pytest

from apps.alerts.engine import (
    _count_vms_repl_unhealthy,
    _count_vms_running,
    _latest_host_perf,
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
    _uplink_traffic_max,
    _wifi_ap_offline_count,
    _wifi_client_count,
    check_device_alerts,
)
from apps.alerts.models import Alert, AlertRule
from apps.collectors.base import InterfaceData, NormalizedData
from apps.devices.models import Interface, TopologyLink
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
        severity="WARNING", channels=[], enabled=True, duration_min=10,
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


def _nd(device, cpu=50.0, ts=None, interfaces=None) -> NormalizedData:
    return NormalizedData(
        device_name=device.name, ip_address=device.ip_address, timestamp=ts or _now(), os_family="cisco_ios",
        cpu_percent=cpu, mem_percent=40.0, interfaces=interfaces or [], extra={},
    )


def _closed_alert(device, rule, *, resolved_secs_ago, closed_reason="", interface=None) -> Alert:
    return Alert.objects.create(
        device=device, rule=rule, severity=rule.severity, message="đã đóng", metric_value=99.0, is_active=False,
        resolved_at=_now(resolved_secs_ago), closed_reason=closed_reason, interface=interface,
    )


# ─── P1 · bằng chứng hồi phục phải sống sót qua lần mất Redis thứ hai ─────────────────────────────────────────

class TestRecoveryEvidenceSurvivesASecondRedisLoss:
    def test_recovered_alert_does_not_refire_from_an_old_fallback_sample(self, cache_mode, monkeypatch):
        """Đúng kịch bản review: Redis lỗi → 99% vào DB → ALERT → Redis lại → 10% → hồi phục → Redis mất snapshot."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Instant CPU", duration_min=0)

        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(100)))                 # mẫu THƯỜNG trong DB (đường lui)
        assert SystemHealth.objects.filter(device=device, cpu_percent=99.0).count() == 1
        check_device_alerts(device, _since())
        assert _active(device, rule)

        monkeypatch.undo()                                                        # Redis phục hồi
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(10)))
        check_device_alerts(device, _since())
        assert not _active(device, rule)                                          # đã hồi phục

        cache_mode.flushdb()                                                      # Redis mất snapshot lần nữa
        check_device_alerts(device, _since())

        assert not _active(device, rule)
        assert Alert.objects.filter(device=device, rule=rule).count() == 1        # không có ALERT thứ hai

    def test_sustained_rule_does_not_refire_from_old_fallback_rows(self, cache_mode, monkeypatch):
        """Cùng chuỗi sự kiện với rule `duration_min>0`: cả chuỗi mẫu 99% trong DB đều cũ hơn lúc hồi phục."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()

        _redis_writes_fail(monkeypatch)
        for off in (700, 610, 520, 430, 340, 250, 160, 70):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))
        check_device_alerts(device, _since())
        assert _active(device, rule)

        monkeypatch.undo()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(5)))
        check_device_alerts(device, _since())
        assert not _active(device, rule)

        cache_mode.flushdb()
        check_device_alerts(device, _since())

        assert not _active(device, rule)
        assert Alert.objects.filter(device=device, rule=rule).count() == 1

    def test_port_alert_does_not_refire_from_an_old_fallback_status(self, cache_mode, monkeypatch):
        """Cổng uplink: DOWN ghi vào DB lúc Redis lỗi → ALERT → UP ở Redis → hồi phục → Redis mất snapshot."""
        device = CiscoSNMPDeviceFactory()
        peer = CiscoSNMPDeviceFactory(name="PEER-SW")
        rule = AlertRule.objects.create(
            name="Uplink Down", device_type="switch", metric="if_status", condition="eq", threshold=0.0,
            severity="CRITICAL", channels=[], enabled=True,
        )

        def nd(status, ts):
            return _nd(device, ts=ts, interfaces=[
                InterfaceData(name="Gi0/1", if_index=1, status=status, in_bytes=0, out_bytes=0)])

        save_metrics(device, nd("up", _now(300)))                                 # tạo Interface (Redis còn ghi được)
        iface = Interface.objects.get(device=device, name="Gi0/1")
        TopologyLink.objects.create(
            local_device=device, local_port=iface.name, local_interface=iface, link_kind="switch",
            remote_device=peer, is_stale=False, protocol="fdb", match_method="mac",
        )
        _redis_writes_fail(monkeypatch)
        save_metrics(device, nd("down", _now(100)))
        assert InterfaceStats.objects.filter(interface=iface, status="down").count() == 1
        check_device_alerts(device, _since())
        assert Alert.objects.filter(device=device, rule=rule, interface=iface, is_active=True).count() == 1

        monkeypatch.undo()
        save_metrics(device, nd("up", _now(10)))
        check_device_alerts(device, _since())
        assert not Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

        cache_mode.flushdb()
        check_device_alerts(device, _since())

        assert not Alert.objects.filter(device=device, rule=rule, is_active=True).exists()
        assert Alert.objects.filter(device=device, rule=rule).count() == 1

    # ── đối chứng: đường lui vẫn phải dùng được ───────────────────────────────────────────────────────────

    def test_a_fallback_sample_newer_than_the_recovery_still_counts(self, cache_mode, monkeypatch):
        """Sau khi hồi phục, Redis lại ghi lỗi và DB có mẫu 99% MỚI HƠN lúc hồi phục → vẫn phải nổ."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Instant CPU", duration_min=0)
        _closed_alert(device, rule, resolved_secs_ago=60)
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(20)))

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_the_latest_recovery_is_the_one_that_counts(self, cache_mode, monkeypatch):
        """Alert đã hồi phục 2 lần: mẫu nằm GIỮA 2 lần hồi phục vẫn cũ hơn lần hồi phục gần nhất."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Instant CPU", duration_min=0)
        _closed_alert(device, rule, resolved_secs_ago=600)
        _closed_alert(device, rule, resolved_secs_ago=30)
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(100)))

        check_device_alerts(device, _since())

        assert not _active(device, rule)

    def test_closing_without_recovery_does_not_discard_samples(self, cache_mode, monkeypatch):
        """Đóng vì tắt rule / đổi phạm vi KHÔNG phải hồi phục: chưa có dữ liệu nào cho thấy sự cố đã hết."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Instant CPU", duration_min=0)
        _closed_alert(device, rule, resolved_secs_ago=5, closed_reason="rule_disabled")
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(60)))

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_recovery_of_another_rule_does_not_hide_this_rules_samples(self, cache_mode, monkeypatch):
        """Bằng chứng hồi phục thuộc về ĐÚNG rule: rule khác hồi phục không làm rule này mất mẫu."""
        device = CiscoSNMPDeviceFactory()
        other = _rule(name="Other", metric="mem_percent", duration_min=0)
        rule = _rule(name="Instant CPU", duration_min=0)
        _closed_alert(device, other, resolved_secs_ago=5)
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(60)))

        check_device_alerts(device, _since())

        assert _active(device, rule)


# ─── P2 · ghép THEO TỪNG CỔNG trước khi lấy max ───────────────────────────────────────────────────────────────

class TestUplinkTrafficIsMergedPerPortBeforeTheMax:
    @staticmethod
    def _setup():
        device = CiscoSNMPDeviceFactory()
        a = Interface.objects.create(device=device, if_index=1, name="Gi0/1", is_uplink=True)
        b = Interface.objects.create(device=device, if_index=2, name="Gi0/2", is_uplink=True)
        rule = _rule(name="Uplink in", metric="uplink_in_mbps_max", threshold=40.0, duration_min=5)
        return device, a, b, rule

    @staticmethod
    def _redis_port(device, iface, ts: datetime, mbps: float, skew: float = 0.0):
        stamp = ts.timestamp() + skew
        metrics_cache.push_series(device.id, {"ts": stamp},
                                  {iface.id: {"ts": stamp, "in_mbps": mbps, "out_mbps": 0.0, "status": "up"}})

    def test_port_present_only_in_the_db_is_not_hidden_by_the_other_port_in_redis(self, cache_mode):
        """DB có cổng A=5 và cổng B=50 cùng 1 thời điểm; Redis chỉ có cổng A=5 → max phải là 50 và rule phải nổ."""
        device, a, b, rule = self._setup()
        for off in (400, 310, 220, 130, 40):
            ts = _now(off)
            InterfaceStats.objects.create(interface=a, timestamp=ts, status="up", in_mbps=5.0, out_mbps=0.0)
            InterfaceStats.objects.create(interface=b, timestamp=ts, status="up", in_mbps=50.0, out_mbps=0.0)
            self._redis_port(device, a, ts, 5.0)

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_the_same_holds_when_the_two_sources_disagree_by_a_fraction_of_a_second(self, cache_mode):
        """Mẫu cùng 1 lần poll có thể lệch dưới 1 giây giữa Redis và DB — vẫn phải coi là 1 thời điểm."""
        device, a, b, rule = self._setup()
        for off in (400, 310, 220, 130, 40):
            ts = _now(off)
            InterfaceStats.objects.create(interface=a, timestamp=ts, status="up", in_mbps=5.0, out_mbps=0.0)
            InterfaceStats.objects.create(interface=b, timestamp=ts, status="up", in_mbps=50.0, out_mbps=0.0)
            self._redis_port(device, a, ts, 5.0, skew=0.4)

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_the_higher_port_wins_whatever_the_port_order(self, cache_mode):
        """Cổng 50 Mbps là cổng ĐẦU, cổng 5 Mbps (đồng thời) ở Redis — max không phụ thuộc thứ tự cổng."""
        device, a, b, rule = self._setup()
        for off in (400, 310, 220, 130, 40):
            ts = _now(off)
            InterfaceStats.objects.create(interface=a, timestamp=ts, status="up", in_mbps=50.0, out_mbps=0.0)
            InterfaceStats.objects.create(interface=b, timestamp=ts, status="up", in_mbps=5.0, out_mbps=0.0)
            self._redis_port(device, b, ts, 5.0)

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_port_present_only_in_redis_is_still_counted(self, cache_mode):
        """Chiều ngược lại: cổng quá ngưỡng ở Redis, cổng còn lại ở DB thấp → vẫn nổ."""
        device, a, b, rule = self._setup()
        for off in (400, 310, 220, 130, 40):
            ts = _now(off)
            InterfaceStats.objects.create(interface=a, timestamp=ts, status="up", in_mbps=5.0, out_mbps=0.0)
            self._redis_port(device, b, ts, 50.0)

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_a_poll_where_no_port_is_over_the_threshold_still_breaks_the_streak(self, cache_mode):
        """Đối chứng: 1 lần poll mà MỌI cổng đều thấp phải làm đứt chuỗi 'luôn vượt ngưỡng'."""
        device, a, b, rule = self._setup()
        for off, high in ((400, 50.0), (310, 50.0), (220, 5.0), (130, 50.0), (40, 50.0)):
            ts = _now(off)
            InterfaceStats.objects.create(interface=a, timestamp=ts, status="up", in_mbps=5.0, out_mbps=0.0)
            InterfaceStats.objects.create(interface=b, timestamp=ts, status="up", in_mbps=high, out_mbps=0.0)
            self._redis_port(device, a, ts, 5.0)

        check_device_alerts(device, _since())

        assert not _active(device, rule)


# ─── P1 · mọi họ getter đọc đường lui đều giữ bằng chứng hồi phục ───────────────────────────────────────────────

_WINDOW_OFFSETS = (700, 610, 520, 430, 340, 250, 160, 70)      # phủ cửa sổ 10 phút + mẫu biên


class TestEveryFamilyKeepsTheRecoveryEvidence:
    """Dựng mẫu DB thường (Redis trống), kiểm trước khi có alert hồi phục (đối chứng: phải đọc được) rồi sau khi có."""

    @staticmethod
    def _check(device, rule, read):
        with _recovery_evidence_for(device, rule):
            assert read() is not None, "đối chứng: chưa có bằng chứng hồi phục thì mẫu DB phải đọc được"
        _closed_alert(device, rule, resolved_secs_ago=30)
        with _recovery_evidence_for(device, rule):
            assert read() is None, "mẫu DB cũ hơn lúc hồi phục không được đọc làm dữ liệu hiện tại"

    def test_mem_instant(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Mem", metric="mem_percent", duration_min=0)
        SystemHealth.objects.create(device=device, timestamp=_now(100), cpu_percent=1.0, mem_percent=95.0)

        self._check(device, rule, lambda: _latest_mem(device, _since()))

    def test_cpu_sustained(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        for off in _WINDOW_OFFSETS:
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=99.0, mem_percent=40.0)

        self._check(device, rule, lambda: _sustained_cpu_mem(device, rule, _now() - timedelta(minutes=10)))

    def test_mem_sustained(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Mem", metric="mem_percent")
        for off in _WINDOW_OFFSETS:
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=1.0, mem_percent=95.0)

        self._check(device, rule, lambda: _sustained_cpu_mem(device, rule, _now() - timedelta(minutes=10)))

    def test_host_perf_instant(self, cache_mode):
        device = HyperVDeviceFactory()
        rule = _rule(name="Latency", metric="disk_read_latency_ms", threshold=20.0, duration_min=0)
        SystemHealth.objects.create(device=device, timestamp=_now(100), cpu_percent=1.0, mem_percent=1.0,
                                    disk_read_latency_ms=50.0)

        self._check(device, rule, lambda: _latest_host_perf(device, _since(), "disk_read_latency_ms"))

    def test_host_perf_sustained(self, cache_mode):
        device = HyperVDeviceFactory()
        rule = _rule(name="Latency", metric="disk_read_latency_ms", threshold=20.0, duration_min=20)
        for off in (1500, 1200, 900, 600, 300, 100):
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=1.0, mem_percent=1.0,
                                        disk_read_latency_ms=50.0)

        self._check(device, rule, lambda: _sustained_host_perf(device, rule, _now() - timedelta(minutes=20)))

    def test_vm_counts_instant(self, cache_mode):
        device = HyperVDeviceFactory()
        run = _rule(name="VM run", metric="vm_count_running", condition="lt", threshold=5.0, duration_min=0)
        bad = _rule(name="VM repl", metric="vm_repl_unhealthy", threshold=0.0, duration_min=0)
        VMStats.objects.create(device=device, timestamp=_now(100), vm_name="vm1", state="Running",
                               repl_health="Critical")

        self._check(device, run, lambda: _count_vms_running(device, _since()))
        self._check(device, bad, lambda: _count_vms_repl_unhealthy(device, _since()))

    def test_vm_sustained(self, cache_mode):
        device = HyperVDeviceFactory()
        rule = _rule(name="VM repl", metric="vm_repl_unhealthy", threshold=0.0, duration_min=10)
        for off in _WINDOW_OFFSETS:
            VMStats.objects.create(device=device, timestamp=_now(off), vm_name="vm1", state="Running",
                                   repl_health="Critical")

        self._check(device, rule, lambda: _sustained_vm_metric(device, rule, _now() - timedelta(minutes=10)))

    def test_wifi_instant(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        clients = _rule(name="Wifi clients", metric="wifi_client_count", threshold=0.0, duration_min=0)
        offline = _rule(name="AP off", metric="wifi_ap_offline", threshold=0.0, duration_min=0)
        WifiApStats.objects.create(device=device, timestamp=_now(100), ap_name="ap1", ap_mac="aa:bb:cc:00:00:01",
                                   ap_ip="10.0.0.9", ap_group="g", is_online=False, run_state="offline",
                                   client_count=3)

        self._check(device, clients, lambda: _wifi_client_count(device, _since()))
        self._check(device, offline, lambda: _wifi_ap_offline_count(device, _since()))

    def test_wifi_sustained(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        clients = _rule(name="Wifi clients", metric="wifi_client_count", threshold=0.0, duration_min=10)
        offline = _rule(name="AP off", metric="wifi_ap_offline", threshold=0.0, duration_min=10)
        for off in _WINDOW_OFFSETS:
            WifiApStats.objects.create(device=device, timestamp=_now(off), ap_name="ap1",
                                       ap_mac="aa:bb:cc:00:00:01", ap_ip="10.0.0.9", ap_group="g", is_online=False,
                                       run_state="offline", client_count=3)
        window = _now() - timedelta(minutes=10)

        self._check(device, clients, lambda: _sustained_wifi_client_count(device, clients, window))
        self._check(device, offline, lambda: _sustained_wifi_ap_offline_count(device, offline, window))

    def test_uplink_traffic_instant_and_sustained(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1", is_uplink=True)
        instant = _rule(name="Up now", metric="uplink_in_mbps_max", threshold=40.0, duration_min=0)
        sustained = _rule(name="Up long", metric="uplink_in_mbps_max", threshold=40.0, duration_min=10)
        for off in _WINDOW_OFFSETS:
            InterfaceStats.objects.create(interface=iface, timestamp=_now(off), status="up", in_mbps=50.0,
                                          out_mbps=0.0)

        self._check(device, instant, lambda: _uplink_traffic_max(device, _since(), "in"))
        self._check(device, sustained,
                    lambda: _sustained_uplink_traffic_max(device, sustained, _now() - timedelta(minutes=10)))

    def test_port_down_streak_and_status_are_per_port(self, cache_mode):
        """Bằng chứng hồi phục của cổng A không làm cổng B mất mẫu."""
        device = CiscoSNMPDeviceFactory()
        a = Interface.objects.create(device=device, if_index=1, name="Gi0/1")
        b = Interface.objects.create(device=device, if_index=2, name="Gi0/2")
        rule = AlertRule.objects.create(
            name="Uplink Down", device_type="switch", metric="if_status", condition="eq", threshold=0.0,
            severity="CRITICAL", channels=[], enabled=True, duration_min=5,
        )
        for off in (400, 310, 220, 130, 40):
            for port in (a, b):
                InterfaceStats.objects.create(interface=port, timestamp=_now(off), status="down")
        window = _now() - timedelta(minutes=5)
        _closed_alert(device, rule, resolved_secs_ago=30, interface=a)

        with _recovery_evidence_for(device, rule):
            assert _port_down_throughout(device, a.pk, window) is False         # đã vượt qua bởi lần hồi phục của A
            assert _port_down_throughout(device, b.pk, window) is True          # B không bị ảnh hưởng
            assert _uplink_statuses(device, [a.pk, b.pk], _since()) == {a.pk: None, b.pk: "down"}

    def test_db_mode_ignores_recovery_evidence(self, settings):
        """DB-mode: mọi mẫu đều ở DB, mẫu cũ hơn lúc hồi phục vẫn là mẫu hợp lệ (không có Redis nào để 'vượt qua')."""
        settings.METRICS_WRITE_MODE = "db"
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Mem", metric="mem_percent", duration_min=0)
        SystemHealth.objects.create(device=device, timestamp=_now(100), cpu_percent=1.0, mem_percent=95.0)
        _closed_alert(device, rule, resolved_secs_ago=30)

        with _recovery_evidence_for(device, rule):
            assert _latest_mem(device, _since()) == 95.0
