"""Cảnh báo uplink theo TỪNG CỔNG — thiết kế B (docs/design-b-per-port-uplink-alerts.md, 2026-10-16).

Quyết định của người vận hành: (1) mỗi cổng giám sát có alert/vòng đời ALERT → RECOVERED riêng; (2) alert
`if_status` cấp thiết bị cũ đóng `superseded`, KHÔNG RECOVERED, rồi từng cổng được đánh giá bằng dữ liệu hiện
tại + điều kiện sustain; (3) ràng buộc DB duy nhất cho (thiết bị, rule, cổng) đang active; (4) tên thiết bị đối
diện chỉ khi link còn mới khớp ĐÚNG cổng, ghi lúc tạo để retry giữ nguyên nội dung; (5) deploy riêng sau khi
chứng minh: hai cổng rớt = hai alert độc lập, từng cổng hồi phục riêng, đánh giá đồng thời không tạo trùng,
outbox/retry giữ đúng thứ tự.

Chỉnh sửa bắt buộc: `duration_min` phải thực sự yêu cầu cổng KHÔNG up liên tục suốt cửa sổ (và dữ liệu phủ đủ
cửa sổ); hồi phục dùng trạng thái MỚI NHẤT. Bản cũ (`_sustained_if_status`) trả DOWN chỉ vì có BẤT KỲ mẫu
không-up nào trong cửa sổ. Mọi kịch bản sustain chạy ở cả DB-mode lẫn cache-mode.
"""
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.db import IntegrityError, connection, transaction

from apps.alerts import engine
from apps.alerts.channels import ChannelTransientError
from apps.alerts.engine import check_device_alerts, retry_pending_alert_notifications
from apps.alerts.models import Alert, AlertNotification, AlertRule, MonitoredUplink
from apps.devices.models import Interface, TopologyLink
from apps.metrics.models import InterfaceStats
from apps.alerts.channels import telegram as _telegram_module
from tests.conftest import CiscoSNMPDeviceFactory

# Autouse fixture bên dưới thay `send_telegram_recovery` bằng mock; test mẫu thông báo cần bản THẬT.
_REAL_SEND_TELEGRAM_RECOVERY = _telegram_module.send_telegram_recovery

SEND_FIRE = "apps.alerts.channels.telegram.send_telegram_alert"
SEND_RECOVERY = "apps.alerts.channels.telegram.send_telegram_recovery"


@pytest.fixture(autouse=True)
def _never_reach_telegram(mocker):
    """Mọi test đi qua engine thật — không test nào được chạm mạng."""
    mocker.patch(SEND_FIRE)
    mocker.patch(SEND_RECOVERY)


def now():
    return datetime.now(tz=UTC)


def evaluate(device):
    check_device_alerts(device, now() - timedelta(minutes=10))


@pytest.fixture
def rule(db):
    """Đúng cấu hình rule trên prod: `eq 0.0`, duration_min=0, kênh telegram."""
    return AlertRule.objects.create(
        name="Switch Uplink Down (per-port test)", device_type="switch", metric="if_status",
        condition="eq", threshold=0.0, severity="CRITICAL", channels=["telegram"], enabled=True,
    )


@pytest.fixture
def sustained_rule(db):
    return AlertRule.objects.create(
        name="Switch Uplink Down sustained (test)", device_type="switch", metric="if_status",
        condition="eq", threshold=0.0, severity="CRITICAL", channels=["telegram"], enabled=True,
        duration_min=5,
    )


@pytest.fixture
def device(db):
    return CiscoSNMPDeviceFactory(last_seen=now())


@pytest.fixture
def peer(db):
    return CiscoSNMPDeviceFactory(name="PEER-SW")


@pytest.fixture(params=["db", "cache"])
def history(request, mocker):
    """Dựng lịch sử trạng thái của 1 cổng theo mốc giây (≤ 0 = quá khứ so với lúc gọi) cho CẢ HAI chế độ đọc
    của engine: DB (`InterfaceStats`) và cache (Redis if-series + snapshot)."""
    store: dict[int, list[dict]] = {}

    if request.param == "cache":
        mocker.patch("apps.alerts.engine._use_cache", return_value=True)

        def get_if_series(interface_id, since=None):
            return [s for s in store.get(interface_id, []) if since is None or s["ts"] >= since.timestamp()]

        def fresh_latest(device, since):
            interfaces = {}
            for pk, samples in store.items():
                if not samples:
                    continue
                if since is not None and samples[-1]["ts"] < since.timestamp():
                    continue
                interfaces[str(pk)] = {"status": samples[-1]["status"]}
            return {"interfaces": interfaces, "ts": time.time()} if interfaces else None

        mocker.patch("apps.alerts.engine.metrics_cache.get_if_series", side_effect=get_if_series)
        mocker.patch("apps.alerts.engine._fresh_latest", side_effect=fresh_latest)

    def put(port, samples):
        for offset_secs, status in samples:
            ts = now() + timedelta(seconds=offset_secs)
            if request.param == "db":
                InterfaceStats.objects.create(interface=port, timestamp=ts, status=status, in_bytes=0, out_bytes=0)
            else:
                store.setdefault(port.pk, []).append({"ts": ts.timestamp(), "status": status})

    put.mode = request.param
    return put


def port(device, name, if_index):
    return Interface.objects.create(device=device, if_index=if_index, name=name)


def link(device, i, remote, *, stale=False):
    return TopologyLink.objects.create(
        local_device=device, local_port=i.name, local_interface=i, link_kind="switch",
        remote_device=remote, is_stale=stale, protocol="fdb", match_method="mac",
    )


