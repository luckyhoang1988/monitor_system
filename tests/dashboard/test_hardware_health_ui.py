"""Regression coverage for iLO visibility and independently refreshed host alerts."""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alerts.models import Alert, AlertRule
from apps.dashboard.hardware import hardware_summary, latest_hardware
from apps.metrics.models import HardwareHealth
from tests.conftest import HyperVDeviceFactory


@pytest.mark.django_db
@pytest.mark.parametrize("metric,value,label", [
    ("raid_controller_health", 2, "Critical"),
    ("ilo_battery_health", 1, "Warning"),
    ("ilo_power_supply_health", 0, "OK"),
    ("ilo_power_redundancy", 0, "DEGRADED"),
    ("ilo_fan_redundancy", 1, "OK"),
    ("raid_missing_disk_count", 2, "2"),
    ("cpu_percent", 85, "85.0%"),
])
def test_alert_value_semantics_on_web(logged_in_client, metric, value, label):
    host = HyperVDeviceFactory()
    rule = AlertRule.objects.create(name="Hardware rule", metric=metric, condition="eq",
                                    threshold=value, severity="CRITICAL")
    alert = Alert.objects.create(device=host, rule=rule, severity="CRITICAL", metric_value=value)
    assert alert.metric_value_label == label
    for url in ("dashboard:index", "alerts:list"):
        html = logged_in_client.get(reverse(url)).content.decode()
        assert label in html
    alert.is_active = False
    alert.save(update_fields=["is_active"])
    assert label in logged_in_client.get(reverse("alerts:list")).content.decode()


@pytest.mark.django_db
def test_dashboard_latest_hardware_when_host_online(logged_in_client, django_assert_num_queries):
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", last_seen=timezone.now())
    ts = timezone.now()
    HardwareHealth.objects.create(device=host, timestamp=ts - timedelta(seconds=300), battery_health_code=0)
    HardwareHealth.objects.create(device=host, timestamp=ts, battery_health_code=2)
    other = HyperVDeviceFactory(ilo_ip_address="10.1.1.2")
    with django_assert_num_queries(2):  # lịch sử + bằng chứng cũ cho field còn None; không N+1
        snapshots = latest_hardware([host, other])
    assert snapshots[host.pk].battery_health_code == 2
    html = logged_in_client.get(reverse("dashboard:index")).content.decode()
    assert "iLO: Critical" in html
    assert "Battery: Critical" in html
    assert ">On</span>" in html
    data = logged_in_client.get(reverse("dashboard:alerts_summary")).json()
    assert "iLO: Critical" in data["hardware_html"][str(host.pk)]
    assert "Chưa có dữ liệu" in data["hardware_html"][str(other.pk)]


@pytest.mark.django_db
def test_host_refreshes_hardware_and_active_host_alerts(logged_in_client):
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
    other = HyperVDeviceFactory()
    rule = AlertRule.objects.create(name="Power lost", metric="ilo_power_redundancy", condition="eq",
                                    threshold=0, severity="CRITICAL")
    own = Alert.objects.create(device=host, rule=rule, severity="CRITICAL", metric_value=0)
    Alert.objects.create(device=other, rule=rule, severity="CRITICAL", metric_value=0)
    url = reverse("dashboard:hyperv_health", args=[host.pk])
    assert "Chưa có dữ liệu" in logged_in_client.get(url).json()["html"]
    HardwareHealth.objects.create(device=host, timestamp=timezone.now(), power_redundancy_ok=False)
    html = logged_in_client.get(url).json()["html"]
    assert "iLO: Critical" in html
    assert "DEGRADED" in html
    assert "Power lost" in html
    assert other.name not in html
    own.is_active = False
    own.save(update_fields=["is_active"])
    HardwareHealth.objects.create(device=host, timestamp=timezone.now(), power_redundancy_ok=True)
    html = logged_in_client.get(url).json()["html"]
    assert "iLO: OK" in html
    assert "Power lost" not in html
    assert "Không có alert active" in html
    detail = logged_in_client.get(reverse("dashboard:hyperv_detail", args=[host.pk])).content.decode()
    assert detail.index('id="hostHealthPanel"') < detail.index('id="hostChart"')
    assert "setInterval(loadHostHealth, 25000)" in detail


