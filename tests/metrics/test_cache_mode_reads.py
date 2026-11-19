"""Export + chart ở cache-mode: đọc Redis khi có, đọc DB khi Redis trống.

Review ngoài 2026-11-19:
* F04 — `api_export` luôn đọc bảng DB cho khoảng "raw"; ở cache-mode DB không còn raw (chỉ dòng bằng chứng) nên export
  1h/24h trả file gần như rỗng dù chart cùng khoảng đầy dữ liệu (Redis).
* F01 — chart/timeline raw chỉ đọc Redis nên Redis down/flush ⇒ 0 điểm dù writer đã ghi đường lui vào DB.
"""
import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from rest_framework.test import APIClient

from django.contrib.auth.models import User

from apps.collectors.base import InterfaceData, NormalizedData
from apps.devices.models import Interface
from apps.metrics import cache as metrics_cache
from apps.metrics.api import _device_metrics_raw, _interface_metrics_raw
from apps.metrics.models import InterfaceStats, SystemHealth
from apps.metrics.writer import save_metrics
from tests.conftest import CiscoSNMPDeviceFactory

pytestmark = pytest.mark.django_db


def _now(offset_secs: float = 0.0) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(seconds=offset_secs)


def _nd(device, cpu=42.0, mem=21.0, ifaces=None, ts=None) -> NormalizedData:
    return NormalizedData(
        device_name=device.name, ip_address=device.ip_address, timestamp=ts or _now(),
        os_family="cisco_ios", cpu_percent=cpu, mem_percent=mem, interfaces=ifaces or [], extra={},
    )


def _if(in_b=0, out_b=0, status="up") -> InterfaceData:
    return InterfaceData(name="Gi0/1", if_index=1, status=status, in_bytes=in_b, out_bytes=out_b, speed_mbps=1000)


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
def redis_down(settings, monkeypatch):
    settings.METRICS_WRITE_MODE = "cache"
    monkeypatch.setattr(metrics_cache, "_get_client", lambda: None)


@pytest.fixture
def api_client():
    client = APIClient()
    client.force_authenticate(user=User.objects.create_user(username="exporter", password="x"))
    return client


def _export(api_client, device, export_type, range_str="1h"):
    response = api_client.get("/api/metrics/export/", {
        "device_id": device.pk, "type": export_type, "range": range_str, "export_format": "csv",
    })
    assert response.status_code == 200
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def _window():
    now = _now()
    return now - timedelta(hours=1), now


# ─── F04 · export đọc Redis ở cache-mode ──────────────────────────────────────

class TestExportInCacheMode:
    def test_system_export_has_the_samples_the_chart_shows(self, cache_mode, api_client):
        device = CiscoSNMPDeviceFactory()
        for off in (240, 120, 0):
            save_metrics(device, _nd(device, cpu=42.0, mem=21.0, ts=_now(off)))
        assert SystemHealth.objects.filter(device=device).count() == 0   # DB không có raw (đúng cache-mode)

        rows = _export(api_client, device, "system")

        assert rows[0][:3] == ["Thời gian", "CPU (%)", "Memory (%)"]
        data = rows[1:]
        assert len(data) == 3
        assert all(r[1] == "42.0" and r[2] == "21.0" for r in data)
        assert [r[0] for r in data] == sorted(r[0] for r in data)        # tăng dần theo thời gian

    def test_system_export_matches_chart_sample_count(self, cache_mode, api_client):
        device = CiscoSNMPDeviceFactory()
        for off in (240, 120, 0):
            save_metrics(device, _nd(device, ts=_now(off)))
        since, until = _window()

        chart = json.loads(_device_metrics_raw(device, since, until).content)

        assert len(_export(api_client, device, "system")) - 1 == len(chart["cpu_percent"]) == 3

    def test_interface_export_has_the_samples_the_chart_shows(self, cache_mode, api_client):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, ifaces=[_if(in_b=1_000_000)], ts=_now(300)))
        save_metrics(device, _nd(device, ifaces=[_if(in_b=2_000_000)], ts=_now(0)))
        assert InterfaceStats.objects.filter(interface__device=device).count() == 0

        rows = _export(api_client, device, "interface")

        assert rows[0][:4] == ["Interface", "Thời gian", "In Mbps", "Out Mbps"]
        data = rows[1:]
        assert len(data) == 2 and all(r[0] == "Gi0/1" for r in data)
        assert float(data[-1][2]) > 0                                      # Mbps tính từ delta, không phải 0 rỗng
        assert not any("﻿" in cell for row in rows for cell in row)   # BOM chỉ ở đầu file (đã bị decode bỏ)

    def test_export_uses_db_when_redis_is_empty(self, redis_down, api_client):
        device = CiscoSNMPDeviceFactory()
        for off in (240, 120, 0):
            save_metrics(device, _nd(device, cpu=77.0, ts=_now(off)))     # Redis lỗi → writer ghi DB

        rows = _export(api_client, device, "system")

        assert len(rows) - 1 == 3 and all(r[1] == "77.0" for r in rows[1:])

    def test_interface_export_uses_db_when_redis_is_empty(self, redis_down, api_client):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, ifaces=[_if(in_b=1_000_000)], ts=_now(300)))
        save_metrics(device, _nd(device, ifaces=[_if(in_b=2_000_000)], ts=_now(0)))
        assert InterfaceStats.objects.filter(interface__device=device).count() == 2   # writer fallback

        rows = _export(api_client, device, "interface")

        assert len(rows) - 1 == 2 and all(r[0].lstrip("﻿") == "Gi0/1" for r in rows[1:])

    def test_hourly_export_is_unchanged_by_cache_mode(self, cache_mode, api_client):
        """Khoảng hourly/daily vẫn đọc bảng rollup — chỉ nhánh raw đổi."""
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device))

        rows = _export(api_client, device, "system", range_str="7d")

        assert rows[0][:3] == ["Thời gian", "CPU (%)", "Memory (%)"] and "CPU Max" in rows[0]
        assert len(rows) == 1                                              # chưa có rollup nào → chỉ header


