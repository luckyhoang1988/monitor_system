import pytest
from datetime import timedelta
from django.contrib.auth.models import User
from django.utils import timezone

from apps.metrics.models import SystemHealth
from tests.conftest import HyperVDeviceFactory


@pytest.fixture
def logged_client(client, db):
    user = User.objects.create_user(username="metrics_user", password="pass123")
    client.login(username="metrics_user", password="pass123")
    return client


@pytest.mark.django_db
def test_status_timeline_returns_series(logged_client):
    device = HyperVDeviceFactory(collect_interval=300)
    now = timezone.now()
    SystemHealth.objects.create(
        device=device,
        timestamp=now - timedelta(minutes=40),
        cpu_percent=20.0,
        mem_percent=40.0,
    )
    SystemHealth.objects.create(
        device=device,
        timestamp=now - timedelta(minutes=5),
        cpu_percent=25.0,
        mem_percent=45.0,
    )

    response = logged_client.get(f"/api/metrics/{device.pk}/status/?range=1h")
    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "raw"
    assert len(payload["labels"]) == len(payload["online"])
    assert set(payload["online"]).issubset({0, 1})
    assert 1 in payload["online"]
    assert 0 in payload["online"]


@pytest.mark.django_db
def test_status_timeline_all_offline_when_no_samples(logged_client):
    device = HyperVDeviceFactory(collect_interval=300)
    response = logged_client.get(f"/api/metrics/{device.pk}/status/?range=1h")
    assert response.status_code == 200
    payload = response.json()
    assert payload["online"]
    assert all(v == 0 for v in payload["online"])
