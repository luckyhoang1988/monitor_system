"""Alert rule engine — đánh giá ngưỡng và tạo Alert record."""
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from apps.devices.models import Device
from apps.metrics import cache as metrics_cache
from . import cooldown
from .channels import ChannelTransientError, redact
from .models import AlertRule, Alert, AlertNotification, MonitoredUplink

logger = logging.getLogger(__name__)

# Metric nhị phân (0/1) — không áp dụng vùng đệm hysteresis.
BINARY_METRICS = {"if_status", "device_online"}
# Metric miễn trừ hysteresis: phục hồi ngay khi điều kiện hết đúng. Ngoài metric
# nhị phân còn có wifi_ap_offline (count, ngưỡng 0 → công thức hysteresis vô nghĩa).
NO_HYSTERESIS_METRICS = BINARY_METRICS | {"wifi_ap_offline"}

CONDITION_FN = {
    "gt":  lambda v, t: v > t,
    "lt":  lambda v, t: v < t,
    "gte": lambda v, t: v >= t,
    "lte": lambda v, t: v <= t,
    "eq":  lambda v, t: v == t,
    "ne":  lambda v, t: v != t,
}

METRIC_GETTERS = {
    "cpu_percent":       lambda device, since: _latest_cpu(device, since),
    "mem_percent":       lambda device, since: _latest_mem(device, since),
    "if_status":         lambda device, since: _check_if_status(device, since),
    "uplink_in_mbps_max":  lambda device, since: _uplink_traffic_max(device, since, direction="in"),
    "uplink_out_mbps_max": lambda device, since: _uplink_traffic_max(device, since, direction="out"),
    "vm_count_running":  lambda device, since: _count_vms_running(device, since),
    "vm_repl_unhealthy": lambda device, since: _count_vms_repl_unhealthy(device, since),
    "device_online":     lambda device, since: _device_online(device),
    "wifi_client_count": lambda device, since: _wifi_client_count(device, since),
    "wifi_ap_offline":   lambda device, since: _wifi_ap_offline_count(device, since),
}

SUSTAINABLE_METRICS = {"cpu_percent", "mem_percent"}

# HyperV host performance counters (Phase 1 MVP) — map metric key → (short-key ring-buffer,
# field name SystemHealth). Không tái dùng _sustained_cpu_mem (hardcode field cpu/mem).
_HOST_PERF_FIELD_MAP = {
    "cpu_hv_percent":        ("chv", "cpu_hv_percent"),
    "mem_available_mb":      ("mav", "mem_available_mb"),
    "disk_read_iops":        ("dri", "disk_read_iops"),
    "disk_write_iops":       ("dwi", "disk_write_iops"),
    "disk_read_latency_ms":  ("drl", "disk_read_latency_ms"),
    "disk_write_latency_ms": ("dwl", "disk_write_latency_ms"),
    "net_mbps_total":        ("nmb", "net_mbps_total"),
    "disk_read_throughput_mbps":  ("drt", "disk_read_throughput_mbps"),
    "disk_write_throughput_mbps": ("dwt", "disk_write_throughput_mbps"),
    "disk_queue_length":          ("dql", "disk_queue_length"),
    "avg_io_size_kb":             ("aio", "avg_io_size_kb"),
}

for _metric in _HOST_PERF_FIELD_MAP:
    METRIC_GETTERS[_metric] = (lambda device, since, m=_metric: _latest_host_perf(device, since, m))
del _metric

# iLO Redfish RAID/disk health (độc lập WinRM, model HardwareHealth riêng — KHÔNG có nhánh
# cache-mode, model này không đi qua METRICS_WRITE_MODE, xem CLAUDE.md "Phạm vi" mục iLO).
# 10 entry ilo_* (2026-09-29, mở rộng ngoài RAID: Battery/AMS/Processor/Memory/Fan/Temperature/
# PowerSupply/Redundancy + 3 bonus vòng 2 BIOS-Hardware/Network/FanRedundancy — CHỈ có dữ liệu
# trên host iLO5, None trên iLO4, xem apps/collectors/ilo_redfish.py) dùng CHUNG field name trên
# HardwareHealth — _latest_ilo/_sustained_ilo bên dưới đã generic theo field name nên không cần
# sửa gì thêm. power_redundancy_ok/fan_redundancy_ok là BooleanField: __isnull=False chỉ loại
# NULL thật (không loại False), float(True)=1.0/float(False)=0.0 qua values_list hoạt động đúng
# không cần nhánh riêng.
_ILO_FIELD_MAP = {
    "raid_controller_health":    "controller_health_code",
    "raid_logical_drive_health": "logical_drive_worst_code",
    "raid_missing_disk_count":   "missing_disk_count",
    "raid_enclosure_mismatch":   "enclosure_mismatch_count",
    "ilo_battery_health":        "battery_health_code",
    "ilo_processor_health":      "processor_health_code",
    "ilo_memory_health":         "memory_health_code",
    "ilo_fan_health":            "fan_worst_code",
    "ilo_temperature_health":    "temperature_worst_code",
    "ilo_power_supply_health":   "power_supply_worst_code",
    "ilo_power_redundancy":      "power_redundancy_ok",
    "ilo_bios_hardware_health":  "bios_hardware_health_code",
    "ilo_network_health":        "network_health_code",
    "ilo_fan_redundancy":        "fan_redundancy_ok",
}

for _metric in _ILO_FIELD_MAP:
    METRIC_GETTERS[_metric] = (lambda device, since, m=_metric: _latest_ilo(device, since, m))
del _metric


# ── Cache-first: khi METRICS_WRITE_MODE="cache", getters đọc từ Redis thay vì DB ──
def _use_cache() -> bool:
    return metrics_cache.is_cache_mode()


def _fresh_latest(device: Device, since) -> dict | None:
    """Snapshot mới nhất từ cache nếu còn trong cửa sổ `since`, ngược lại None.

    Giữ đúng ngữ nghĩa DB (`timestamp__gte=since`): snapshot cũ hơn `since` bị bỏ
    → tránh cảnh báo trên số liệu ôi khi thiết bị ngừng poll.
    """
    snap = metrics_cache.get_latest(device.id)
    if not snap:
        return None
    if since is not None and snap.get("ts", 0) < since.timestamp():
        return None
    return snap


def _sustained_verdict(values: list, rule: AlertRule) -> float | None:
    """Logic sustained dùng chung: điều kiện phải đúng TRÊN TOÀN cửa sổ.

    gt/gte → min(values) vượt ngưỡng; lt/lte → max(values) dưới ngưỡng; eq/ne →
    xét giá trị mới nhất. Trả latest (để dựng message) nếu sustained, else None.
    """
    if not values:
        return None
    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition
    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None
    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _sustained_cpu_mem(device: Device, rule: AlertRule, window_since) -> float | None:
    """Evaluate sustained condition for CPU/MEM over a time window.

    If rule.duration_min > 0, we require the condition to hold for the whole window.
    Returns the latest value (for messaging) if sustained, else None.
    """
    if _use_cache():
        field = "cpu" if rule.metric == "cpu_percent" else "mem"
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [s[field] for s in series if s.get(field) is not None]
        if rule.metric == "mem_percent":
            values = [v for v in values if v]  # bỏ sentinel mem==0
        return _sustained_verdict(values, rule)

    from apps.metrics.models import SystemHealth

    qs = (SystemHealth.objects
          .filter(device=device, timestamp__gte=window_since)
          .order_by("timestamp")
          .values_list(rule.metric, flat=True))
    values = list(qs)
    if rule.metric == "mem_percent":
        # Loại mẫu mem == 0 (sentinel "không đo được") trước khi đánh giá sustained —
        # tránh rule lt/lte fire giả trên thiết bị không expose mem qua SNMP.
        values = [v for v in values if v]
    if not values:
        return None

    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    # For eq/ne, fall back to latest-only.
    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _latest_host_perf(device: Device, since, metric: str) -> float | None:
    """Latest value cho 1 trong 7 HyperV host performance counter (Phase 1 MVP)."""
    short_key, field_name = _HOST_PERF_FIELD_MAP[metric]
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        val = snap.get(field_name)
        return float(val) if val is not None else None

    from apps.metrics.models import SystemHealth
    rec = (SystemHealth.objects
           .filter(device=device, timestamp__gte=since, **{f"{field_name}__isnull": False})
           .order_by("-timestamp")
           .values_list(field_name, flat=True)
           .first())
    return float(rec) if rec is not None else None


def _sustained_host_perf(device: Device, rule: AlertRule, window_since) -> float | None:
    """Sustained version cho 7 HyperV host performance counter — tái dùng _sustained_verdict."""
    short_key, field_name = _HOST_PERF_FIELD_MAP[rule.metric]
    if _use_cache():
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [float(s[short_key]) for s in series if s.get(short_key) is not None]
        return _sustained_verdict(values, rule)

    from apps.metrics.models import SystemHealth
    qs = (SystemHealth.objects
          .filter(device=device, timestamp__gte=window_since, **{f"{field_name}__isnull": False})
          .order_by("timestamp")
          .values_list(field_name, flat=True))
    values = [float(v) for v in qs]
    return _sustained_verdict(values, rule)


def _latest_ilo(device: Device, since, metric: str) -> float | None:
    """Latest value cho iLO RAID/disk health — luôn đọc DB trực tiếp (HardwareHealth không có
    nhánh cache-mode, ghi thẳng Postgres mỗi poll, xem apps/collectors/tasks.py poll_all_ilo)."""
    field_name = _ILO_FIELD_MAP[metric]
    from apps.metrics.models import HardwareHealth
    rec = (HardwareHealth.objects
           .filter(device=device, timestamp__gte=since, **{f"{field_name}__isnull": False})
           .order_by("-timestamp")
           .values_list(field_name, flat=True)
           .first())
    return float(rec) if rec is not None else None


def _sustained_ilo(device: Device, rule: AlertRule, window_since) -> float | None:
    """Sustained version cho iLO RAID/disk health — tái dùng _sustained_verdict. State phần
    cứng rời rạc (không noisy) nên seed rule dùng duration_min=0 (xem seed_alert_rules.py),
    hàm này chỉ giữ nhất quán API nếu ai đổi duration_min>0 qua UI."""
    field_name = _ILO_FIELD_MAP[rule.metric]
    from apps.metrics.models import HardwareHealth
    qs = (HardwareHealth.objects
          .filter(device=device, timestamp__gte=window_since, **{f"{field_name}__isnull": False})
          .order_by("timestamp")
          .values_list(field_name, flat=True))
    values = [float(v) for v in qs]
    return _sustained_verdict(values, rule)


def _latest_cpu(device: Device, since) -> float | None:
    if _use_cache():
        snap = _fresh_latest(device, since)
        return snap.get("cpu") if snap else None
    from apps.metrics.models import SystemHealth
    rec = SystemHealth.objects.filter(device=device, timestamp__gte=since).order_by("-timestamp").first()
    return rec.cpu_percent if rec else None


def _latest_mem(device: Device, since) -> float | None:
    if _use_cache():
        snap = _fresh_latest(device, since)
        # mem == 0 là sentinel "không đo được" → bỏ qua (xem chú thích DB path bên dưới).
        return (snap.get("mem") or None) if snap else None
    from apps.metrics.models import SystemHealth
    rec = SystemHealth.objects.filter(device=device, timestamp__gte=since).order_by("-timestamp").first()
    if rec is None:
        return None
    # mem_percent == 0 là sentinel "không đo được" (Cisco Business/SMB không expose mem
    # qua SNMP, hoặc walk rỗng) — KHÔNG phải mem thật 0% → bỏ qua, tránh rule lt fire giả.
    if not rec.mem_percent:
        return None
    return rec.mem_percent


@dataclass(frozen=True)
class UplinkScope:
    """Kết quả `_sync_uplink_scope`: các cổng đang được giám sát, tên `uplink_ports` không khớp cổng nào, và tên
    khớp NHIỀU cổng (mơ hồ — không tự chọn) kèm tên các cổng ứng viên."""
    interface_ids: tuple[int, ...]
    unmatched_manual: tuple[str, ...] = ()
    ambiguous_manual: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _port_key(name) -> str:
    return (name or "").strip().casefold()


# Tên viết tắt có BẰNG CHỨNG trên prod (đo 2026-10-15: `uplink_ports` ghi `Gi1/0/20`, `XGE0/0/1` còn cổng là
# `GigabitEthernet1/0/20`, `XGigabitEthernet0/0/1`). Cố ý chỉ 2 cặp: `GE0/0/25` cũng có trên prod nhưng không có
# bằng chứng nó ứng với cổng nào, và đoán tên cổng là cách thiết bị bị giám sát nhầm cổng mà không ai biết.
_PORT_ALIAS_RE = re.compile(r"^(xgigabitethernet|xge|gigabitethernet|gi)(?=\d)")
_PORT_ALIAS_FULL = {"xge": "xgigabitethernet", "gi": "gigabitethernet"}


def _port_alias_key(name) -> str:
    """Khoá so khớp có xét viết tắt: `Gi`→`GigabitEthernet`, `XGE`→`XGigabitEthernet`, phần còn lại (toàn bộ số
    cổng/stack/slot) giữ NGUYÊN — `Gi1/0/2` không bao giờ trùng `GigabitEthernet1/0/20`."""
    key = _port_key(name)
    m = _PORT_ALIAS_RE.match(key)
    if not m:
        return key
    prefix = m.group(1)
    return _PORT_ALIAS_FULL.get(prefix, prefix) + key[m.end():]


def _manual_port_names(value) -> list[str]:
    """Tên cổng trong `Device.uplink_ports`, đã làm phẳng và bỏ ký tự thừa.

    Dữ liệu thật trên prod (đo 2026-10-15) có cả `"['Gi1/0/20']"` (list bị lưu thành 1 chuỗi), `'[]'` và
    `"['GE0/0/25']"` lồng trong list — nên không thể so khớp thẳng từng phần tử."""
    names: list[str] = []

    def walk(v) -> None:
        if v is None:
            return
        if isinstance(v, (list, tuple, set)):
            for item in v:
                walk(item)
            return
        for part in re.split(r"[,;]", str(v)):
            name = part.strip().strip("[]()'\"").strip()
            if name:
                names.append(name)

    walk(value)
    return names


