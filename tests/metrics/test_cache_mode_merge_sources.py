"""Chart/export ở cache-mode phải GHÉP Redis + DB theo thời điểm và theo từng cổng (review ngoài 2026-11-19 lần 4, P2).

Lần 1 (F01) chỉ cho đọc DB khi Redis TRỐNG. Nhưng khi Redis ghi lỗi mà vẫn đọc được, writer rơi về ghi DB (mẫu mới 99% nằm
ở DB) còn Redis giữ mẫu cũ (10%) — chart/export có dữ liệu Redis nên bỏ qua hẳn DB và che mất mẫu mới. Với interface thì tệ
hơn: 1 cổng có cache khiến cổng khác chỉ có dữ liệu DB bị bỏ qua (cờ "Redis có dữ liệu" tính chung cho mọi cổng).
"""
import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from apps.collectors.base import NormalizedData
from apps.devices.models import Interface
from apps.metrics import cache as metrics_cache
from apps.metrics.api import _device_metrics_raw, _interface_metrics_raw
from apps.metrics.models import EVIDENCE_KEY, InterfaceStats, SystemHealth
from apps.metrics.writer import save_metrics
from tests.conftest import CiscoSNMPDeviceFactory

pytestmark = pytest.mark.django_db


def _now(offset_secs: float = 0.0) -> datetime:
    return datetime.now(tz=UTC) - timedelta(seconds=offset_secs)


def _window():
    now = _now()
    return now - timedelta(hours=1), now


def _nd(device, cpu=42.0, ts=None) -> NormalizedData:
    return NormalizedData(
        device_name=device.name, ip_address=device.ip_address, timestamp=ts or _now(), os_family="cisco_ios",
        cpu_percent=cpu, mem_percent=21.0, interfaces=[], extra={},
    )


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


@pytest.fixture
def api_client():
    client = APIClient()
    client.force_authenticate(user=User.objects.create_user(username="merger", password="x"))
    return client


def _redis_writes_fail(monkeypatch):
    monkeypatch.setattr(metrics_cache, "set_latest", lambda *a, **k: False)
    monkeypatch.setattr(metrics_cache, "push_series", lambda *a, **k: False)


def _export(api_client, device, export_type):
    response = api_client.get("/api/metrics/export/", {
        "device_id": device.pk, "type": export_type, "range": "1h", "export_format": "csv",
    })
    assert response.status_code == 200
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def _push_if(device, iface, off, in_mbps=1.0):
    ts = _now(off).timestamp()
    metrics_cache.push_series(device.id, {"ts": ts},
                              {iface.id: {"ts": ts, "in_mbps": in_mbps, "out_mbps": 0.0, "status": "up"}})


def _db_if(iface, off, in_mbps=1.0):
    InterfaceStats.objects.create(interface=iface, timestamp=_now(off), status="up", in_mbps=in_mbps, out_mbps=0.0)


def _two_ports(device):
    a = Interface.objects.create(device=device, if_index=1, name="Gi0/1")
    b = Interface.objects.create(device=device, if_index=2, name="Gi0/2")
    return a, b


# ─── mẫu thiết bị (CPU/mem) ──────────────────────────────────────────────────────────────────────────────────

