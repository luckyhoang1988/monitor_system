"""Phạm vi giám sát của rule `if_status` ("Switch Uplink Down") — hướng A (2026-10-15).

Gốc (đo prod 2026-10-15): `is_uplink` = MỌI cổng trunk/hybrid, rule = "bất kỳ cổng uplink nào không up" nên
11 alert "Switch Uplink Down" kẹt 53–115 ngày: trên 11 switch có 103 cổng gắn cờ, 67 không-up, 61 cổng chưa từng
có dòng `up` — cổng trunk không dùng, không phải sự cố. Một uplink đứt thật trên các switch đó cũng không tạo
thông báo nào (alert đã active).

Quy tắc mới (theo quyết định của người vận hành):
  * Phạm vi = cổng local của link switch↔switch (topology) + `Device.uplink_ports` thủ công. KHÔNG phải mọi
    cổng trunk — `port_mode` giữ riêng, cổng trunk không tự trở thành đối tượng cảnh báo.
  * Cổng đã xác lập được GIỮ khi link down hoặc topology chuyển stale (nếu không, uplink đứt biến mất khỏi
    phạm vi rồi bị coi là hồi phục). Link stale mà chưa từng được thấy còn mới KHÔNG tự đưa vào.
  * Chưa xác định được phạm vi → "chưa đủ dữ liệu": không fire, không resolve, WARNING mỗi lượt đánh giá;
    KHÔNG mặc định "tất cả uplink đều up".
  * Alert mà cổng gây ra nó bị loại khỏi phạm vi → đóng với `closed_reason="scope_changed"`, KHÔNG gửi
    RECOVERED; còn cổng thuộc phạm vi mới vẫn down → giữ active.
"""
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection

from apps.alerts import engine
from apps.alerts.engine import check_device_alerts
from apps.alerts.models import Alert, AlertNotification, AlertRule, MonitoredUplink
from apps.devices.models import Interface, TopologyLink
from apps.metrics.models import InterfaceStats
from tests.conftest import CiscoSNMPDeviceFactory

SEND_FIRE = "apps.alerts.channels.telegram.send_telegram_alert"
SEND_RECOVERY = "apps.alerts.channels.telegram.send_telegram_recovery"


@pytest.fixture(autouse=True)
def _never_reach_telegram(mocker):
    """Mọi test đi qua engine thật — không test nào được chạm mạng."""
    mocker.patch(SEND_FIRE)
    mocker.patch(SEND_RECOVERY)


def now():
    return datetime.now(tz=UTC)


def since():
    return now() - timedelta(minutes=10)


def evaluate(device):
    check_device_alerts(device, since())


@pytest.fixture
def rule(db):
    return AlertRule.objects.create(
        name="Switch Uplink Down (test)", device_type="switch", metric="if_status", condition="lt",
        threshold=1.0, severity="CRITICAL", channels=["telegram"], enabled=True,
    )


@pytest.fixture
def device(db):
    return CiscoSNMPDeviceFactory(last_seen=now())


@pytest.fixture
def parent(db):
    return CiscoSNMPDeviceFactory()


def iface(device, name, if_index, *, status=None, port_mode="", is_uplink=False):
    i = Interface.objects.create(
        device=device, if_index=if_index, name=name, port_mode=port_mode, is_uplink=is_uplink,
    )
    if status is not None:
        set_status(i, status)
    return i


def set_status(i, status):
    InterfaceStats.objects.create(interface=i, timestamp=now(), status=status, in_bytes=0, out_bytes=0)


def link(device, i, remote, *, stale=False):
    return TopologyLink.objects.create(
        local_device=device, local_port=i.name, local_interface=i, link_kind="switch",
        remote_device=remote, is_stale=stale, protocol="fdb", match_method="mac",
    )


def legacy_alert(device, rule, *, down_interface_ids=None):
    """Alert như đang kẹt trên prod: nổ theo ngữ nghĩa cũ, đã gửi fire thành công."""
    alert = Alert.objects.create(
        device=device, rule=rule, severity=rule.severity, message=f"{device.name}: Uplink Down",
        metric_value=0.0, is_active=True, down_interface_ids=down_interface_ids,
    )
    AlertNotification.objects.create(alert=alert, channel="telegram", kind="fire", status="sent")
    return alert


def recoveries(alert):
    return AlertNotification.objects.filter(alert=alert, kind="recovery")


