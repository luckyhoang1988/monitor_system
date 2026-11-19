"""Review ngoài lần 5 (2026-11-19) — P1 còn lại: mẫu Redis đã nhận rồi MẤT nằm GIỮA lịch sử DB cũ và mẫu Redis mới.

Dấu `Device.metrics_seen_at` (lần 4) chỉ bảo vệ ĐUÔI chuỗi: DB(99,99) → Redis nhận 10% → Redis mất dữ liệu → Redis nhận
99% mới ⇒ dấu nhảy sang mẫu mới, đuôi là mẫu Redis nên không bị coi cũ, và engine nối mẫu mới với lịch sử DB cũ qua cái
lỗ nơi mẫu 10% từng nằm (khoảng trống ≤ dung sai "poll lỡ" ~3 chu kỳ) ⇒ rule "CPU cao liên tục 4 phút" nổ giả dù 60 giây
trước CPU bình thường.

Cách chặn: writer ghi BỀN `Device.metrics_lost_upto` — thời điểm của mẫu cuối cùng Redis đã nhận rồi mất — ngay lúc nó thấy
Redis không còn giữ mẫu của dấu `metrics_seen_at`; engine không bao giờ nối chuỗi qua mốc đó (mọi mẫu ≤ mốc bị bỏ ⇒ chuỗi
phải bắt đầu SAU lần mất dữ liệu — cùng tinh thần "Redis flush đặt lại cửa sổ sustain").
"""
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.alerts.engine import (
    _port_down_throughout,
    _recovery_evidence_for,
    _sustained_cpu_mem,
    _sustained_host_perf,
    _sustained_uplink_traffic_max,
    _sustained_vm_metric,
    _sustained_wifi_ap_offline_count,
    _sustained_wifi_client_count,
    check_device_alerts,
)
from apps.alerts.models import Alert, AlertRule
from apps.collectors.base import InterfaceData, NormalizedData
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
    _lose_redis_data(client)


def _lose_redis_data(client) -> None:
    """Redis mất dữ liệu (restart/flush không có persistence): xoá mọi khoá metrics — chỉ `m:*`, không `flushdb`."""
    for pattern in ("m:latest:*", "m:series:sys:*", "m:series:if:*"):
        keys = client.keys(pattern)
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


def _nd_ports(device, ts, *, status="up", ports=("Gi0/1",), cpu=10.0) -> NormalizedData:
    data = _nd(device, cpu=cpu, ts=ts)
    data.interfaces = [InterfaceData(name=name, if_index=i + 1, status=status, in_bytes=0, out_bytes=0)
                       for i, name in enumerate(ports)]
    return data


def _if_key(device, name="Gi0/1") -> str:
    return f"m:series:if:{Interface.objects.get(device=device, name=name).pk}"


def _seen_at(device):
    return Device.objects.get(pk=device.pk).metrics_seen_at


def _lost_upto(device):
    return Device.objects.get(pk=device.pk).metrics_lost_upto


def _set_lost(device, off_secs):
    Device.objects.filter(pk=device.pk).update(metrics_lost_upto=_now(off_secs))


# ─── P1 · mẫu Redis đã nhận rồi mất, nằm giữa lịch sử DB cũ và mẫu Redis mới ─────────────────────────────────────

