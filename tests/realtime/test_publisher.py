import pytest
from django.utils import timezone

from apps.collectors.base import NormalizedData
from apps.realtime.publisher import build_payload
from tests.conftest import HuaweiACDeviceFactory


@pytest.mark.django_db
def test_build_payload_includes_ap_counts_for_wlan_controller():
    ac = HuaweiACDeviceFactory(name="ac-1")
    data = NormalizedData(
        device_name=ac.name,
        ip_address=ac.ip_address,
        timestamp=timezone.now(),
        os_family="huawei_vrp",
        cpu_percent=1.0,
        mem_percent=2.0,
        interfaces=[],
        extra={
            "wifi_aps": [
                {"name": "AP1", "is_online": True},
                {"name": "AP2", "is_online": False},
            ]
        },
    )
    payload = build_payload(ac, online=True, data=data)
    assert payload["ap_total"] == 2
    assert payload["ap_online"] == 1
    assert payload["ap_offline"] == 1

