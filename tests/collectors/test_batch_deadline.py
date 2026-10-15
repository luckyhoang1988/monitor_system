"""1.7 (A7) — batch poll_all_hyperv / poll_all_ilo phải dừng SẠCH theo deadline, không chờ hard-kill.

Gốc: SoftTimeLimitExceeded kế thừa Exception nên bị `except Exception` (trong _poll_device_once và
trong chính vòng lặp batch) nuốt → batch chạy tiếp quá soft limit cho tới khi Celery SIGKILL cả task
(prod 10 ngày: 6 hard-kill hyperv, batch chạy tới 269,8s). Test dùng đồng hồ giả: mỗi lần poll "tốn"
N giây trên đồng hồ giả, không ngủ thật.
"""
from datetime import timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

from apps.collectors import tasks
from apps.collectors.ilo_redfish import IloRedfishClient
from apps.devices.models import Device
from tests.conftest import HyperVDeviceFactory


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    # raising=False: trên code cũ (chưa có _monotonic) test vẫn chạy và đỏ vì HÀNH VI, không vì AttributeError.
    monkeypatch.setattr(tasks, "_monotonic", c, raising=False)
    return c


# Số trong test dựa trên: hyperv soft=250s, margin=60s → deadline 190s; iLO soft=60s, margin=20s → 40s.
@pytest.mark.django_db
class TestHypervDeadline:
    def test_does_not_start_new_host_after_deadline(self, mocker, clock, caplog):
        a = HyperVDeviceFactory(enabled=True)
        b = HyperVDeviceFactory(enabled=True)
        c = HyperVDeviceFactory(enabled=True)
        polled: list[int] = []

        def fake_poll(pk):
            polled.append(pk)
            clock.advance(100)  # mỗi host "tốn" 100s → host thứ 3 bắt đầu ở t=200 > deadline 190

        mocker.patch("apps.collectors.tasks._poll_device_once", side_effect=fake_poll)
        with caplog.at_level("WARNING", logger="apps.collectors.tasks"):
            tasks.poll_all_hyperv.run()

        assert len(polled) == 2, "host thứ 3 bắt đầu sau deadline phải bị bỏ qua"
        skipped = ({a.pk, b.pk, c.pk} - set(polled)).pop()
        skipped_name = Device.objects.get(pk=skipped).name
        assert skipped_name in caplog.text, "log phải nêu tên host bị bỏ qua"

    def test_swallowed_soft_limit_does_not_let_batch_continue(self, mocker, clock):
        """Soft limit bị nuốt bên trong host (poll quay lại bình thường nhưng đã quá hạn)."""
        for _ in range(3):
            HyperVDeviceFactory(enabled=True)
        calls = []

        def fake_poll(pk):
            calls.append(pk)
            clock.advance(tasks.POLL_HYPERV_BATCH_SOFT_LIMIT + 5)  # quá soft limit, rồi trả về bình thường

        mocker.patch("apps.collectors.tasks._poll_device_once", side_effect=fake_poll)
        tasks.poll_all_hyperv.run()

        assert len(calls) == 1, "đã quá soft limit thì không được bắt đầu host kế tiếp"

    def test_soft_limit_from_host_stops_batch(self, mocker, clock):
        for _ in range(3):
            HyperVDeviceFactory(enabled=True)
        mock_once = mocker.patch(
            "apps.collectors.tasks._poll_device_once",
            side_effect=SoftTimeLimitExceeded(),
        )
        tasks.poll_all_hyperv.run()  # không được ném ra ngoài task

        assert mock_once.call_count == 1, "SoftTimeLimitExceeded phải dừng batch, không tính là 1 host lỗi rồi đi tiếp"

    def test_healthy_hosts_polled_before_failing_host(self, mocker, clock):
        now = timezone.now()
        # Tạo theo thứ tự pk: failed → ok_new → ok_old. Thứ tự mong đợi: ok_old, ok_new, failed.
        failed = HyperVDeviceFactory(enabled=True, last_seen=None, last_ok_seen=now - timedelta(minutes=5))
        ok_new = HyperVDeviceFactory(enabled=True, last_seen=now, last_ok_seen=now - timedelta(minutes=1))
        ok_old = HyperVDeviceFactory(enabled=True, last_seen=now, last_ok_seen=now - timedelta(minutes=10))
        order: list[int] = []
        mocker.patch("apps.collectors.tasks._poll_device_once", side_effect=lambda pk: order.append(pk))

        tasks.poll_all_hyperv.run()

        assert order == [ok_old.pk, ok_new.pk, failed.pk]

    def test_never_polled_host_goes_after_healthy_ones(self, mocker, clock):
        now = timezone.now()
        fresh = HyperVDeviceFactory(enabled=True, last_seen=None, last_ok_seen=None)
        ok = HyperVDeviceFactory(enabled=True, last_seen=now, last_ok_seen=now)
        order: list[int] = []
        mocker.patch("apps.collectors.tasks._poll_device_once", side_effect=lambda pk: order.append(pk))

        tasks.poll_all_hyperv.run()

        assert order == [ok.pk, fresh.pk]

    def test_normal_run_polls_every_host(self, mocker, clock, caplog):
        """Đối chứng: batch nhanh (51s như prod) phải poll đủ mọi host, không cắt nhầm."""
        for _ in range(3):
            HyperVDeviceFactory(enabled=True)
        mock_once = mocker.patch(
            "apps.collectors.tasks._poll_device_once",
            side_effect=lambda pk: clock.advance(17),
        )
        with caplog.at_level("INFO", logger="apps.collectors.tasks"):
            tasks.poll_all_hyperv.run()

        assert mock_once.call_count == 3
        assert "Polled 3/3 hyperv hosts" in caplog.text