@pytest.mark.django_db
class TestWhatIsMonitored:
    def test_trunk_port_is_not_monitored_just_because_it_is_trunk(self, device, rule):
        # port_mode trunk + is_uplink (vai trò cũ) nhưng KHÔNG có link topology/uplink_ports → không phải
        # đối tượng cảnh báo; thiết bị ở trạng thái "chưa xác định", không fire.
        iface(device, "Gi0/24", 24, status="down", port_mode="trunk", is_uplink=True)
        evaluate(device)
        assert not Alert.objects.filter(device=device).exists()
        assert not MonitoredUplink.objects.filter(device=device).exists()

    def test_port_on_fresh_switch_link_fires_when_down(self, device, parent, rule):
        up = iface(device, "Gi0/1", 1, status="down")
        link(device, up, parent)
        evaluate(device)
        alert = Alert.objects.get(device=device, rule=rule)
        assert alert.is_active
        assert alert.down_interface_ids == [up.pk]
        assert MonitoredUplink.objects.get(device=device).interface_id == up.pk

    def test_only_linked_port_counts_other_trunk_ports_do_not(self, device, parent, rule):
        linked = iface(device, "Gi0/1", 1, status="up")
        iface(device, "Gi0/2", 2, status="down", port_mode="trunk", is_uplink=True)
        iface(device, "Gi0/3", 3, status="down", port_mode="hybrid", is_uplink=True)
        link(device, linked, parent)
        evaluate(device)
        assert not Alert.objects.filter(device=device).exists()

    def test_manual_uplink_ports_define_scope(self, device, rule):
        device.uplink_ports = ["Gi0/5"]
        device.save()
        p = iface(device, "Gi0/5", 5, status="down")
        evaluate(device)
        alert = Alert.objects.get(device=device, rule=rule)
        assert alert.down_interface_ids == [p.pk]
        assert MonitoredUplink.objects.get(device=device).source == MonitoredUplink.SOURCE_MANUAL

    def test_dirty_manual_values_found_on_prod_are_parsed(self, device, rule):
        # Giá trị thật trên prod: list bị lưu thành 1 chuỗi, và '[]' dạng chuỗi.
        device.uplink_ports = ["['Gi0/7']", "[]"]
        device.save()
        p = iface(device, "Gi0/7", 7, status="down")
        evaluate(device)
        assert Alert.objects.get(device=device, rule=rule).down_interface_ids == [p.pk]

    def test_manual_name_matching_is_case_insensitive(self, device, rule):
        device.uplink_ports = ["gi0/8"]
        device.save()
        p = iface(device, "Gi0/8", 8, status="down")
        evaluate(device)
        assert Alert.objects.get(device=device, rule=rule).down_interface_ids == [p.pk]


@pytest.mark.django_db
class TestScopeIsRetained:
    def test_port_stays_monitored_after_its_link_goes_stale(self, device, parent, rule):
        p = iface(device, "Gi0/1", 1, status="up")
        tl = link(device, p, parent)
        evaluate(device)                                   # engine thấy link còn mới → xác lập
        assert MonitoredUplink.objects.filter(device=device, interface=p).exists()

        # Cáp đứt: LLDP/FDB không còn thấy → link stale (sau STALE_MISS_THRESHOLD lượt), cổng down.
        TopologyLink.objects.filter(pk=tl.pk).update(is_stale=True, miss_count=3)
        set_status(p, "down")
        evaluate(device)
        alert = Alert.objects.get(device=device, rule=rule)
        assert alert.is_active and alert.down_interface_ids == [p.pk]

    def test_stale_link_never_seen_fresh_is_not_added_to_scope(self, device, parent, rule):
        p = iface(device, "Gi0/1", 1, status="down")
        link(device, p, parent, stale=True)               # link cũ có sẵn trong DB, chưa từng được thấy mới
        evaluate(device)
        assert not MonitoredUplink.objects.filter(device=device).exists()
        assert not Alert.objects.filter(device=device).exists()

    def test_retained_port_keeps_active_alert_instead_of_declaring_recovery(self, device, parent, rule):
        # Điểm mù cần tránh: uplink đứt → link stale → rơi khỏi phạm vi → "all up" → RECOVERED giả.
        p = iface(device, "Gi0/1", 1, status="up")
        tl = link(device, p, parent)
        evaluate(device)
        TopologyLink.objects.filter(pk=tl.pk).update(is_stale=True, miss_count=3)
        set_status(p, "down")
        evaluate(device)
        evaluate(device)
        alert = Alert.objects.get(device=device, rule=rule)
        assert alert.is_active and alert.closed_reason == ""
        assert not recoveries(alert).exists()


@pytest.mark.django_db
class TestUndeterminedScope:
    def test_no_scope_means_no_fire_and_a_warning_every_round(self, device, rule, caplog):
        iface(device, "Gi0/24", 24, status="down", port_mode="trunk", is_uplink=True)
        with caplog.at_level(logging.WARNING, logger="apps.alerts.engine"):
            evaluate(device)
            evaluate(device)
        msgs = [r.getMessage() for r in caplog.records if "chưa xác định" in r.getMessage()]
        assert len(msgs) == 2
        assert device.name in msgs[0]
        assert not Alert.objects.filter(device=device).exists()

    def test_no_scope_does_not_resolve_active_alert_as_if_everything_were_up(self, device, rule):
        # Mã cũ: không có cổng nào "không up" → value 1.0 → _resolve_alert → gửi RECOVERED cho alert chưa
        # từng được chứng minh là đã hết. Mới: không xác định thì không đụng tới alert.
        iface(device, "Gi0/24", 24, status="up", port_mode="trunk", is_uplink=True)
        alert = legacy_alert(device, rule)
        evaluate(device)
        alert.refresh_from_db()
        assert alert.is_active and alert.closed_reason == ""
        assert not recoveries(alert).exists()

    def test_missing_status_only_neither_fires_nor_resolves(self, device, parent, rule):
        p = iface(device, "Gi0/1", 1)                       # chưa có dòng trạng thái nào
        link(device, p, parent)
        alert = legacy_alert(device, rule, down_interface_ids=[p.pk])
        evaluate(device)
        alert.refresh_from_db()
        assert alert.is_active and alert.closed_reason == ""
        assert Alert.objects.filter(device=device).count() == 1

    def test_partially_missing_status_names_the_port_in_a_warning(self, device, parent, rule, caplog):
        ok = iface(device, "Gi0/1", 1, status="up")
        gone = iface(device, "Gi0/2", 2)                    # đã ghim nhưng không còn trạng thái nào
        link(device, ok, parent)
        link(device, gone, CiscoSNMPDeviceFactory())
        with caplog.at_level(logging.WARNING, logger="apps.alerts.engine"):
            evaluate(device)
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "chưa có trạng thái" in text and "Gi0/2" in text
        assert not Alert.objects.filter(device=device).exists()

    def test_no_status_at_all_is_left_to_the_offline_alert(self, device, parent, rule, caplog):
        a = iface(device, "Gi0/1", 1)
        link(device, a, parent)
        with caplog.at_level(logging.WARNING, logger="apps.alerts.engine"):
            evaluate(device)
        assert "chưa có trạng thái" not in " ".join(r.getMessage() for r in caplog.records)

    def test_unmatched_manual_port_is_called_out(self, device, rule, caplog):
        # Tên không ứng với cổng nào (khác cả số cổng) — viết tắt có bằng chứng được khớp ở TestAbbreviatedPortNames.
        device.uplink_ports = ["XGE0/0/99"]
        device.save()
        iface(device, "XGigabitEthernet0/0/9", 9, status="down")
        with caplog.at_level(logging.WARNING, logger="apps.alerts.engine"):
            evaluate(device)
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "XGE0/0/99" in text
        assert not Alert.objects.filter(device=device).exists()


