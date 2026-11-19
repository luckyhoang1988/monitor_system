"""Số cảnh báo active trên dashboard phải là số THẬT, không phải độ dài danh sách hiển thị (đã cắt ở 20).

Review ngoài 2026-11-19 (F07): `_dashboard_counts` lấy tối đa 20 bản ghi để hiển thị rồi dùng `len()` làm
`alert_count` — nên thẻ "Cảnh báo" và panel luôn dừng ở 20 dù có 25 alert đang active.
"""
import pytest
from django.urls import reverse

from apps.alerts.models import Alert, AlertRule
from apps.dashboard.views import _dashboard_counts
from tests.conftest import CiscoSNMPDeviceFactory

pytestmark = pytest.mark.django_db

DISPLAY_LIMIT = 20


def _active_alerts(device, total: int, *, resolved: int = 0) -> None:
    for i in range(total + resolved):
        rule = AlertRule.objects.create(
            name=f"count-rule-{i}", device_type="all", metric="cpu_percent",
            condition="gt", threshold=90.0, severity="WARNING", channels=[], enabled=True,
        )
        Alert.objects.create(
            device=device, rule=rule, severity="WARNING", message=f"m{i}", metric_value=95.0,
            is_active=i < total,
        )


def test_alert_count_is_not_capped_at_the_display_limit():
    device = CiscoSNMPDeviceFactory()
    _active_alerts(device, DISPLAY_LIMIT + 5)

    counts = _dashboard_counts([device])

    assert counts["alert_count"] == DISPLAY_LIMIT + 5
    # Danh sách hiển thị vẫn giữ nguyên giới hạn 20 (panel không được dài vô hạn).
    assert len(counts["active_alerts"]) == DISPLAY_LIMIT


def test_alert_count_ignores_resolved_alerts():
    device = CiscoSNMPDeviceFactory()
    _active_alerts(device, 3, resolved=4)

    counts = _dashboard_counts([device])

    assert counts["alert_count"] == 3
    assert len(counts["active_alerts"]) == 3


def test_alerts_summary_json_reports_the_real_total(logged_in_client):
    device = CiscoSNMPDeviceFactory()
    _active_alerts(device, DISPLAY_LIMIT + 7)

    response = logged_in_client.get(reverse("dashboard:alerts_summary"))

    assert response.status_code == 200
    assert response.json()["alert_count"] == DISPLAY_LIMIT + 7