class TestRedisLosesAnAcceptedSampleThenReceivesANewOne:
    def test_new_high_sample_does_not_join_the_old_db_history(self, cache_mode, monkeypatch):
        """Đúng kịch bản review: CPU 99% ở DB (Redis lỗi) → Redis nhận CPU BÌNH THƯỜNG 60s trước → Redis mất mẫu đó →
        Redis nhận mẫu CPU cao mới. Rule "CPU cao liên tục 4 phút" không được nổ."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        _redis_writes_fail(monkeypatch)
        for off in (335, 245):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))
        monkeypatch.undo()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(60)))               # bình thường, nằm ở Redis
        _lose_redis_data(cache_mode)                                            # mẫu bình thường mất
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(5)))                 # mẫu cao mới

        check_device_alerts(device, _since())

        assert not _active(device, rule)

    def test_double_fault_variant_redis_fails_again_after_losing_the_sample(self, cache_mode, monkeypatch):
        """Biến thể "lỗi kép": Redis nhận 10% → mất dữ liệu VÀ ghi lỗi lại ⇒ mẫu cao mới lại vào DB. Dấu `metrics_seen_at`
        đứng yên ở mẫu 10% đã mất, mà đuôi chuỗi ghép là mẫu DB MỚI HƠN dấu nên không bị coi cũ."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        _redis_writes_fail(monkeypatch)
        for off in (335, 245):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))
        monkeypatch.undo()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(120)))
        _lose_redis_data(cache_mode)
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(30)))

        check_device_alerts(device, _since())

        assert not _active(device, rule)

    # ── đối chứng: KHÔNG mất dữ liệu thì mẫu 10% còn trong Redis và rule cũng không nổ (đã đúng từ trước) ─────────

    def test_control_redis_keeps_the_normal_sample(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        _redis_writes_fail(monkeypatch)
        for off in (335, 245):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))
        monkeypatch.undo()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(60)))
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(5)))

        check_device_alerts(device, _since())

        assert not _active(device, rule)
        assert _lost_upto(device) is None

    def test_control_double_fault_without_data_loss(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        _redis_writes_fail(monkeypatch)
        for off in (335, 245):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))
        monkeypatch.undo()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(120)))
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(30)))

        check_device_alerts(device, _since())

        assert not _active(device, rule)
        assert _lost_upto(device) is None

    # ── đối chứng: mốc mất dữ liệu chỉ chặn chuỗi VẮT QUA nó, không chặn chuỗi bắt đầu sau nó ────────────────────

    def test_a_chain_that_starts_after_the_loss_can_still_fire(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(1000)))
        _lose_redis_data(cache_mode)
        for off in (335, 245, 155, 65):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_an_instant_rule_is_not_affected_by_a_lost_sample(self, cache_mode):
        """Rule tức thời đọc mẫu mới nhất, không nối chuỗi nên không có "lỗ" nào để rơi vào."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule(name="Instant CPU", duration_min=0)
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(60)))
        _lose_redis_data(cache_mode)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(5)))

        check_device_alerts(device, _since())

        assert _active(device, rule)

    def test_healthy_redis_never_records_a_loss_and_sustained_rules_still_fire(self, cache_mode):
        """Chốt chặn chống báo mất dữ liệu GIẢ ở vận hành bình thường: nếu mỗi poll đều bị coi là "mất" thì mọi rule
        sustained bị chặn vĩnh viễn."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        for off in (610, 520, 430, 340, 250, 160, 70, 5):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))

        check_device_alerts(device, _since())

        assert _lost_upto(device) is None
        assert _active(device, rule)

    def test_loss_then_a_failed_write_then_recovery_does_not_bridge_either(self, cache_mode, monkeypatch):
        """Mất dữ liệu → Redis ghi lỗi (mẫu vào DB) → Redis lại nhận mẫu: chuỗi có cả mẫu DB lẫn mẫu Redis sau lần mất."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(300)))
        _lose_redis_data(cache_mode)
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(210)))
        monkeypatch.undo()
        for off in (120, 30):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))

        check_device_alerts(device, _since())

        assert not _active(device, rule)                                      # mọi mẫu sau mốc nhưng chưa có mẫu biên

    def test_two_losses_in_a_row_keep_blocking_the_newer_one(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        for off in (700, 610, 520, 430, 340):
            save_metrics(device, _nd(device, cpu=99.0, ts=_now(off)))
        _lose_redis_data(cache_mode)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(250)))            # mất lần 1 (mẫu −340s)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(160)))
        _lose_redis_data(cache_mode)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(70)))             # mất lần 2 (mẫu −160s)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(5)))

        check_device_alerts(device, _since())

        assert not _active(device, rule)


# ─── dấu bền `metrics_lost_upto` do writer ghi ───────────────────────────────────────────────────────────────

class TestWriterRecordsWhenRedisLostWhatItAccepted:
    def test_first_write_after_the_loss_records_the_last_accepted_sample(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        lost = _now(60)
        save_metrics(device, _nd(device, cpu=10.0, ts=lost))
        _lose_redis_data(cache_mode)

        save_metrics(device, _nd(device, cpu=99.0, ts=_now(5)))

        assert abs((_lost_upto(device) - lost).total_seconds()) < 0.001

    def test_the_loss_is_recorded_even_when_the_new_sample_falls_back_to_the_db(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()
        lost = _now(120)
        save_metrics(device, _nd(device, cpu=10.0, ts=lost))
        _lose_redis_data(cache_mode)
        _redis_writes_fail(monkeypatch)

        save_metrics(device, _nd(device, cpu=99.0, ts=_now(30)))

        assert abs((_lost_upto(device) - lost).total_seconds()) < 0.001
        assert SystemHealth.objects.filter(device=device).count() == 1            # mẫu vẫn vào DB như cũ
        assert abs((_seen_at(device) - lost).total_seconds()) < 0.001             # dấu đứng yên: Redis không nhận mẫu mới

    def test_a_series_restored_only_up_to_an_older_sample_counts_as_a_loss(self, cache_mode):
        """Redis khởi động lại từ bản RDB cũ: khoá còn nhưng mẫu mới nhất đã nhận biến mất."""
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(250)))
        newest = _now(160)
        save_metrics(device, _nd(device, cpu=10.0, ts=newest))
        cache_mode.lpop(f"m:series:sys:{device.pk}")                              # mất đúng mẫu mới nhất

        save_metrics(device, _nd(device, cpu=99.0, ts=_now(70)))

        assert abs((_lost_upto(device) - newest).total_seconds()) < 0.001

    def test_the_loss_point_never_moves_backwards(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(300)))
        _lose_redis_data(cache_mode)
        later = _now(100)
        Device.objects.filter(pk=device.pk).update(metrics_lost_upto=later)

        save_metrics(device, _nd(device, cpu=99.0, ts=_now(5)))                  # phát hiện mất mẫu lúc −300s

        assert abs((_lost_upto(device) - later).total_seconds()) < 0.001

    def test_a_second_loss_moves_the_point_forward(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(400)))
        _lose_redis_data(cache_mode)
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(300)))                # phát hiện lần 1 (mất mẫu −400s)
        second = _now(200)
        save_metrics(device, _nd(device, cpu=10.0, ts=second))
        _lose_redis_data(cache_mode)

        save_metrics(device, _nd(device, cpu=10.0, ts=_now(100)))                # phát hiện lần 2 (mất mẫu −200s)

        assert abs((_lost_upto(device) - second).total_seconds()) < 0.001

    def test_a_late_sample_older_than_the_marker_is_not_a_loss(self, cache_mode):
        """Mẫu cũ đến muộn (poll chậm): nó không làm dấu tiến lên nên không có gì để kiểm chứng/ghi."""
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(10)))

        save_metrics(device, _nd(device, cpu=10.0, ts=_now(200)))

        assert _lost_upto(device) is None

    def test_two_polls_arriving_out_of_order_are_not_a_loss(self, cache_mode):
        """Poll N+1 ghi xong TRƯỚC poll N (poll chậm đè lên nhau): danh sách Redis lệch thứ tự nhưng mẫu nào cũng còn."""
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(300)))
        stale = Device.objects.get(pk=device.pk)                                # poll N nạp thiết bị TRƯỚC khi N+1 ghi
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(100)))               # poll N+1 xong trước
        save_metrics(stale, _nd(stale, cpu=10.0, ts=_now(200)))                 # poll N xong muộn
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(10)))                # poll kế tiếp

        assert _lost_upto(device) is None

    def test_no_loss_is_recorded_when_nothing_was_ever_accepted(self, cache_mode):
        """Chưa có dấu `metrics_seen_at` (thiết bị mới / chưa poll thành công sau deploy) ⇒ không có gì để mất."""
        device = CiscoSNMPDeviceFactory()

        save_metrics(device, _nd(device, cpu=99.0, ts=_now(5)))

        assert _lost_upto(device) is None

    def test_no_loss_is_recorded_in_db_mode(self, settings):
        settings.METRICS_WRITE_MODE = "db"
        device = CiscoSNMPDeviceFactory()
        Device.objects.filter(pk=device.pk).update(metrics_seen_at=_now(100))
        device.refresh_from_db()

        save_metrics(device, _nd(device, ts=_now(5)))

        assert _lost_upto(device) is None

    def test_an_unreachable_redis_is_not_evidence_of_a_loss(self, cache_mode, monkeypatch):
        """Redis chết hẳn: mẫu vào DB, dấu đứng yên. Chưa hỏi được Redis thì chưa kết luận gì — lần phát hiện thật nằm ở lần
        đầu tiên hỏi được (Redis sống lại mà đã mất dữ liệu thì khoá/mẫu mới nhất thiếu)."""
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(120)))
        monkeypatch.setattr(metrics_cache, "_get_client", lambda: None)

        save_metrics(device, _nd(device, cpu=99.0, ts=_now(30)))

        assert _lost_upto(device) is None

    def test_a_write_that_succeeds_while_the_check_cannot_read_is_treated_as_unverified(self, cache_mode, monkeypatch):
        """Mẫu mới được nhận mà không kiểm chứng được Redis còn giữ mẫu cũ ⇒ dấu sắp nhảy qua mẫu cũ mà không có bằng chứng
        liền mạch ⇒ ghi mất-mẫu (an toàn: chỉ làm alert trễ, không bao giờ nổ giả)."""
        device = CiscoSNMPDeviceFactory()
        lost = _now(120)
        save_metrics(device, _nd(device, cpu=10.0, ts=lost))
        real = metrics_cache._get_client()

        class _ReadsFail:
            """Proxy: ghi chạy bình thường, mọi lệnh ĐỌC danh sách ném lỗi."""

            def __getattr__(self, name):
                return getattr(real, name)

            def lrange(self, *a, **k):
                raise ConnectionError("đọc lỗi")

            def lindex(self, *a, **k):
                raise ConnectionError("đọc lỗi")

        monkeypatch.setattr(metrics_cache, "_get_client", lambda: _ReadsFail())

        save_metrics(device, _nd(device, cpu=99.0, ts=_now(30)))

        assert abs((_lost_upto(device) - lost).total_seconds()) < 0.001
        assert abs((_seen_at(device) - (_now(30))).total_seconds()) < 2             # mẫu vẫn được nhận như thường

    def test_the_loss_point_is_on_the_in_memory_device_too(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        lost = _now(60)
        save_metrics(device, _nd(device, cpu=10.0, ts=lost))
        _lose_redis_data(cache_mode)

        save_metrics(device, _nd(device, cpu=99.0, ts=_now(5)))

        assert device.metrics_lost_upto == lost

    def test_a_late_sample_that_falls_back_to_the_db_is_not_a_loss(self, cache_mode, monkeypatch):
        """Nhánh ghi đường lui cũng chỉ kiểm chứng khi mẫu này MỚI HƠN dấu: mẫu cũ đến muộn không phải bằng chứng mất mẫu."""
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(10)))
        _redis_writes_fail(monkeypatch)

        save_metrics(device, _nd(device, cpu=10.0, ts=_now(200)))

        assert _lost_upto(device) is None

    @pytest.mark.parametrize("fail_write", [False, True], ids=["write-ok", "write-falls-back"])
    def test_an_unexpected_error_while_verifying_never_breaks_the_poll(self, cache_mode, monkeypatch, fail_write):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(120)))

        def boom(*args, **kwargs):
            raise RuntimeError("redis trả dữ liệu lạ")

        monkeypatch.setattr(metrics_cache, "recent_sys_ts", boom)
        if fail_write:
            _redis_writes_fail(monkeypatch)

        save_metrics(device, _nd(device, cpu=42.0, ts=_now(30)))                # không được ném

        if fail_write:
            assert SystemHealth.objects.filter(device=device).count() == 1
        else:
            assert metrics_cache.get_latest(device.id)["cpu"] == 42.0
            assert abs((_seen_at(device) - _now(30)).total_seconds()) < 2         # dấu vẫn tiến lên như thường

    def test_failing_to_record_the_loss_never_breaks_the_poll(self, cache_mode, monkeypatch, caplog):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(60)))
        _lose_redis_data(cache_mode)

        def boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(Device.objects, "filter", boom)

        with caplog.at_level("WARNING", logger="apps.metrics.writer"):
            save_metrics(device, _nd(device, cpu=42.0, ts=_now(5)))              # không được ném

        assert metrics_cache.get_latest(device.id)["cpu"] == 42.0
        # lỗi nằm ở bước GHI mốc (không phải bước kiểm chứng) và phải được báo đúng như vậy
        assert "không ghi được metrics_lost_upto" in caplog.text


# ─── engine: mọi họ chuỗi sustained đều không nối chuỗi qua mốc mất mẫu ────────────────────────────────────────

_WINDOW_OFFSETS = (700, 610, 520, 430, 340, 250, 160, 70)


class TestEveryFamilyRefusesToBridgeALostSample:
    """Dựng chuỗi DB (Redis trống): đối chứng đọc được; mốc nằm TRONG chuỗi (sau mẫu biên) ⇒ không còn bằng chứng; mốc nằm
    TRƯỚC mẫu biên ⇒ chuỗi bắt đầu sau lần mất nên vẫn đọc được."""

    @staticmethod
    def _check(device, rule, read, *, inside=300, before=650):
        with _recovery_evidence_for(device, rule):
            assert read() is not None, "đối chứng: chưa có mốc mất mẫu thì chuỗi phải đọc được"
        _set_lost(device, inside)
        with _recovery_evidence_for(device, rule):
            assert read() is None, "chuỗi vắt qua mẫu đã mất không được là bằng chứng 'điều kiện đúng suốt cửa sổ'"
        _set_lost(device, before)
        with _recovery_evidence_for(device, rule):
            assert read() is not None, "mốc nằm trước mẫu biên: chuỗi bắt đầu sau lần mất nên vẫn dùng được"

    def test_cpu_sustained(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = _rule(duration_min=10)
        for off in _WINDOW_OFFSETS:
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=99.0, mem_percent=40.0)

        self._check(device, rule, lambda: _sustained_cpu_mem(device, rule, _now() - timedelta(minutes=10)))

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

        self._check(device, rule, lambda: _sustained_host_perf(device, rule, _now() - timedelta(minutes=20)),
                    inside=500, before=1350)

    def test_vm_sustained(self, cache_mode):
        device = HyperVDeviceFactory()
        rule = _rule(name="VM repl", metric="vm_repl_unhealthy", threshold=0.0, duration_min=10)
        for off in _WINDOW_OFFSETS:
            VMStats.objects.create(device=device, timestamp=_now(off), vm_name="vm1", state="Running",
                                   repl_health="Critical")

        self._check(device, rule, lambda: _sustained_vm_metric(device, rule, _now() - timedelta(minutes=10)))

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
        Device.objects.filter(pk=device.pk).update(metrics_lost_upto=None)
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

        self._check(device, rule, lambda: True if _port_down_throughout(device, iface.pk, window) else None,
                    inside=200, before=350)

    def test_redis_samples_older_than_the_loss_point_are_not_a_bridge_either(self, cache_mode):
        """Mất một phần (RDB cũ): Redis còn mẫu cũ, mất đoạn giữa, rồi nhận mẫu mới. Chuỗi Redis liền mạch NHÌN THẤY nhưng
        đoạn đã mất nằm giữa — cũng không được nối."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule()
        ts_now = _now().timestamp()
        for off in (335, 245):
            metrics_cache.push_series(device.id, {"ts": ts_now - off, "cpu": 99.0, "mem": 40.0})
        metrics_cache.push_series(device.id, {"ts": ts_now - 5, "cpu": 99.0, "mem": 40.0})
        window = _now() - timedelta(minutes=4)

        with _recovery_evidence_for(device, rule):
            assert _sustained_cpu_mem(device, rule, window) is not None, "đối chứng: chưa có mốc thì chuỗi Redis đọc được"
        _set_lost(device, 60)
        with _recovery_evidence_for(device, rule):
            assert _sustained_cpu_mem(device, rule, window) is None


