"""Test bộ lọc khung thời gian tùy chọn (?from/?to) cho API metrics."""
import pytest
from datetime import timedelta
from django.contrib.auth.models import User
from django.utils import timezone

from apps.metrics.api import _select_source, _parse_local
from apps.metrics.models import SystemHealth
from tests.conftest import HyperVDeviceFactory


@pytest.fixture
def logged_client(client, db):
    User.objects.create_user(username="window_user", password="pass123")
    client.login(username="window_user", password="pass123")
    return client


def test_select_source_recent_small_span_is_raw():
    now = timezone.now()
    assert _select_source(now - timedelta(hours=6), now) == "raw"


def test_select_source_old_window_falls_to_hourly():
    now = timezone.now()
    # Khoảng 6h nhưng cách đây 10 ngày → raw đã bị dọn → hourly
    since = now - timedelta(days=10)
    assert _select_source(since, since + timedelta(hours=6)) == "hourly"


def test_select_source_wide_span_is_daily():
    now = timezone.now()
    assert _select_source(now - timedelta(days=30), now) == "daily"


def test_parse_local_invalid_returns_none():
    assert _parse_local("not-a-date") is None
    assert _parse_local("") is None


@pytest.mark.django_db
def test_device_metrics_custom_window_filters_range(logged_client):
    device = HyperVDeviceFactory(collect_interval=300)
    now = timezone.now()
    inside = now - timedelta(hours=3)
    outside = now - timedelta(hours=20)
    for ts, cpu in [(inside, 50.0), (outside, 99.0)]:
        SystemHealth.objects.create(device=device, timestamp=ts, cpu_percent=cpu, mem_percent=10.0)

    # Trình duyệt gửi giờ địa phương (wall-clock) → format theo localtime.
    frm = timezone.localtime(now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")
    to = timezone.localtime(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    resp = logged_client.get(f"/api/metrics/{device.pk}/?from={frm}&to={to}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "raw"
    # Chỉ điểm trong khoảng (cpu 50) lọt, điểm 20h trước (cpu 99) bị loại.
    assert payload["cpu_percent"] == [50.0]


@pytest.mark.django_db
def test_device_metrics_reversed_window_is_swapped(logged_client):
    device = HyperVDeviceFactory(collect_interval=300)
    now = timezone.now()
    inside = now - timedelta(hours=3)
    outside = now - timedelta(hours=20)
    for ts, cpu in [(inside, 50.0), (outside, 99.0)]:
        SystemHealth.objects.create(device=device, timestamp=ts, cpu_percent=cpu, mem_percent=10.0)

    # from > to (chọn ngược) → server tự hoán đổi, lọc đúng khoảng [-5h, -1h].
    frm = timezone.localtime(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    to = timezone.localtime(now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")
    resp = logged_client.get(f"/api/metrics/{device.pk}/?from={frm}&to={to}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "raw"
    assert payload["cpu_percent"] == [50.0]
