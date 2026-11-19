"""Rule `duration_min>0` phải có đủ BẰNG CHỨNG THEO THỜI GIAN, và Redis trống không được làm alert im lặng.

Review ngoài 2026-11-19:
* F02 — `_sustained_verdict` chỉ xét GIÁ TRỊ các mẫu, không xét mẫu có phủ cửa sổ hay không: 1 mẫu CPU 99% vừa
  xuất hiện (thiết bị mới thêm, Redis vừa restart/flush, vừa hết một khoảng mất poll) đã đủ cho
  `duration_min=10` nổ ngay. Hợp đồng ghi trong CLAUDE.md ("Redis restart → alert trễ tối đa duration_min")
  ngược với hành vi thật.
* F01 — cache-mode: writer có đường lui ghi DB khi Redis lỗi, nhưng getter của alert chỉ đọc Redis nên
  `cpu_percent` (và mọi rule cùng họ) trả None → alert im lặng suốt thời gian Redis lỗi dù DB có mẫu mới.

Cặp test "dữ liệu Redis có nhưng không vượt ngưỡng + DB có dòng bằng chứng cũ" khoá chiều ngược lại: chỉ khi
Redis KHÔNG có dữ liệu mới được đọc DB — nếu không, dòng bằng chứng (ghi lúc alert nổ) sẽ làm alert nổ/giữ lại sai.
"""
from datetime import datetime, timedelta, timezone

import pytest

from apps.alerts.engine import (
    _count_vms_repl_unhealthy, _count_vms_running, _latest_mem, _port_samples, _uplink_statuses,
    _uplink_traffic_max, _wifi_ap_offline_count, _wifi_client_count, check_device_alerts,
)
from apps.alerts.models import Alert, AlertRule
from apps.collectors.base import NormalizedData
from apps.devices.models import Interface
from apps.metrics import cache as metrics_cache
from apps.metrics.models import HardwareHealth, InterfaceStats, SystemHealth, VMStats, WifiApStats
from apps.metrics.writer import save_metrics
from tests.conftest import CiscoSNMPDeviceFactory, HuaweiACDeviceFactory, HyperVDeviceFactory

pytestmark = pytest.mark.django_db


def _now(offset_secs: float = 0.0) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(seconds=offset_secs)


def _since() -> datetime:
    """Cửa sổ `since` mà nơi gọi thật (poll/evaluate_alert_rules) truyền cho check_device_alerts."""
    return _now() - timedelta(minutes=10)


def _rule(**overrides) -> AlertRule:
    defaults = dict(
        name="Sustained CPU", device_type="all", metric="cpu_percent", condition="gt", threshold=90.0,
        severity="WARNING", channels=[], enabled=True, duration_min=10,
    )
    defaults.update(overrides)
    return AlertRule.objects.create(**defaults)


def _db_cpu(device, offsets_secs, cpu=99.0) -> None:
    for off in offsets_secs:
        SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=cpu, mem_percent=40.0)


def _fires(device) -> bool:
    return Alert.objects.filter(device=device, is_active=True).exists()


# Mẫu mỗi 90s (nhịp poll mạng thật) phủ trọn 10 phút (+ mẫu ngay đầu cửa sổ).
FULL_WINDOW_90S = list(range(660, -1, -90))


# ─── F02 · DB-mode: độ phủ theo thời gian ─────────────────────────────────────