def _sync_uplink_scope(device: Device) -> UplinkScope:
    """Xác lập + trả phạm vi giám sát cổng uplink của `device` (bảng `MonitoredUplink`).

    Nguồn: (1) cổng local của link switch↔switch ĐANG CÒN MỚI (`is_stale=False`) — được ghim và GIỮ LẠI
    kể cả khi link down hay chuyển stale; (2) tên trong `Device.uplink_ports`. Link stale mà chưa từng được
    thấy còn mới KHÔNG được đưa vào. Dòng `manual` bị bỏ khi tên không còn trong `uplink_ports` (trừ khi cổng
    cũng có link topology). `Interface.port_mode`/`is_uplink` KHÔNG tham gia — trunk không tự là đối tượng
    cảnh báo.

    Tên thủ công: khớp chính xác (không phân biệt hoa/thường) luôn được ưu tiên; không có thì thử viết tắt
    (`_port_alias_key`, chỉ Gi/GigabitEthernet và XGE/XGigabitEthernet) và chỉ nhận khi ra ĐÚNG MỘT cổng —
    nhiều cổng ứng viên là mơ hồ (WARNING nêu tên, không tự chọn).

    Gọi mỗi lượt đánh giá (≈3 truy vấn đọc); chỉ ghi khi phạm vi đổi."""
    from apps.devices.models import Interface, TopologyLink

    rows = list(Interface.objects.filter(device=device).values_list("pk", "name"))
    by_key = {_port_key(name): pk for pk, name in rows}
    valid = set(by_key.values())
    exact_ids: dict[str, set[int]] = {}
    alias_ids: dict[str, set[int]] = {}
    for pk, name in rows:
        exact_ids.setdefault(_port_key(name), set()).add(pk)
        alias_ids.setdefault(_port_alias_key(name), set()).add(pk)
    name_of = dict(rows)

    from_topology: set[int] = set()
    for iface_id, port in TopologyLink.objects.filter(
        local_device=device, link_kind="switch", is_stale=False,
    ).values_list("local_interface_id", "local_port"):
        pk = iface_id if iface_id in valid else by_key.get(_port_key(port))
        if pk is not None:
            from_topology.add(pk)

    from_manual: set[int] = set()
    unmatched: list[str] = []
    ambiguous: list[tuple[str, tuple[str, ...]]] = []
    for name in _manual_port_names(device.uplink_ports):
        candidates = exact_ids.get(_port_key(name)) or alias_ids.get(_port_alias_key(name)) or set()
        if len(candidates) == 1:
            from_manual.add(next(iter(candidates)))
        elif candidates:
            ambiguous.append((name, tuple(sorted(name_of[pk] for pk in candidates))))
        else:
            unmatched.append(name)

    pins = dict(MonitoredUplink.objects.filter(device=device).values_list("interface_id", "source"))

    new_rows = []
    for pk in sorted(from_topology | from_manual):
        if pk not in pins:
            source = MonitoredUplink.SOURCE_TOPOLOGY if pk in from_topology else MonitoredUplink.SOURCE_MANUAL
            new_rows.append(MonitoredUplink(device=device, interface_id=pk, source=source))
            pins[pk] = source
    if new_rows:
        # ignore_conflicts: 2 lượt đánh giá đồng thời cùng ghim 1 cổng (unique theo interface) không được ném lỗi.
        MonitoredUplink.objects.bulk_create(new_rows, ignore_conflicts=True)

    upgrade = [pk for pk, src in pins.items()
               if src == MonitoredUplink.SOURCE_MANUAL and pk in from_topology]
    if upgrade:
        MonitoredUplink.objects.filter(device=device, interface_id__in=upgrade).update(
            source=MonitoredUplink.SOURCE_TOPOLOGY)
        for pk in upgrade:
            pins[pk] = MonitoredUplink.SOURCE_TOPOLOGY

    drop = [pk for pk, src in pins.items()
            if src == MonitoredUplink.SOURCE_MANUAL and pk not in from_manual and pk not in from_topology]
    if drop:
        MonitoredUplink.objects.filter(device=device, interface_id__in=drop).delete()
        for pk in drop:
            pins.pop(pk, None)

    return UplinkScope(tuple(sorted(pins)), tuple(unmatched), tuple(ambiguous))


def _uplink_statuses(device: Device, interface_ids, since) -> dict[int, str | None]:
    """Trạng thái MỚI NHẤT (trong cửa sổ `since`) của từng cổng; `None` = không có dữ liệu."""
    ids = list(interface_ids)
    if _use_cache():
        snap = _fresh_latest(device, since)
        ifs = (snap or {}).get("interfaces") or {}
        return {pk: (ifs.get(str(pk)) or {}).get("status") for pk in ids}

    from apps.devices.models import Interface
    from apps.metrics.models import InterfaceStats
    from django.db.models import OuterRef, Subquery

    # 1 truy vấn: trạng thái mới nhất của mọi cổng (thay vì N truy vấn).
    latest_sq = (InterfaceStats.objects
                 .filter(interface=OuterRef("pk"), timestamp__gte=since)
                 .order_by("-timestamp")
                 .values("status")[:1])
    got = {i.pk: i.latest_status
           for i in Interface.objects.filter(pk__in=ids).annotate(latest_status=Subquery(latest_sq))}
    return {pk: got.get(pk) for pk in ids}


def _reduce_if_status(statuses: dict[int, str | None]) -> float | None:
    """0.0 nếu có cổng đã BIẾT là không up; None nếu chưa đủ dữ liệu; 1.0 nếu mọi cổng đều up.

    Cổng không up thắng cổng thiếu dữ liệu: một cổng đã down thì phải báo, dù cổng khác chưa có trạng thái
    (bản cũ trả None ngay khi gặp cổng thiếu đầu tiên, nên che mất cổng down nằm phía sau)."""
    if any(s is not None and s != "up" for s in statuses.values()):
        return 0.0
    if any(s is None for s in statuses.values()):
        return None
    return 1.0


def _check_if_status(device: Device, since, interface_ids=None) -> float | None:
    """0.0 nếu có cổng giám sát nào DOWN, 1.0 nếu tất cả UP, None nếu chưa xác định phạm vi / thiếu dữ liệu."""
    ids = list(interface_ids) if interface_ids is not None else list(_sync_uplink_scope(device).interface_ids)
    if not ids:
        return None
    return _reduce_if_status(_uplink_statuses(device, ids, since))


def _port_samples(interface_id: int, window_since) -> list[tuple]:
    """Mẫu trạng thái (thời điểm, status) của 1 cổng trong cửa sổ, cũ → mới.

    Cùng nguồn dữ liệu với `_uplink_statuses`: cache-mode đọc if-series Redis, DB-mode đọc `InterfaceStats`."""
    if _use_cache():
        return [
            (metrics_cache.epoch_to_dt(s.get("ts", 0)), s.get("status"))
            for s in metrics_cache.get_if_series(interface_id, window_since)
        ]
    from apps.metrics.models import InterfaceStats

    return list(
        InterfaceStats.objects.filter(interface_id=interface_id, timestamp__gte=window_since)
        .order_by("timestamp")
        .values_list("timestamp", "status")
    )


def _port_down_throughout(device: Device, interface_id: int, window_since) -> bool:
    """True khi cổng KHÔNG up trong MỌI mẫu của cửa sổ `duration_min` VÀ dữ liệu phủ đủ cửa sổ.

    Điều kiện FIRE của rule `if_status` có `duration_min>0` (thiết kế B). Bản trước (`_sustained_if_status`)
    trả DOWN ngay khi có BẤT KỲ mẫu không-up nào trong cửa sổ, nên cổng vừa rớt 1 phút — hoặc đã up lại —
    vẫn nổ. Nay:
      * mọi mẫu trong cửa sổ phải khác "up" (mẫu thiếu status không phải bằng chứng "down");
      * mẫu CŨ NHẤT không được cách đầu cửa sổ quá một chu kỳ poll (`ALERT_GRACE_PERIOD_SECS`): cổng mới
        được thấy down gần đây, hoặc Redis vừa bị xoá, thì chưa có bằng chứng nó đã down đủ `duration_min`;
      * mẫu MỚI NHẤT phải còn mới (poll không kẹt).
    Hồi phục KHÔNG dùng hàm này: chỉ cần trạng thái mới nhất là "up" (xem `_evaluate_uplink_port`)."""
    samples = _port_samples(interface_id, window_since)
    if not samples:
        return False
    if any(status is None or status == "up" for _, status in samples):
        return False
    grace_secs = int(getattr(settings, "ALERT_GRACE_PERIOD_SECS", 135))
    if samples[0][0] > window_since + timedelta(seconds=grace_secs):
        return False
    fresh_secs = max(grace_secs, int(getattr(device, "collect_interval", 300) or 300) * 2)
    return samples[-1][0] >= timezone.now() - timedelta(seconds=fresh_secs)


def _peer_name_for_port(device: Device, interface) -> str:
    """Tên thiết bị đối diện của `interface`; "" nếu không biết CHẮC.

    Chỉ khi có link switch còn MỚI khớp ĐÚNG cổng (id interface, hoặc tên không phân biệt hoa/thường) và các
    link khớp trỏ tới đúng MỘT thiết bị. Không suy từ link của cổng/thiết bị khác, không đoán: link được lưu ở
    phía CHA nên cổng phía lá (đối diện ở phía cha) thường không có tên — vẫn cảnh báo đầy đủ."""
    from apps.devices.models import TopologyLink

    key = _port_key(interface.name)
    names = {
        remote
        for local_interface_id, local_port, remote in TopologyLink.objects.filter(
            local_device=device, link_kind="switch", is_stale=False, remote_device__isnull=False,
        ).values_list("local_interface_id", "local_port", "remote_device__name")
        if local_interface_id == interface.pk or _port_key(local_port) == key
    }
    return next(iter(names)) if len(names) == 1 else ""


def _active_alert_for(device: Device, rule: AlertRule, interface) -> Alert | None:
    """Alert đang active của (thiết bị, rule, cổng). `interface=None` = alert cấp thiết bị (mọi rule khác)."""
    return Alert.objects.filter(device=device, rule=rule, is_active=True, interface=interface).first()


def _close_alert_without_recovery(alert: Alert, *, reason: str, note: str, label: str) -> None:
    """Đóng 1 alert `if_status` mà KHÔNG phải vì sự cố đã hồi phục. Gọi dưới khoá Device.

    KHÔNG ghi AlertNotification, KHÔNG gửi RECOVERED: chưa có dữ liệu nào cho thấy sự cố đã hết.
    `closed_reason` giữ cho late-recovery (`_queue_late_recovery_if_resolved`) bỏ qua alert này."""
    now = timezone.now()
    stamp = timezone.localtime(now).strftime("%H:%M %d/%m/%Y")
    Alert.objects.filter(pk=alert.pk).update(
        is_active=False, resolved_at=now, closed_reason=reason,
        message=f"{alert.message} [{note} lúc {stamp} — không phải đã hồi phục]",
    )
    logger.info("ALERT closed (%s): %s — alert #%s", label, alert.device.name, alert.pk)


def _supersede_device_level_alerts(device: Device, rule: AlertRule) -> int:
    """Đóng `superseded` (không RECOVERED) mọi alert `if_status` CẤP THIẾT BỊ còn active (`interface` NULL).

    Từ thiết kế B mỗi cổng có alert riêng. Alert cấp thiết bị kiểu cũ không còn đúng mô hình: không biết cổng
    nào, không hồi phục từng phần được. Đóng nó rồi để từng cổng được đánh giá bằng DỮ LIỆU HIỆN TẠI + điều kiện
    sustain của rule — chỉ cổng thực sự lỗi mới nổ alert mới (sự cố đang kéo dài được thông báo lại 1 lần,
    có tên cổng). Gọi SAU khi phạm vi đã xác định (phạm vi rỗng → không đụng gì, như hướng A)."""
    if not Alert.objects.filter(device=device, rule=rule, is_active=True, interface__isnull=True).exists():
        return 0
    closed = 0
    with transaction.atomic():
        Device.objects.select_for_update().filter(pk=device.pk).first()
        for alert in Alert.objects.select_for_update().filter(
            device=device, rule=rule, is_active=True, interface__isnull=True,
        ):
            _close_alert_without_recovery(
                alert, reason=Alert.CLOSED_SUPERSEDED,
                note="Đã đóng vì được thay bằng cảnh báo theo từng cổng", label="thay bằng alert theo cổng",
            )
            closed += 1
    return closed


def _close_port_alerts_out_of_scope(device: Device, rule: AlertRule, monitored_ids, observed: dict) -> int:
    """Đóng `scope_changed` (không RECOVERED) alert theo cổng mà cổng đó không còn trong phạm vi giám sát.

    Chỉ áp cho alert ĐÃ THẤY (`observed` = {pk alert: pk cổng}, chụp trước khi đọc trạng thái) và đọc lại dưới
    khoá Device — alert do worker khác tạo sau đó dựa trên dữ liệu mới hơn nên không thuộc quyết định này."""
    monitored = set(monitored_ids)
    left = [pk for pk, port_id in observed.items() if port_id not in monitored]
    if not left:
        return 0
    closed = 0
    with transaction.atomic():
        Device.objects.select_for_update().filter(pk=device.pk).first()
        for alert in Alert.objects.select_for_update().filter(
            pk__in=left, device=device, rule=rule, is_active=True,
        ):
            if alert.interface_id in monitored:
                continue
            _close_alert_without_recovery(
                alert, reason=Alert.CLOSED_SCOPE_CHANGED,
                note="Đã đóng vì thay đổi phạm vi giám sát cổng uplink", label="thay đổi phạm vi giám sát",
            )
            closed += 1
    return closed


