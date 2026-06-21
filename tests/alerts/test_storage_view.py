"""Tests cho trang theo dõi dung lượng (alerts:storage)."""
import pytest
from django.urls import reverse
from apps.alerts.views import _fmt_bytes, _db_size_bytes


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
