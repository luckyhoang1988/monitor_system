import pytest
from datetime import timedelta
from django.utils import timezone

from tests.conftest import CiscoSNMPDeviceFactory


@pytest.mark.django_db
def test_is_online_uses_minimum_grace_window():
    device = CiscoSNMPDeviceFactory(collect_interval=3)
    device.last_seen = timezone.now() - timedelta(seconds=120)
    device.save(update_fields=["last_seen"])

    assert device.is_online is True