def _evaluate_uplink_port(
    device: Device, rule: AlertRule, iface, status: str | None, window_since, observed: dict,
) -> None:
    """Quyết định fire/resolve cho MỘT cổng giám sát (độc lập với các cổng khác của thiết bị).

    * `status` = trạng thái MỚI NHẤT của cổng (None = chưa có dữ liệu → không fire, không resolve).
    * Hồi phục chỉ cần trạng thái mới nhất là "up" — không cần sustain.
    * Fire: không up; nếu rule có `duration_min>0` thì phải không-up LIÊN TỤC suốt cửa sổ (`_port_down_throughout`).
      Cổng đang down mà chưa đủ thời gian: giữ nguyên (alert đang active vẫn active, chưa nổ thêm)."""
    if status is None:
        return
    active_pks = {pk for pk, port_id in observed.items() if port_id == iface.pk}
    has_active = bool(active_pks)
    if status == "up":
        value = 1.0
    else:
        if window_since is not None and not _port_down_throughout(device, iface.pk, window_since):
            return
        value = 0.0
    action = _decide_transition(rule, value, has_active)
    if action == "fire":
        _fire_alert(device, rule, value, interface=iface)
    elif action == "resolve":
        # Chỉ resolve alert ĐÃ THẤY lúc quyết định: alert nổ sau đó (worker khác, dữ liệu mới hơn) để vòng sau.
        _resolve_alert(device, rule, interface=iface, only_if=lambda a: a.pk in active_pks)


def _evaluate_if_status(device: Device, rule: AlertRule, since) -> None:
    """Đánh giá rule `if_status` TỪNG CỔNG trong phạm vi giám sát (topology switch↔switch + `uplink_ports`).

    Thiết kế B (2026-10-16): mỗi cổng giám sát có alert và vòng đời ALERT → RECOVERED riêng. Phạm vi vẫn do
    `_sync_uplink_scope` (hướng A) xác lập; khác hướng A ở chỗ KHÔNG gộp mọi cổng của thiết bị thành 1 alert:
      * cổng thứ hai rớt = alert + thông báo riêng; từng cổng hồi phục riêng (RECOVERED nêu tên cổng);
      * cổng thiếu dữ liệu không fire/resolve và không che cổng down khác;
      * chống flapping/nhắc-lại tính theo từng cổng; ràng buộc DB duy nhất cho (thiết bị, rule, cổng) active;
      * phạm vi chưa xác định → WARNING và KHÔNG làm gì (kể cả không đóng alert cũ);
      * alert cấp thiết bị kiểu cũ bị đóng `superseded` (không RECOVERED) rồi từng cổng được đánh giá lại;
      * alert của cổng rời phạm vi → đóng `scope_changed` (không RECOVERED).
    Lỗi ở 1 cổng được cô lập (log kèm traceback), `SoftTimeLimitExceeded` vẫn nổi lên."""
    from apps.devices.models import Interface

    scope = _sync_uplink_scope(device)
    if scope.unmatched_manual:
        logger.warning(
            "Switch %s: uplink_ports có tên không khớp cổng nào của thiết bị (kiểm tra tên cổng): %s",
            device.name, ", ".join(scope.unmatched_manual),
        )
    for name, candidates in scope.ambiguous_manual:
        logger.warning(
            "Switch %s: uplink_ports '%s' khớp nhiều cổng (%s) — mơ hồ nên không tự chọn; ghi đúng tên cổng",
            device.name, name, ", ".join(candidates),
        )
    ids = list(scope.interface_ids)
    if not ids:
        logger.warning(
            "Rule '%s' (id=%s) — %s: chưa xác định được cổng uplink cần giám sát (chưa có link switch↔switch "
            "còn mới và uplink_ports không khớp cổng nào) — chưa đủ dữ liệu, không fire/resolve",
            rule.name, rule.pk, device.name,
        )
        return

    _supersede_device_level_alerts(device, rule)

    # Ảnh chụp alert theo cổng đang active, đọc TRƯỚC trạng thái cổng: mọi quyết định đóng/resolve bên dưới
    # chỉ áp cho đúng những alert này (xem `_close_port_alerts_out_of_scope`, `_evaluate_uplink_port`).
    observed = dict(
        Alert.objects.filter(device=device, rule=rule, is_active=True, interface__isnull=False)
        .values_list("pk", "interface_id")
    )

    statuses = _uplink_statuses(device, ids, since)

    # Thiếu trạng thái của MỘT PHẦN cổng giám sát (còn cổng khác có): thường là cổng đã ghim không còn trong
    # snapshot (tháo module, đổi tên) — nếu không nêu ra, cổng đó im lặng mãi. Thiếu TOÀN BỘ là thiết bị không
    # trả dữ liệu (đã có alert offline) nên không ồn thêm ở đây.
    missing = [pk for pk, s in statuses.items() if s is None]
    if missing and len(missing) < len(ids):
        names = sorted(Interface.objects.filter(pk__in=missing).values_list("name", flat=True))
        logger.warning(
            "Rule '%s' (id=%s) — %s: %d/%d cổng giám sát chưa có trạng thái (%s) — chưa đủ dữ liệu cho cổng đó; "
            "nếu cổng không còn tồn tại, bỏ giám sát bằng cách xoá dòng trong 'Cổng uplink giám sát' (admin)",
            rule.name, rule.pk, device.name, len(missing), len(ids), ", ".join(names),
        )

    window_since = None
    if rule.duration_min and rule.duration_min > 0:
        window_since = timezone.now() - timedelta(minutes=int(rule.duration_min))

    interfaces = {i.pk: i for i in Interface.objects.filter(pk__in=ids)}
    for pk in ids:
        iface = interfaces.get(pk)
        if iface is None:
            continue
        try:
            _evaluate_uplink_port(device, rule, iface, statuses.get(pk), window_since, observed)
        except SoftTimeLimitExceeded:
            # Kế thừa `Exception` (celery) — nuốt nó thì task poll không bao giờ dừng đúng hạn (bẫy A7/A9).
            raise
        except Exception:
            logger.exception(
                "Rule '%s' (id=%s) — %s: lỗi khi đánh giá cổng %s — bỏ qua cổng này, các cổng còn lại vẫn được xét",
                rule.name, rule.pk, device.name, iface.name,
            )

    _close_port_alerts_out_of_scope(device, rule, ids, observed)


def _uplink_traffic_max(device: Device, since, direction: str) -> float | None:
    """Return max IN/OUT Mbps among uplink interfaces since time."""
    from apps.devices.models import Interface

    uplink_ids = list(Interface.objects.filter(device=device, is_uplink=True).values_list("pk", flat=True))
    if not uplink_ids:
        return None

    field = "in_mbps" if direction == "in" else "out_mbps"

    if _use_cache():
        peak = None
        for uid in uplink_ids:
            for s in metrics_cache.get_if_series(uid, since):
                v = s.get(field)
                if v is not None:
                    peak = float(v) if peak is None else max(peak, float(v))
        return peak

    from apps.metrics.models import InterfaceStats
    qs = (InterfaceStats.objects
          .filter(interface_id__in=uplink_ids, timestamp__gte=since)
          .order_by(f"-{field}")
          .values_list(field, flat=True))
    val = qs.first()
    return float(val) if val is not None else None


def _sustained_uplink_traffic_max(device: Device, rule: AlertRule, window_since) -> float | None:
    """Sustained version of uplink traffic max.

    We compute 'max uplink Mbps' per poll-snapshot timestamp, then require the condition
    to hold for all snapshots in the window.
    """
    from apps.devices.models import Interface

    uplink_ids = list(Interface.objects.filter(device=device, is_uplink=True).values_list("pk", flat=True))
    if not uplink_ids:
        return None

    field = "in_mbps" if rule.metric == "uplink_in_mbps_max" else "out_mbps"

    if _use_cache():
        per_ts: dict = {}
        for uid in uplink_ids:
            for s in metrics_cache.get_if_series(uid, window_since):
                ts = s.get("ts")
                per_ts[ts] = max(per_ts.get(ts, 0.0), float(s.get(field) or 0.0))
        values = [per_ts[ts] for ts in sorted(per_ts)]
        return _sustained_verdict(values, rule)

    from django.db.models import Max
    from apps.metrics.models import InterfaceStats
    # 1 query: gom max(field) theo từng snapshot timestamp (thay vòng lặp N query).
    rows = (InterfaceStats.objects
            .filter(interface_id__in=uplink_ids, timestamp__gte=window_since)
            .values("timestamp")
            .annotate(m=Max(field))
            .order_by("timestamp"))
    values: list[float] = [float(r["m"] or 0.0) for r in rows]
    if not values:
        return None

    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _sustained_vm_metric(device: Device, rule: AlertRule, window_since) -> float | None:
    """Evaluate sustained VM metrics across snapshots in window.

    VMStats are stored per VM with the same poll timestamp. We compute the metric per timestamp snapshot,
    then require the condition to hold for ALL snapshots in the window.
    Returns latest snapshot value (for messaging) if sustained, else None.
    """
    if _use_cache():
        field = "vmr" if rule.metric == "vm_count_running" else "vmu"
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [float(s[field]) for s in series if field in s]
        return _sustained_verdict(values, rule)

    from django.db.models import Count
    from apps.metrics.models import VMStats

    # timestamps present in window (snapshots)
    timestamps = list(
        VMStats.objects.filter(device=device, timestamp__gte=window_since)
        .order_by("timestamp")
        .values_list("timestamp", flat=True)
        .distinct()
    )
    if not timestamps:
        return None

    # 1 query gom nhóm theo timestamp; snapshot không khớp điều kiện → count = 0.
    if rule.metric == "vm_count_running":
        rows = (VMStats.objects
                .filter(device=device, timestamp__gte=window_since, state="Running")
                .values("timestamp").annotate(c=Count("id")))
    elif rule.metric == "vm_repl_unhealthy":
        _HEALTHY = {"Normal", "NotConfigured"}
        rows = (VMStats.objects
                .filter(device=device, timestamp__gte=window_since)
                .exclude(repl_health__in=_HEALTHY)
                .values("timestamp").annotate(c=Count("id")))
    else:
        return None

    counts = {r["timestamp"]: r["c"] for r in rows}
    values: list[float] = [float(counts.get(ts, 0)) for ts in timestamps]
    if not values:
        return None

    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _count_vms_running(device: Device, since) -> float | None:
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        vms = snap.get("vms") or []
        if not vms:  # không có VM ghi nhận → None (đồng nhất DB path)
            return None
        return float(sum(1 for v in vms if v.get("state") == "Running"))
    from apps.metrics.models import VMStats
    latest = (VMStats.objects.filter(device=device, timestamp__gte=since)
              .order_by("-timestamp").values("timestamp").first())
    if not latest:
        return None
    return float(VMStats.objects.filter(
        device=device, timestamp=latest["timestamp"], state="Running"
    ).count())


def _count_vms_repl_unhealthy(device: Device, since) -> float | None:
    _HEALTHY = {"Normal", "NotConfigured"}
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        vms = snap.get("vms") or []
        if not vms:
            return None
        return float(sum(1 for v in vms if (v.get("repl_health") or "") not in _HEALTHY))
    from apps.metrics.models import VMStats
    latest = (VMStats.objects.filter(device=device, timestamp__gte=since)
              .order_by("-timestamp").values("timestamp").first())
    if not latest:
        return None
    return float(VMStats.objects.filter(
        device=device, timestamp=latest["timestamp"]
    ).exclude(repl_health__in=_HEALTHY).count())


def _device_online(device: Device) -> float:
    """1.0 nếu thiết bị online, 0.0 nếu offline — DÙNG CHO CẢNH BÁO.

    Dựa trên `is_online_for_alert` (mốc `last_ok_seen` + grace), KHÔNG dùng `is_online`
    (mốc `last_seen` bị xoá mỗi lần poll trượt). Nhờ đó 1 vòng poll lỗi tạm không bắn
    cảnh báo offline giả; chỉ báo khi mất tín hiệu thật vượt grace.
    """
    return 1.0 if device.is_online_for_alert else 0.0


def _sustained_device_online(device: Device, window_since) -> float | None:
    """Yêu cầu trạng thái offline duy trì trong cửa sổ duration_min.

    Dùng `last_ok_seen` (không bị xoá khi poll trượt) làm mốc, dự phòng `created_at`.
    - Còn trong grace → coi online (1.0).
    - Offline và mốc OK gần nhất đã cũ hơn cửa sổ → xác nhận offline (0.0).
    - Mới rớt, chưa đủ cửa sổ → None (bỏ qua, chờ thêm).
    """
    if device.is_online_for_alert:
        return 1.0
    ref = device.last_ok_seen or device.created_at
    if ref and ref < window_since:
        return 0.0
    return None


def _wifi_client_count_at_ts(device: Device, ts) -> float:
    """Tổng client tại 1 snapshot — fallback sum AP khi không có bảng STA."""
    from django.db.models import Sum
    from apps.metrics.models import WifiClientStats, WifiApStats

    cl_count = WifiClientStats.objects.filter(device=device, timestamp=ts).count()
    if cl_count > 0:
        return float(cl_count)
    total = (
        WifiApStats.objects.filter(device=device, timestamp=ts)
        .aggregate(s=Sum("client_count"))["s"]
    )
    return float(total or 0)


def _wifi_client_count(device: Device, since) -> float | None:
    """Tổng số client WiFi ở snapshot mới nhất của WLAN controller."""
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        clients = snap.get("wifi_clients")
        aps = snap.get("wifi_aps")
        if not clients and not aps:
            return None
        if clients:
            return float(len(clients))
        return float(sum(int(a.get("client_count") or 0) for a in (aps or [])))

    from apps.metrics.models import WifiClientStats, WifiApStats

    latest_ts = (WifiClientStats.objects
                 .filter(device=device, timestamp__gte=since)
                 .order_by("-timestamp")
                 .values_list("timestamp", flat=True)
                 .first())
    if latest_ts is None:
        latest_ts = (WifiApStats.objects
                     .filter(device=device, timestamp__gte=since)
                     .order_by("-timestamp")
                     .values_list("timestamp", flat=True)
                     .first())
    if latest_ts is None:
        return None
    return _wifi_client_count_at_ts(device, latest_ts)


