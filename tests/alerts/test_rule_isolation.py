"""Hạng mục 1.8 — lỗi ở MỘT rule không được làm mất các rule còn lại của cùng thiết bị.

Trước đây `check_device_alerts` không có `try` theo từng rule: 1 getter ném exception (dữ liệu
lạ, bug ở 1 metric) thì vòng `for rule` dừng hẳn, mọi rule phía sau của thiết bị đó không được xét
trong chu kỳ này, và nơi gọi chỉ log 1 dòng warning không kèm traceback. Rule hỏng nằm sớm trong
thứ tự (`severity`, `name`) có thể che cả rule offline/RAID phía sau — hỏng im lặng, lặp mỗi vòng.
"""
import logging
from datetime import UTC, datetime, timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone as dj_tz

from apps.alerts import engine
from apps.alerts.engine import check_device_alerts
from apps.alerts.models import Alert, AlertRule
from apps.metrics.models import SystemHealth
from tests.conftest import CiscoSNMPDeviceFactory

pytestmark = pytest.mark.django_db


def _now():
    return datetime.now(tz=UTC)


def _since():
    return _now() - timedelta(minutes=10)


def _rule(name: str, metric: str, **kwargs) -> AlertRule:
    defaults = dict(
        device_type="all", metric=metric, condition="gt", threshold=90.0,
        severity="WARNING", channels=[], enabled=True,
    )
    defaults.update(kwargs)
    return AlertRule.objects.create(name=name, **defaults)


@pytest.fixture
def device():
    dev = CiscoSNMPDeviceFactory(last_seen=dj_tz.now())
    # CPU và RAM cùng vượt ngưỡng 90 → cả 2 rule đều phải fire nếu được xét.
    SystemHealth.objects.create(
        device=dev, timestamp=_now(), cpu_percent=95.0, mem_percent=95.0,
    )
    return dev


@pytest.fixture
def two_rules():
    # Cùng severity → `Meta.ordering = ["severity", "name"]` xếp "A-…" TRƯỚC "B-…",
    # nên rule hỏng luôn đứng trước rule lành (kịch bản xấu nhất).
    return _rule("A-broken", "cpu_percent"), _rule("B-healthy", "mem_percent")


def _active(device, rule) -> bool:
    return Alert.objects.filter(device=device, rule=rule, is_active=True).exists()


class TestFailingRuleDoesNotHideOthers:
    def test_getter_exception_does_not_block_later_rules(self, device, two_rules, monkeypatch):
        broken, healthy = two_rules

        def boom(dev, since):
            raise RuntimeError("getter hỏng")

        monkeypatch.setitem(engine.METRIC_GETTERS, "cpu_percent", boom)

        check_device_alerts(device, _since())  # không được ném

        assert _active(device, healthy), "rule lành phía sau phải vẫn fire"
        assert not _active(device, broken)

    def test_fire_exception_does_not_block_later_rules(self, device, two_rules, monkeypatch):
        broken, healthy = two_rules
        real_fire = engine._fire_alert

        def fire(dev, rule, value):
            if rule.pk == broken.pk:
                raise RuntimeError("ghi alert hỏng")
            return real_fire(dev, rule, value)

        monkeypatch.setattr(engine, "_fire_alert", fire)

        check_device_alerts(device, _since())

        assert _active(device, healthy)

    def test_resolve_exception_does_not_block_later_rules(self, device, monkeypatch):
        # Cả 2 alert đang active, dữ liệu đã hồi phục → cả 2 rule đều phải resolve.
        SystemHealth.objects.all().delete()
        SystemHealth.objects.create(
            device=device, timestamp=_now(), cpu_percent=10.0, mem_percent=10.0,
        )
        broken = _rule("A-broken", "cpu_percent")
        healthy = _rule("B-healthy", "mem_percent")
        for rule in (broken, healthy):
            Alert.objects.create(
                device=device, rule=rule, severity="WARNING",
                message="x", metric_value=95.0, is_active=True,
            )
        real_resolve = engine._resolve_alert

        def resolve(dev, rule):
            if rule.pk == broken.pk:
                raise RuntimeError("resolve hỏng")
            return real_resolve(dev, rule)

        monkeypatch.setattr(engine, "_resolve_alert", resolve)

        check_device_alerts(device, _since())

        assert not _active(device, healthy), "alert lành phải được resolve dù rule trước đó hỏng"
        assert _active(device, broken), "rule hỏng giữ nguyên trạng thái, không bị đoán"

    def test_failure_is_logged_with_rule_device_and_traceback(
        self, device, two_rules, monkeypatch, caplog
    ):
        broken, _ = two_rules

        def boom(dev, since):
            raise RuntimeError("getter hỏng")

        monkeypatch.setitem(engine.METRIC_GETTERS, "cpu_percent", boom)

        with caplog.at_level(logging.ERROR, logger="apps.alerts.engine"):
            check_device_alerts(device, _since())

        records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(records) == 1, "mỗi rule hỏng ghi đúng 1 bản ghi ERROR"
        rec = records[0]
        assert rec.exc_info is not None, "phải có traceback để điều tra"
        text = rec.getMessage()
        assert "A-broken" in text and device.name in text and "cpu_percent" in text


class TestSoftTimeLimitStillPropagates:
    """`SoftTimeLimitExceeded` kế thừa `Exception` (đã kiểm trong venv) nên `except Exception`
    thuần sẽ nuốt nó — task poll sẽ không bao giờ dừng đúng hạn (cùng họ bug A7/A9)."""

    def test_soft_limit_is_not_swallowed_and_stops_evaluation(
        self, device, two_rules, monkeypatch
    ):
        _, healthy = two_rules

        def timeout(dev, since):
            raise SoftTimeLimitExceeded()

        monkeypatch.setitem(engine.METRIC_GETTERS, "cpu_percent", timeout)

        with pytest.raises(SoftTimeLimitExceeded):
            check_device_alerts(device, _since())

        assert not _active(device, healthy), "đã hết giờ thì không được tiếp tục xét rule sau"
