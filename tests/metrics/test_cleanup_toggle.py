"""Tests cho công tắc auto-cleanup metrics (METRICS_AUTO_CLEANUP)."""
import pytest
from datetime import timedelta
from django.utils import timezone
from apps.metrics.models import SystemHealth
from apps.metrics.tasks import cleanup_old_metrics
from tests.conftest import CiscoSNMPDeviceFactory


@pytest.mark.django_db
class TestAutoCleanupToggle:
    def _old_health(self, device):
        old = timezone.now() - timedelta(days=200)  # quá retention 90d
        SystemHealth.objects.create(device=device, timestamp=old, cpu_percent=1, mem_percent=1)

    def test_disabled_keeps_data(self, settings, db):
        settings.METRICS_AUTO_CLEANUP = False
        device = CiscoSNMPDeviceFactory()
        self._old_health(device)
        cleanup_old_metrics()
        assert SystemHealth.objects.count() == 1  # KHÔNG xóa

    def test_enabled_deletes_old(self, settings, db):
        settings.METRICS_AUTO_CLEANUP = True
        device = CiscoSNMPDeviceFactory()
        self._old_health(device)
        cleanup_old_metrics()
        assert SystemHealth.objects.count() == 0  # xóa khi bật
