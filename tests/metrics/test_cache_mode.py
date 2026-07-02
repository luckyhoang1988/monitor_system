"""Cache-first metrics — end-to-end với Redis thật (METRICS_WRITE_MODE=cache).

Kiểm: writer ghi Redis (không ghi raw DB), Mbps tính từ prev cache, evidence khi
đổi trạng thái, alert engine + chart API đọc cache, fallback DB khi Redis chết.
"""
import json
import pytest
from datetime import datetime, timezone, timedelta

from django.utils import timezone as dj_tz

from apps.collectors.base import NormalizedData, InterfaceData
from apps.devices.models import Interface
from apps.metrics.models import InterfaceStats, SystemHealth
from apps.metrics import cache as metrics_cache
from apps.metrics.writer import save_metrics
from tests.conftest import CiscoSNMPDeviceFactory

pytestmark = pytest.mark.django_db


def _now(offset=0):
    return datetime.now(tz=timezone.utc) - timedelta(seconds=offset)


def _nd(device, cpu=50.0, mem=40.0, ifaces=None, ts=None, extra=None):
    return NormalizedData(
        device_name=device.name, ip_address=device.ip_address,
        timestamp=ts or _now(), os_family="cisco_ios",
        cpu_percent=cpu, mem_percent=mem,
        interfaces=ifaces or [], extra=extra or {},
    )


def _if(name="Gi0/1", idx=1, status="up", in_b=0, out_b=0, speed=1000):
    return InterfaceData(name=name, if_index=idx, status=status,
                         in_bytes=in_b, out_bytes=out_b, speed_mbps=speed)


@pytest.fixture
def cache_mode(settings):
    """Bật cache-mode + dọn key Redis test (trước & sau)."""
    settings.METRICS_WRITE_MODE = "cache"
    client = metrics_cache._get_client()
    if client is None:
        pytest.skip("Redis /1 không sẵn sàng")
    yield client
    for pat in ("m:latest:*", "m:series:sys:*", "m:series:if:*"):
        keys = client.keys(pat)
        if keys:
            client.delete(*keys)


def test_write_goes_to_cache_not_raw_db(cache_mode):
    dev = CiscoSNMPDeviceFactory()
    save_metrics(dev, _nd(dev, cpu=55.5, mem=33.3, ifaces=[_if(in_b=1000, out_b=2000)]))

    snap = metrics_cache.get_latest(dev.id)
    assert snap is not None
    assert snap["cpu"] == 55.5 and snap["mem"] == 33.3
    iface = Interface.objects.get(device=dev, name="Gi0/1")  # inventory VẪN ghi DB
    assert str(iface.id) in snap["interfaces"]
    # KHÔNG ghi raw metrics (chưa có sự cố).
    assert SystemHealth.objects.filter(device=dev).count() == 0
    assert InterfaceStats.objects.filter(interface__device=dev).count() == 0
    assert len(metrics_cache.get_sys_series(dev.id)) == 1


def test_mbps_from_cache_prev(cache_mode):
    dev = CiscoSNMPDeviceFactory()
    save_metrics(dev, _nd(dev, ifaces=[_if(in_b=1_000_000, out_b=0)], ts=_now(300)))
    save_metrics(dev, _nd(dev, ifaces=[_if(in_b=2_000_000, out_b=0)], ts=_now(0)))

    iface = Interface.objects.get(device=dev, name="Gi0/1")
    snap = metrics_cache.get_latest(dev.id)
    in_mbps = snap["interfaces"][str(iface.id)]["in_mbps"]
    assert abs(in_mbps - 0.027) < 0.002  # delta 1e6 bytes / 300s


def test_status_change_persists_evidence(cache_mode):
    dev = CiscoSNMPDeviceFactory()
    save_metrics(dev, _nd(dev, ifaces=[_if(status="up")]))
    assert InterfaceStats.objects.filter(interface__device=dev).count() == 0
    save_metrics(dev, _nd(dev, ifaces=[_if(status="down")]))
    assert InterfaceStats.objects.filter(interface__device=dev).count() == 1
    assert SystemHealth.objects.filter(device=dev).count() == 1


def test_fallback_to_db_when_redis_down(cache_mode, settings):
    dev = CiscoSNMPDeviceFactory()
    metrics_cache._client = None
    settings.CACHE_REDIS_URL = "redis://127.0.0.1:6399/1"  # port chết
    save_metrics(dev, _nd(dev, ifaces=[_if(in_b=1, out_b=1)]))
    metrics_cache._client = None  # reset cho test sau
    assert SystemHealth.objects.filter(device=dev).count() == 1
    assert InterfaceStats.objects.filter(interface__device=dev).count() == 1


def test_latest_cpu_and_sustained_from_cache(cache_mode):
    from apps.alerts.engine import _latest_cpu, _sustained_cpu_mem
    from apps.alerts.models import AlertRule

    dev = CiscoSNMPDeviceFactory()
    since = dj_tz.now() - timedelta(minutes=10)
    for off in (240, 120, 0):
        save_metrics(dev, _nd(dev, cpu=95.0, mem=40.0, ts=_now(off)))

    assert _latest_cpu(dev, since) == 95.0
    rule = AlertRule(metric="cpu_percent", condition="gt", threshold=90, duration_min=3)
    assert _sustained_cpu_mem(dev, rule, dj_tz.now() - timedelta(minutes=3)) == 95.0


def test_mem_zero_sentinel_ignored(cache_mode):
    from apps.alerts.engine import _latest_mem
    dev = CiscoSNMPDeviceFactory()
    save_metrics(dev, _nd(dev, cpu=10.0, mem=0.0))
    assert _latest_mem(dev, dj_tz.now() - timedelta(minutes=10)) is None


def test_device_metrics_raw_api_from_cache(cache_mode):
    from apps.metrics.api import _device_metrics_raw

    dev = CiscoSNMPDeviceFactory()
    for off in (240, 120, 0):
        save_metrics(dev, _nd(dev, cpu=42.0, mem=21.0, ts=_now(off)))

    now = dj_tz.now()
    resp = _device_metrics_raw(dev, now - timedelta(hours=1), now)
    data = json.loads(resp.content)
    assert data["source"] == "raw"
    assert len(data["cpu_percent"]) == 3
    assert data["cpu_percent"][-1] == 42.0