@pytest.mark.django_db
def test_unknown_and_stale_hardware(settings):
    settings.POLL_ILO_INTERVAL_SECS = 300
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
    unknown = HardwareHealth(device=host, timestamp=timezone.now())
    assert hardware_summary(host, unknown)["color"] == "secondary"
    old = HardwareHealth(device=host, timestamp=timezone.now() - timedelta(seconds=601),
                         controller_health_code=0)
    summary = hardware_summary(host, old)
    assert summary["stale"] and summary["color"] == "secondary"
    old.battery_health_code = 2
    assert hardware_summary(host, old)["color"] == "danger"


@pytest.mark.django_db
@pytest.mark.parametrize("fields,label", [
    ({"controller_health_code": 2}, "Critical"),
    ({"power_supply_worst_code": 2}, "Critical"),
    ({"battery_health_code": 1}, "Warning"),
    ({"power_redundancy_ok": False}, "Critical"),
    ({"fan_redundancy_ok": False}, "Critical"),
    ({"missing_disk_count": 1}, "Critical"),
    ({"enclosure_mismatch_count": 1}, "Critical"),
    ({"controller_health_code": 0}, "OK"),
])
def test_hardware_summary_components(fields, label):
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
    health = HardwareHealth(device=host, timestamp=timezone.now(), **fields)
    assert hardware_summary(host, health)["label"] == label


@pytest.mark.django_db
def test_health_endpoint_requires_login(client):
    host = HyperVDeviceFactory()
    url = reverse("dashboard:hyperv_health", args=[host.pk])
    assert client.get(url).status_code == 302


@pytest.mark.django_db
def test_health_endpoint_host_type(logged_in_client):
    host = HyperVDeviceFactory()
    url = reverse("dashboard:hyperv_health", args=[host.pk])
    host.device_type = "switch"
    host.save(update_fields=["device_type"])
    assert logged_in_client.get(url).status_code == 404


@pytest.mark.django_db
def test_poll_error_without_data_shows_disconnected(logged_in_client):
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_last_error="401 Unauthorized")
    summary = hardware_summary(host, None)
    assert summary["label"] == "Mất kết nối" and summary["color"] == "warning"
    html = logged_in_client.get(reverse("dashboard:index")).content.decode()
    assert "iLO: Mất kết nối" in html
    assert "401 Unauthorized" in html


@pytest.mark.django_db
def test_poll_error_with_ok_data_not_reported_ok():
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_last_error="timeout")
    health = HardwareHealth(device=host, timestamp=timezone.now(), controller_health_code=0)
    assert hardware_summary(host, health)["label"] == "Mất kết nối"


@pytest.mark.django_db
def test_poll_error_keeps_real_critical():
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_last_error="timeout")
    health = HardwareHealth(device=host, timestamp=timezone.now(), battery_health_code=2)
    summary = hardware_summary(host, health)
    assert summary["label"] == "Critical" and summary["error"] == "timeout"


@pytest.mark.django_db
def test_ilo_problem_shown_in_notice_block_not_counted_offline(logged_in_client):
    bad = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", last_seen=timezone.now())
    good = HyperVDeviceFactory(ilo_ip_address="10.1.1.2", last_seen=timezone.now())
    HardwareHealth.objects.create(device=bad, timestamp=timezone.now(), battery_health_code=2)
    HardwareHealth.objects.create(device=good, timestamp=timezone.now(), controller_health_code=0)
    data = logged_in_client.get(reverse("dashboard:alerts_summary")).json()
    html = data["offline_notice_html"]
    assert "Cảnh báo phần cứng (iLO)" in html
    assert bad.name in html and "Battery: Critical" in html
    assert good.name not in html
    assert data["offline_count"] == 0
    assert "Cảnh báo phần cứng (iLO)" in logged_in_client.get(reverse("dashboard:index")).content.decode()