@pytest.mark.django_db
class TestIloDeadline:
    @pytest.fixture
    def collect_calls(self, mocker):
        calls: list[int] = []

        def fake_collect(self):
            calls.append(self.device.pk)
            return None  # coi như iLO không trả lời; chỉ quan tâm thứ tự/số lần gọi

        mocker.patch.object(IloRedfishClient, "collect_raw", autospec=True, side_effect=fake_collect)
        return calls

    def test_does_not_start_new_host_after_deadline(self, mocker, clock):
        for i in range(3):
            HyperVDeviceFactory(enabled=True, ilo_ip_address=f"10.9.9.{i + 1}")
        calls: list[int] = []

        def slow_collect(self):
            calls.append(self.device.pk)
            clock.advance(30)  # 2 host đầu: t=0→30→60; host thứ 3 bắt đầu ở t=60 > deadline 40
            return None

        mocker.patch.object(IloRedfishClient, "collect_raw", autospec=True, side_effect=slow_collect)
        tasks.poll_all_ilo()

        assert len(calls) == 2

    def test_host_with_last_error_polled_last(self, clock, collect_calls):
        bad = HyperVDeviceFactory(enabled=True, ilo_ip_address="10.9.9.1", ilo_last_error="401 Unauthorized")
        good = HyperVDeviceFactory(enabled=True, ilo_ip_address="10.9.9.2")

        tasks.poll_all_ilo()

        assert collect_calls == [good.pk, bad.pk]

    def test_soft_limit_in_host_is_not_blamed_on_host_and_stops_batch(self, mocker, clock):
        first = HyperVDeviceFactory(enabled=True, ilo_ip_address="10.9.9.1")
        HyperVDeviceFactory(enabled=True, ilo_ip_address="10.9.9.2")
        mock_collect = mocker.patch.object(
            IloRedfishClient, "collect_raw", side_effect=SoftTimeLimitExceeded()
        )

        tasks.poll_all_ilo()

        assert mock_collect.call_count == 1, "soft limit phải dừng batch"
        first.refresh_from_db()
        assert first.ilo_last_error == "", "hết giờ của batch không phải lỗi của iLO — không được ghi lên UI"

    def test_normal_run_polls_every_host(self, mocker, clock, caplog):
        for i in range(3):
            HyperVDeviceFactory(enabled=True, ilo_ip_address=f"10.9.9.{i + 1}")

        def quick_collect(self):
            clock.advance(10)  # như prod ~10s/host
            return None

        mock_collect = mocker.patch.object(
            IloRedfishClient, "collect_raw", autospec=True, side_effect=quick_collect
        )
        tasks.poll_all_ilo()

        assert mock_collect.call_count == 3