def _sustained_wifi_client_count(device: Device, rule: AlertRule, window_since) -> float | None:
    if _use_cache():
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [float(s["wc"]) for s in series if "wc" in s]
        return _sustained_verdict(values, rule)

    from apps.metrics.models import WifiApStats

    timestamps = list(
        WifiApStats.objects.filter(device=device, timestamp__gte=window_since)
        .order_by("timestamp")
        .values_list("timestamp", flat=True)
        .distinct()
    )
    if not timestamps:
        return None

    values = [_wifi_client_count_at_ts(device, ts) for ts in timestamps]
    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _wifi_ap_offline_at_ts(device: Device, ts) -> float:
    from apps.metrics.models import WifiApStats
    return float(WifiApStats.objects.filter(
        device=device, timestamp=ts, is_online=False,
    ).count())


def _sustained_wifi_ap_offline_count(device: Device, rule: AlertRule, window_since) -> float | None:
    if _use_cache():
        series = metrics_cache.get_sys_series(device.id, window_since)
        values = [float(s["wao"]) for s in series if "wao" in s]
        return _sustained_verdict(values, rule)

    from apps.metrics.models import WifiApStats

    timestamps = list(
        WifiApStats.objects.filter(device=device, timestamp__gte=window_since)
        .order_by("timestamp")
        .values_list("timestamp", flat=True)
        .distinct()
    )
    if not timestamps:
        return None

    values = [_wifi_ap_offline_at_ts(device, ts) for ts in timestamps]
    latest = float(values[-1])
    threshold = float(rule.threshold)
    cond = rule.condition

    if cond in ("gt", "gte"):
        ok = (min(values) > threshold) if cond == "gt" else (min(values) >= threshold)
        return latest if ok else None
    if cond in ("lt", "lte"):
        ok = (max(values) < threshold) if cond == "lt" else (max(values) <= threshold)
        return latest if ok else None

    cond_fn = CONDITION_FN.get(cond)
    return latest if (cond_fn and cond_fn(latest, threshold)) else None


def _wifi_ap_offline_count(device: Device, since) -> float | None:
    """Số AP offline ở snapshot WifiApStats mới nhất của WLAN controller.

    Lọc theo `since` để khi AC mất kết nối (không có snapshot mới) → trả None
    (bỏ qua), tránh báo AP offline giả khi chính AC đang down (AC down đã có
    rule device_online riêng).
    """
    if _use_cache():
        snap = _fresh_latest(device, since)
        if not snap:
            return None
        aps = snap.get("wifi_aps")
        if not aps:  # AC không có AP ghi nhận → None (đồng nhất DB path)
            return None
        return float(sum(1 for a in aps if not a.get("is_online")))

    from apps.metrics.models import WifiApStats
    latest_ts = (WifiApStats.objects
                 .filter(device=device, timestamp__gte=since)
                 .order_by("-timestamp")
                 .values_list("timestamp", flat=True).first())
    if latest_ts is None:
        return None
    return float(WifiApStats.objects.filter(
        device=device, timestamp=latest_ts, is_online=False).count())


def _wifi_offline_ap_names(device: Device) -> list[str]:
    """Tên các AP đang offline ở snapshot WifiApStats mới nhất (cho message cảnh báo)."""
    if _use_cache():
        snap = metrics_cache.get_latest(device.id)
        if not snap:
            return []
        return sorted(
            str(a.get("name") or "")
            for a in (snap.get("wifi_aps") or [])
            if not a.get("is_online")
        )

    from apps.metrics.models import WifiApStats
    latest_ts = (WifiApStats.objects
                 .filter(device=device)
                 .order_by("-timestamp")
                 .values_list("timestamp", flat=True).first())
    if latest_ts is None:
        return []
    return list(WifiApStats.objects
                .filter(device=device, timestamp=latest_ts, is_online=False)
                .order_by("ap_name")
                .values_list("ap_name", flat=True))


def _recovered(rule: AlertRule, value: float) -> bool:
    """True nếu value đã ra khỏi vùng đệm hysteresis (đủ điều kiện phục hồi).

    - Metric nhị phân (if_status) và eq/ne: không có vùng đệm → phục hồi ngay.
    - gt/gte: phục hồi khi value < threshold * (1 - pct).
    - lt/lte: phục hồi khi value > threshold * (1 + pct).
    """
    if rule.metric in NO_HYSTERESIS_METRICS or rule.condition in ("eq", "ne"):
        return True
    pct = float(getattr(settings, "ALERT_HYSTERESIS_PCT", 0.1) or 0)
    t = float(rule.threshold)
    if rule.condition in ("gt", "gte"):
        return value < t * (1 - pct)
    if rule.condition in ("lt", "lte"):
        return value > t * (1 + pct)
    return True


def _decide_transition(rule: AlertRule, value: float, has_active: bool) -> str:
    """Quyết định hành động: 'fire' | 'resolve' | 'hold' | 'none'."""
    cond_fn = CONDITION_FN.get(rule.condition)
    if cond_fn and cond_fn(value, rule.threshold):
        return "fire"
    if not has_active:
        return "none"
    return "resolve" if _recovered(rule, value) else "hold"


def _is_flapping(device: Device, rule: AlertRule, interface=None) -> bool:
    """True nếu (device, rule[, cổng]) fire quá nhiều lần trong cửa sổ → nên chặn notification spam.

    `interface` (rule `if_status`, thiết kế B): đếm RIÊNG từng cổng — 1 cổng nhấp nháy không được làm chặn
    thông báo của cổng khác. `interface=None` = đếm alert cấp thiết bị (mọi rule khác), như trước."""
    window = int(getattr(settings, "ALERT_FLAP_WINDOW_MIN", 30))
    threshold = int(getattr(settings, "ALERT_FLAP_THRESHOLD", 4))
    if threshold <= 0:
        return False
    flap_since = timezone.now() - timedelta(minutes=window)
    recent_fires = Alert.objects.filter(
        device=device, rule=rule, interface=interface, triggered_at__gte=flap_since
    ).count()
    return recent_fires >= threshold


