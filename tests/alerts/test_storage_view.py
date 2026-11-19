"""Tests cho trang theo dõi dung lượng (alerts:storage)."""
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from django.urls import reverse
from apps.alerts.views import _fmt_bytes, _db_size_bytes
from apps.metrics.models import SystemHealth
from tests.conftest import CiscoSNMPDeviceFactory


class TestFmtBytes:
    def test_bytes(self):
        assert _fmt_bytes(512) == "512 B"

    def test_mb(self):
        assert _fmt_bytes(23 * 1024 * 1024) == "23.0 MB"

    def test_gb(self):
        assert _fmt_bytes(55 * 1024**3).endswith("GB")

    def test_zero_and_none(self):
        assert _fmt_bytes(0) == "0 B"
        assert _fmt_bytes(None) == "0 B"


@pytest.mark.django_db
class TestStorageView:
    def test_db_size_positive(self):
        # SQLite test DB tồn tại → size >= 0 (không lỗi)
        assert _db_size_bytes() >= 0

    def test_page_renders(self, logged_in_client):
        resp = logged_in_client.get(reverse("alerts:storage"))
        assert resp.status_code == 200
        assert "Dung lượng lưu trữ" in resp.content.decode()
        assert "Database" in resp.content.decode()

    def test_requires_login(self, client, db):
        resp = client.get(reverse("alerts:storage"))
        assert resp.status_code in (302, 301)  # redirect tới login


@pytest.mark.django_db
class TestPurgeMetrics:
    def _health(self, device, dt):
        return SystemHealth.objects.create(device=device, timestamp=dt,
                                           cpu_percent=1, mem_percent=1)

    def test_purge_deletes_only_in_range(self, logged_in_client):
        device = CiscoSNMPDeviceFactory()
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        in_range  = datetime(2026, 1, 15, 12, 0, tzinfo=tz)
        out_range = datetime(2026, 2, 15, 12, 0, tzinfo=tz)
        self._health(device, in_range)
        self._health(device, out_range)

        resp = logged_in_client.post(reverse("alerts:storage"),
                                     {"from_date": "2026-01-01", "to_date": "2026-01-31"})
        assert resp.status_code in (302, 301)
        assert SystemHealth.objects.count() == 1  # ngoài khoảng giữ lại
        assert SystemHealth.objects.first().timestamp == out_range

    def test_purge_covers_every_table_the_scheduled_cleanup_covers(self, logged_in_client):
        """Review ngoài 2026-11-19 (F05): `_purge_metrics` bỏ sót HardwareHealth (iLO) và VolumeStats
        trong khi `cleanup_old_metrics` có cả hai — xoá tay theo khoảng ngày để lại dữ liệu cũ."""
        from apps.metrics.models import HardwareHealth, VolumeStats

        device = CiscoSNMPDeviceFactory()
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        in_range = datetime(2026, 1, 15, 12, 0, tzinfo=tz)
        out_range = datetime(2026, 2, 15, 12, 0, tzinfo=tz)
        for dt in (in_range, out_range):
            HardwareHealth.objects.create(device=device, timestamp=dt)
            VolumeStats.objects.create(device=device, timestamp=dt, volume_name="d:")

        resp = logged_in_client.post(reverse("alerts:storage"),
                                     {"from_date": "2026-01-01", "to_date": "2026-01-31"})

        assert resp.status_code in (302, 301)
        assert [h.timestamp for h in HardwareHealth.objects.all()] == [out_range]
        assert [v.timestamp for v in VolumeStats.objects.all()] == [out_range]

    def test_purge_table_list_matches_scheduled_cleanup(self):
        """Khoá drift: 2 danh sách bảng (xoá tay vs task định kỳ) phải bao phủ cùng các model."""
        import inspect
        import re

        from apps.alerts import views as alert_views
        from apps.metrics import tasks as metric_tasks

        def models_used(src: str) -> set[str]:
            return set(re.findall(r"\b([A-Z]\w+(?:Stats|Health|Hourly|Daily))\b", src))

        purge = models_used(inspect.getsource(alert_views._purge_metrics))
        cleanup = models_used(inspect.getsource(metric_tasks.cleanup_old_metrics))
        assert cleanup - purge == set(), f"_purge_metrics thiếu: {sorted(cleanup - purge)}"

    def test_purge_forbidden_for_readonly(self, readonly_client):
        resp = readonly_client.post(reverse("alerts:storage"),
                                    {"from_date": "2026-01-01", "to_date": "2026-01-31"})
        assert resp.status_code == 403

    def test_invalid_dates_redirect_without_error(self, logged_in_client):
        resp = logged_in_client.post(reverse("alerts:storage"),
                                     {"from_date": "", "to_date": ""})
        assert resp.status_code in (302, 301)