@pytest.mark.django_db
class TestDownBeatsMissing:
    def test_a_known_down_port_fires_even_if_another_monitored_port_has_no_status(self, device, parent, rule):
        no_data = iface(device, "Gi0/1", 1)                 # if_index thấp hơn → mã cũ gặp "thiếu" trước
        down = iface(device, "Gi0/2", 2, status="down")
        link(device, no_data, parent)
        link(device, down, CiscoSNMPDeviceFactory())
        evaluate(device)
        assert Alert.objects.get(device=device, rule=rule).down_interface_ids == [down.pk]


@pytest.mark.django_db
class TestScopeChangeClosure:
    def test_legacy_alert_whose_monitored_ports_are_all_up_is_closed_without_recovered(self, device, parent, rule):
        p = iface(device, "Gi0/1", 1, status="up")
        link(device, p, parent)
        iface(device, "Gi0/2", 2, status="down", port_mode="trunk", is_uplink=True)   # lý do alert cũ
        alert = legacy_alert(device, rule)

        evaluate(device)

        alert.refresh_from_db()
        assert alert.is_active is False
        assert alert.closed_reason == Alert.CLOSED_SCOPE_CHANGED
        assert alert.resolved_at is not None
        assert "phạm vi" in alert.message
        assert not recoveries(alert).exists()                         # không RECOVERED
        assert AlertNotification.objects.filter(alert=alert).count() == 1   # chỉ còn dòng fire cũ

    def test_legacy_alert_is_kept_and_adopts_the_down_port_when_a_monitored_port_is_down(self, device, parent, rule):
        p = iface(device, "Gi0/1", 1, status="down")
        link(device, p, parent)
        alert = legacy_alert(device, rule)

        evaluate(device)

        alert.refresh_from_db()
        assert alert.is_active and alert.closed_reason == ""
        assert alert.down_interface_ids == [p.pk]
        assert Alert.objects.filter(device=device).count() == 1       # không tạo alert thứ hai

        # Cổng hồi phục thật → RECOVERED bình thường (alert đã được nhận về phạm vi mới).
        set_status(p, "up")
        evaluate(device)
        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == ""
        assert recoveries(alert).count() == 1

    def test_alert_whose_cause_left_the_scope_is_closed_without_recovered(self, device, parent, rule):
        a = iface(device, "Gi0/1", 1, status="down")
        b = iface(device, "Gi0/2", 2, status="up")
        device.uplink_ports = ["Gi0/1"]
        device.save()
        evaluate(device)                                   # a được xác lập (manual) + alert nổ
        alert = Alert.objects.get(device=device, rule=rule)
        assert alert.down_interface_ids == [a.pk]
        AlertNotification.objects.filter(alert=alert).update(status="sent")

        # Người vận hành đổi uplink_ports: Gi0/1 không còn là uplink, Gi0/2 mới là.
        device.uplink_ports = ["Gi0/2"]
        device.save()
        evaluate(device)

        alert.refresh_from_db()
        assert not MonitoredUplink.objects.filter(device=device, interface=a).exists()
        assert MonitoredUplink.objects.filter(device=device, interface=b).exists()
        assert alert.is_active is False
        assert alert.closed_reason == Alert.CLOSED_SCOPE_CHANGED
        assert not recoveries(alert).exists()
        assert Alert.objects.filter(device=device, is_active=True).count() == 0

    def test_cause_that_left_the_scope_but_new_port_is_down_keeps_alert_active(self, device, parent, rule):
        a = iface(device, "Gi0/1", 1, status="up")
        b = iface(device, "Gi0/2", 2, status="down")
        link(device, b, parent)
        alert = legacy_alert(device, rule, down_interface_ids=[a.pk])   # nguyên nhân cũ không còn trong phạm vi
        evaluate(device)
        alert.refresh_from_db()
        assert alert.is_active and alert.closed_reason == ""
        assert sorted(alert.down_interface_ids) == sorted([a.pk, b.pk])

    def test_genuine_recovery_of_a_monitored_port_still_sends_recovered(self, device, parent, rule):
        p = iface(device, "Gi0/1", 1, status="down")
        link(device, p, parent)
        evaluate(device)
        alert = Alert.objects.get(device=device, rule=rule)
        AlertNotification.objects.filter(alert=alert, kind="fire").update(status="sent")

        set_status(p, "up")
        evaluate(device)

        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == ""
        assert recoveries(alert).count() == 1

    def test_second_port_dropping_while_alert_is_active_extends_the_cause(self, device, parent, rule):
        a = iface(device, "Gi0/1", 1, status="down")
        b = iface(device, "Gi0/2", 2, status="up")
        link(device, a, parent)
        link(device, b, CiscoSNMPDeviceFactory())
        evaluate(device)
        alert = Alert.objects.get(device=device, rule=rule)
        AlertNotification.objects.filter(alert=alert, kind="fire").update(status="sent")

        set_status(b, "down")
        evaluate(device)
        alert.refresh_from_db()
        assert sorted(alert.down_interface_ids) == sorted([a.pk, b.pk])

        set_status(a, "up")                                 # a hồi phục, b vẫn down → vẫn active
        evaluate(device)
        alert.refresh_from_db()
        assert alert.is_active

        set_status(b, "up")
        evaluate(device)
        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == ""
        assert recoveries(alert).count() == 1

    def test_many_devices_changing_scope_at_once_send_no_recovered_burst(self, rule, mocker):
        send_recovery = mocker.patch(SEND_RECOVERY)
        alerts = []
        for _ in range(5):
            dev = CiscoSNMPDeviceFactory(last_seen=now())
            p = iface(dev, "Gi0/1", 1, status="up")
            link(dev, p, CiscoSNMPDeviceFactory())
            iface(dev, "Gi0/2", 2, status="down", port_mode="trunk", is_uplink=True)
            alerts.append((dev, legacy_alert(dev, rule)))
        for dev, _ in alerts:
            evaluate(dev)
        assert send_recovery.call_count == 0
        assert AlertNotification.objects.filter(kind="recovery").count() == 0
        assert all(
            Alert.objects.get(pk=a.pk).closed_reason == Alert.CLOSED_SCOPE_CHANGED for _, a in alerts
        )

    def test_scope_closed_alert_is_not_given_a_late_recovery(self, device, parent, rule):
        # Alert đóng vì đổi phạm vi mà fire còn pending: sweep vẫn có thể gửi fire, nhưng KHÔNG được thêm RECOVERED.
        p = iface(device, "Gi0/1", 1, status="up")
        link(device, p, parent)
        alert = Alert.objects.create(
            device=device, rule=rule, severity="CRITICAL", message="x", metric_value=0.0, is_active=True,
        )
        fire = AlertNotification.objects.create(alert=alert, channel="telegram", kind="fire", status="pending")
        evaluate(device)
        alert.refresh_from_db()
        assert alert.closed_reason == Alert.CLOSED_SCOPE_CHANGED
        engine._queue_late_recovery_if_resolved(alert.pk, "telegram")
        assert not recoveries(alert).exists()
        assert AlertNotification.objects.filter(pk=fire.pk).exists()