def device_level_alert(device, rule, *, down_interface_ids=None):
    """Alert cấp thiết bị kiểu cũ (interface NULL) như đang kẹt trên prod: đã gửi fire thành công."""
    alert = Alert.objects.create(
        device=device, rule=rule, severity=rule.severity, message=f"{device.name}: Uplink Down",
        metric_value=0.0, is_active=True, down_interface_ids=down_interface_ids,
    )
    AlertNotification.objects.create(alert=alert, channel="telegram", kind="fire", status="sent")
    return alert


def alert_of(device, rule, i, *, active=True):
    return Alert.objects.get(device=device, rule=rule, interface=i, is_active=active)


def fires(alert):
    return AlertNotification.objects.filter(alert=alert, kind="fire")


def recoveries(alert):
    return AlertNotification.objects.filter(alert=alert, kind="recovery")


def mark_fire_sent(alert):
    AlertNotification.objects.filter(alert=alert, kind="fire").update(status="sent")


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 1. Hai cổng = hai alert độc lập; từng cổng hồi phục riêng
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestEachPortHasItsOwnLifecycle:
    def test_two_ports_down_make_two_independent_alerts(self, history, device, peer, rule):
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory(name="OTHER-SW"))
        history(a, [(-1, "down")])
        history(b, [(-1, "down")])

        evaluate(device)

        alerts = Alert.objects.filter(device=device, rule=rule, is_active=True)
        assert alerts.count() == 2
        by_port = {x.interface_id: x for x in alerts}
        assert set(by_port) == {a.pk, b.pk}
        for p in (a, b):
            assert by_port[p.pk].interface_name == p.name
            assert by_port[p.pk].down_interface_ids == [p.pk]     # engine bản A (rollback) vẫn hiểu
            assert fires(by_port[p.pk]).filter(channel="telegram").count() == 1
            assert p.name in by_port[p.pk].message

    def test_second_port_dropping_gets_its_own_alert_and_its_own_notification(self, history, device, peer, rule):
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory())
        history(a, [(-100, "down")])
        history(b, [(-100, "up")])
        evaluate(device)
        first = alert_of(device, rule, a)
        mark_fire_sent(first)
        assert Alert.objects.filter(device=device, rule=rule).count() == 1

        history(b, [(-1, "down")])
        evaluate(device)

        second = alert_of(device, rule, b)
        assert second.pk != first.pk
        assert fires(second).count() == 1
        assert fires(first).count() == 1                       # alert cũ không bị đụng tới
        assert Alert.objects.filter(device=device, rule=rule, is_active=True).count() == 2

    def test_each_port_recovers_on_its_own(self, history, device, peer, rule):
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory())
        history(a, [(-100, "down")])
        history(b, [(-100, "down")])
        evaluate(device)
        alert_a, alert_b = alert_of(device, rule, a), alert_of(device, rule, b)
        mark_fire_sent(alert_a)
        mark_fire_sent(alert_b)

        history(a, [(-1, "up")])                               # a hồi phục, b VẪN down
        evaluate(device)

        alert_a.refresh_from_db()
        alert_b.refresh_from_db()
        assert alert_a.is_active is False and alert_a.closed_reason == ""
        assert recoveries(alert_a).count() == 1                # RECOVERED riêng của cổng a
        assert alert_b.is_active is True
        assert not recoveries(alert_b).exists()

        history(b, [(0, "up")])
        evaluate(device)
        alert_b.refresh_from_db()
        assert alert_b.is_active is False and recoveries(alert_b).count() == 1

    def test_a_port_that_is_up_creates_no_alert_and_is_not_blamed_for_the_down_one(self, history, device, peer, rule):
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory())
        history(a, [(-1, "up")])
        history(b, [(-1, "down")])
        evaluate(device)
        assert list(Alert.objects.filter(device=device, rule=rule).values_list("interface_id", flat=True)) == [b.pk]

    def test_port_with_no_status_neither_fires_nor_hides_a_down_port(self, history, device, peer, rule):
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)   # a: không có dữ liệu
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory())
        history(b, [(-1, "down")])
        evaluate(device)
        assert list(Alert.objects.filter(device=device, rule=rule).values_list("interface_id", flat=True)) == [b.pk]

    def test_still_down_port_keeps_a_single_alert_over_many_rounds(self, history, device, peer, rule):
        a = port(device, "Gi0/1", 1)
        link(device, a, peer)
        history(a, [(-1, "down")])
        for _ in range(3):
            evaluate(device)
        assert Alert.objects.filter(device=device, rule=rule).count() == 1
        assert fires(Alert.objects.get(device=device, rule=rule)).count() == 1

    def test_resolving_one_port_never_touches_the_alert_of_another_port(self, device, rule):
        """Gọi thẳng `_resolve_alert` (không qua `only_if` của vòng đánh giá): bản thân hàm phải chỉ resolve alert
        của ĐÚNG cổng được truyền. Hai lớp (lọc theo cổng + only_if) che nhau nên cần test riêng cho từng lớp."""
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        alerts = {}
        for p in (a, b):
            alerts[p.pk] = Alert.objects.create(
                device=device, rule=rule, severity="CRITICAL", message=p.name, metric_value=0.0,
                is_active=True, interface=p, interface_name=p.name, down_interface_ids=[p.pk],
            )
            AlertNotification.objects.create(alert=alerts[p.pk], channel="telegram", kind="fire", status="sent")

        engine._resolve_alert(device, rule, interface=a)

        alert_a, alert_b = alerts[a.pk], alerts[b.pk]
        alert_a.refresh_from_db()
        alert_b.refresh_from_db()
        assert alert_a.is_active is False and recoveries(alert_a).count() == 1
        assert alert_b.is_active is True and not recoveries(alert_b).exists()


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 2. `duration_min`: cổng phải KHÔNG up liên tục đủ cửa sổ; hồi phục dùng trạng thái mới nhất (DB + cache)
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestDurationMinMeansDownForTheWholeWindow:
    """Cửa sổ 5 phút; chu kỳ poll ≈ 90s (mẫu cách nhau 80–90s)."""

    @pytest.fixture
    def p(self, device, peer):
        i = port(device, "Gi0/1", 1)
        link(device, i, peer)
        return i

    def test_down_for_less_than_the_window_does_not_fire(self, history, device, sustained_rule, p):
        # up đầu cửa sổ, down 2 phút gần nhất — bản cũ báo DOWN vì có mẫu không-up trong cửa sổ.
        history(p, [(-290, "up"), (-200, "up"), (-110, "down"), (-20, "down")])
        evaluate(device)
        assert not Alert.objects.filter(device=device).exists()

    def test_down_only_since_a_couple_of_minutes_with_no_older_samples_does_not_fire(
        self, history, device, sustained_rule, p,
    ):
        # Chỉ thấy cổng down trong 110s gần nhất; chưa có bằng chứng nó đã down đủ 5 phút.
        history(p, [(-110, "down"), (-20, "down")])
        evaluate(device)
        assert not Alert.objects.filter(device=device).exists()

    def test_down_for_the_whole_window_fires(self, history, device, sustained_rule, p):
        history(p, [(-290, "down"), (-200, "down"), (-110, "down"), (-20, "down")])
        evaluate(device)
        alert = Alert.objects.get(device=device, rule=sustained_rule)
        assert alert.interface_id == p.pk and alert.is_active

    def test_an_up_sample_inside_the_window_breaks_the_streak(self, history, device, sustained_rule, p):
        history(p, [(-290, "down"), (-200, "up"), (-110, "down"), (-20, "down")])
        evaluate(device)
        assert not Alert.objects.filter(device=device).exists()

    def test_back_up_after_a_long_down_does_not_fire(self, history, device, sustained_rule, p):
        # down gần hết cửa sổ nhưng mẫu MỚI NHẤT đã up → không báo DOWN.
        history(p, [(-290, "down"), (-200, "down"), (-110, "down"), (-20, "up")])
        evaluate(device)
        assert not Alert.objects.filter(device=device).exists()

    def test_recovery_uses_the_latest_state_not_the_window(self, history, device, sustained_rule, p):
        history(p, [(-290, "down"), (-200, "down"), (-110, "down"), (-60, "down")])
        evaluate(device)
        alert = Alert.objects.get(device=device, rule=sustained_rule)
        mark_fire_sent(alert)

        history(p, [(-5, "up")])                               # trong cửa sổ vẫn còn các mẫu down trước đó
        evaluate(device)

        alert.refresh_from_db()
        assert alert.is_active is False and alert.closed_reason == ""
        assert recoveries(alert).count() == 1

    def test_flap_down_again_after_up_must_sustain_again(self, history, device, sustained_rule, p):
        history(p, [(-290, "down"), (-200, "down"), (-110, "down"), (-60, "down")])
        evaluate(device)
        alert = Alert.objects.get(device=device, rule=sustained_rule)
        mark_fire_sent(alert)
        history(p, [(-50, "up")])
        evaluate(device)
        alert.refresh_from_db()
        assert alert.is_active is False

        history(p, [(-20, "down")])                            # vừa rớt lại, cửa sổ còn mẫu up → chưa đủ
        evaluate(device)
        assert Alert.objects.filter(device=device, rule=sustained_rule, is_active=True).count() == 0

    def test_missing_data_neither_fires_nor_resolves(self, history, device, sustained_rule, p):
        evaluate(device)
        assert not Alert.objects.filter(device=device).exists()

        active = Alert.objects.create(
            device=device, rule=sustained_rule, severity="CRITICAL", message="x", metric_value=0.0,
            is_active=True, interface=p, interface_name=p.name, down_interface_ids=[p.pk],
        )
        evaluate(device)                                       # vẫn không có mẫu nào
        active.refresh_from_db()
        assert active.is_active is True
        assert not recoveries(active).exists()

    def test_stale_samples_are_not_evidence(self, history, device, sustained_rule, p):
        # Mẫu mới nhất cũ hơn 135s với chu kỳ quét 60s: poll kẹt → không đủ cơ sở báo DOWN.
        type(device).objects.filter(pk=device.pk).update(collect_interval=60)
        device.refresh_from_db()
        history(p, [(-290, "down"), (-250, "down"), (-200, "down")])
        evaluate(device)
        assert not Alert.objects.filter(device=device).exists()

    def test_each_port_is_judged_on_its_own_history(self, history, device, peer, sustained_rule):
        long_down = port(device, "Gi0/1", 1)
        short_down = port(device, "Gi0/2", 2)
        link(device, long_down, peer)
        link(device, short_down, CiscoSNMPDeviceFactory())
        history(long_down, [(-290, "down"), (-200, "down"), (-110, "down"), (-20, "down")])
        history(short_down, [(-290, "up"), (-200, "up"), (-110, "up"), (-20, "down")])
        evaluate(device)
        assert list(
            Alert.objects.filter(device=device, rule=sustained_rule).values_list("interface_id", flat=True)
        ) == [long_down.pk]


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 3. Alert cấp thiết bị cũ → `superseded` (không RECOVERED), rồi đánh giá từng cổng bằng dữ liệu hiện tại
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestDeviceLevelAlertIsSuperseded:
    def test_device_level_alert_is_closed_superseded_without_recovered(self, history, device, peer, rule):
        a = port(device, "Gi0/1", 1)
        link(device, a, peer)
        history(a, [(-1, "up")])
        old = device_level_alert(device, rule)

        evaluate(device)

        old.refresh_from_db()
        assert old.is_active is False
        assert old.closed_reason == Alert.CLOSED_SUPERSEDED == "superseded"
        assert old.resolved_at is not None
        assert "không phải đã hồi phục" in old.message
        assert not recoveries(old).exists()
        assert AlertNotification.objects.filter(alert=old).count() == 1      # chỉ còn dòng fire cũ
        assert not Alert.objects.filter(device=device, is_active=True).exists()   # cổng up → không alert mới

    def test_only_ports_that_are_really_down_get_a_new_alert(self, history, device, peer, rule):
        up, down = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, up, peer)
        link(device, down, CiscoSNMPDeviceFactory())
        history(up, [(-1, "up")])
        history(down, [(-1, "down")])
        old = device_level_alert(device, rule, down_interface_ids=[up.pk, down.pk])

        evaluate(device)

        old.refresh_from_db()
        assert old.closed_reason == Alert.CLOSED_SUPERSEDED
        assert not recoveries(old).exists()
        new = list(Alert.objects.filter(device=device, rule=rule, is_active=True))
        assert [x.interface_id for x in new] == [down.pk]
        assert fires(new[0]).count() == 1                      # sự cố đang kéo dài được thông báo lại 1 lần

    def test_new_per_port_alert_respects_the_sustain_condition(self, history, device, peer, sustained_rule):
        a = port(device, "Gi0/1", 1)
        link(device, a, peer)
        history(a, [(-110, "down"), (-20, "down")])            # down chưa đủ 5 phút
        old = device_level_alert(device, sustained_rule)

        evaluate(device)

        old.refresh_from_db()
        assert old.closed_reason == Alert.CLOSED_SUPERSEDED
        assert not Alert.objects.filter(device=device, is_active=True).exists()

    def test_nothing_is_superseded_while_the_scope_is_undetermined(self, history, device, rule):
        port(device, "Gi0/1", 1)                               # không link, không uplink_ports → chưa xác định
        old = device_level_alert(device, rule)
        evaluate(device)
        old.refresh_from_db()
        assert old.is_active is True and old.closed_reason == ""

    def test_superseded_alert_never_gets_a_late_recovery(self, history, device, peer, rule):
        a = port(device, "Gi0/1", 1)
        link(device, a, peer)
        history(a, [(-1, "up")])
        old = Alert.objects.create(
            device=device, rule=rule, severity="CRITICAL", message="x", metric_value=0.0, is_active=True,
        )
        fire = AlertNotification.objects.create(alert=old, channel="telegram", kind="fire", status="pending")
        evaluate(device)
        engine._queue_late_recovery_if_resolved(old.pk, "telegram")
        assert not recoveries(old).exists()
        assert AlertNotification.objects.filter(pk=fire.pk).exists()

    def test_several_devices_superseded_at_once_send_no_recovered_burst(self, history, rule, mocker):
        send_recovery = mocker.patch(SEND_RECOVERY)
        olds = []
        for n in range(5):
            dev = CiscoSNMPDeviceFactory(last_seen=now())
            p = port(dev, "Gi0/1", 1)
            link(dev, p, CiscoSNMPDeviceFactory())
            history(p, [(-1, "up")])
            olds.append((dev, device_level_alert(dev, rule)))
        for dev, _ in olds:
            evaluate(dev)
        assert send_recovery.call_count == 0
        assert not AlertNotification.objects.filter(kind="recovery").exists()
        assert all(Alert.objects.get(pk=a.pk).closed_reason == "superseded" for _, a in olds)

    def test_interface_removed_turns_the_alert_into_a_superseded_one(self, history, device, peer, rule):
        gone, kept = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, gone, peer)
        link(device, kept, CiscoSNMPDeviceFactory())
        history(gone, [(-1, "down")])
        history(kept, [(-1, "up")])
        evaluate(device)
        alert = alert_of(device, rule, gone)
        mark_fire_sent(alert)

        Interface.objects.filter(pk=gone.pk).delete()          # SET_NULL: lịch sử còn, tên cổng được chụp sẵn
        evaluate(device)

        alert.refresh_from_db()
        assert alert.interface_id is None and alert.interface_name == "Gi0/1"
        assert alert.is_active is False and alert.closed_reason == "superseded"
        assert not recoveries(alert).exists()


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 4. Đổi phạm vi: cổng rời phạm vi → đóng `scope_changed` theo từng cổng
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestScopeChangeIsPerPort:
    def test_only_the_port_that_left_the_scope_is_closed(self, history, device, rule):
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        device.uplink_ports = ["Gi0/1", "Gi0/2"]
        device.save()
        history(a, [(-1, "down")])
        history(b, [(-1, "down")])
        evaluate(device)
        alert_a, alert_b = alert_of(device, rule, a), alert_of(device, rule, b)
        mark_fire_sent(alert_a)
        mark_fire_sent(alert_b)

        device.uplink_ports = ["Gi0/2"]                        # a không còn là uplink
        device.save()
        evaluate(device)

        alert_a.refresh_from_db()
        alert_b.refresh_from_db()
        assert alert_a.is_active is False and alert_a.closed_reason == Alert.CLOSED_SCOPE_CHANGED
        assert not recoveries(alert_a).exists()
        assert alert_b.is_active is True and alert_b.closed_reason == ""
        assert not MonitoredUplink.objects.filter(device=device, interface=a).exists()


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 5. Tên thiết bị đối diện: chỉ khi link còn mới KHỚP ĐÚNG cổng; ghi lúc tạo; thiếu vẫn cảnh báo
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestPeerName:
    def test_fresh_link_on_the_exact_port_names_the_peer_in_alert_and_columns(self, history, device, peer, rule):
        a = port(device, "Gi0/1", 1)
        link(device, a, peer)
        history(a, [(-1, "down")])
        evaluate(device)
        alert = alert_of(device, rule, a)
        assert alert.peer_name == "PEER-SW"
        assert "Gi0/1" in alert.message and "PEER-SW" in alert.message

    def test_no_link_still_alerts_without_a_peer(self, history, device, rule):
        a = port(device, "Gi0/1", 1)
        device.uplink_ports = ["Gi0/1"]
        device.save()
        history(a, [(-1, "down")])
        evaluate(device)
        alert = alert_of(device, rule, a)
        assert alert.peer_name == ""
        assert "Gi0/1" in alert.message
        assert fires(alert).count() == 1

    def test_stale_link_gives_no_peer_but_the_port_is_still_reported(self, history, device, peer, rule):
        a = port(device, "Gi0/1", 1)
        tl = link(device, a, peer)
        history(a, [(-1, "up")])
        evaluate(device)                                       # xác lập cổng khi link còn mới
        TopologyLink.objects.filter(pk=tl.pk).update(is_stale=True)
        history(a, [(0, "down")])
        evaluate(device)
        alert = alert_of(device, rule, a)
        assert alert.peer_name == ""
        assert fires(alert).count() == 1

    def test_link_of_another_port_is_not_used(self, history, device, peer, rule):
        a, other = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        device.uplink_ports = ["Gi0/1"]
        device.save()
        link(device, other, peer)                              # link nằm ở cổng khác
        history(a, [(-1, "down")])
        evaluate(device)
        assert alert_of(device, rule, a).peer_name == ""

    def test_ambiguous_peer_on_the_same_port_is_not_guessed(self, history, device, peer, rule):
        a = port(device, "Gi0/1", 1)
        link(device, a, peer)
        TopologyLink.objects.create(
            local_device=device, local_port="GI0/1", local_interface=None, link_kind="switch",
            remote_device=CiscoSNMPDeviceFactory(name="SECOND-SW"), is_stale=False, protocol="fdb",
            match_method="mac",
        )
        history(a, [(-1, "down")])
        evaluate(device)
        assert alert_of(device, rule, a).peer_name == ""

    def test_retry_resends_exactly_the_recorded_content_even_if_topology_changed(
        self, history, device, peer, rule, mocker,
    ):
        sent = []
        mocker.patch(SEND_FIRE, side_effect=ChannelTransientError("Telegram HTTP 503"))
        a = port(device, "Gi0/1", 1)
        tl = link(device, a, peer)
        history(a, [(-1, "down")])
        evaluate(device)
        alert = alert_of(device, rule, a)
        original = alert.message
        row = fires(alert).get()
        assert row.status == "pending"

        # Topology đổi trong lúc chờ gửi lại: link stale + đối diện khác.
        TopologyLink.objects.filter(pk=tl.pk).update(is_stale=True)
        AlertNotification.objects.filter(pk=row.pk).update(
            next_attempt_at=now() - timedelta(seconds=1), sent_at=now() - timedelta(seconds=300),
        )
        mocker.patch(SEND_FIRE, side_effect=lambda a_: sent.append((a_.message, a_.peer_name, a_.interface_name)))
        retry_pending_alert_notifications()

        assert sent == [(original, "PEER-SW", "Gi0/1")]
        assert fires(alert).get().status == "sent"


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 6. Mẫu ALERT / RECOVERED nêu cổng (+ đối diện) — RECOVERED KHÔNG dùng alert.message nên phải thêm dòng riêng
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestNotificationTemplates:
    @pytest.fixture
    def resolved_port_alert(self, device, rule):
        return Alert.objects.create(
            device=device, rule=rule, severity="CRITICAL", message=f"{device.name}: cổng Gi0/7 DOWN",
            metric_value=0.0, is_active=False, resolved_at=now(), interface_name="Gi0/7",
            peer_name="PEER-SW", closed_reason="",
        )

    @pytest.fixture
    def resolved_device_alert(self, device):
        r = AlertRule.objects.create(
            name="CPU test", device_type="all", metric="cpu_percent", condition="gt", threshold=90.0,
            severity="WARNING", channels=["telegram"], enabled=True,
        )
        return Alert.objects.create(
            device=device, rule=r, severity="WARNING", message="x", metric_value=95.0, is_active=False,
            resolved_at=now(),
        )

    def test_telegram_recovery_names_port_and_peer(self, resolved_port_alert, mocker):
        from apps.alerts.channels import telegram
        mocker.patch.object(telegram, "_credentials", return_value=("t", "c"))
        post = mocker.patch.object(telegram, "_post")
        _REAL_SEND_TELEGRAM_RECOVERY(resolved_port_alert)
        text = post.call_args.args[2]
        assert "Gi0/7" in text and "PEER-SW" in text

    def test_email_recovery_names_port_and_peer(self, resolved_port_alert, mocker):
        from apps.alerts.channels import email_channel
        mocker.patch.object(email_channel, "_recipients", return_value=["a@example.test"])
        send = mocker.patch.object(email_channel, "_send")
        email_channel.send_email_recovery(resolved_port_alert)
        body = send.call_args.args[1]
        assert "Gi0/7" in body and "PEER-SW" in body

    @pytest.mark.parametrize("fn_name", ["send_slack_recovery", "send_teams_recovery"])
    def test_webhook_recovery_names_port_and_peer(self, resolved_port_alert, mocker, fn_name):
        from apps.alerts.channels import webhook
        mocker.patch.object(webhook, "_webhook_url", return_value="https://example.test/hook")
        post = mocker.patch.object(webhook, "post_json")
        getattr(webhook, fn_name)(resolved_port_alert)
        text = post.call_args.args[1]["text"]
        assert "Gi0/7" in text and "PEER-SW" in text

    def test_recovery_of_a_device_level_alert_is_unchanged(self, resolved_device_alert, mocker):
        from apps.alerts.channels import telegram
        mocker.patch.object(telegram, "_credentials", return_value=("t", "c"))
        post = mocker.patch.object(telegram, "_post")
        _REAL_SEND_TELEGRAM_RECOVERY(resolved_device_alert)
        text = post.call_args.args[2]
        assert "Cổng" not in text and "Nối tới" not in text

    def test_telegram_fire_for_a_port_alert_shows_the_port_via_message(self, history, device, peer, rule, mocker):
        a = port(device, "Gi0/1", 1)
        link(device, a, peer)
        history(a, [(-1, "down")])
        captured = []
        mocker.patch(SEND_FIRE, side_effect=lambda al: captured.append(al.message))
        evaluate(device)
        assert len(captured) == 1 and "Gi0/1" in captured[0] and "PEER-SW" in captured[0]


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 7. Outbox/retry: ALERT trước RECOVERED theo từng (alert, channel); cổng này không chặn thứ tự cổng kia
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestOutboxOrderingPerPort:
    def test_fire_retried_after_the_port_recovered_still_precedes_its_recovery(
        self, history, device, peer, rule, mocker,
    ):
        """Lỗi tạm thời (không phải 429) ở cổng a: cổng b vẫn gửi ngay; a hồi phục trong lúc fire còn chờ →
        khi fire a gửi được thì recovery trễ được xếp SAU nó, và cổng b không bị đụng tới."""
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory())
        order = []
        failing = {"interface": a.pk}

        def send_fire(al):
            if al.interface_id == failing["interface"]:
                raise ChannelTransientError("Telegram: không kết nối được")
            order.append(("fire", al.interface_name))

        mocker.patch(SEND_FIRE, side_effect=send_fire)
        mocker.patch(SEND_RECOVERY, side_effect=lambda al: order.append(("recovery", al.interface_name)))
        history(a, [(-100, "down")])
        history(b, [(-100, "down")])
        evaluate(device)

        alert_a, alert_b = alert_of(device, rule, a), alert_of(device, rule, b)
        assert fires(alert_a).get().status == "pending"        # lỗi tạm thời → hẹn lại
        assert fires(alert_b).get().status == "sent"           # cổng b không bị cổng a chặn
        assert order == [("fire", "Gi0/2")]

        history(a, [(-1, "up")])                               # a hồi phục trong lúc fire còn chờ
        evaluate(device)
        assert not recoveries(alert_a).exists()                # chưa có fire 'sent' → chưa tạo recovery

        failing["interface"] = None
        row = fires(alert_a).get()
        AlertNotification.objects.filter(pk=row.pk).update(
            next_attempt_at=now() - timedelta(seconds=1), sent_at=now() - timedelta(seconds=300),
        )
        retry_pending_alert_notifications()                    # fire a được gửi → recovery trễ được xếp
        rec = recoveries(alert_a).get()
        AlertNotification.objects.filter(pk=rec.pk).update(sent_at=now() - timedelta(seconds=300))
        retry_pending_alert_notifications()

        assert order == [("fire", "Gi0/2"), ("fire", "Gi0/1"), ("recovery", "Gi0/1")]
        assert alert_of(device, rule, b).is_active is True     # cổng b vẫn active, không bị đụng

    def test_flood_control_defers_both_ports_but_loses_nothing_and_keeps_each_order(
        self, history, device, peer, rule, mocker, settings,
    ):
        """429 là sự thật về KÊNH (cooldown dùng chung, 1.2): cả hai cổng bị hoãn chứ không mất; sau cooldown mỗi
        cổng vẫn gửi ALERT trước RECOVERED."""
        settings.TELEGRAM_BOT_TOKEN = "123456789:AAH-secretTokenValue_xyz"
        settings.TELEGRAM_CHAT_ID = "-1001234"
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory())
        order = []
        flooded = {"on": True}

        def send_fire(al):
            if flooded["on"]:
                raise ChannelTransientError("Telegram HTTP 429", retry_after=30)
            order.append(("fire", al.interface_name))

        mocker.patch(SEND_FIRE, side_effect=send_fire)
        mocker.patch(SEND_RECOVERY, side_effect=lambda al: order.append(("recovery", al.interface_name)))
        history(a, [(-100, "down")])
        history(b, [(-100, "down")])
        evaluate(device)

        alert_a, alert_b = alert_of(device, rule, a), alert_of(device, rule, b)
        for al in (alert_a, alert_b):
            row = fires(al).get()
            assert row.status == "pending" and row.next_attempt_at is not None   # hoãn, không failed/mất

        history(a, [(-1, "up")])                               # a hồi phục khi fire của nó còn chờ
        evaluate(device)
        assert not recoveries(alert_a).exists()

        flooded["on"] = False
        from apps.alerts.models import ChannelCooldown
        ChannelCooldown.objects.all().delete()                 # cooldown hết hạn
        AlertNotification.objects.filter(kind="fire").update(
            next_attempt_at=now() - timedelta(seconds=1), sent_at=now() - timedelta(seconds=300),
        )
        retry_pending_alert_notifications()
        rec = recoveries(alert_a).get()
        AlertNotification.objects.filter(pk=rec.pk).update(sent_at=now() - timedelta(seconds=300))
        retry_pending_alert_notifications()

        assert ("fire", "Gi0/1") in order and ("fire", "Gi0/2") in order
        assert order.index(("fire", "Gi0/1")) < order.index(("recovery", "Gi0/1"))
        assert ("recovery", "Gi0/2") not in order              # cổng b vẫn đang down
        assert alert_of(device, rule, b).is_active is True

    def test_each_port_keeps_its_own_alert_then_recovered_order(self, history, device, peer, rule, mocker):
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory())
        order = []
        mocker.patch(SEND_FIRE, side_effect=lambda al: order.append(("fire", al.interface_name)))
        mocker.patch(SEND_RECOVERY, side_effect=lambda al: order.append(("recovery", al.interface_name)))
        history(a, [(-100, "down")])
        history(b, [(-100, "down")])
        evaluate(device)
        history(b, [(-1, "up")])
        evaluate(device)
        history(a, [(0, "up")])
        evaluate(device)

        assert order.index(("fire", "Gi0/1")) < order.index(("recovery", "Gi0/1"))
        assert order.index(("fire", "Gi0/2")) < order.index(("recovery", "Gi0/2"))
        assert order.index(("recovery", "Gi0/2")) < order.index(("recovery", "Gi0/1"))


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 8. Chống flapping và nhắc-lại theo cổng
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestFlappingIsPerPort:
    def test_a_flapping_port_does_not_suppress_the_notification_of_another_port(
        self, history, device, peer, rule, settings,
    ):
        settings.ALERT_FLAP_THRESHOLD = 3
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory())
        for _ in range(2):                                     # cổng a đã nổ 2 lần trong cửa sổ flapping
            Alert.objects.create(
                device=device, rule=rule, severity="CRITICAL", message="old", metric_value=0.0,
                is_active=False, interface=a, interface_name=a.name, closed_reason="",
            )
        history(a, [(-1, "down")])
        history(b, [(-1, "down")])

        evaluate(device)

        assert fires(alert_of(device, rule, a)).count() == 0   # lần nổ thứ 3 của a → flapping, bị chặn
        assert fires(alert_of(device, rule, b)).count() == 1   # b chưa từng nổ → vẫn được thông báo

    def test_renotify_after_suppression_is_counted_per_port(self, history, device, peer, rule, settings):
        settings.ALERT_FLAP_THRESHOLD = 3
        settings.ALERT_FLAP_RENOTIFY_MIN = 30
        a = port(device, "Gi0/1", 1)
        link(device, a, peer)
        for _ in range(2):
            Alert.objects.create(
                device=device, rule=rule, severity="CRITICAL", message="old", metric_value=0.0,
                is_active=False, interface=a, interface_name=a.name, closed_reason="",
            )
        history(a, [(-1, "down")])
        evaluate(device)
        alert = alert_of(device, rule, a)
        assert fires(alert).count() == 0
        Alert.objects.filter(pk=alert.pk).update(triggered_at=now() - timedelta(minutes=31))
        Alert.objects.filter(device=device, rule=rule, is_active=False).update(
            triggered_at=now() - timedelta(minutes=31),
        )
        evaluate(device)
        assert fires(alert).count() == 1                       # nhắc đúng 1 lần cho alert của cổng này
        evaluate(device)
        assert fires(alert).count() == 1


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 9. Ràng buộc DB + đánh giá đồng thời không tạo trùng
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestUniqueActiveAlertPerPort:
    def _alert(self, device, rule, i, *, active=True):
        return Alert.objects.create(
            device=device, rule=rule, severity="CRITICAL", message="x", metric_value=0.0, is_active=active,
            interface=i, interface_name=i.name if i else "",
        )

    def test_database_rejects_a_second_active_alert_for_the_same_port(self, device, rule):
        a = port(device, "Gi0/1", 1)
        self._alert(device, rule, a)
        with pytest.raises(IntegrityError), transaction.atomic():
            self._alert(device, rule, a)

    def test_a_closed_alert_does_not_block_a_new_active_one(self, device, rule):
        a = port(device, "Gi0/1", 1)
        self._alert(device, rule, a, active=False)
        self._alert(device, rule, a, active=True)
        assert Alert.objects.filter(interface=a).count() == 2

    def test_different_ports_and_different_rules_do_not_conflict(self, device, rule):
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        other = AlertRule.objects.create(
            name="Other", device_type="switch", metric="if_status", condition="eq", threshold=0.0,
            severity="WARNING", channels=[], enabled=True,
        )
        self._alert(device, rule, a)
        self._alert(device, rule, b)
        self._alert(device, other, a)

    def test_device_level_alerts_are_not_covered_by_the_constraint(self, device):
        r = AlertRule.objects.create(
            name="CPU", device_type="all", metric="cpu_percent", condition="gt", threshold=90.0,
            severity="WARNING", channels=[], enabled=True,
        )
        self._alert(device, r, None)
        self._alert(device, r, None)                           # ngữ nghĩa cũ: DB không ràng buộc rule khác

    def test_fire_swallows_a_constraint_violation_instead_of_duplicating_or_raising(self, device, peer, rule, mocker):
        a = port(device, "Gi0/1", 1)
        existing = self._alert(device, rule, a)
        # Giả lập khoá Device có sơ hở: bước tra "đã có alert active chưa" không thấy alert đang tồn tại.
        mocker.patch.object(engine, "_active_alert_for", return_value=None)

        engine._fire_alert(device, rule, 0.0, interface=a)      # không được ném IntegrityError

        assert Alert.objects.filter(interface=a, is_active=True).count() == 1
        assert Alert.objects.get(interface=a, is_active=True).pk == existing.pk
        assert not AlertNotification.objects.filter(kind="fire").exists()