# ─── F01 · chart/timeline đọc DB khi Redis trống ──────────────────────────────

class TestChartsFallBackToDbWhenRedisIsEmpty:
    def test_device_chart_reads_db_rows_written_by_the_writer_fallback(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        for off in (240, 120, 0):
            save_metrics(device, _nd(device, cpu=63.0, ts=_now(off)))
        since, until = _window()

        data = json.loads(_device_metrics_raw(device, since, until).content)

        assert data["cpu_percent"] == [63.0, 63.0, 63.0]

    def test_interface_chart_reads_db_rows_when_redis_is_down(self, redis_down):
        device = CiscoSNMPDeviceFactory()
        save_metrics(device, _nd(device, ifaces=[_if(in_b=1_000_000)], ts=_now(300)))
        save_metrics(device, _nd(device, ifaces=[_if(in_b=2_000_000)], ts=_now(0)))
        since, until = _window()

        iface = Interface.objects.filter(device=device)
        data = json.loads(_interface_metrics_raw(iface, since, until).content)

        assert len(data["interfaces"][0]["in_mbps"]) == 2

    def test_status_timeline_reads_db_when_redis_is_down(self, redis_down, logged_in_client):
        device = CiscoSNMPDeviceFactory()
        for off in (240, 120, 0):
            save_metrics(device, _nd(device, ts=_now(off)))

        response = logged_in_client.get(f"/api/metrics/{device.pk}/status/", {"range": "1h"})

        assert response.status_code == 200, response.content
        assert 1 in response.json()["online"]

    def test_db_sample_between_redis_samples_is_drawn_in_time_order(self, cache_mode):
        """Đổi hợp đồng (review ngoài 2026-11-19 lần 4, P2): bản cũ "Redis có dữ liệu thì chỉ vẽ Redis" làm mẫu DB mới hơn
        (writer rơi về ghi DB khi Redis ghi lỗi) bị che. Nay chart GHÉP Redis + DB theo thời điểm; dòng DB TRÙNG thời điểm
        với mẫu Redis (dòng bằng chứng) vẫn chỉ vẽ 1 lần — xem tests/metrics/test_cache_mode_merge_sources.py."""
        device = CiscoSNMPDeviceFactory()
        for off in (240, 120, 0):
            save_metrics(device, _nd(device, cpu=10.0, ts=_now(off)))
        SystemHealth.objects.create(device=device, timestamp=_now(60), cpu_percent=99.0, mem_percent=1.0)
        since, until = _window()

        data = json.loads(_device_metrics_raw(device, since, until).content)

        assert data["cpu_percent"] == [10.0, 10.0, 99.0, 10.0]