@pytest.mark.django_db
class TestManualPinLifecycle:
    def test_removing_a_manual_port_unpins_it_but_a_topology_pin_is_kept(self, device, parent, rule):
        m = iface(device, "Gi0/1", 1, status="up")
        t = iface(device, "Gi0/2", 2, status="up")
        link(device, t, parent)
        device.uplink_ports = ["Gi0/1", "Gi0/2"]
        device.save()
        evaluate(device)
        assert set(MonitoredUplink.objects.filter(device=device).values_list("interface_id", flat=True)) == {m.pk, t.pk}

        device.uplink_ports = []
        device.save()
        evaluate(device)
        assert set(MonitoredUplink.objects.filter(device=device).values_list("interface_id", flat=True)) == {t.pk}

    def test_deleting_the_pin_is_how_an_operator_stops_monitoring_a_port(self, device, parent, rule):
        p = iface(device, "Gi0/1", 1, status="up")
        tl = link(device, p, parent)
        evaluate(device)
        MonitoredUplink.objects.filter(device=device).delete()
        TopologyLink.objects.filter(pk=tl.pk).update(is_stale=True)   # cáp đã gỡ hẳn → link stale
        set_status(p, "down")
        evaluate(device)
        assert not MonitoredUplink.objects.filter(device=device).exists()
        assert not Alert.objects.filter(device=device).exists()


@pytest.mark.django_db
class TestCacheMode:
    """Cùng ngữ nghĩa khi đọc trạng thái từ snapshot Redis (prod chạy METRICS_WRITE_MODE=cache)."""

    @pytest.fixture(autouse=True)
    def _cache_mode(self, mocker):
        mocker.patch("apps.alerts.engine._use_cache", return_value=True)

    def _snapshot(self, mocker, statuses):
        mocker.patch(
            "apps.alerts.engine._fresh_latest",
            return_value={"interfaces": {str(pk): {"status": s} for pk, s in statuses.items()}},
        )

    def test_monitored_port_down_fires(self, device, parent, rule, mocker):
        p = iface(device, "Gi0/1", 1)
        iface(device, "Gi0/2", 2, port_mode="trunk", is_uplink=True)
        link(device, p, parent)
        self._snapshot(mocker, {p.pk: "down"})
        evaluate(device)
        assert Alert.objects.get(device=device, rule=rule).down_interface_ids == [p.pk]

    def test_unmonitored_trunk_down_does_not_fire(self, device, parent, rule, mocker):
        p = iface(device, "Gi0/1", 1)
        t = iface(device, "Gi0/2", 2, port_mode="trunk", is_uplink=True)
        link(device, p, parent)
        self._snapshot(mocker, {p.pk: "up", t.pk: "down"})
        evaluate(device)
        assert not Alert.objects.filter(device=device).exists()

    def test_down_beats_missing(self, device, parent, rule, mocker):
        a = iface(device, "Gi0/1", 1)
        b = iface(device, "Gi0/2", 2)
        link(device, a, parent)
        link(device, b, CiscoSNMPDeviceFactory())
        self._snapshot(mocker, {b.pk: "down"})              # a không có trạng thái
        evaluate(device)
        assert Alert.objects.get(device=device, rule=rule).down_interface_ids == [b.pk]

    def test_sustained_rule_uses_only_monitored_ports(self, device, parent, mocker):
        rule = AlertRule.objects.create(
            name="Uplink sustained", device_type="switch", metric="if_status", condition="lt",
            threshold=1.0, severity="CRITICAL", channels=[], enabled=True, duration_min=5,
        )
        p = iface(device, "Gi0/1", 1)
        t = iface(device, "Gi0/2", 2, port_mode="trunk", is_uplink=True)
        link(device, p, parent)
        series = {
            p.pk: [{"ts": time.time(), "status": "up"}],
            t.pk: [{"ts": time.time(), "status": "down"}],
        }
        mocker.patch("apps.alerts.engine.metrics_cache.get_if_series",
                     side_effect=lambda iid, window: series.get(iid, []))
        mocker.patch("apps.alerts.engine.metrics_cache.epoch_to_dt",
                     side_effect=lambda ts: datetime.fromtimestamp(ts, tz=UTC))
        self._snapshot(mocker, {p.pk: "up", t.pk: "down"})
        evaluate(device)
        assert not Alert.objects.filter(device=device, rule=rule).exists()