class TestSystemSeriesAreMergedByTime:
    def test_chart_shows_the_newer_db_sample_next_to_the_older_redis_one(self, cache_mode, monkeypatch):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(100)))               # Redis còn ghi được
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(10)))                # từ đây ghi lỗi → DB
        since, until = _window()

        data = json.loads(_device_metrics_raw(device, since, until).content)

        assert data["cpu_percent"] == [10.0, 99.0]

    def test_export_has_both_samples(self, cache_mode, monkeypatch, api_client):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, cpu=10.0, ts=_now(100)))
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, cpu=99.0, ts=_now(10)))

        rows = _export(api_client, device, "system")

        assert [r[1] for r in rows[1:]] == ["10.0", "99.0"]

    def test_status_timeline_counts_the_db_sample_as_data(self, cache_mode, monkeypatch, logged_in_client):
        """Mẫu Redis cuối cách 'bây giờ' quá grace → bản cũ báo Offline dù DB có mẫu poll thật vài giây trước."""
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, ts=_now(2000)))
        _redis_writes_fail(monkeypatch)
        save_metrics(device, _nd(device, ts=_now(5)))

        response = logged_in_client.get(f"/api/metrics/{device.pk}/status/", {"range": "1h"})

        assert response.status_code == 200, response.content
        assert response.json()["online"][-1] == 1

    def test_the_same_sample_held_by_both_sources_is_drawn_once(self, cache_mode):
        """Dòng bằng chứng (bản sao của mẫu Redis) trùng thời điểm → không nhân đôi điểm."""
        device = CiscoSNMPDeviceFactory()
        ts = _now(60)
        save_metrics(device, _nd(device, cpu=42.0, ts=ts))
        SystemHealth.objects.create(device=device, timestamp=ts, cpu_percent=42.0, mem_percent=21.0,
                                    extra={EVIDENCE_KEY: True})
        since, until = _window()

        data = json.loads(_device_metrics_raw(device, since, until).content)

        assert data["cpu_percent"] == [42.0]

    def test_a_sample_slightly_off_in_time_is_still_the_same_poll(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        ts = _now(60)
        save_metrics(device, _nd(device, cpu=42.0, ts=ts))
        SystemHealth.objects.create(device=device, timestamp=ts + timedelta(milliseconds=400), cpu_percent=42.0,
                                    mem_percent=21.0)
        since, until = _window()

        data = json.loads(_device_metrics_raw(device, since, until).content)

        assert len(data["cpu_percent"]) == 1

    def test_host_perf_columns_survive_the_merge(self, cache_mode, monkeypatch):
        """Mẫu DB mang theo cột host-perf (đổi sang dạng short-key của Redis rồi vẽ chung)."""
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, ts=_now(100)))
        SystemHealth.objects.create(device=device, timestamp=_now(10), cpu_percent=1.0, mem_percent=1.0,
                                    disk_read_latency_ms=55.0)
        since, until = _window()

        data = json.loads(_device_metrics_raw(device, since, until).content)

        assert data["disk_read_latency_ms"] == [None, 55.0]


# ─── interface: ghép THEO TỪNG CỔNG ──────────────────────────────────────────────────────────────────────────

class TestInterfaceSeriesAreMergedPerPort:
    def test_a_port_with_only_db_data_is_not_dropped_because_another_port_has_cache(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        a, b = _two_ports(device)
        _push_if(device, a, 100)                                                # cổng A có Redis
        _db_if(b, 100, in_mbps=7.0)                                             # cổng B chỉ có DB
        since, until = _window()

        data = json.loads(_interface_metrics_raw(Interface.objects.filter(device=device).order_by("name"),
                                                 since, until).content)
        by_port = {i["port"]: i for i in data["interfaces"]}

        assert by_port["Gi0/1"]["in_mbps"] == [1.0]
        assert by_port["Gi0/2"]["in_mbps"] == [7.0]

    def test_a_port_shows_its_redis_and_db_samples_together(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        a, _ = _two_ports(device)
        _push_if(device, a, 100, in_mbps=1.0)
        _db_if(a, 10, in_mbps=9.0)                                              # mẫu mới hơn chỉ ở DB
        since, until = _window()

        data = json.loads(_interface_metrics_raw(Interface.objects.filter(pk=a.pk), since, until).content)

        assert data["interfaces"][0]["in_mbps"] == [1.0, 9.0]
        assert data["interfaces"][0]["current_status"] == "up"

    def test_the_same_port_sample_in_both_sources_is_drawn_once(self, cache_mode):
        device = CiscoSNMPDeviceFactory()
        a, _ = _two_ports(device)
        ts = _now(60)
        metrics_cache.push_series(device.id, {"ts": ts.timestamp()},
                                  {a.id: {"ts": ts.timestamp(), "in_mbps": 3.0, "out_mbps": 0.0, "status": "up"}})
        InterfaceStats.objects.create(interface=a, timestamp=ts, status="up", in_mbps=3.0, out_mbps=0.0)
        since, until = _window()

        data = json.loads(_interface_metrics_raw(Interface.objects.filter(pk=a.pk), since, until).content)

        assert data["interfaces"][0]["in_mbps"] == [3.0]

    def test_export_merges_per_port_too(self, cache_mode, api_client):
        device = CiscoSNMPDeviceFactory()
        a, b = _two_ports(device)
        _push_if(device, a, 100)
        _db_if(b, 100, in_mbps=7.0)
        _db_if(a, 10, in_mbps=9.0)

        rows = _export(api_client, device, "interface")[1:]
        got = sorted((r[0].lstrip("﻿"), r[2]) for r in rows)

        assert got == [("Gi0/1", "1.0"), ("Gi0/1", "9.0"), ("Gi0/2", "7.0")]