class TestSustainedNeedsTimeCoverageDbMode:
    def test_single_fresh_sample_does_not_fire_a_10_minute_rule(self):
        """Thiết bị mới thêm / vừa hết mất poll: chưa có bằng chứng 10 phút."""
        device = CiscoSNMPDeviceFactory()
        _rule()
        _db_cpu(device, [0])

        check_device_alerts(device, _since())

        assert not _fires(device)

    def test_samples_only_in_the_last_two_minutes_do_not_fire(self):
        device = CiscoSNMPDeviceFactory()
        _rule()
        _db_cpu(device, [120, 30, 0])

        check_device_alerts(device, _since())

        assert not _fires(device)

    def test_samples_covering_the_whole_window_do_fire(self):
        device = CiscoSNMPDeviceFactory()
        _rule()
        _db_cpu(device, FULL_WINDOW_90S)

        check_device_alerts(device, _since())

        assert _fires(device)

    def test_a_long_gap_inside_the_window_is_not_evidence(self):
        """Có mẫu biên (680s, 650s trước) và mẫu mới nhất (0s) nhưng mất poll ~9 phút ở giữa: không biết CPU lúc đó.

        Cả mẫu biên lẫn mẫu cuối đều đạt kiểm tra riêng của chúng — chỉ kiểm tra khoảng trống mới chặn được ca này
        (bản đầu của test dùng mẫu 660s nằm NGOÀI cửa sổ nên chết ở kiểm tra đầu cửa sổ, đột biến M4 sống sót)."""
        device = CiscoSNMPDeviceFactory()
        _rule()
        _db_cpu(device, [680, 650, 90, 0])

        check_device_alerts(device, _since())

        assert not _fires(device)

    def test_tolerates_missing_up_to_two_consecutive_polls(self):
        """Lỡ 1-2 lần poll liên tiếp (khoảng 250s, dung sai 270s) vẫn đủ — poll chập chờn không làm mất alert."""
        device = CiscoSNMPDeviceFactory()
        _rule()
        _db_cpu(device, [680, 430, 180, 0])

        check_device_alerts(device, _since())

        assert _fires(device)

    def test_stale_newest_sample_does_not_fire(self):
        """Thiết bị đã ngừng poll 5 phút: dãy cũ không được coi là 'đang kéo dài'."""
        device = CiscoSNMPDeviceFactory()
        _rule()
        _db_cpu(device, list(range(960, 299, -90)))

        check_device_alerts(device, _since())

        assert not _fires(device)

    def test_a_good_sample_in_the_window_still_breaks_the_streak(self):
        device = CiscoSNMPDeviceFactory()
        _rule()
        _db_cpu(device, [x for x in FULL_WINDOW_90S if x != 300])
        _db_cpu(device, [300], cpu=40.0)

        check_device_alerts(device, _since())

        assert not _fires(device)

    def test_a_300s_cadence_host_is_not_starved_by_the_coverage_check(self):
        """HyperV poll 300s: dung sai phải theo nhịp THẬT, không dùng 135s (nếu không, mọi rule HyperV
        có duration_min sẽ không bao giờ nổ)."""
        device = HyperVDeviceFactory()
        _rule(name="HV latency", device_type="hyperv", metric="disk_read_latency_ms",
              threshold=20.0, duration_min=15)
        for off in (1050, 750, 450, 150):     # mẫu biên 1050s trước đầu cửa sổ 900s, nhịp 300s
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=10.0,
                                        mem_percent=40.0, disk_read_latency_ms=55.0)

        check_device_alerts(device, _now() - timedelta(minutes=10))

        assert _fires(device)

    def test_single_sample_does_not_fire_a_host_perf_rule(self):
        device = HyperVDeviceFactory()
        _rule(name="HV latency", device_type="hyperv", metric="disk_read_latency_ms",
              threshold=20.0, duration_min=15)
        SystemHealth.objects.create(device=device, timestamp=_now(), cpu_percent=10.0, mem_percent=40.0,
                                    disk_read_latency_ms=55.0)

        check_device_alerts(device, _now() - timedelta(minutes=10))

        assert not _fires(device)

    def test_resolve_does_not_need_window_coverage(self):
        """Chỉ FIRE cần sustain; hồi phục dùng giá trị tức thời (đã sửa 2026-09-28) — không được hỏng."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        Alert.objects.create(device=device, rule=rule, severity="WARNING", message="m",
                             metric_value=95.0, is_active=True)
        _db_cpu(device, [0], cpu=20.0)

        check_device_alerts(device, _since())

        assert not _fires(device)


class TestEverySustainedFamilyNeedsCoverage:
    """Kiểm tra độ phủ áp cho CẢ 7 nhóm getter sustained, không chỉ CPU/mem + host-perf: với mỗi nhóm, 1 mẫu đơn
    lẻ vượt ngưỡng không được nổ rule 10 phút, còn dữ liệu phủ đủ cửa sổ (đúng nhịp) thì nổ."""

    def test_ilo_single_sample_does_not_fire_but_full_window_does(self):
        device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
        _rule(name="iLO disk", device_type="hyperv", metric="raid_missing_disk_count",
              condition="gte", threshold=1.0, duration_min=10)
        HardwareHealth.objects.create(device=device, timestamp=_now(), missing_disk_count=2)
        check_device_alerts(device, _since())
        assert not _fires(device)

        for off in (870, 570, 270):           # mẫu biên 870s (trước đầu cửa sổ 600s), nhịp 300s
            HardwareHealth.objects.create(device=device, timestamp=_now(off), missing_disk_count=2)
        check_device_alerts(device, _since())
        assert _fires(device)

    def test_uplink_traffic_single_sample_does_not_fire_but_full_window_does(self):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1", is_uplink=True)
        _rule(name="Uplink in", metric="uplink_in_mbps_max", threshold=10.0, duration_min=10)
        InterfaceStats.objects.create(interface=iface, timestamp=_now(), status="up", in_mbps=50.0)
        check_device_alerts(device, _since())
        assert not _fires(device)

        for off in FULL_WINDOW_90S[:-1]:
            InterfaceStats.objects.create(interface=iface, timestamp=_now(off), status="up", in_mbps=50.0)
        check_device_alerts(device, _since())
        assert _fires(device)

    def test_vm_metric_single_snapshot_does_not_fire_but_full_window_does(self):
        device = HyperVDeviceFactory()
        _rule(name="VM low", device_type="hyperv", metric="vm_count_running", condition="lt",
              threshold=3.0, duration_min=10)
        VMStats.objects.create(device=device, timestamp=_now(), vm_name="vm1", state="Running")
        check_device_alerts(device, _since())
        assert not _fires(device)

        for off in (870, 570, 270):           # mẫu biên 870s (trước đầu cửa sổ 600s), nhịp 300s
            VMStats.objects.create(device=device, timestamp=_now(off), vm_name="vm1", state="Running")
        check_device_alerts(device, _since())
        assert _fires(device)

    def test_wifi_single_snapshot_does_not_fire_but_full_window_does(self):
        ac = HuaweiACDeviceFactory()
        _rule(name="AP offline X", device_type="wlan_controller", metric="wifi_ap_offline",
              condition="gte", threshold=1.0, duration_min=10)
        _rule(name="Client high X", device_type="wlan_controller", metric="wifi_client_count",
              condition="gt", threshold=5.0, duration_min=10)
        mine = lambda: Alert.objects.filter(  # noqa: E731
            device=ac, is_active=True, rule__name__in=["AP offline X", "Client high X"]).count()
        WifiApStats.objects.create(device=ac, timestamp=_now(), ap_name="AP1", is_online=False, client_count=9)
        check_device_alerts(ac, _since())
        assert mine() == 0

        for off in FULL_WINDOW_90S[:-1]:
            WifiApStats.objects.create(device=ac, timestamp=_now(off), ap_name="AP1", is_online=False,
                                       client_count=9)
        check_device_alerts(ac, _since())
        assert mine() == 2


# ─── F02 · cache-mode: cùng hợp đồng trên ring-buffer Redis ───────────────────

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


def _nd(device, cpu=50.0, mem=40.0, ts=None) -> NormalizedData:
    return NormalizedData(
        device_name=device.name, ip_address=device.ip_address, timestamp=ts or _now(),
        os_family="cisco_ios", cpu_percent=cpu, mem_percent=mem, interfaces=[], extra={},
    )


def _cache_cpu(device, offsets_secs, cpu=99.0) -> None:
    for off in offsets_secs:
        save_metrics(device, _nd(device, cpu=cpu, ts=_now(off)))


class TestSustainedNeedsTimeCoverageCacheMode:
    def test_single_fresh_sample_does_not_fire(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        _rule()
        _cache_cpu(device, [0])

        check_device_alerts(device, _since())

        assert not _fires(device)

    def test_samples_covering_the_whole_window_do_fire(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        _rule()
        _cache_cpu(device, FULL_WINDOW_90S)

        check_device_alerts(device, _since())

        assert _fires(device)

    def test_host_perf_series_needs_coverage_too(self, cache_mode):
        device = HyperVDeviceFactory()
        _rule(name="HV latency", device_type="hyperv", metric="disk_read_latency_ms",
              threshold=20.0, duration_min=15)

        def push(offsets):
            for off in offsets:
                save_metrics(device, NormalizedData(
                    device_name=device.name, ip_address=device.ip_address, timestamp=_now(off),
                    os_family="hyperv_winrm", cpu_percent=1.0, mem_percent=1.0,
                    disk_read_latency_ms=55.0, interfaces=[], extra={}))

        push([0])
        check_device_alerts(device, _now() - timedelta(minutes=10))
        assert not _fires(device)               # mẫu đơn lẻ

        push([1140, 840, 540, 240])            # mẫu biên 1140s (trước đầu cửa sổ 900s)
        check_device_alerts(device, _now() - timedelta(minutes=10))
        assert _fires(device)                   # phủ đủ 15 phút ở nhịp 300s

    def test_vm_and_wifi_series_need_coverage_too(self, cache_mode):
        """Ring-buffer sys còn mang `vmr`/`vmu` (VM) và `wc`/`wao` (WiFi): cùng hợp đồng độ phủ."""
        hv = HyperVDeviceFactory()
        _rule(name="VM low", device_type="hyperv", metric="vm_count_running", condition="lt",
              threshold=3.0, duration_min=10)
        ac = HuaweiACDeviceFactory()
        _rule(name="AP offline X", device_type="wlan_controller", metric="wifi_ap_offline",
              condition="gte", threshold=1.0, duration_min=10)
        _rule(name="Client high X", device_type="wlan_controller", metric="wifi_client_count",
              condition="gt", threshold=5.0, duration_min=10)
        vm_extra = {"vms": [{"name": "vm1", "state": "Running", "repl_health": "Normal"}]}
        ap_extra = {"wifi_aps": [{"name": "AP1", "is_online": False, "client_count": 9}]}

        def push(device, extra, offsets):
            for off in offsets:
                save_metrics(device, NormalizedData(
                    device_name=device.name, ip_address=device.ip_address, timestamp=_now(off),
                    os_family="x", cpu_percent=1.0, mem_percent=1.0, interfaces=[], extra=extra))

        def mine():
            return Alert.objects.filter(is_active=True, rule__name__in=["VM low", "AP offline X",
                                                                       "Client high X"]).count()

        push(hv, vm_extra, [0])
        push(ac, ap_extra, [0])
        check_device_alerts(hv, _since())
        check_device_alerts(ac, _since())
        assert mine() == 0                      # mẫu đơn lẻ

        push(hv, vm_extra, [870, 570, 270])    # mẫu biên 870s (trước đầu cửa sổ 600s), nhịp 300s
        push(ac, ap_extra, FULL_WINDOW_90S[:-1])
        check_device_alerts(hv, _since())
        check_device_alerts(ac, _since())
        assert mine() == 3                      # đủ độ phủ

    def test_redis_flush_resets_the_window_instead_of_firing_on_one_sample(self, cache_mode):
        """Đúng điều CLAUDE.md hứa: Redis restart → alert TRỄ tối đa duration_min (không nổ ngay)."""
        device = CiscoSNMPDeviceFactory()
        _rule()
        _cache_cpu(device, FULL_WINDOW_90S)
        cache_mode.flushdb()                       # Redis restart/flush: mất cả chuỗi mẫu
        _cache_cpu(device, [0])                    # chỉ còn mẫu vừa poll

        check_device_alerts(device, _since())

        assert not _fires(device)


# ─── F01 · Redis không có dữ liệu → đọc DB (đường lui đã được writer ghi) ────

@pytest.fixture
def redis_down(settings, monkeypatch):
    """Cache-mode nhưng Redis không kết nối được: writer rơi về ghi DB, getter phải đọc lại từ DB."""
    settings.METRICS_WRITE_MODE = "cache"
    monkeypatch.setattr(metrics_cache, "_get_client", lambda: None)


class TestRedisOutageFallsBackToDb:
    def test_sustained_cpu_rule_fires_from_db_rows_written_by_the_writer_fallback(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        _rule()
        _cache_cpu(device, FULL_WINDOW_90S)        # save_metrics → Redis lỗi → ghi SystemHealth

        assert SystemHealth.objects.filter(device=device).count() == len(FULL_WINDOW_90S)
        check_device_alerts(device, _since())

        assert _fires(device)

    def test_instant_cpu_rule_fires_from_db_when_redis_is_down(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        _rule(name="Instant CPU", duration_min=0)
        _cache_cpu(device, [0])

        check_device_alerts(device, _since())

        assert _fires(device)

    def test_host_perf_rule_fires_from_db_when_redis_is_down(self, redis_down):
        device = HyperVDeviceFactory()
        _rule(name="HV latency", device_type="hyperv", metric="disk_read_latency_ms",
              threshold=20.0, duration_min=0)
        SystemHealth.objects.create(device=device, timestamp=_now(), cpu_percent=1.0, mem_percent=1.0,
                                    disk_read_latency_ms=80.0)

        check_device_alerts(device, _since())

        assert _fires(device)

    def test_sustained_host_perf_rule_fires_from_db_when_redis_is_down(self, redis_down):
        """Rule sustained (duration_min>0) của HyperV: dữ liệu do writer fallback ghi, nhịp 300s."""
        device = HyperVDeviceFactory()
        _rule(name="HV latency", device_type="hyperv", metric="disk_read_latency_ms",
              threshold=20.0, duration_min=15)
        for off in (1140, 840, 540, 240, 0):    # mẫu biên 1140s (trước đầu cửa sổ 900s)
            save_metrics(device, NormalizedData(
                device_name=device.name, ip_address=device.ip_address, timestamp=_now(off),
                os_family="hyperv_winrm", cpu_percent=10.0, mem_percent=40.0,
                disk_read_latency_ms=55.0, interfaces=[], extra={},
            ))

        assert SystemHealth.objects.filter(device=device, disk_read_latency_ms=55.0).count() == 5
        check_device_alerts(device, _now() - timedelta(minutes=10))

        assert _fires(device)

    def test_sustained_vm_rule_fires_from_db_when_redis_is_down(self, redis_down):
        device = HyperVDeviceFactory()
        _rule(name="VM count low", device_type="hyperv", metric="vm_count_running", condition="lt",
              threshold=3.0, duration_min=10)
        for off in (870, 570, 270, 0):          # mẫu biên 870s (trước đầu cửa sổ 600s), nhịp 300s
            for vm in ("vm1", "vm2"):
                VMStats.objects.create(device=device, timestamp=_now(off), vm_name=vm, state="Running")

        check_device_alerts(device, _since())

        assert _fires(device)

    def test_sustained_uplink_traffic_rule_fires_from_db_when_redis_is_down(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1", is_uplink=True)
        _rule(name="Uplink in", metric="uplink_in_mbps_max", threshold=10.0, duration_min=10)
        for off in FULL_WINDOW_90S:
            InterfaceStats.objects.create(interface=iface, timestamp=_now(off), status="up",
                                          in_mbps=50.0, out_mbps=1.0)

        check_device_alerts(device, _since())

        assert _fires(device)

    def test_sustained_wifi_rules_fire_from_db_when_redis_is_down(self, redis_down):
        ac = HuaweiACDeviceFactory()
        _rule(name="AP offline", device_type="wlan_controller", metric="wifi_ap_offline",
              condition="gte", threshold=1.0, duration_min=10)
        _rule(name="Client high", device_type="wlan_controller", metric="wifi_client_count",
              condition="gt", threshold=5.0, duration_min=10)
        for off in FULL_WINDOW_90S:
            WifiApStats.objects.create(device=ac, timestamp=_now(off), ap_name="AP1", is_online=False,
                                       client_count=9)

        check_device_alerts(ac, _since())

        # Chỉ đếm 2 rule của test: migration seed còn có rule "AP offline" riêng cũng nổ trên cùng dữ liệu.
        fired = Alert.objects.filter(device=ac, is_active=True, rule__name__in=["AP offline", "Client high"])
        assert fired.count() == 2

    def test_the_outage_does_not_invent_a_breach(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        _rule()
        _cache_cpu(device, FULL_WINDOW_90S, cpu=30.0)

        check_device_alerts(device, _since())

        assert not _fires(device)


class TestInstantReadersFallBackToDbWhenRedisIsEmpty:
    """Các getter 'latest' (không sustain) còn lại đọc trực tiếp — mỗi nhóm một test, gọi thẳng hàm."""

    def test_uplink_status_reads_the_db_when_redis_is_down(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1")
        InterfaceStats.objects.create(interface=iface, timestamp=_now(30), status="down")

        assert _uplink_statuses(device, [iface.pk], _since()) == {iface.pk: "down"}

    def test_uplink_status_ignores_the_db_while_redis_has_a_snapshot(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1")
        save_metrics(device, _nd(device))                       # Redis có snapshot (không có cổng này)
        InterfaceStats.objects.create(interface=iface, timestamp=_now(30), status="down")  # dòng bằng chứng

        assert _uplink_statuses(device, [iface.pk], _since()) == {iface.pk: None}

    def test_port_samples_read_the_db_when_redis_is_down(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1")
        for off in (120, 30):
            InterfaceStats.objects.create(interface=iface, timestamp=_now(off), status="down")

        samples = _port_samples(iface.pk, _since())

        assert [status for _, status in samples] == ["down", "down"]

    def test_latest_uplink_traffic_reads_the_db_when_redis_is_down(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        iface = Interface.objects.create(device=device, if_index=1, name="Gi0/1", is_uplink=True)
        InterfaceStats.objects.create(interface=iface, timestamp=_now(30), status="up", in_mbps=42.0)

        assert _uplink_traffic_max(device, _since(), "in") == 42.0

    def test_vm_counts_read_the_db_when_redis_is_down(self, redis_down):
        device = HyperVDeviceFactory()
        snapshot = _now(30)  # cùng 1 lần poll → cùng timestamp (getter lấy snapshot mới nhất theo timestamp)
        VMStats.objects.create(device=device, timestamp=snapshot, vm_name="a", state="Running",
                               repl_health="Normal")
        VMStats.objects.create(device=device, timestamp=snapshot, vm_name="b", state="Off",
                               repl_health="Critical")

        assert _count_vms_running(device, _since()) == 1.0
        assert _count_vms_repl_unhealthy(device, _since()) == 1.0

    def test_wifi_instant_counts_read_the_db_when_redis_is_down(self, redis_down):
        ac = HuaweiACDeviceFactory()
        snapshot = _now(30)  # cùng 1 lần poll → cùng timestamp
        WifiApStats.objects.create(device=ac, timestamp=snapshot, ap_name="AP1", is_online=False, client_count=4)
        WifiApStats.objects.create(device=ac, timestamp=snapshot, ap_name="AP2", is_online=True, client_count=3)

        assert _wifi_ap_offline_count(ac, _since()) == 1.0
        assert _wifi_client_count(ac, _since()) == 7.0

    def test_instant_mem_reads_the_db_when_redis_is_down(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        SystemHealth.objects.create(device=device, timestamp=_now(30), cpu_percent=1.0, mem_percent=77.0)

        assert _latest_mem(device, _since()) == 77.0


class TestHealthyRedisIsTheSourceOfTruth:
    """Redis CÓ dữ liệu thì DB (chỉ còn dòng bằng chứng ghi lúc alert nổ/đổi trạng thái) không được can thiệp."""

    def test_old_evidence_row_does_not_fire_a_sustained_rule(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        _rule()
        _cache_cpu(device, FULL_WINDOW_90S, cpu=30.0)
        _db_cpu(device, [300], cpu=99.0)           # dòng bằng chứng ở giữa cửa sổ

        check_device_alerts(device, _since())

        assert not _fires(device)

    def test_old_evidence_row_does_not_fire_an_instant_rule(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        _rule(name="Instant CPU", duration_min=0)
        _cache_cpu(device, [0], cpu=30.0)
        _db_cpu(device, [60], cpu=99.0)

        check_device_alerts(device, _since())

        assert not _fires(device)

    def test_a_stale_redis_snapshot_does_not_fall_back_to_a_fresher_looking_db_row(self, cache_mode):
        """Redis có snapshot nhưng cũ hơn cửa sổ (thiết bị ngừng poll): vẫn là 'không có dữ liệu mới',
        KHÔNG phải 'Redis trống' — không được nhặt dòng bằng chứng trong DB thay thế. (Dòng bằng chứng THẬT mang dấu
        `_evidence`; một dòng poll thường mới hơn thì được đọc — xem test_review_followup_2026_11_19.)"""
        device = CiscoSNMPDeviceFactory()
        _rule(name="Instant CPU", duration_min=0)
        _cache_cpu(device, [1500], cpu=30.0)       # snapshot 25 phút trước, ngoài cửa sổ since
        SystemHealth.objects.create(device=device, timestamp=_now(120), cpu_percent=99.0, mem_percent=40.0,
                                    extra={"_evidence": True})   # dòng bằng chứng trong cửa sổ

        check_device_alerts(device, _since())

        assert not _fires(device)


def test_ilo_sustained_uses_the_ilo_poll_cadence(settings):
    """Dung sai độ phủ của rule iLO phải theo `POLL_ILO_INTERVAL_SECS`, KHÔNG theo nhịp HyperV/WinRM.

    Hai nhịp mặc định cùng 300s nên phải ghi đè để tách chúng: iLO 900s, HyperV 300s. Mẫu cách nhau 1000s
    (đúng nhịp iLO chậm) chỉ nằm trong dung sai nếu engine dùng nhịp iLO."""
    settings.POLL_ILO_INTERVAL_SECS = 900
    settings.POLL_HYPERV_INTERVAL_SECS = 300
    device = HyperVDeviceFactory(ilo_ip_address="10.0.198.254")
    _rule(name="iLO missing disk", device_type="hyperv", metric="raid_missing_disk_count",
          condition="gte", threshold=1.0, duration_min=30)
    for off in (2750, 1750, 750, 0):        # mẫu biên 2750s (trước đầu cửa sổ 1800s)
        HardwareHealth.objects.create(device=device, timestamp=_now(off), missing_disk_count=2)

    check_device_alerts(device, _now() - timedelta(minutes=30))

    assert _fires(device)