@pytest.mark.django_db
class TestPresentation:
    def test_history_page_labels_scope_closed_alerts_and_not_recovered_ones(self, logged_in_client, rule):
        from django.urls import reverse

        scoped = CiscoSNMPDeviceFactory(last_seen=now())
        p = iface(scoped, "Gi0/1", 1, status="up")
        link(scoped, p, CiscoSNMPDeviceFactory())
        legacy_alert(scoped, rule)
        evaluate(scoped)                                   # → đóng scope_changed

        healthy = CiscoSNMPDeviceFactory(last_seen=now())
        q = iface(healthy, "Gi0/1", 1, status="down")
        link(healthy, q, CiscoSNMPDeviceFactory())
        evaluate(healthy)
        set_status(q, "up")
        evaluate(healthy)                                  # → hồi phục thật

        body = logged_in_client.get(reverse("alerts:list")).content.decode("utf-8")
        assert body.count("Đổi phạm vi") == 1
        assert "Rule bị tắt" not in body

    def test_admin_lists_monitored_uplinks_and_deleting_one_unpins_the_port(self, admin_client, device, parent, rule):
        from django.urls import reverse

        p = iface(device, "Gi0/1", 1, status="up")
        link(device, p, parent)
        evaluate(device)
        pin = MonitoredUplink.objects.get(device=device)

        resp = admin_client.get(reverse("admin:alerts_monitoreduplink_changelist"))
        assert resp.status_code == 200
        assert device.name in resp.content.decode("utf-8")

        resp = admin_client.post(
            reverse("admin:alerts_monitoreduplink_delete", args=[pin.pk]), {"post": "yes"},
        )
        assert resp.status_code == 302
        assert not MonitoredUplink.objects.filter(pk=pin.pk).exists()


@pytest.mark.django_db
class TestSustainedFireRecordsTheScopeWhenPortIsAlreadyBackUp:
    def test_cause_unknown_at_fire_time_is_the_whole_scope_so_recovery_is_genuine(self, device, parent, mocker):
        """duration_min>0: cửa sổ có mẫu down nên FIRE, nhưng lần đọc tức thời cổng đã up lại (danh sách down
        rỗng). Nguyên nhân phải ghi là cả phạm vi — nếu ghi rỗng thì lượt sau coi alert "đã rời phạm vi" và
        đóng scope_changed thay vì RECOVERED dù cổng hồi phục thật."""
        mocker.patch("apps.alerts.engine._use_cache", return_value=True)
        rule = AlertRule.objects.create(
            name="Uplink sustained 2", device_type="switch", metric="if_status", condition="lt",
            threshold=1.0, severity="CRITICAL", channels=["telegram"], enabled=True, duration_min=5,
        )
        p = iface(device, "Gi0/1", 1)
        link(device, p, parent)
        mocker.patch("apps.alerts.engine._fresh_latest",
                     return_value={"interfaces": {str(p.pk): {"status": "up"}}})
        mocker.patch("apps.alerts.engine.metrics_cache.get_if_series",
                     return_value=[{"ts": time.time() - 120, "status": "down"},
                                   {"ts": time.time(), "status": "up"}])
        mocker.patch("apps.alerts.engine.metrics_cache.epoch_to_dt",
                     side_effect=lambda ts: datetime.fromtimestamp(ts, tz=UTC))

        evaluate(device)
        alert = Alert.objects.get(device=device, rule=rule)
        assert alert.down_interface_ids == [p.pk]
        AlertNotification.objects.filter(alert=alert, kind="fire").update(status="sent")

        # Cửa sổ đã sạch, cổng up → hồi phục thật.
        mocker.patch("apps.alerts.engine.metrics_cache.get_if_series",
                     return_value=[{"ts": time.time(), "status": "up"}])
        evaluate(device)
        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == ""
        assert recoveries(alert).count() == 1


def _wait_until_a_backend_is_waiting_for_a_lock(give_up: threading.Event, deadline_secs: float = 10.0) -> bool:
    """True khi có backend Postgres (khác connection này) đang chờ khoá; False nếu `give_up` đã set hoặc hết hạn."""
    deadline = time.monotonic() + deadline_secs
    while time.monotonic() < deadline and not give_up.is_set():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND wait_event_type = 'Lock'"
            )
            if cursor.fetchone()[0]:
                return True
        time.sleep(0.02)
    return False