def _queue_renotify_if_suppressed(alert: Alert, rule: AlertRule) -> bool:
    """Alert ĐANG ACTIVE mà chưa từng có fire notification (bị flapping-suppress lúc fire) và đã kéo dài
    `ALERT_FLAP_RENOTIFY_MIN` phút → ghi fire notification `pending` cho từng channel. True nếu đã ghi.

    Hạng mục 1.1 (A1, 2026-10-15). Gốc: lần fire thứ N trong cửa sổ flapping tạo Alert nhưng không tạo
    notification (`_fire_alert`), các vòng sau thấy `is_active` nên im lặng cho tới khi tự hồi phục, và vì
    fire chưa từng gửi nên `_resolve_alert` cũng không gửi RECOVERED. Prod 2026-10-15: alert #662 "Thiết bị
    offline" active 19,7 ngày (thiết bị mất kết nối thật) không một thông báo nào.

    * Chỉ gọi từ `_fire_alert`, BÊN TRONG transaction đã khoá `Device`: "chưa có dòng fire" kiểm ở đây và
      dòng pending ghi ở đây cùng nằm dưới khoá đó, nên 2 đường eval đồng thời (poll inline + safety net)
      không nhắc đôi.
    * Trần = ĐÚNG 1 lần cho mỗi alert: điều kiện là "KHÔNG có dòng fire nào" nên sau khi ghi dòng pending
      (dù sau đó gửi thành công hay thất bại) sẽ không nhắc lại. Hết lượt gửi → xem hạng mục 1.2.
    * Đi qua outbox như fire thường: dòng `pending` sống sót khi worker chết (sweep nhặt), và
      `_finalize_fire_sent` + late-recovery sẵn có lo thứ tự ALERT → RECOVERED.
    * Chỉ nhắc alert KHÔNG có dòng fire nào. Alert có dòng fire `failed` (kênh lỗi, hết lượt) không thuộc
      hạng mục này — đó là chuyện gửi lại của 1.2 và nhắc lại ở đây chỉ tái hiện vòng lặp gửi thất bại.
    """
    minutes = int(getattr(settings, "ALERT_FLAP_RENOTIFY_MIN", 30))
    if minutes <= 0 or not rule.channels:
        return False
    lasted = timezone.now() - alert.triggered_at
    if lasted < timedelta(minutes=minutes):
        return False
    if AlertNotification.objects.filter(alert=alert, kind="fire").exists():
        return False

    window = int(getattr(settings, "ALERT_FLAP_WINDOW_MIN", 30))
    threshold = int(getattr(settings, "ALERT_FLAP_THRESHOLD", 4))
    # Số lần fire trong cửa sổ TÍNH ĐẾN lúc alert này nổ — đúng phép đếm `_is_flapping` đã dùng để chặn nó
    # (đếm cả chính nó). Không cần cận trên: alert còn active thì chưa thể có alert nổ sau nó (mỗi
    # (device, rule) chỉ 1 alert active, dưới khoá Device). Dùng cho tiền tố, không quyết định gửi.
    fires = Alert.objects.filter(
        device_id=alert.device_id, rule_id=alert.rule_id, interface_id=alert.interface_id,
        triggered_at__gte=alert.triggered_at - timedelta(minutes=window),
    ).count()
    elapsed = int(lasted.total_seconds() // 60)
    if threshold > 0 and fires >= threshold:
        prefix = (f"⚠️ FLAPPING ({fires} lần/{window} phút) — thông báo trước đó bị chặn, "
                  f"sự cố vẫn đang kéo dài {elapsed} phút. ")
    else:
        prefix = f"⚠️ Chưa từng có thông báo nào cho cảnh báo này — sự cố vẫn đang kéo dài {elapsed} phút. "
    message = prefix + alert.message
    Alert.objects.filter(pk=alert.pk).update(message=message)
    alert.message = message
    AlertNotification.objects.bulk_create([
        AlertNotification(alert=alert, channel=ch, kind="fire", status="pending")
        for ch in rule.channels
    ])
    return True


def check_device_alerts(device: Device, since) -> None:
    rules = AlertRule.objects.filter(enabled=True).filter(
        device_type__in=[device.device_type, "all"]
    )
    for rule in rules:
        # Cô lập lỗi THEO TỪNG RULE (hạng mục 1.8, 2026-10-15): trước đây 1 getter/_fire_alert/
        # _resolve_alert ném exception là cả vòng này dừng — mọi rule phía sau của thiết bị (kể cả
        # rule offline/RAID) không được xét trong chu kỳ đó, lặp lại mỗi vòng, và nơi gọi chỉ log 1
        # dòng không có traceback. Nay rule hỏng bị log kèm traceback rồi bỏ qua, rule còn lại chạy.
        try:
            _evaluate_rule(device, rule, since)
        except SoftTimeLimitExceeded:
            # Kế thừa `Exception` (celery.exceptions) nên `except Exception` bên dưới sẽ nuốt nó và
            # task poll không bao giờ dừng đúng hạn (cùng họ bug A7/A9) → phải bắt riêng và raise lại.
            raise
        except Exception:
            logger.exception(
                "Alert rule '%s' (id=%s, metric=%s) lỗi khi đánh giá cho %s — bỏ qua rule này, "
                "các rule còn lại của thiết bị vẫn được xét",
                rule.name, rule.pk, rule.metric, device.name,
            )


def _evaluate_rule(device: Device, rule: AlertRule, since) -> None:
    """Đánh giá 1 rule cho 1 thiết bị: đọc giá trị rồi quyết định fire / resolve / giữ nguyên."""
    getter = METRIC_GETTERS.get(rule.metric)
    if not getter:
        return

    if rule.metric == "if_status":
        # Có phạm vi giám sát riêng + đóng alert khi đổi phạm vi (không RECOVERED) — xem `_evaluate_if_status`.
        _evaluate_if_status(device, rule, since)
        return

    has_active = Alert.objects.filter(device=device, rule=rule, is_active=True).exists()

    # duration_min: if set, require condition to be sustained for the whole window.
    if rule.duration_min and rule.duration_min > 0:
        window_since = timezone.now() - timedelta(minutes=int(rule.duration_min))
        if rule.metric in SUSTAINABLE_METRICS:
            value = _sustained_cpu_mem(device, rule, window_since)
        elif rule.metric in ("uplink_in_mbps_max", "uplink_out_mbps_max"):
            value = _sustained_uplink_traffic_max(device, rule, window_since)
        elif rule.metric in ("vm_count_running", "vm_repl_unhealthy"):
            value = _sustained_vm_metric(device, rule, window_since)
        elif rule.metric == "device_online":
            value = _sustained_device_online(device, window_since)
        elif rule.metric == "wifi_client_count":
            value = _sustained_wifi_client_count(device, rule, window_since)
        elif rule.metric == "wifi_ap_offline":
            value = _sustained_wifi_ap_offline_count(device, rule, window_since)
        elif rule.metric in _HOST_PERF_FIELD_MAP:
            value = _sustained_host_perf(device, rule, window_since)
        elif rule.metric in _ILO_FIELD_MAP:
            value = _sustained_ilo(device, rule, window_since)
        else:
            value = getter(device, since)

        # ⚠️ 2026-09-28: các hàm `_sustained_*` dùng `_sustained_verdict` (mọi metric TRỪ
        # device_online/if_status — 2 metric đó tự trả 0.0/1.0 rõ ràng) trả về `None` bất
        # cứ khi nào điều kiện KHÔNG còn đúng suốt window — nhưng đó CHÍNH LÀ lúc metric đã
        # hồi phục. Trước đây `value is None` → `continue` thẳng, không bao giờ tới nhánh
        # "resolve" bên dưới → alert đã fire (duration_min>0) KHÔNG BAO GIỜ tự resolve được,
        # kẹt `is_active=True` vĩnh viễn dù metric đã về ngưỡng bình thường từ lâu (verify
        # runtime prod: 11/13 alert active thuộc nhóm rule này đã hồi phục thật từ nhiều
        # ngày/tháng trước nhưng chưa từng resolve — xem memory `alert-sustained-never-resolve.md`).
        # Fix: khi sustained=None mà đang có alert active, fallback đọc giá trị TỨC THỜI
        # (không sustain, đúng hàm `getter` dùng cho nhánh duration_min=0) để xét resolve qua
        # hysteresis — resolve KHÔNG cần sustain (giống cách device_online/if_status đã làm
        # đúng từ đầu: chỉ sustain lúc FIRE để lọc nhiễu, resolve thì tức thời ngay khi có bằng
        # chứng hồi phục mới nhất). Không đổi bán kính "fire" (vẫn phải sustain đủ window).
        if value is None:
            if has_active:
                instant = getter(device, since)
                if instant is not None and _decide_transition(rule, instant, has_active) == "resolve":
                    _resolve_alert(device, rule)
            return
    else:
        value = getter(device, since)

    if value is None:
        return

    action = _decide_transition(rule, value, has_active)
    if action == "fire":
        _fire_alert(device, rule, value)
    elif action == "resolve":
        _resolve_alert(device, rule)
    # "hold" (trong vùng đệm hysteresis) / "none" (không có alert active): không làm gì


def _persist_incident_snapshot(device: Device) -> None:
    """Cache-mode: ghi 1 SystemHealth từ snapshot Redis làm bằng chứng lúc alert fire.

    Nhờ đó sự cố có điểm dữ liệu CPU/mem trong Postgres (chart/điều tra sau này) dù
    metrics thường xuyên không còn ghi raw. Bỏ qua nếu đã có row cùng timestamp.
    """
    if not _use_cache():
        return
    snap = metrics_cache.get_latest(device.id)
    if not snap or snap.get("ts") is None:
        return
    from apps.metrics.models import SystemHealth, VolumeStats
    try:
        ts = metrics_cache.epoch_to_dt(snap["ts"])
        if not SystemHealth.objects.filter(device=device, timestamp=ts).exists():
            SystemHealth.objects.create(
                device=device,
                timestamp=ts,
                cpu_percent=snap.get("cpu") or 0,
                mem_percent=snap.get("mem") or 0,
                uptime_secs=snap.get("uptime"),
                extra=snap.get("extra") or {},
                cpu_hv_percent=snap.get("cpu_hv_percent"),
                mem_available_mb=snap.get("mem_available_mb"),
                disk_read_iops=snap.get("disk_read_iops"),
                disk_write_iops=snap.get("disk_write_iops"),
                disk_read_latency_ms=snap.get("disk_read_latency_ms"),
                disk_write_latency_ms=snap.get("disk_write_latency_ms"),
                net_mbps_total=snap.get("net_mbps_total"),
                disk_read_throughput_mbps=snap.get("disk_read_throughput_mbps"),
                disk_write_throughput_mbps=snap.get("disk_write_throughput_mbps"),
                disk_queue_length=snap.get("disk_queue_length"),
                avg_io_size_kb=snap.get("avg_io_size_kb"),
            )
        # Per-volume evidence (nếu host có volumes) — bằng chứng cho "VM nào bị ảnh hưởng"
        # tại đúng thời điểm alert fire. Bỏ qua nếu đã có row cùng timestamp.
        volumes = snap.get("volumes") or []
        if volumes and not VolumeStats.objects.filter(device=device, timestamp=ts).exists():
            VolumeStats.objects.bulk_create([
                VolumeStats(
                    device=device,
                    timestamp=ts,
                    volume_name=str(vol.get("name") or "")[:100],
                    read_iops=vol.get("read_iops"),
                    write_iops=vol.get("write_iops"),
                    read_mbps=vol.get("read_mbps"),
                    write_mbps=vol.get("write_mbps"),
                    read_latency_ms=vol.get("read_latency_ms"),
                    write_latency_ms=vol.get("write_latency_ms"),
                    queue_length=vol.get("queue_length"),
                    avg_io_size_kb=vol.get("avg_io_size_kb"),
                    current_queue_length=vol.get("current_queue_length"),
                    transfers_per_sec=vol.get("transfers_per_sec"),
                    split_io_per_sec=vol.get("split_io_per_sec"),
                    idle_time_percent=vol.get("idle_time_percent"),
                    vm_names=vol.get("vm_names") or [],
                )
                for vol in volumes
            ])
    except Exception as exc:
        logger.warning("persist incident snapshot (dev=%s) failed: %s", device.name, exc)


def _fire_alert(device: Device, rule: AlertRule, value: float, interface=None) -> None:
    """Tạo alert (+ ý định gửi) cho (thiết bị, rule[, cổng]). `interface` (Interface) chỉ dùng cho rule
    `if_status` (thiết kế B): alert theo từng cổng, mỗi cổng tối đa 1 alert active — khoá Device lo trường hợp
    thường, ràng buộc DB `uniq_active_alert_per_port` là lớp thứ hai. `interface=None` = hành vi cũ."""
    def _fmt_metric(metric: str, v: float) -> str:
        if metric in ("cpu_percent", "mem_percent"):
            return f"{v:.1f}%"
        if metric in ("uplink_in_mbps_max", "uplink_out_mbps_max"):
            return f"{v:.3f} Mbps"
        if metric in ("vm_count_running", "vm_repl_unhealthy", "wifi_client_count"):
            return f"{v:.0f}"
        if metric == "wifi_ap_offline":
            return f"{v:.0f} AP"
        if metric == "if_status":
            return "DOWN" if v == 0 else "UP"
        if metric == "device_online":
            return "OFFLINE" if v == 0 else "ONLINE"
        if metric == "cpu_hv_percent":
            return f"{v:.1f}%"
        if metric == "mem_available_mb":
            return f"{v:.0f} MB"
        if metric in ("disk_read_iops", "disk_write_iops"):
            return f"{v:.0f} IOPS"
        if metric in ("disk_read_latency_ms", "disk_write_latency_ms"):
            return f"{v:.1f} ms"
        if metric == "net_mbps_total":
            return f"{v:.1f} Mbps"
        if metric in ("disk_read_throughput_mbps", "disk_write_throughput_mbps"):
            return f"{v:.1f} MB/s"
        if metric == "disk_queue_length":
            return f"{v:.2f}"
        if metric == "avg_io_size_kb":
            return f"{v:.1f} KB"
        if metric in ("raid_controller_health", "raid_logical_drive_health",
                      "ilo_battery_health", "ilo_processor_health", "ilo_memory_health",
                      "ilo_fan_health", "ilo_temperature_health", "ilo_power_supply_health",
                      "ilo_bios_hardware_health", "ilo_network_health"):
            return AlertRule.HEALTH_CODE_NAMES.get(int(v), f"code={v:.0f}")
        if metric in ("raid_missing_disk_count", "raid_enclosure_mismatch"):
            return f"{v:.0f}"
        if metric in ("ilo_power_redundancy", "ilo_fan_redundancy"):
            return "OK" if v == 1 else "DEGRADED"
        return f"{v:.2f}"

    metric_value_str = _fmt_metric(rule.metric, float(value))
    threshold_str = _fmt_metric(rule.metric, float(rule.threshold))

    # Tên thiết bị đối diện được CHỤP Ở ĐÂY (lúc tạo alert) rồi lưu vào Alert.peer_name/message: outbox, retry
    # và mẫu RECOVERED chỉ đọc lại giá trị đã lưu, không tra topology lúc gửi.
    peer_name = ""
    if interface is not None:
        message = ""   # dựng ở nhánh TẠO MỚI bên dưới (cần tra topology — không làm mỗi vòng cho alert đã active)
    elif rule.metric == "wifi_ap_offline":
        names = _wifi_offline_ap_names(device)
        suffix = f" ({', '.join(names)})" if names else ""
        message = f"{device.name}: {metric_value_str} offline{suffix}"
    else:
        # metric_label (tên người đọc được, vd "RAID Controller Health (iLO)") thay vì rule.metric
        # thô (vd "raid_controller_health") — trước đây dùng raw key + _fmt_metric không có nhánh
        # raid_* nên message thực tế là "raid_controller_health = 2.00 (ngưỡng gte 2.00)", không
        # đọc được với người trực (phát hiện khi review lại alert iLO 2026-09-29).
        message = (f"{device.name}: {rule.metric_label} = {metric_value_str} "
                   f"(ngưỡng {rule.condition} {threshold_str})")

    # ⚠️ Transactional-outbox-lite (2026-09-28, theo báo cáo review — cùng root cause với
    # _resolve_alert bên dưới nên sửa đối xứng cả 2): ghi Alert + "ý định gửi" (AlertNotification
    # status="pending") CÙNG 1 transaction. Nếu worker chết NGAY SAU khi commit (trước khi kịp
    # gọi _dispatch_notifications thật bên dưới) thì DB vẫn có bằng chứng "còn nợ gửi" (row
    # pending) để task retry_pending_alert_notifications (định kỳ) nhặt lại — trước đây không
    # có bằng chứng nào, và vòng eval kế tiếp coi alert đã is_active=True là "đã xử lý xong" nên
    # không bao giờ tự gửi lại → mất trắng thông báo. is_flapping tính TRONG transaction (cùng
    # connection nên vẫn thấy `alert` vừa create dù chưa commit — read-committed) để quyết định
    # có tạo pending row hay không (flapping thì không notification lúc nổ — nhưng nếu alert đó VẪN
    # active sau ALERT_FLAP_RENOTIFY_MIN phút thì nhắc đúng 1 lần, xem _queue_renotify_if_suppressed).
    renotify: Alert | None = None
    with transaction.atomic():
        Device.objects.select_for_update().filter(pk=device.pk).first()
        active = _active_alert_for(device, rule, interface)
        if active is not None:
            # Đã có alert active → không tạo alert mới. Chỉ có việc phải làm nếu nó từng bị flapping-
            # suppress và nay đã kéo dài: ghi pending (cùng transaction, dưới khoá Device) rồi gửi bên dưới.
            if not _queue_renotify_if_suppressed(active, rule):
                return
            renotify = active
        else:
            # Hạng mục 1.4 (2026-10-15): `rule` được nạp lúc vòng eval bắt đầu — người dùng có thể đã TẮT
            # (hoặc xoá) rule rồi alert cũ của nó bị đóng (`close_alerts_for_disabled_rule`) trước khi tới
            # đây. Tạo alert mới cho rule đã tắt = alert không bao giờ được resolve (engine chỉ xét rule
            # enabled). Kiểm TRONG khoá Device, chỉ ở nhánh tạo mới (không tốn query ở nhánh alert đang active).
            if not AlertRule.objects.filter(pk=rule.pk, enabled=True).exists():
                return
            if interface is not None:
                peer_name = _peer_name_for_port(device, interface)
                message = f"{device.name}: cổng {interface.name} DOWN"
                if peer_name:
                    message += f" — nối tới {peer_name}"
            try:
                # Savepoint: vi phạm ràng buộc không làm hỏng transaction bên ngoài.
                with transaction.atomic():
                    alert = Alert.objects.create(
                        device=device,
                        rule=rule,
                        severity=rule.severity,
                        message=message,
                        metric_value=float(value),
                        is_active=True,
                        interface=interface,
                        interface_name=interface.name if interface is not None else "",
                        peer_name=peer_name,
                        # Giữ `down_interface_ids=[pk]` để engine bản A (khi rollback) vẫn hiểu alert này.
                        down_interface_ids=[interface.pk] if interface is not None else None,
                    )
            except IntegrityError:
                if interface is None:
                    raise
                # Đã có alert active cho cổng này (khoá Device lẽ ra đã chặn — đây là lớp phòng thủ thứ hai).
                logger.warning(
                    "ALERT theo cổng đã tồn tại, ràng buộc DB chặn bản trùng: %s cổng %s — bỏ qua",
                    device.name, interface.name,
                )
                return
            is_flapping = _is_flapping(device, rule, interface)
            if not is_flapping:
                AlertNotification.objects.bulk_create([
                    AlertNotification(alert=alert, channel=ch, kind="fire", status="pending")
                    for ch in rule.channels
                ])
    if renotify is not None:
        _dispatch_notifications(renotify, rule.channels, kind="fire")
        logger.warning("ALERT re-notified (từng bị flapping-suppress, vẫn active): %s", renotify.message)
        return
    # Cache-mode: lưu bằng chứng CPU/mem vào Postgres cho sự cố này.
    _persist_incident_snapshot(device)
    if is_flapping:
        logger.warning("ALERT flapping — bỏ qua notification: %s", alert.message)
    else:
        _dispatch_notifications(alert, rule.channels, kind="fire")
    logger.warning("ALERT fired: %s", alert.message)


def _resolve_alert(
    device: Device, rule: AlertRule, only_if: Callable[[Alert], bool] | None = None, interface=None,
) -> None:
    # `interface` (rule `if_status`, thiết kế B): chỉ resolve alert của ĐÚNG cổng đó; `None` = alert cấp thiết bị
    # (mọi rule khác) — không bao giờ chạm alert theo cổng của cổng khác.
    # `only_if(alert)` (tuỳ chọn, gọi dưới khoá Device với dòng vừa đọc lại): chỉ resolve alert thoả — người gọi
    # đã quyết định trên 1 tập alert cụ thể (xem `_evaluate_uplink_port`), alert xuất hiện/đổi sau đó thì không.
    #
    # ⚠️ Alert được eval từ CẢ inline sau mỗi poll (_poll_device_once) LẪN periodic
    # evaluate_alert_rules (safety net) — 2 đường có thể chạy gần như đồng thời trên các
    # worker Celery khác nhau (--concurrency=4). Bản cũ đọc alerts_to_resolve (không lock)
    # → gửi RECOVERED → mới update is_active=False: 2 lời gọi trùng thời điểm cùng đọc thấy
    # is_active=True, cùng gửi Telegram/email RECOVERED trùng lặp. Fix: "claim" atomically
    # (lock Device + update is_active=False TRƯỚC, cùng pattern select_for_update đã dùng ở
    # _fire_alert) rồi MỚI gửi notification ngoài transaction — lệnh gọi thứ 2 tới sau sẽ
    # thấy is_active đã False (đã commit) → alerts_to_resolve rỗng → tự return, không gửi lại.
    with transaction.atomic():
        Device.objects.select_for_update().filter(pk=device.pk).first()
        alerts_to_resolve = list(
            Alert.objects.select_for_update().filter(
                device=device, rule=rule, is_active=True, interface=interface,
            )
        )
        if only_if is not None:
            alerts_to_resolve = [a for a in alerts_to_resolve if only_if(a)]
        if not alerts_to_resolve:
            return
        resolved_at = timezone.now()
        Alert.objects.filter(pk__in=[a.pk for a in alerts_to_resolve]).update(
            is_active=False,
            resolved_at=resolved_at,
        )
        # Transactional-outbox-lite — cùng transaction với claim is_active=False ở trên (xem
        # comment đầy đủ ở _fire_alert). Chỉ tạo pending cho (alert, channel) nào ĐÃ có fire
        # notification "sent" — giữ nguyên logic chống dội RECOVERED cho alert bị flapping-
        # suppress lúc fire.
        # ⚠️ 2026-09-29 (review vòng 2): quyết định recovery PHẢI theo TỪNG (alert, channel), KHÔNG
        # theo alert — bản trước chỉ cần 1 channel đã "sent" là tạo recovery cho MỌI channel của
        # rule, kể cả channel khác còn chưa gửi fire (vd SMTP treo) → channel đó nhận RECOVERED
        # TRƯỚC khi từng nhận ALERT. `sent_fire_channels` là set (alert_id, channel).
        sent_fire_channels = set(
            AlertNotification.objects.filter(
                alert__in=alerts_to_resolve, kind="fire", status="sent"
            ).values_list("alert_id", "channel")
        )
        pending_rows = [
            AlertNotification(alert=alert, channel=ch, kind="recovery", status="pending")
            for alert in alerts_to_resolve
            for ch in rule.channels
            if (alert.pk, ch) in sent_fire_channels
        ]
        if pending_rows:
            AlertNotification.objects.bulk_create(pending_rows)

    for alert in alerts_to_resolve:
        # Gán resolved_at lên object TRƯỚC khi gửi để tin RECOVERED có ngày giờ
        # (trước đây gửi trước update → alert.resolved_at=None → hiện "N/A").
        alert.resolved_at = resolved_at
        channels_to_notify = [ch for ch in rule.channels if (alert.pk, ch) in sent_fire_channels]
        if channels_to_notify:
            _dispatch_notifications(alert, channels_to_notify, kind="recovery")
    logger.info("ALERT resolved: %s — %s", device.name, rule.name)


def _every_device_id() -> list[int]:
    """pk MỌI Device, sắp theo pk (thứ tự khoá ổn định).

    Cố ý KHÔNG thu hẹp theo `rule.device_type`. Phạm vi của rule có thể bị đổi cùng lúc với việc tắt nó (lưu
    rule "Tất cả" → "HyperV" rồi tắt): worker đã nạp rule theo phạm vi CŨ đang nổ alert cho 1 Switch, mà
    phạm vi MỚI không chứa Switch → Device đó không bị khoá, alert đang tạo (chưa commit) lại không có trong
    truy vấn "Device đã có alert active" → đóng 0 alert (review thứ ba sau 30b09aa, tái hiện bằng 2
    connection Postgres). Tập Device của 1 fleet giám sát chỉ cỡ vài chục dòng; mỗi Device khoá trong
    transaction riêng, ngắn."""
    return list(Device.objects.order_by("pk").values_list("pk", flat=True))


def close_alerts_for_disabled_rule(rule: AlertRule, by: str = "") -> int:
    """Đóng mọi alert ĐANG ACTIVE của `rule` vì rule vừa bị tắt. Trả số alert đã đóng.

    Hạng mục 1.4 (A4, 2026-10-15). `check_device_alerts` chỉ đánh giá rule `enabled=True`, nên alert đang
    active của 1 rule đã tắt không còn đường nào tới `_resolve_alert` — kẹt `is_active=True` vĩnh viễn trên
    panel Active Alerts và bộ đếm dashboard (cùng họ lỗi "alert kẹt hàng chục ngày"; 11 alert kẹt hiện có
    trên prod KHÔNG do việc này — 0 rule nào đang tắt lúc đo 2026-10-15).

    * KHÔNG ghi AlertNotification nào, KHÔNG gửi "RECOVERED": tắt rule không có nghĩa sự cố đã hồi phục.
      Lý do đóng được ghi vào cuối `Alert.message` (cho người đọc) VÀ vào `Alert.closed_reason` (cho engine):
      `_queue_late_recovery_if_resolved` bỏ qua alert có `closed_reason`, nên fire đang chờ/đang gửi của alert
      vừa đóng vẫn có thể được gửi (sự cố đã xảy ra) nhưng KHÔNG có RECOVERED theo sau — kể cả khi rule được
      bật lại sau đó. Alert đã hồi phục THẬT trước khi rule bị tắt không bị đụng tới (không còn active).
    * Lịch sử giữ nguyên (chỉ đổi is_active/resolved_at/closed_reason/message). Bật lại rule: điều kiện còn
      đúng thì engine tạo alert MỚI và thông báo như thường.
    * ⚠️ KHOÁ — đây là chỗ review sau 30b09aa bắt được (2 vòng): `_fire_alert` kiểm `enabled=True` rồi tạo
      Alert dưới khoá `Device`. Vòng 1: chỉ khoá các Device ĐÃ có alert active thì alert đang được tạo (chưa
      commit) không thấy được → đóng 0 alert → worker tạo tiếp → alert active của rule đã tắt, mãi mãi.
      Vòng 2: khoá theo `rule.device_type` HIỆN TẠI cũng không đủ — lưu rule đổi phạm vi và tắt cùng lúc thì
      Device thuộc phạm vi CŨ (worker đang xử lý) nằm ngoài phạm vi mới. Nên khoá MỌI Device
      (`_every_device_id`), từng cái một, rồi mới đọc alert: Device nào đang có worker giữ khoá thì ta chờ nó
      commit rồi thấy alert của nó và đóng. Chứng minh: worker đã đọc thấy `enabled=True` thì nó đang giữ
      khoá Device từ TRƯỚC lúc `enabled=False` commit; ta chỉ xin khoá SAU lúc đó nên phải chờ tới khi nó
      commit. Worker đọc SAU commit thì thấy `enabled=False` và không tạo gì (Device tạo sau lúc liệt kê
      cũng vậy: worker của nó chỉ chạy sau commit). Khoá từng Device trong transaction riêng (không giữ
      nhiều khoá cùng lúc) nên không thể deadlock với `_fire_alert`/`_resolve_alert` (cùng thứ tự Device →
      Alert).
    * Người gọi phải ĐÃ COMMIT `enabled=False` TRƯỚC khi gọi (và KHÔNG gọi trong 1 transaction còn mở chứa
      lệnh tắt — Django admin bọc cả request trong `atomic()`, nên admin dùng `transaction.on_commit`).
    """
    closed = 0
    for device_id in _every_device_id():
        with transaction.atomic():
            Device.objects.select_for_update().filter(pk=device_id).first()
            alerts = list(
                Alert.objects.select_for_update().filter(device_id=device_id, rule=rule, is_active=True)
            )
            if not alerts:
                continue
            now = timezone.now()
            stamp = timezone.localtime(now).strftime("%H:%M %d/%m/%Y")
            who = f" bởi {by}" if by else ""
            note = f" [Đã đóng vì rule bị tắt lúc {stamp}{who} — không phải đã hồi phục]"
            for alert in alerts:
                Alert.objects.filter(pk=alert.pk).update(
                    is_active=False, resolved_at=now, message=alert.message + note,
                    closed_reason=Alert.CLOSED_RULE_DISABLED,
                )
            closed += len(alerts)
    if closed:
        logger.info("ALERT closed (rule bị tắt): %s — %d alert", rule.name, closed)
    return closed


def _queue_late_recovery_if_resolved(alert_id: int, channel: str) -> None:
    """Gọi ngay sau khi 1 fire notification của `channel` vừa chuyển "sent" thật — LUÔN từ bên
    trong `_finalize_fire_sent` (transaction đã khoá Device), KHÔNG gọi trực tiếp nơi khác trừ
    nhánh fallback "chưa từng có outbox row" trong `_dispatch_notifications` (gọi ngoài luồng
    chuẩn, vd test) — xem 2 hàm đó.

    ⚠️ 2026-09-29 (theo báo cáo review): `_resolve_alert` chỉ queue recovery cho (alert, channel)
    có fire "sent" TẠI THỜI ĐIỂM resolve chạy — nếu fire còn "pending"/"processing" lúc đó (worker
    chết giữa lúc _fire_alert commit outbox và lúc kịp dispatch), resolve bỏ qua VĨNH VIỄN (quyết
    định chỉ đưa ra 1 lần, không có gì kích hoạt lại). Sau đó `retry_pending_alert_notifications`
    vẫn gửi fire trễ đó thành công → người nhận thấy "sự cố mới" dù nó đã hồi phục từ trước, và
    KHÔNG BAO GIỜ nhận được RECOVERED theo sau. Fix: coi "recovery còn nợ" là điều kiện được
    re-check tại CẢ 2 nơi có thể làm nó đúng — _resolve_alert (khi resolve chạy sau) VÀ đây (khi
    fire cuối cùng cũng gửi xong sau khi resolve đã chạy trước) — bên nào xảy ra SAU sẽ là bên
    phát hiện + tạo pending recovery.

    ⚠️ 2026-09-29 (review vòng 2 — 2 khoảng hở còn sót ở bản đầu):
    (a) Bản đầu gọi hàm này bằng 1 lệnh DB RIÊNG, sau khi đã COMMIT "sent" ở 1 lệnh khác — worker
        chết đúng giữa 2 lệnh làm mất VĨNH VIỄN cơ hội tạo recovery (fire đã "sent" — trạng thái
        cuối, không còn gì kích hoạt lại được). Fix: `_finalize_fire_sent` gộp finalize "sent" +
        gọi hàm này vào CHUNG 1 `transaction.atomic()` — crash giữa chừng thì TOÀN BỘ rollback
        (row vẫn "processing" như cũ), sweep sau reclaim và làm lại nguyên vẹn cả 2 bước.
    (b) Đọc `is_active=False` ở đây và đọc `sent_fire_channels` trong `_resolve_alert` là 2 lệnh
        SELECT độc lập không cùng khoá gì — fire chuyển "sent" đúng lúc nằm GIỮA lúc _resolve_alert
        đọc xong danh sách "đã gửi" và lúc nó COMMIT is_active=False: cả 2 bên đều đọc phải giá
        trị "cũ" của phía kia → KHÔNG bên nào tạo recovery. Fix: `_finalize_fire_sent` khoá
        `Device` (`select_for_update()`) giống hệt `_fire_alert`/`_resolve_alert` TRƯỚC KHI đọc
        `is_active` ở đây — 2 giao dịch cạnh tranh cùng device luôn serialize (Postgres, KHÔNG
        phải SQLite — SQLite bỏ qua select_for_update, xem test), không còn khoảng hở giữa đọc và
        commit của bên kia.

    An toàn không cần lock RIÊNG bên trong hàm này (lock đã có ở caller `_finalize_fire_sent`): 1
    fire-notification row chỉ chuyển "sent" ĐÚNG 1 LẦN (claim token chặn double-send cùng row)
    nên helper này chỉ có thể tự kích hoạt đúng 1 lần cho mỗi (alert, channel) — vẫn giữ check
    `.exists()` để an toàn kép, tránh tạo trùng nếu `_resolve_alert` đã lỡ tạo sẵn.
    """
    try:
        alert = Alert.objects.select_related("rule").get(pk=alert_id, is_active=False)
    except Alert.DoesNotExist:
        return  # Alert chưa resolve (hoặc đã bị xoá) — chưa có gì "nợ", nhánh resolve lo sau.
    if alert.closed_reason:
        # Đóng vì lý do KHÁC hồi phục (rule bị tắt, hạng mục 1.4): `is_active=False` ở đây không có nghĩa sự cố
        # đã hết, nên không được gửi RECOVERED — kể cả khi fire còn pending/processing lúc đóng và chỉ được
        # gửi xong SAU đó (review sau 30b09aa), và kể cả khi rule đã được bật lại.
        return
    if AlertNotification.objects.filter(alert=alert, channel=channel, kind="recovery").exists():
        return
    AlertNotification.objects.create(alert=alert, channel=channel, kind="recovery", status="pending")
    logger.info(
        "Recovery queued trễ (fire [%s] gửi xong SAU khi alert đã resolve): %s — %s",
        channel, alert.device.name, alert.rule.name,
    )


def _finalize_fire_sent(alert: Alert, channel: str, token: str) -> bool:
    """Chuyển 1 AlertNotification(kind="fire") sang "sent" + (nếu alert đã resolve) queue
    recovery pending cho ĐÚNG channel đó — trong CÙNG 1 transaction, khoá `Device` giống hệt
    `_fire_alert`/`_resolve_alert`. Xem 2 bẫy (a)/(b) ở docstring `_queue_late_recovery_if_resolved`
    — hàm này là chỗ sửa cho cả 2.

    Trả về True nếu claim_token khớp (finalize thành công); False nếu đã bị reclaim (1 tiến trình
    khác đã/đang xử lý row này — caller KHÔNG được tạo row mới thay thế, không ghi đè).
    """
    with transaction.atomic():
        Device.objects.select_for_update().filter(pk=alert.device_id).first()
        updated = AlertNotification.objects.filter(
            alert=alert, channel=channel, kind="fire", status="processing", claim_token=token,
        ).update(status="sent", next_attempt_at=None, updated_at=timezone.now())
        if not updated:
            return False
        _queue_late_recovery_if_resolved(alert.pk, channel)
    return True


# --- Thử lại có giới hạn cho lỗi gửi tạm thời (review ngoài 2026-10-15, hạng mục 1.2) ---------------
# Số đo prod (1.843 dòng AlertNotification): 309 `failed` — 172 là 429 Telegram, 124 lỗi kết nối, 12
# SoftTimeLimit, 1 là 4xx; trước đây `failed` là trạng thái cuối và sweep chỉ nhặt pending/processing.
# Nay lỗi TẠM THỜI trả row về `pending` kèm `next_attempt_at` (lưu trong DB, sống sót khi worker chết);
# sweep chỉ nhặt row đã đến hạn. Lỗi vĩnh viễn / hết số lần thử → `failed` như cũ.
NOTIFY_RETRY_BACKOFF_SECS = (30, 60, 120, 300, 600)  # theo số lần đã thử; bậc cuối lặp lại
NOTIFY_RETRY_AFTER_CAP_SECS = 3600                   # trần cho retry_after do phía kia đưa ra
# Kênh có hạn mức tốc độ → sweep tự giãn cách giữa 2 tin (Telegram: ≤1 tin/giây/chat, ≤20 tin/phút/nhóm
# — core.telegram.org/bots/faq). Gửi lại 15 tin cùng lúc chỉ tái hiện đúng đợt 429 vừa gây ra lỗi.
PACED_CHANNELS = frozenset({"telegram"})
# Số lỗi tạm thời LIÊN TIẾP trên 1 kênh trong 1 vòng sweep thì ngừng kênh đó đến hết vòng (mạng/dịch vụ
# đang sập — thử tiếp chỉ đốt lượt thử của những dòng còn lại).
NOTIFY_SWEEP_TRANSIENT_STREAK = 3


def _notify_max_attempts() -> int:
    return int(getattr(settings, "ALERT_NOTIFY_MAX_ATTEMPTS", 8))


def _fire_resolved_max_age_secs() -> int:
    return int(getattr(settings, "ALERT_NOTIFY_FIRE_RESOLVED_MAX_AGE_SECS", 3600))


def _sweep_budget_secs() -> float:
    return float(getattr(settings, "ALERT_NOTIFY_SWEEP_BUDGET_SECS", 45))


def _telegram_min_interval_secs() -> float:
    return float(getattr(settings, "ALERT_NOTIFY_TELEGRAM_MIN_INTERVAL_SECS", 3.0))


def _retry_delay_secs(attempts: int, retry_after: int | None) -> int:
    """Chờ bao lâu trước lần thử kế: bậc backoff theo `attempts` (đã thử mấy lần), nhưng không bao giờ
    sớm hơn `retry_after` mà Telegram/Slack yêu cầu (+1s dư)."""
    idx = min(max(attempts, 1), len(NOTIFY_RETRY_BACKOFF_SECS)) - 1
    delay = NOTIFY_RETRY_BACKOFF_SECS[idx]
    if retry_after:
        delay = max(delay, min(int(retry_after), NOTIFY_RETRY_AFTER_CAP_SECS) + 1)
    return delay


def _safe_error_text(exc: BaseException) -> str:
    """Thông điệp lỗi an toàn để LƯU DB/ghi log: không token bot, không path URL (requests nhét nguyên
    URL `.../bot<token>/sendMessage` vào str(exc) — 294 dòng prod đang chứa token)."""
    if isinstance(exc, SoftTimeLimitExceeded):
        return "Tác vụ bị cắt bởi giới hạn thời gian đúng lúc đang gửi — sẽ thử lại"
    text = redact(str(exc))
    if not text:
        text = type(exc).__name__
    return text[:1000]


def _channel_cooldown_key(channel: str) -> str | None:
    """Khoá cooldown dùng chung của kênh (theo bot/chat hoặc webhook) hoặc None nếu kênh không có/chưa
    cấu hình. Lỗi tra cứu KHÔNG được chặn việc gửi — cooldown chỉ là tối ưu để khỏi đốt lượt thử."""
    try:
        if channel == "telegram":
            from .channels.telegram import cooldown_key
            return cooldown_key()
        if channel in ("slack", "teams"):
            from .channels.webhook import cooldown_key
            return cooldown_key(channel)
    except Exception:
        logger.exception("Không dựng được khoá cooldown cho kênh %s — bỏ qua cooldown", channel)
    return None


def _settle_failed_attempt(owned: QuerySet, exc: BaseException, channel: str) -> str:
    """Ghi kết quả 1 lần gửi LỖI cho row mà tiến trình này đang giữ.

    Nếu lỗi là flood control (có `retry_after`) thì ghi cooldown DÙNG CHUNG theo bot/chat trước khi làm
    gì khác — kể cả khi row đã bị reclaim hay hết lượt, vì 429 là sự thật về kênh, không phải về row
    (review 2026-10-15, P2: cooldown chỉ sống trong 1 vòng quét thì vòng sau và mọi lần gửi trực tiếp
    vẫn đâm vào 429 và đốt lượt thử).

    `owned` = queryset đã khoá theo (status="processing", claim_token=<token của MÌNH>): nếu row đã bị
    reclaim (token đổi) thì cả đọc lẫn ghi đều khớp 0 dòng → trả "lost" và KHÔNG đụng vào kết quả của chủ
    mới — claim_token vẫn là hàng rào duy nhất chống ghi đè (không thêm cơ chế khoá khác).

    Trả "rescheduled" (lỗi tạm thời, còn lượt → pending + next_attempt_at), "failed" (vĩnh viễn hoặc hết
    lượt) hoặc "lost".
    """
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(exc, ChannelTransientError) and retry_after:
        try:
            cooldown.register(_channel_cooldown_key(channel),
                              min(int(retry_after), NOTIFY_RETRY_AFTER_CAP_SECS))
        except Exception:
            logger.exception("Ghi cooldown kênh %s lỗi — vẫn hẹn gửi lại theo lịch của dòng", channel)
    row = owned.values("attempts").first()
    if row is None:
        return "lost"
    attempts = row["attempts"]
    now_ = timezone.now()
    message = _safe_error_text(exc)
    transient = isinstance(exc, (ChannelTransientError, SoftTimeLimitExceeded))
    if transient and attempts < _notify_max_attempts():
        delay = _retry_delay_secs(attempts, getattr(exc, "retry_after", None))
        updated = owned.update(
            status="pending", error=message, updated_at=now_,
            next_attempt_at=now_ + timedelta(seconds=delay),
        )
        return "rescheduled" if updated else "lost"
    if transient:
        message = f"{message} — bỏ cuộc sau {attempts} lần gửi"
    updated = owned.update(status="failed", error=message, next_attempt_at=None, updated_at=now_)
    return "failed" if updated else "lost"


def _expiry_reason(n: AlertNotification, now_) -> str:
    """Lý do KHÔNG nên gửi row này nữa (rỗng = cứ gửi). Gọi SAU khi đã claim (attempts đã +1).

    * Quá số lần thử — chặn vòng lặp vô hạn khi tiến trình bị SIGKILL giữa chừng nhiều lần (mỗi lần
      reclaim vẫn tính 1 lượt).
    * Fire của alert ĐÃ HỒI PHỤC và đã nằm quá lâu — báo "🔴 sự cố" cho thứ đã hết là cảnh báo hết hiệu
      lực (sau sự cố sập cả stack hàng giờ). Fire của alert còn active thì VẪN gửi (sự cố đang diễn
      ra); recovery luôn gửi (bỏ nó khiến người trực tưởng sự cố còn đó). Trong cửa sổ hợp lệ giữ
      nguyên hành vi late-recovery đã duyệt (fire gửi trễ rồi recovery theo sau).
    """
    max_attempts = _notify_max_attempts()
    if n.attempts > max_attempts:
        return f"Đã vượt {max_attempts} lần gửi — không thử lại nữa"
    if (n.kind == "fire" and not n.alert.is_active
            and (now_ - n.sent_at).total_seconds() > _fire_resolved_max_age_secs()):
        return ("Cảnh báo đã hồi phục từ trước khi gửi được thông báo này — bỏ qua để không báo "
                "một cảnh báo đã hết hiệu lực")
    return ""


def _send_channel_message(kind: str, channel: str, alert: Alert) -> None:
    """Gọi thẳng hàm gửi thật theo (kind, channel) — không tự ghi AlertNotification, raise nếu
    lỗi (caller _dispatch_notifications bắt exception để cập nhật status)."""
    if kind == "recovery":
        if channel == "email":
            from .channels.email_channel import send_email_recovery
            send_email_recovery(alert)
        elif channel == "telegram":
            from .channels.telegram import send_telegram_recovery
            send_telegram_recovery(alert)
        elif channel == "slack":
            from .channels.webhook import send_slack_recovery
            send_slack_recovery(alert)
        elif channel == "teams":
            from .channels.webhook import send_teams_recovery
            send_teams_recovery(alert)
    else:
        if channel == "email":
            from .channels.email_channel import send_email_alert
            send_email_alert(alert)
        elif channel == "telegram":
            from .channels.telegram import send_telegram_alert
            send_telegram_alert(alert)
        elif channel == "slack":
            from .channels.webhook import send_slack_alert
            send_slack_alert(alert)
        elif channel == "teams":
            from .channels.webhook import send_teams_alert
            send_teams_alert(alert)


def _dispatch_notifications(alert: Alert, channels: list[str], kind: str) -> None:
    """Gửi notification thật cho từng channel.

    ⚠️ 2026-09-28 (theo báo cáo review — "2 retry task có thể gửi trùng"): TRƯỚC khi gửi phải
    claim nguyên tử row AlertNotification(status="pending") đã ghi sẵn trong transaction lúc
    fire/resolve (transactional-outbox-lite) sang "processing" bằng UPDATE có điều kiện
    (`.filter(status="pending").update(...)` — chỉ 1 caller nhận được số dòng >0, caller khác
    tới sau nhận 0 và tự bỏ qua). Không chỉ 2 lần gọi retry_pending_alert_notifications() chồng
    nhau mới cần claim — chính lệnh gọi GỐC này (từ _fire_alert/_resolve_alert) cũng có thể bị
    sweep định kỳ tranh mất cùng 1 row nếu gửi CHẬM hơn grace_secs (90s mặc định của sweep).
    Không thấy row pending nào (gọi ngoài luồng chuẩn, vd test gọi thẳng hàm) → vẫn gửi + tự tạo
    row mới, giữ hàm hoạt động độc lập.

    ⚠️ 2026-09-29 (theo báo cáo review tiếp theo — "gửi trùng khi SMTP treo quá
    stale_processing_secs"): claim ở trên chỉ chặn 2 lệnh UPDATE THEO ĐÚNG NGHĨA ĐEN chạy đồng
    thời trên CÙNG row (compare-and-swap DB). Nó KHÔNG chặn được trường hợp: lệnh gọi NÀY claim
    xong rồi treo thật sự lâu (channel `email` dùng Django `send_mail`, trước đây KHÔNG set
    `EMAIL_TIMEOUT` nên smtplib có thể treo vô thời hạn) > `stale_processing_secs` (300s), trong
    lúc đó sweep coi row "kẹt" và RECLAIM (ghi `claim_token` mới) rồi tự gửi+finalize xong TRƯỚC —
    khi lệnh gọi NÀY cuối cùng cũng gửi xong (dù thành công hay lỗi), nó không biết mình đã mất
    quyền sở hữu row, dễ ghi đè `status` mà sweep vừa set. Fix 2 lớp: (1) `EMAIL_TIMEOUT` (settings,
    config/settings/base.py) bound thời gian gửi email dưới `stale_processing_secs` thật sự — chặn
    gốc rễ; (2) `claim_token` ngẫu nhiên ghi lúc claim, finalize PHẢI match đúng token đó — nếu ai
    đó đã reclaim (token đổi), UPDATE finalize match 0 dòng → tự bỏ qua, KHÔNG tạo row mới đè
    (khác best-effort cũ luôn tạo row mới khi update=0). Lớp (2) là phòng thủ thứ 2 cho các nguyên
    nhân trễ khác ngoài SMTP (GC pause, network buffering...) mà (1) không bound hết được.

    ⚠️ 2026-09-29 (review vòng 2): finalize riêng cho `kind=="fire"` giờ đi qua
    `_finalize_fire_sent` — gộp "chuyển sent" + "queue recovery trễ nếu alert đã resolve" vào
    CHUNG 1 transaction có khoá `Device`, đóng 2 khoảng hở (crash giữa 2 bước; race đọc/ghi
    `is_active` với `_resolve_alert`) — xem docstring đầy đủ ở `_finalize_fire_sent`/
    `_queue_late_recovery_if_resolved`. `kind=="recovery"` không cần lock/late-check (không có gì
    phụ thuộc trạng thái sau khi recovery gửi xong) nên vẫn finalize đơn giản như cũ.
    """
    for channel in channels:
        token = uuid.uuid4().hex
        now_ = timezone.now()
        # Kênh đang trong cooldown dùng chung (vừa có lần gửi khác nhận 429): KHÔNG claim, KHÔNG gửi,
        # KHÔNG tốn lượt thử — hẹn row đúng lúc hết cooldown để sweep nhặt (review 2026-10-15, P2: trước
        # đây đường gửi trực tiếp bỏ qua hoàn toàn và tiếp tục đâm vào 429).
        wait = cooldown.remaining_secs(_channel_cooldown_key(channel))
        if wait > 0:
            until = now_ + timedelta(seconds=wait)
            deferred = AlertNotification.objects.filter(
                alert=alert, channel=channel, kind=kind, status="pending",
            ).filter(
                Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lt=until)
            ).update(next_attempt_at=until, updated_at=now_)
            if not deferred and not AlertNotification.objects.filter(
                alert=alert, channel=channel, kind=kind
            ).exists():
                AlertNotification.objects.create(  # gọi ngoài luồng chuẩn: chưa có row outbox
                    alert=alert, channel=channel, kind=kind, status="pending", next_attempt_at=until,
                )
            logger.warning("%s notification [%s] hoãn %.0fs: kênh đang trong thời gian chờ flood control",
                           kind, channel, wait)
            continue
        # Chỉ claim row ĐẾN HẠN: row đã được lên lịch gửi lại (next_attempt_at ở tương lai, vd theo
        # retry_after của Telegram) không được gửi sớm bởi lời gọi trực tiếp này.
        claimed = AlertNotification.objects.filter(
            alert=alert, channel=channel, kind=kind, status="pending",
        ).filter(
            Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now_)
        ).update(status="processing", updated_at=now_, claim_token=token, attempts=F("attempts") + 1)
        if not claimed and AlertNotification.objects.filter(
            alert=alert, channel=channel, kind=kind
        ).exists():
            # Có row nhưng KHÔNG claim được — 1 tiến trình khác (sweep, hoặc lời gọi khác) đã
            # hoặc đang xử lý rồi, hoặc đã lên lịch gửi lại. Không gửi trùng.
            continue
        owned = AlertNotification.objects.filter(
            alert=alert, channel=channel, kind=kind, status="processing", claim_token=token,
        )
        try:
            _send_channel_message(kind, channel, alert)
        except SoftTimeLimitExceeded as exc:
            # Tác vụ poll bị cắt đúng lúc đang gọi kênh (A9: 12 dòng `failed` vĩnh viễn trên prod vì
            # `except Exception` bên dưới nuốt nó). Không phải lỗi của kênh → giữ pending để sweep gửi
            # lại, rồi raise lại để tác vụ gọi dừng đúng nghĩa. Các kênh còn lại chưa claim sẽ được
            # sweep nhặt sau `grace_secs`.
            if claimed:
                _settle_failed_attempt(owned, exc, channel)
            else:
                AlertNotification.objects.create(
                    alert=alert, channel=channel, kind=kind, status="pending", attempts=1,
                    error=_safe_error_text(exc), next_attempt_at=timezone.now(),
                )
            raise
        except Exception as exc:
            if claimed:
                outcome = _settle_failed_attempt(owned, exc, channel)
                if outcome == "lost":
                    logger.warning(
                        "%s notification [%s] bị reclaim trước khi ghi lỗi xong — bỏ qua, tiến "
                        "trình reclaim chịu trách nhiệm ghi kết quả cuối", kind, channel,
                    )
                elif outcome == "rescheduled":
                    logger.warning("%s notification [%s] lỗi tạm thời, sẽ thử lại: %s",
                                   kind, channel, _safe_error_text(exc))
                else:
                    logger.error("%s notification failed [%s]: %s", kind, channel, _safe_error_text(exc))
            else:
                AlertNotification.objects.create(
                    alert=alert, channel=channel, kind=kind, status="failed", attempts=1,
                    error=_safe_error_text(exc),
                )
                logger.error("%s notification failed [%s]: %s", kind, channel, _safe_error_text(exc))
            continue

        if kind == "fire":
            if claimed:
                if not _finalize_fire_sent(alert, channel, token):
                    logger.warning(
                        "%s notification [%s] bị reclaim trước khi gửi xong (gửi chậm hơn "
                        "stale_processing_secs) — bỏ qua cập nhật, tiến trình reclaim chịu "
                        "trách nhiệm ghi kết quả cuối, không ghi đè", kind, channel,
                    )
            else:
                # Chưa từng có outbox row (gọi ngoài luồng chuẩn, vd test) — không có gì để
                # claim/khoá theo pattern chuẩn, tạo thẳng row "sent" rồi vẫn check late-recovery.
                AlertNotification.objects.create(alert=alert, channel=channel, kind=kind, status="sent")
                _queue_late_recovery_if_resolved(alert.pk, channel)
        else:
            updated = AlertNotification.objects.filter(
                alert=alert, channel=channel, kind=kind, status="processing", claim_token=token,
            ).update(status="sent", next_attempt_at=None, updated_at=timezone.now())
            if not updated:
                if claimed:
                    logger.warning(
                        "%s notification [%s] bị reclaim trước khi gửi xong (gửi chậm hơn "
                        "stale_processing_secs) — bỏ qua cập nhật, tiến trình reclaim chịu "
                        "trách nhiệm ghi kết quả cuối, không ghi đè", kind, channel,
                    )
                    continue
                AlertNotification.objects.create(alert=alert, channel=channel, kind=kind, status="sent")