@pytest.mark.django_db(transaction=True)
class TestConcurrentEvaluationDoesNotDuplicate:
    def test_two_evaluations_of_the_same_down_port_create_one_alert(self, history, mocker):
        dev = CiscoSNMPDeviceFactory(last_seen=now())
        rule = AlertRule.objects.create(
            name="Uplink concurrent", device_type="switch", metric="if_status", condition="eq",
            threshold=0.0, severity="CRITICAL", channels=["telegram"], enabled=True,
        )
        p = port(dev, "Gi0/1", 1)
        link(dev, p, CiscoSNMPDeviceFactory())
        history(p, [(-1, "down")])

        barrier = threading.Barrier(2, timeout=10)
        errors: list[BaseException] = []

        def run():
            try:
                barrier.wait()
                evaluate(dev)
            except BaseException as exc:   # noqa: BLE001 — để test thấy lỗi thật
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        alerts = Alert.objects.filter(device=dev, rule=rule, interface=p)
        assert alerts.filter(is_active=True).count() == 1
        assert AlertNotification.objects.filter(alert__in=alerts, kind="fire", channel="telegram").count() == 1

    def test_resolving_one_port_while_another_is_being_created_does_not_touch_the_new_alert(self, history, mocker):
        """Giữa lúc cổng a hồi phục (resolve) có worker khác tạo alert cho cổng b: resolve của a chỉ chạm a."""
        dev = CiscoSNMPDeviceFactory(last_seen=now())
        rule = AlertRule.objects.create(
            name="Uplink resolve race", device_type="switch", metric="if_status", condition="eq",
            threshold=0.0, severity="CRITICAL", channels=[], enabled=True,
        )
        a, b = port(dev, "Gi0/1", 1), port(dev, "Gi0/2", 2)
        link(dev, a, CiscoSNMPDeviceFactory())
        link(dev, b, CiscoSNMPDeviceFactory())
        history(a, [(-100, "down")])
        history(b, [(-100, "up")])
        evaluate(dev)
        alert_a = alert_of(dev, rule, a)

        history(a, [(-5, "up")])
        history(b, [(-1, "down")])
        original = engine._resolve_alert

        def resolve_then_other_worker_creates_b(device, rule_, *args, **kwargs):
            # "worker khác" nổ alert cho b NGAY TRƯỚC khi bước resolve của a chạy.
            engine._fire_alert(device, rule_, 0.0, interface=b)
            return original(device, rule_, *args, **kwargs)

        mocker.patch.object(engine, "_resolve_alert", side_effect=resolve_then_other_worker_creates_b)
        evaluate(dev)

        alert_a.refresh_from_db()
        assert alert_a.is_active is False
        assert alert_of(dev, rule, b).is_active is True


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 10. Cô lập lỗi theo cổng; rule khác không đổi ngữ nghĩa
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestIsolation:
    def test_an_error_on_one_port_does_not_stop_the_others(self, history, device, peer, rule, mocker):
        a, b = port(device, "Gi0/1", 1), port(device, "Gi0/2", 2)
        link(device, a, peer)
        link(device, b, CiscoSNMPDeviceFactory())
        history(a, [(-1, "down")])
        history(b, [(-1, "down")])
        original = engine._fire_alert

        def flaky(device_, rule_, value, interface=None, **kwargs):
            if interface is not None and interface.pk == a.pk:
                raise RuntimeError("lỗi giả lập ở cổng a")
            return original(device_, rule_, value, interface=interface, **kwargs)

        mocker.patch.object(engine, "_fire_alert", side_effect=flaky)
        evaluate(device)
        assert list(Alert.objects.filter(device=device, rule=rule).values_list("interface_id", flat=True)) == [b.pk]

    def test_soft_time_limit_still_propagates_from_a_port(self, history, device, peer, rule, mocker):
        a = port(device, "Gi0/1", 1)
        link(device, a, peer)
        history(a, [(-1, "down")])
        mocker.patch.object(engine, "_fire_alert", side_effect=SoftTimeLimitExceeded())
        with pytest.raises(SoftTimeLimitExceeded):
            evaluate(device)

    def test_other_rules_still_have_no_interface_and_one_alert_per_device(self, device, mocker):
        r = AlertRule.objects.create(
            name="CPU high", device_type="all", metric="cpu_percent", condition="gt", threshold=90.0,
            severity="WARNING", channels=["telegram"], enabled=True,
        )
        mocker.patch("apps.alerts.engine.METRIC_GETTERS", {**engine.METRIC_GETTERS, "cpu_percent": lambda d, s: 99.0})
        evaluate(device)
        evaluate(device)
        alerts = Alert.objects.filter(device=device, rule=r)
        assert alerts.count() == 1
        a = alerts.get()
        assert a.interface_id is None and a.interface_name == "" and a.peer_name == ""