@pytest.mark.django_db(transaction=True)
class TestScopeClosureIsRaceSafe:
    def test_two_evaluations_closing_the_same_legacy_alert_close_it_once(self, mocker):
        """Poll inline và `evaluate_alert_rules` có thể chạy đồng thời trên 2 worker. Cả hai thấy alert cũ +
        mọi cổng giám sát đều up → đều đóng nó. Không có khoá Device thì cả hai cùng ghi và ghi chú
        "đã đóng vì thay đổi phạm vi" bị nối 2 lần; có khoá thì bên sau thấy alert đã đóng và bỏ qua."""
        dev = CiscoSNMPDeviceFactory(last_seen=now())
        rule = AlertRule.objects.create(
            name="Uplink race", device_type="switch", metric="if_status", condition="lt",
            threshold=1.0, severity="CRITICAL", channels=["telegram"], enabled=True,
        )
        p = iface(dev, "Gi0/1", 1, status="up")
        link(dev, p, CiscoSNMPDeviceFactory())
        alert = legacy_alert(dev, rule)

        in_close = threading.Event()
        release = threading.Event()
        calls = []
        original = engine._close_alert_scope_changed

        def slow_close(a, *args, **kwargs):
            calls.append(a.pk)
            if len(calls) == 1:
                in_close.set()
                release.wait(timeout=10)
            return original(a, *args, **kwargs)

        mocker.patch.object(engine, "_close_alert_scope_changed", side_effect=slow_close)

        def run():
            try:
                evaluate(dev)
            finally:
                connection.close()

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        assert in_close.wait(timeout=10)
        t2.start()
        t2_done = threading.Event()

        def watch():
            t2.join()
            t2_done.set()

        threading.Thread(target=watch, daemon=True).start()
        blocked = _wait_until_a_backend_is_waiting_for_a_lock(give_up=t2_done)
        release.set()
        t1.join(timeout=15)
        t2.join(timeout=15)

        alert.refresh_from_db()
        assert alert.closed_reason == Alert.CLOSED_SCOPE_CHANGED
        assert alert.message.count("phạm vi giám sát") == 1
        assert blocked, "bên đóng thứ hai phải bị chặn ở khoá trong lúc bên đầu chưa commit"
        assert len(calls) == 1

    def test_a_fire_racing_with_the_closure_still_ends_with_an_active_alert(self, mocker):
        """Lượt A thấy mọi cổng up → đóng alert cũ (scope_changed). Lượt B (worker khác, snapshot mới hơn)
        thấy 1 cổng down → `_fire_alert`. Nếu việc đóng không đi qua khoá Device thì B đọc thấy alert cũ "còn
        active" (chưa commit đóng) và bỏ qua; xong việc đóng, thiết bị có cổng down mà KHÔNG có alert nào. Có
        khoá Device thì B chờ, thấy alert cũ đã đóng và tạo alert mới."""
        dev = CiscoSNMPDeviceFactory(last_seen=now())
        rule = AlertRule.objects.create(
            name="Uplink race fire", device_type="switch", metric="if_status", condition="lt",
            threshold=1.0, severity="CRITICAL", channels=[], enabled=True,
        )
        p = iface(dev, "Gi0/1", 1, status="up")
        link(dev, p, CiscoSNMPDeviceFactory())
        old = legacy_alert(dev, rule)

        in_close = threading.Event()
        release = threading.Event()
        original = engine._close_alert_scope_changed

        def slow_close(a, *args, **kwargs):
            in_close.set()
            release.wait(timeout=10)
            return original(a, *args, **kwargs)

        mocker.patch.object(engine, "_close_alert_scope_changed", side_effect=slow_close)

        errors: list[BaseException] = []

        def closer():
            try:
                engine._close_alerts_out_of_scope(dev, rule, [p.pk])
            except BaseException as exc:   # noqa: BLE001 — để test thấy lỗi thật thay vì "0 alert"
                errors.append(exc)
            finally:
                connection.close()

        def firer():
            try:
                engine._fire_alert(dev, rule, 0.0, down_interface_ids=[p.pk])
            except BaseException as exc:   # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        t1 = threading.Thread(target=closer)
        t2 = threading.Thread(target=firer)
        t1.start()
        assert in_close.wait(timeout=10)
        t2.start()
        t2_done = threading.Event()

        def watch():
            t2.join()
            t2_done.set()

        threading.Thread(target=watch, daemon=True).start()
        blocked = _wait_until_a_backend_is_waiting_for_a_lock(give_up=t2_done)
        release.set()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert not errors, errors
        old.refresh_from_db()
        assert old.closed_reason == Alert.CLOSED_SCOPE_CHANGED
        assert blocked, "việc tạo alert phải chờ việc đóng (cùng khoá Device)"
        active = list(Alert.objects.filter(device=dev, rule=rule, is_active=True))
        assert len(active) == 1 and active[0].pk != old.pk