def retry_pending_alert_notifications(grace_secs: int = 90, stale_processing_secs: int = 300) -> int:
    """Quét + claim nguyên tử rồi gửi lại AlertNotification bị "kẹt" — 2 nhóm:
    (1) status="pending" cũ hơn `grace_secs` — worker chết giữa lúc commit trạng thái Alert và
        lúc kịp claim/gửi (transactional-outbox-lite, xem _fire_alert/_resolve_alert/
        _dispatch_notifications).
    (2) status="processing" cũ hơn `stale_processing_secs` — ĐÃ có 1 tiến trình claim (kể cả
        chính hàm này ở lần gọi trước) nhưng chết giữa chừng, không kịp chuyển sent/failed →
        "thu hồi" row bị bỏ rơi. `stale_processing_secs` (300s, dài hơn hẳn `grace_secs`) vì
        1 tiến trình ĐANG THẬT SỰ gửi (không chết) có thể hợp lệ mất vài chục giây (email
        không timeout — xem comment ở _dispatch_notifications) nên không được vội thu hồi.

    ⚠️ Claim bằng UPDATE có điều kiện, re-check `stale_cutoff` NGAY TRONG WHERE của chính câu
    UPDATE claim (không chỉ ở bước chọn candidate_ids) — đây là phần mấu chốt chặn "2 retry task
    chạy chồng nhau (task trước kéo dài quá chu kỳ beat 120s, nhiều beat, gọi tay...) cùng gửi
    trùng 1 row" mà review chỉ ra: candidate_ids có thể chứa cùng 1 pk ở cả 2 lần gọi, nhưng
    UPDATE claim dùng `stale_cutoff` CỐ ĐỊNH (tính 1 lần trước vòng lặp) — lệnh UPDATE nào tới
    DB trước sẽ set `updated_at=NOW()` (rất mới) và COMMIT; lệnh UPDATE tới sau (dù đã bị
    Postgres tự khoá row chờ lệnh trước commit) re-evaluate WHERE trên dữ liệu MỚI COMMIT đó —
    `updated_at` giờ không còn `< stale_cutoff` nữa (stale_cutoff là mốc cũ, "vừa claim xong"
    luôn mới hơn) → khớp 0 dòng → tự bỏ qua, không gửi trùng. Trả về số row đã claim + xử lý
    (gửi hoặc đánh failed). Gọi từ task Celery định kỳ (apps/alerts/tasks.py) — KHÔNG lock
    Device vì đây là xử lý bù trên chính bảng AlertNotification, không cạnh tranh trực tiếp với
    fire/resolve của cùng device tại đúng thời điểm này.

    ⚠️ 2026-09-29: claim ở trên chỉ chặn 2 lệnh UPDATE claim tranh nhau — KHÔNG chặn được việc
    hàm này reclaim 1 row mà chủ cũ (lệnh gọi gốc `_dispatch_notifications`, hoặc chính hàm này ở
    lần gọi trước) thực ra vẫn đang gửi hợp lệ (chỉ là chậm hơn `stale_processing_secs`, vd SMTP
    không timeout trước fix `EMAIL_TIMEOUT`) chứ chưa chết hẳn — cả 2 phía khi đó đều gọi
    `_send_channel_message` thật (gửi trùng không tránh được nếu đã xảy ra), nhưng finalize phải
    dùng `claim_token` để đảm bảo chỉ chủ MỚI NHẤT (người reclaim) được ghi kết quả cuối — chủ cũ
    finalize trễ hơn sẽ thấy `claim_token` không khớp (đã bị ghi đè lúc reclaim) → tự bỏ qua,
    không ghi đè lại kết quả đúng. Xem comment đầy đủ ở `_dispatch_notifications`.
    """
    t0 = time.monotonic()
    now_ = timezone.now()
    pending_cutoff = now_ - timedelta(seconds=grace_secs)
    stale_cutoff = now_ - timedelta(seconds=stale_processing_secs)

    # Row pending "đến hạn": (a) chưa từng lên lịch và đã nằm quá grace_secs (worker chết giữa commit và
    # gửi), hoặc (b) đã lên lịch gửi lại và next_attempt_at <= bây giờ. Row còn đang chờ lịch
    # (next_attempt_at ở tương lai) KHÔNG được nhặt sớm — đó là chỗ tuân theo retry_after của Telegram.
    # Row `failed` (kể cả 309 dòng cũ trên prod, đều thuộc alert đã hồi phục) không bao giờ được nhặt.
    due_pending = Q(status="pending") & (
        Q(next_attempt_at__isnull=True, sent_at__lt=pending_cutoff) | Q(next_attempt_at__lte=now_)
    )
    stale_processing = Q(status="processing", updated_at__lt=stale_cutoff)

    candidates = list(
        AlertNotification.objects.filter(due_pending | stale_processing)
        .order_by("sent_at").values_list("pk", "channel")
    )

    budget = _sweep_budget_secs()
    min_gap = _telegram_min_interval_secs()
    # Chỉ còn dùng cho "kênh đang sập" (chuỗi lỗi tạm thời liên tiếp) — hết vòng là bỏ. Flood control
    # (retry_after) KHÔNG nằm ở đây nữa mà ở bảng ChannelCooldown dùng chung giữa các vòng và đường
    # gửi trực tiếp.
    held_until: dict[str, float] = {}
    cooldown_keys: dict[str, str | None] = {}
    last_attempt: dict[str, float] = {}
    streak: dict[str, int] = {}
    processed = 0
    for idx, (pk, channel) in enumerate(candidates):
        if time.monotonic() - t0 >= budget:
            logger.warning(
                "Retry notification: hết ngân sách %.0fs, còn %d dòng đến hạn để dành cho lượt sau",
                budget, len(candidates) - idx,
            )
            break
        if time.monotonic() < held_until.get(channel, 0.0):
            continue  # kênh đang sập trong vòng này — chưa claim nên không tốn lượt thử của dòng này
        if channel not in cooldown_keys:
            cooldown_keys[channel] = _channel_cooldown_key(channel)
        if cooldown.remaining_secs(cooldown_keys[channel]) > 0:
            continue  # flood control dùng chung (vòng trước hoặc lần gửi trực tiếp ghi) — không tốn lượt
        if channel in PACED_CHANNELS and channel in last_attempt:
            wait = min_gap - (time.monotonic() - last_attempt[channel])
            if wait > 0:
                time.sleep(wait)

        token = uuid.uuid4().hex
        claimed = AlertNotification.objects.filter(
            Q(pk=pk) & (due_pending | stale_processing)
        ).update(status="processing", updated_at=timezone.now(), claim_token=token,
                 attempts=F("attempts") + 1)
        if not claimed:
            continue  # bị 1 tiến trình khác claim mất giữa lúc chọn candidate và lúc tới lượt
        n = AlertNotification.objects.select_related("alert").get(pk=pk)
        owned = AlertNotification.objects.filter(pk=pk, status="processing", claim_token=token)

        reason = _expiry_reason(n, timezone.now())
        if reason:
            owned.update(status="failed", error=reason, next_attempt_at=None, updated_at=timezone.now())
            processed += 1
            logger.warning("Retry %s notification [%s] bỏ qua: %s", n.kind, n.channel, reason)
            continue

        try:
            _send_channel_message(n.kind, n.channel, n.alert)
        except SoftTimeLimitExceeded as exc:
            _settle_failed_attempt(owned, exc, channel)  # giữ pending để vòng sau gửi lại, rồi cho tác vụ dừng
            raise
        except Exception as exc:
            outcome = _settle_failed_attempt(owned, exc, channel)  # 429 → ghi cooldown dùng chung
            processed += 1
            last_attempt[channel] = time.monotonic()
            if isinstance(exc, ChannelTransientError):
                streak[channel] = streak.get(channel, 0) + 1
                if not getattr(exc, "retry_after", None) and streak[channel] >= NOTIFY_SWEEP_TRANSIENT_STREAK:
                    held_until[channel] = float("inf")
            else:
                streak[channel] = 0
            if outcome == "lost":
                logger.warning(
                    "Retry %s notification [%s] bị reclaim trong lúc gửi (chủ mới đã ghi kết quả) — "
                    "bỏ qua ghi lỗi, không ghi đè", n.kind, n.channel,
                )
            elif outcome == "rescheduled":
                logger.warning("Retry %s notification [%s] lỗi tạm thời, hẹn lại: %s",
                               n.kind, n.channel, _safe_error_text(exc))
            else:
                logger.error("Retry %s notification failed [%s]: %s",
                             n.kind, n.channel, _safe_error_text(exc))
            continue

        streak[channel] = 0
        last_attempt[channel] = time.monotonic()
        if n.kind == "fire":
            # _finalize_fire_sent gộp "chuyển sent" + "queue recovery trễ nếu đã resolve" vào
            # chung 1 transaction có khoá Device — xem docstring đầy đủ ở đó (review vòng 2).
            finalized = _finalize_fire_sent(n.alert, n.channel, token)
            processed += 1
            if not finalized:
                logger.warning(
                    "Retry %s notification [%s] bị reclaim trước khi gửi xong — bỏ qua cập "
                    "nhật, chủ mới chịu trách nhiệm ghi kết quả cuối", n.kind, n.channel,
                )
            continue

        updated = owned.update(status="sent", next_attempt_at=None, updated_at=timezone.now())
        processed += 1
        if not updated:
            logger.warning(
                "Retry %s notification [%s] bị reclaim trước khi gửi xong — bỏ qua cập nhật, "
                "chủ mới chịu trách nhiệm ghi kết quả cuối", n.kind, n.channel,
            )
    if processed:
        logger.warning("Retried %d pending/stuck alert notification(s)", processed)
    return processed