@pytest.mark.django_db
def test_partial_poll_does_not_mask_earlier_critical(django_assert_num_queries):
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
    now = timezone.now()
    HardwareHealth.objects.create(device=host, timestamp=now - timedelta(seconds=300),
                                  controller_health_code=0, power_supply_worst_code=2)
    # Poll một phần: RAID đọc được, nhóm Power không đọc được (None).
    HardwareHealth.objects.create(device=host, timestamp=now, controller_health_code=0)
    with django_assert_num_queries(2):
        snapshots = latest_hardware([host])
    summary = hardware_summary(host, snapshots[host.pk])
    assert summary["label"] == "Critical"
    assert "Power Supplies: Critical" in summary["problems"][0]
    assert summary["carried"]  # có mục lấy từ snapshot cũ, kèm thời điểm


@pytest.mark.django_db
def test_partial_poll_recovered_group_reads_again_is_ok():
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
    now = timezone.now()
    HardwareHealth.objects.create(device=host, timestamp=now - timedelta(seconds=300),
                                  controller_health_code=0, power_supply_worst_code=2)
    HardwareHealth.objects.create(device=host, timestamp=now, controller_health_code=0,
                                  power_supply_worst_code=0)
    summary = hardware_summary(host, latest_hardware([host])[host.pk])
    assert summary["label"] == "OK" and not summary["carried"]


@pytest.mark.django_db
def test_never_available_field_not_flagged_as_carried():
    # iLO4: bios/network luôn None — không phải "thiếu dữ liệu".
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
    now = timezone.now()
    for delta in (300, 0):
        HardwareHealth.objects.create(device=host, timestamp=now - timedelta(seconds=delta),
                                      controller_health_code=0)
    summary = hardware_summary(host, latest_hardware([host])[host.pk])
    assert summary["label"] == "OK" and not summary["carried"]


@pytest.mark.django_db
def test_notice_card_shows_stale_and_timestamp(logged_in_client, settings):
    settings.POLL_ILO_INTERVAL_SECS = 300
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", last_seen=timezone.now())
    HardwareHealth.objects.create(device=host, timestamp=timezone.now() - timedelta(seconds=900),
                                  battery_health_code=2)
    html = logged_in_client.get(reverse("dashboard:alerts_summary")).json()["offline_notice_html"]
    assert "Cảnh báo phần cứng (iLO)" in html
    assert "Dữ liệu cũ" in html
    assert "Cập nhật" in html


@pytest.mark.django_db
def test_critical_older_than_window_becomes_undetermined_not_ok():
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
    now = timezone.now()
    HardwareHealth.objects.create(device=host, timestamp=now - timedelta(hours=25),
                                  controller_health_code=0, power_supply_worst_code=2)
    HardwareHealth.objects.create(device=host, timestamp=now, controller_health_code=0)
    summary = hardware_summary(host, latest_hardware([host])[host.pk])
    assert summary["label"] == "Chưa xác định" and summary["color"] == "warning"
    assert "Power Supplies" in summary["expired"]


@pytest.mark.django_db
def test_detail_page_and_health_endpoint_match_dashboard(logged_in_client):
    host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", last_seen=timezone.now())
    now = timezone.now()
    HardwareHealth.objects.create(device=host, timestamp=now - timedelta(seconds=300),
                                  controller_health_code=0, power_supply_worst_code=2)
    HardwareHealth.objects.create(device=host, timestamp=now, controller_health_code=0, raw={})
    dash = logged_in_client.get(reverse("dashboard:index")).content.decode()
    assert "iLO: Critical" in dash
    detail = logged_in_client.get(reverse("dashboard:hyperv_detail", args=[host.pk])).content.decode()
    assert "iLO: Critical" in detail and "iLO: OK" not in detail
    assert "đang dùng dữ liệu lúc" in detail
    live = logged_in_client.get(reverse("dashboard:hyperv_health", args=[host.pk])).json()["html"]
    assert "iLO: Critical" in live and "iLO: OK" not in live