@pytest.mark.django_db
class TestSettleOnlyTouchesWhatItDecidedOn:
    """Review ngoài sau bản đầu của A (2026-10-15): `_settle_if_status_alerts` đóng alert ngoài phạm vi ở một
    transaction, rồi `_resolve_alert` ở transaction khác lấy MỌI alert đang active của (thiết bị, rule). Worker
    khác tạo alert DOWN mới giữa hai bước (hoặc giữa lúc quyết định và lúc xử lý) thì alert đó bị resolve nhầm
    — dù cổng của nó đang down, và dù quyết định "mọi cổng đều up" được đưa ra từ trạng thái cũ hơn nó."""

    def test_alert_created_by_another_worker_between_close_and_resolve_stays_active(
        self, device, parent, rule, mocker,
    ):
        p = iface(device, "Gi0/1", 1, status="up")
        link(device, p, parent)
        old = legacy_alert(device, rule)          # nổ theo ngữ nghĩa cũ → sẽ bị đóng scope_changed
        original = engine._close_alerts_out_of_scope

        def close_then_another_worker_fires(*args, **kwargs):
            closed = original(*args, **kwargs)
            engine._fire_alert(device, rule, 0.0, down_interface_ids=[p.pk])
            return closed

        mocker.patch.object(engine, "_close_alerts_out_of_scope", side_effect=close_then_another_worker_fires)
        evaluate(device)

        old.refresh_from_db()
        assert old.closed_reason == Alert.CLOSED_SCOPE_CHANGED
        fresh = Alert.objects.get(device=device, rule=rule, is_active=True)
        assert fresh.pk != old.pk
        assert fresh.down_interface_ids == [p.pk]
        assert not recoveries(fresh).exists()

    def test_alert_created_after_the_decision_is_not_resolved_by_it(self, device, parent, rule, mocker):
        """Giữa lúc quyết định (alert A cũ, mọi cổng up) và lúc xử lý: worker khác resolve A rồi tạo alert B
        (cổng down). Quyết định "hồi phục" chỉ có giá trị với A, không phải B."""
        p = iface(device, "Gi0/1", 1, status="up")
        link(device, p, parent)
        decided_on = legacy_alert(device, rule, down_interface_ids=[p.pk])
        original = engine._settle_if_status_alerts

        def another_worker_swaps_the_alert_then_we_settle(*args, **kwargs):
            Alert.objects.filter(pk=decided_on.pk).update(is_active=False, resolved_at=now())
            engine._fire_alert(device, rule, 0.0, down_interface_ids=[p.pk])
            return original(*args, **kwargs)

        mocker.patch.object(engine, "_settle_if_status_alerts",
                            side_effect=another_worker_swaps_the_alert_then_we_settle)
        evaluate(device)

        fresh = Alert.objects.get(device=device, rule=rule, is_active=True)
        assert fresh.pk != decided_on.pk
        assert not recoveries(fresh).exists()

    def test_cause_added_after_the_decision_keeps_the_alert_active(self, device, parent, rule, mocker):
        """Quyết định "hồi phục" dựa trên nguyên nhân p1 đã up. Trong lúc đó worker có dữ liệu mới hơn thêm p2
        (đang down) vào nguyên nhân — resolve lúc này là báo hồi phục khi p2 còn down."""
        p1 = iface(device, "Gi0/1", 1, status="up")
        p2 = iface(device, "Gi0/2", 2, status="up")
        link(device, p1, parent)
        link(device, p2, parent)
        alert = legacy_alert(device, rule, down_interface_ids=[p1.pk])
        original = engine._settle_if_status_alerts

        def another_worker_adds_a_cause_then_we_settle(*args, **kwargs):
            engine._extend_if_status_causes(device, rule, [p2.pk])
            return original(*args, **kwargs)

        mocker.patch.object(engine, "_settle_if_status_alerts",
                            side_effect=another_worker_adds_a_cause_then_we_settle)
        evaluate(device)

        alert.refresh_from_db()
        assert alert.is_active is True
        assert alert.down_interface_ids == sorted([p1.pk, p2.pk])
        assert not recoveries(alert).exists()

    def test_alert_for_a_port_outside_the_scope_we_decided_on_is_not_closed_by_us(
        self, device, parent, rule, mocker,
    ):
        """Worker có phạm vi MỚI HƠN (vừa ghim thêm cổng p2) tạo alert cho p2. Phạm vi mà ta quyết định không
        có p2, nên nếu việc đóng "ngoài phạm vi" không giới hạn theo alert đã thấy, ta đóng nhầm alert đó."""
        p1 = iface(device, "Gi0/1", 1, status="up")
        p2 = iface(device, "Gi0/2", 2, status="down")
        link(device, p1, parent)
        decided_on = legacy_alert(device, rule, down_interface_ids=[p1.pk])
        original = engine._settle_if_status_alerts

        def another_worker_with_a_newer_scope_fires_then_we_settle(*args, **kwargs):
            Alert.objects.filter(pk=decided_on.pk).update(is_active=False, resolved_at=now())
            engine._fire_alert(device, rule, 0.0, down_interface_ids=[p2.pk])
            return original(*args, **kwargs)

        mocker.patch.object(engine, "_settle_if_status_alerts",
                            side_effect=another_worker_with_a_newer_scope_fires_then_we_settle)
        evaluate(device)

        fresh = Alert.objects.get(device=device, rule=rule, is_active=True)
        assert fresh.down_interface_ids == [p2.pk]
        assert fresh.closed_reason == ""

    def test_alert_created_while_we_were_reading_port_state_is_not_ours_to_resolve(
        self, device, parent, rule, mocker,
    ):
        """Ảnh chụp alert phải đọc TRƯỚC trạng thái cổng: alert xuất hiện trong lúc ta đọc trạng thái dựa trên
        dữ liệu mới hơn thứ ta đang cầm, nên "mọi cổng đều up" (từ dữ liệu cũ) không áp cho nó."""
        p = iface(device, "Gi0/1", 1, status="up")
        link(device, p, parent)
        decided_on = legacy_alert(device, rule, down_interface_ids=[p.pk])
        original = engine._uplink_statuses

        def another_worker_swaps_the_alert_while_we_read(*args, **kwargs):
            Alert.objects.filter(pk=decided_on.pk).update(is_active=False, resolved_at=now())
            engine._fire_alert(device, rule, 0.0, down_interface_ids=[p.pk])
            return original(*args, **kwargs)

        mocker.patch.object(engine, "_uplink_statuses", side_effect=another_worker_swaps_the_alert_while_we_read)
        evaluate(device)

        fresh = Alert.objects.get(device=device, rule=rule, is_active=True)
        assert fresh.pk != decided_on.pk
        assert not recoveries(fresh).exists()

    def test_unchanged_alert_is_still_resolved_normally(self, device, parent, rule):
        """Đối chứng: không có gì xen vào thì hồi phục thật vẫn RECOVERED đúng 1 lần."""
        p = iface(device, "Gi0/1", 1, status="up")
        link(device, p, parent)
        alert = legacy_alert(device, rule, down_interface_ids=[p.pk])
        evaluate(device)
        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == ""
        assert recoveries(alert).count() == 1