class TestPerInterfaceSeriesAreVerifiedToo:
    """Chuỗi trạng thái cổng / uplink đọc if-series (khoá RIÊNG từng cổng), không phải sys-series. Mất if-series mà sys-series
    còn nguyên (xoá khoá thủ công, bị đẩy khỏi bộ nhớ) cũng phải bị coi là mất mẫu — nếu không chuỗi cổng nối qua cái lỗ đó."""

    def test_losing_only_the_interface_series_is_a_loss(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        lost = _now(120)
        save_metrics(device, _nd_ports(device, lost))
        cache_mode.delete(_if_key(device))                                         # sys-series còn nguyên

        save_metrics(device, _nd_ports(device, _now(30)))

        assert abs((_lost_upto(device) - lost).total_seconds()) < 0.001

    def test_losing_only_the_newest_interface_sample_is_a_loss(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd_ports(device, _now(250)))
        newest = _now(160)
        save_metrics(device, _nd_ports(device, newest))
        cache_mode.lpop(_if_key(device))

        save_metrics(device, _nd_ports(device, _now(70)))

        assert abs((_lost_upto(device) - newest).total_seconds()) < 0.001

    def test_the_failed_write_path_checks_the_interface_series_too(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()
        lost = _now(120)
        save_metrics(device, _nd_ports(device, lost))
        cache_mode.delete(_if_key(device))
        _redis_writes_fail(monkeypatch)

        save_metrics(device, _nd_ports(device, _now(30)))

        assert abs((_lost_upto(device) - lost).total_seconds()) < 0.001

    def test_port_down_streak_does_not_bridge_a_lost_up_sample(self, cache_mode, monkeypatch):
        """Đúng hình dạng lỗi của CPU nhưng ở cổng: DB(down,down) → Redis nhận 'up' → mất if-series → Redis nhận 'down'."""
        device = CiscoSNMPDeviceFactory()
        rule = AlertRule.objects.create(
            name="Uplink Down", device_type="switch", metric="if_status", condition="eq", threshold=0.0,
            severity="CRITICAL", channels=[], enabled=True, duration_min=4,
        )
        _redis_writes_fail(monkeypatch)
        for off in (335, 245):
            save_metrics(device, _nd_ports(device, _now(off), status="down"))
        monkeypatch.undo()
        save_metrics(device, _nd_ports(device, _now(60), status="up"))
        cache_mode.delete(_if_key(device))
        save_metrics(device, _nd_ports(device, _now(5), status="down"))
        iface = Interface.objects.get(device=device, name="Gi0/1")

        with _recovery_evidence_for(device, rule):
            assert not _port_down_throughout(device, iface.pk, _now() - timedelta(minutes=4))

    def test_losing_the_series_of_just_one_of_several_ports_is_a_loss(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        lost = _now(120)
        save_metrics(device, _nd_ports(device, lost, ports=("Gi0/1", "Gi0/2", "Gi0/3")))
        cache_mode.delete(_if_key(device, "Gi0/2"))                                # chỉ cổng thứ hai mất

        save_metrics(device, _nd_ports(device, _now(30), ports=("Gi0/1", "Gi0/2", "Gi0/3")))

        assert abs((_lost_upto(device) - lost).total_seconds()) < 0.001

    def test_no_extra_snapshot_read_before_the_first_accepted_sample(self, cache_mode, monkeypatch):
        """Chưa có dấu `metrics_seen_at` thì chưa có gì để mất ⇒ không đọc thêm snapshot chỉ để lấy danh sách cổng."""
        device = CiscoSNMPDeviceFactory()
        real = metrics_cache.get_latest
        calls = []
        monkeypatch.setattr(metrics_cache, "get_latest", lambda dev_id: calls.append(dev_id) or real(dev_id))

        save_metrics(device, _nd_ports(device, _now(120)))
        assert len(calls) == 1                                                    # chỉ lần đọc `prev` của chính việc ghi

        save_metrics(device, _nd_ports(device, _now(30)))
        assert len(calls) == 3                                                    # có dấu: thêm đúng 1 lần đọc danh sách cổng

    def test_a_garbled_previous_snapshot_never_breaks_the_poll(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd_ports(device, _now(120)))
        cache_mode.set(f"m:latest:{device.pk}", '{"ts": 1.0, "interfaces": {"không-phải-số": {}}}')

        save_metrics(device, _nd_ports(device, _now(30), ports=("Gi0/1",)))        # không được ném

        assert metrics_cache.get_latest(device.id)["cpu"] == 10.0

    def test_control_port_down_streak_without_a_loss_is_still_recognised(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = AlertRule.objects.create(
            name="Uplink Down", device_type="switch", metric="if_status", condition="eq", threshold=0.0,
            severity="CRITICAL", channels=[], enabled=True, duration_min=4,
        )
        for off in (335, 245, 155, 65, 5):
            save_metrics(device, _nd_ports(device, _now(off), status="down"))
        iface = Interface.objects.get(device=device, name="Gi0/1")

        with _recovery_evidence_for(device, rule):
            assert _port_down_throughout(device, iface.pk, _now() - timedelta(minutes=4))
        assert _lost_upto(device) is None

    # ── đối chứng: biến động danh sách cổng bình thường KHÔNG phải mất mẫu ───────────────────────────────────

    def test_a_port_that_appears_later_is_not_a_loss(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd_ports(device, _now(300), ports=("Gi0/1",)))
        save_metrics(device, _nd_ports(device, _now(210), ports=("Gi0/1", "Gi0/2")))      # cổng mới xuất hiện
        save_metrics(device, _nd_ports(device, _now(120), ports=("Gi0/1", "Gi0/2")))
        save_metrics(device, _nd_ports(device, _now(30), ports=("Gi0/1", "Gi0/2")))

        assert _lost_upto(device) is None

    def test_a_port_that_disappears_is_not_a_loss(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd_ports(device, _now(300), ports=("Gi0/1", "Gi0/2")))
        save_metrics(device, _nd_ports(device, _now(210), ports=("Gi0/1",)))             # Gi0/2 không có trong lần poll này
        save_metrics(device, _nd_ports(device, _now(120), ports=("Gi0/1",)))
        save_metrics(device, _nd_ports(device, _now(30), ports=("Gi0/1", "Gi0/2")))      # rồi quay lại

        assert _lost_upto(device) is None

    def test_a_device_without_ports_is_not_a_loss(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        for off in (300, 210, 120, 30):
            save_metrics(device, _nd(device, cpu=10.0, ts=_now(off)))

        assert _lost_upto(device) is None

    def test_an_unreadable_interface_series_is_unverified(self, cache_mode, monkeypatch):
        """Sys-series đọc được nhưng pipeline đọc if-series lỗi: mẫu vẫn được nhận mà không kiểm chứng được ⇒ ghi mất-mẫu."""
        device = CiscoSNMPDeviceFactory()
        lost = _now(120)
        save_metrics(device, _nd_ports(device, lost))
        monkeypatch.setattr(metrics_cache, "recent_if_ts", lambda *a, **k: metrics_cache.UNREADABLE)

        save_metrics(device, _nd_ports(device, _now(30)))

        assert abs((_lost_upto(device) - lost).total_seconds()) < 0.001


class TestRecentSysTs:
    """`metrics_cache.recent_sys_ts`: nguồn của phép kiểm chứng "Redis còn giữ mẫu cũ" ở writer."""

    def test_missing_key_is_an_empty_list_not_unreadable(self, cache_mode):
        assert metrics_cache.recent_sys_ts(987654) == []

    def test_returns_the_newest_samples_first_and_respects_the_count(self, cache_mode):
        for ts in (100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0):
            metrics_cache.push_series(555, {"ts": ts, "cpu": 1.0, "mem": 1.0})

        assert metrics_cache.recent_sys_ts(555, 5) == [700.0, 600.0, 500.0, 400.0, 300.0]
        assert metrics_cache.recent_sys_ts(555, 2) == [700.0, 600.0]

    def test_skips_corrupt_items(self, cache_mode):
        key = "m:series:sys:556"
        cache_mode.lpush(key, '{"ts": 100.0, "cpu": 1, "mem": 1}')
        cache_mode.lpush(key, "không phải json")
        cache_mode.lpush(key, '{"cpu": 1}')
        cache_mode.lpush(key, '{"ts": 300.0, "cpu": 1, "mem": 1}')

        assert metrics_cache.recent_sys_ts(556, 5) == [300.0, 100.0]

    def test_unreachable_redis_is_not_an_empty_list(self, cache_mode, monkeypatch):
        monkeypatch.setattr(metrics_cache, "_get_client", lambda: None)
        assert metrics_cache.recent_sys_ts(555) is metrics_cache.UNREADABLE

    def test_a_read_error_is_not_an_empty_list(self, cache_mode, monkeypatch):
        real = metrics_cache._get_client()

        class _ReadsFail:
            def __getattr__(self, name):
                return getattr(real, name)

            def lrange(self, *a, **k):
                raise ConnectionError("đọc lỗi")

        monkeypatch.setattr(metrics_cache, "_get_client", lambda: _ReadsFail())

        assert metrics_cache.recent_sys_ts(555) is metrics_cache.UNREADABLE

    def test_recent_if_ts_reads_each_interface_and_marks_missing_ones_empty(self, cache_mode):
        for ts in (100.0, 200.0, 300.0):
            metrics_cache.push_series(557, None, {701: {"ts": ts, "status": "up"}})
        metrics_cache.push_series(557, None, {702: {"ts": 50.0, "status": "up"}})

        got = metrics_cache.recent_if_ts([701, 702, 703], 2)

        assert got == {701: [300.0, 200.0], 702: [50.0], 703: []}

    def test_recent_if_ts_with_no_interfaces_needs_no_redis(self, cache_mode, monkeypatch):
        monkeypatch.setattr(metrics_cache, "_get_client", lambda: None)
        assert metrics_cache.recent_if_ts([]) == {}

    def test_recent_if_ts_unreachable_is_unreadable(self, cache_mode, monkeypatch):
        monkeypatch.setattr(metrics_cache, "_get_client", lambda: None)
        assert metrics_cache.recent_if_ts([701]) is metrics_cache.UNREADABLE

    def test_recent_if_ts_pipeline_error_is_unreadable(self, cache_mode, monkeypatch):
        real = metrics_cache._get_client()

        class _PipeFails:
            def __getattr__(self, name):
                return getattr(real, name)

            def pipeline(self, *a, **k):
                raise ConnectionError("pipeline lỗi")

        monkeypatch.setattr(metrics_cache, "_get_client", lambda: _PipeFails())

        assert metrics_cache.recent_if_ts([701]) is metrics_cache.UNREADABLE


class TestTheLossPointOnlyMattersInCacheMode:
    def test_nothing_to_cut_costs_no_query(self, cache_mode):
        """Chưa có mẫu nào thì không có gì để cắt: không đọc mốc."""
        device = CiscoSNMPDeviceFactory()
        rule = _rule(duration_min=10)

        with _recovery_evidence_for(device, rule), CaptureQueriesContext(connection) as ctx:
            assert _sustained_cpu_mem(device, rule, _now() - timedelta(minutes=10)) is None

        assert not [q["sql"] for q in ctx.captured_queries if "devices_device" in q["sql"]]

    def test_db_mode_ignores_it(self, settings):
        settings.METRICS_WRITE_MODE = "db"
        device = CiscoSNMPDeviceFactory()
        rule = _rule(duration_min=10)
        for off in _WINDOW_OFFSETS:
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=99.0, mem_percent=40.0)
        _set_lost(device, 300)

        with _recovery_evidence_for(device, rule):
            assert _sustained_cpu_mem(device, rule, _now() - timedelta(minutes=10)) is not None

    def test_marks_are_read_with_one_query_per_rule_evaluation(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        rule = _rule(duration_min=10)
        for off in _WINDOW_OFFSETS:
            SystemHealth.objects.create(device=device, timestamp=_now(off), cpu_percent=99.0, mem_percent=40.0)
        _set_lost(device, 650)

        with _recovery_evidence_for(device, rule), CaptureQueriesContext(connection) as ctx:
            _sustained_cpu_mem(device, rule, _now() - timedelta(minutes=10))

        marks = [q["sql"] for q in ctx.captured_queries if "devices_device" in q["sql"]]
        assert len(marks) == 1, marks