@pytest.mark.django_db
class TestAbbreviatedPortNames:
    """`uplink_ports` thật trên prod (đo 2026-10-15) ghi `Gi1/0/20` và `XGE0/0/1` trong khi cổng là
    `GigabitEthernet1/0/20` và `XGigabitEthernet0/0/1`. Chỉ 2 cặp này có bằng chứng, nên chỉ 2 cặp này được
    nhận; tên khớp chính xác luôn thắng; số cổng phải giống TOÀN BỘ; mơ hồ thì không tự chọn."""

    def _manual(self, device, *names):
        device.uplink_ports = list(names)
        device.save()

    def test_gi_matches_gigabitethernet(self, device, rule):
        self._manual(device, "Gi1/0/20")
        p = iface(device, "GigabitEthernet1/0/20", 1, status="down")
        evaluate(device)
        assert Alert.objects.get(device=device, rule=rule).down_interface_ids == [p.pk]
        assert MonitoredUplink.objects.get(device=device).source == MonitoredUplink.SOURCE_MANUAL

    def test_xge_matches_xgigabitethernet(self, device, rule):
        self._manual(device, "XGE0/0/1")
        p = iface(device, "XGigabitEthernet0/0/1", 1, status="down")
        evaluate(device)
        assert Alert.objects.get(device=device, rule=rule).down_interface_ids == [p.pk]

    def test_matching_works_in_both_directions_and_ignores_case(self, device, rule):
        self._manual(device, "gigabitethernet1/0/20")
        p = iface(device, "Gi1/0/20", 1, status="down")
        evaluate(device)
        assert Alert.objects.get(device=device, rule=rule).down_interface_ids == [p.pk]

    def test_other_abbreviations_are_not_guessed(self, device, rule, caplog):
        # `GE0/0/25` có trên prod nhưng KHÔNG có bằng chứng ánh xạ → không nhận, vẫn WARNING nêu tên.
        self._manual(device, "GE0/0/25")
        iface(device, "GigabitEthernet0/0/25", 1, status="down")
        with caplog.at_level(logging.WARNING, logger="apps.alerts.engine"):
            evaluate(device)
        assert not Alert.objects.filter(device=device).exists()
        assert not MonitoredUplink.objects.filter(device=device).exists()
        assert any("GE0/0/25" in r.getMessage() for r in caplog.records)

    def test_the_whole_port_number_must_match(self, device, rule):
        self._manual(device, "Gi1/0/2")
        iface(device, "GigabitEthernet1/0/20", 1, status="down")     # chỉ khác chữ số cuối
        iface(device, "GigabitEthernet2/0/2", 2, status="down")      # chỉ khác chỉ số stack
        iface(device, "XGigabitEthernet1/0/2", 3, status="down")     # khác loại cổng
        evaluate(device)
        assert not MonitoredUplink.objects.filter(device=device).exists()
        assert not Alert.objects.filter(device=device).exists()

    def test_exact_name_wins_over_alias(self, device, rule):
        self._manual(device, "Gi1/0/20")
        exact = iface(device, "Gi1/0/20", 1, status="up")
        iface(device, "GigabitEthernet1/0/20", 2, status="down")
        evaluate(device)
        assert list(MonitoredUplink.objects.filter(device=device).values_list("interface_id", flat=True)) == [exact.pk]
        assert not Alert.objects.filter(device=device).exists()

    def test_ambiguous_alias_is_not_guessed_and_warns(self, device, rule, caplog):
        self._manual(device, "XGE0/0/1")
        iface(device, "XGigabitEthernet0/0/1", 1, status="down")
        iface(device, "Xgigabitethernet0/0/1", 2, status="down")
        with caplog.at_level(logging.WARNING, logger="apps.alerts.engine"):
            evaluate(device)
        assert not MonitoredUplink.objects.filter(device=device).exists()
        assert not Alert.objects.filter(device=device).exists()
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "XGE0/0/1" in text and "XGigabitEthernet0/0/1" in text and "Xgigabitethernet0/0/1" in text

    def test_alias_pin_is_dropped_when_the_name_is_removed(self, device, rule):
        self._manual(device, "Gi1/0/20")
        p = iface(device, "GigabitEthernet1/0/20", 1, status="up")
        evaluate(device)
        assert MonitoredUplink.objects.filter(device=device, interface=p).exists()
        self._manual(device)
        evaluate(device)
        assert not MonitoredUplink.objects.filter(device=device).exists()
