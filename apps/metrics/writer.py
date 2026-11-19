"""Ghi NormalizedData vào PostgreSQL qua Django ORM."""
import logging
from datetime import datetime
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from apps.collectors.base import NormalizedData, InterfaceData
from apps.devices.models import Device, Interface
from . import cache as metrics_cache
from .models import (
    EVIDENCE_KEY, InterfaceStats, SystemHealth, VMStats, VolumeStats, WifiApStats, WifiClientStats,
)

logger = logging.getLogger(__name__)


TRUNK_NAME_PREFIXES = (
    "po",              # Cisco Port-channel short name
    "port-channel",
    "eth-trunk",
    "bridge-aggregation",
    "bond",
    "lag",
    "ae",              # Juniper aggregated ethernet style
    "trunk",
    "xgigabit",        # Huawei 10G
    "xge",
    "10ge",
    "25ge",
    "40ge",
    "100ge",
)
TRUNK_DESC_KEYWORDS = (
    "trunk",
    "uplink",
    "up-link",
    "backbone",
    "inter-switch",
    "to-core",
    "to core",
)


_EXTRA_SKIP_KEYS = ("vms", "volumes", "wifi_aps", "wifi_clients")


def _fit(model, field: str, value) -> str:
    """Cắt chuỗi cho vừa `max_length` của cột — Postgres TỪ CHỐI (DataError) chứ không tự cắt, và
    1 giá trị quá dài làm hỏng cả bulk_create/transaction của vòng ghi (1.5, 2026-10-15)."""
    return str(value or "")[: model._meta.get_field(field).max_length]


def _clamp_interface_text(data: NormalizedData) -> None:
    """Chuẩn hoá tên/mô tả interface cho vừa cột TRƯỚC mọi bước khác.

    Phải làm ở đầu `save_metrics` (không phải lúc tạo `Interface(...)`): khoá tra cứu là
    `_iface_key(name)` và `unique_together=(device, name)`. Nếu chỉ cắt lúc ghi mà vẫn tra cứu bằng
    tên chưa cắt thì mỗi poll sẽ coi interface là "mới" và cố tạo lại → IntegrityError lặp mãi.
    Mô tả (ifAlias) do người vận hành gõ trên thiết bị nên độ dài không do mình kiểm soát.
    """
    for iface in data.interfaces:
        iface.name = _fit(Interface, "name", iface.name)
        iface.description = _fit(Interface, "description", iface.description)


def save_metrics(device: Device, data: NormalizedData) -> None:
    """Ghi metrics 1 vòng poll.

    - Mode "cache" (METRICS_WRITE_MODE): metrics vào Redis (latest + ring-buffer),
      Postgres chỉ ghi evidence khi đổi trạng thái quan trọng. Redis lỗi → fallback DB.
    - Mode "db" (mặc định): ghi raw như cũ.
    """
    _clamp_interface_text(data)
    if metrics_cache.is_cache_mode():
        expected_ifaces = _previous_interface_ids(device)
        if _save_metrics_cache(device, data):
            _mark_metrics_seen(device, data.timestamp, expected_ifaces)
            return
        _note_redis_loss(device, data.timestamp, expected_ifaces)
        logger.warning("Device %s: ghi cache thất bại → fallback ghi DB", device.name)
    _save_metrics_db(device, data)


# Dung sai (giây) khi so thời điểm mẫu: cùng lấy từ `NormalizedData.timestamp` nên bằng nhau trong trường hợp bình thường;
# chỉ bù làm tròn float epoch ↔ microsecond.
_CONTINUITY_TOLERANCE_SECS = 1.0
# Số mẫu sys mới nhất đọc để tìm "mẫu liền trước" — dư vài mẫu để 2 lần poll đến lệch thứ tự không gây báo mất mẫu giả.
_CONTINUITY_PROBE_SAMPLES = 5


def _previous_interface_ids(device: Device) -> list[int]:
    """Cổng có mẫu ở lần poll TRƯỚC (theo snapshot Redis, đọc TRƯỚC khi nó bị ghi đè): if-series của chúng phải còn mẫu liền
    trước. Chuỗi trạng thái cổng/uplink đọc if-series (khoá RIÊNG từng cổng) nên mất if-series mà sys-series còn nguyên cũng
    là mất mẫu. Chỉ cổng ĐÃ có mẫu lần trước mới kiểm chứng — cổng mới xuất hiện không phải mất mẫu. Chưa có dấu
    `metrics_seen_at` thì chưa có gì để mất. Không bao giờ ném: bước phụ này không được làm hỏng việc ghi mẫu."""
    if device.metrics_seen_at is None:
        return []
    try:
        interfaces = (metrics_cache.get_latest(device.id) or {}).get("interfaces") or {}
        return [int(key) for key in interfaces]
    except Exception:
        logger.warning("Device %s: không đọc được danh sách cổng của lần poll trước", device.name, exc_info=True)
        return []


def _holds_a_sample_since(stamps: list[float], new_ts, seen) -> bool:
    """`stamps` (các mẫu mới nhất của 1 chuỗi Redis) có mẫu LIỀN TRƯỚC mẫu vừa ghi mà không cũ hơn dấu `seen` không?
    Mẫu vừa ghi (có thể đã nằm trong danh sách) bị bỏ."""
    previous = [t for t in stamps if t < new_ts.timestamp() - _CONTINUITY_TOLERANCE_SECS]
    return bool(previous) and max(previous) >= seen.timestamp() - _CONTINUITY_TOLERANCE_SECS


def _redis_holds_previous_sample(device: Device, new_ts, expected_if_ids=()) -> bool | None:
    """Redis còn giữ mẫu mà dấu `metrics_seen_at` trỏ tới (hoặc mẫu mới hơn nó) không — ở sys-series VÀ ở if-series của mọi cổng
    trong `expected_if_ids`? None = không hỏi được Redis.

    Chỉ gọi khi `device.metrics_seen_at < new_ts`. Redis mất dữ liệu (khoá biến mất) hoặc khôi phục từ bản cũ (mất đúng mẫu
    mới nhất đã nhận) đều ra False. Sys-series mất ⇒ False ngay, không cần đọc if-series."""
    seen = device.metrics_seen_at
    stamps = metrics_cache.recent_sys_ts(device.id, _CONTINUITY_PROBE_SAMPLES)
    if stamps is metrics_cache.UNREADABLE:
        return None
    if not _holds_a_sample_since(stamps, new_ts, seen):
        return False
    if not expected_if_ids:
        return True
    by_iface = metrics_cache.recent_if_ts(expected_if_ids, _CONTINUITY_PROBE_SAMPLES)
    if by_iface is metrics_cache.UNREADABLE:
        return None
    return all(_holds_a_sample_since(by_iface.get(iid, []), new_ts, seen) for iid in expected_if_ids)


def _record_lost_upto(device: Device, upto) -> None:
    """Ghi BỀN `Device.metrics_lost_upto` = `upto` — thời điểm của mẫu cuối cùng Redis đã nhận rồi mất (chỉ tăng).

    Engine không nối chuỗi sustained qua mốc này (xem `engine._after_loss`). Lỗi ghi chỉ mất lớp bảo vệ này, không làm hỏng
    poll."""
    try:
        Device.objects.filter(pk=device.pk).filter(
            Q(metrics_lost_upto__isnull=True) | Q(metrics_lost_upto__lt=upto)
        ).update(metrics_lost_upto=upto)
        if device.metrics_lost_upto is None or device.metrics_lost_upto < upto:
            device.metrics_lost_upto = upto
            logger.warning("Device %s: Redis không còn giữ mẫu đã nhận lúc %s — chuỗi sustained không được nối qua mốc này",
                           device.name, upto)
    except Exception:
        logger.warning("Device %s: không ghi được metrics_lost_upto", device.name, exc_info=True)


def _note_redis_loss(device: Device, ts, expected_if_ids=()) -> None:
    """Nhánh ghi đường lui (Redis nhận mẫu thất bại): nếu ĐỌC được Redis và thấy nó đã mất mẫu đã nhận trước đó thì vẫn ghi mốc
    mất mẫu — mẫu mới vào DB mà dấu `metrics_seen_at` đứng yên nên engine không có cách nào khác để biết (kịch bản "lỗi
    kép": Redis nhận 10% → mất dữ liệu → ghi lỗi lại → 99% vào DB). Không hỏi được Redis thì chưa kết luận gì."""
    prior = device.metrics_seen_at
    if prior is None or not prior < ts:
        return
    try:
        if _redis_holds_previous_sample(device, ts, expected_if_ids) is False:
            _record_lost_upto(device, prior)
    except Exception:
        logger.warning("Device %s: không kiểm chứng được Redis còn giữ mẫu cũ", device.name, exc_info=True)


def _mark_metrics_seen(device: Device, ts, expected_if_ids=()) -> None:
    """Ghi BỀN mốc của mẫu vừa đưa vào Redis: `Device.metrics_seen_at` (chỉ tăng).

    Mẫu mới nằm ở Redis; nếu Redis mất dữ liệu thì mẫu lẫn dấu hiệu của nó cùng mất, còn dòng DB cũ (ghi lúc Redis lỗi)
    vẫn nằm đó và engine đọc lại như "giá trị hiện tại" — kể cả khi chưa có alert nào hồi phục để làm mốc (review ngoài
    2026-11-19 lần 4, P1). Mốc này cho engine biết dòng DB nào đã bị mẫu mới hơn vượt qua. Chỉ ghi ở nhánh Redis THÀNH
    CÔNG: nhánh ghi đường lui đã có chính dòng DB làm bằng chứng. Lỗi ghi mốc không được làm hỏng poll (mẫu đã nằm ở
    Redis) — chỉ mất lớp bảo vệ này cho lần poll đó.

    Review ngoài lần 5, P1: dấu chỉ trỏ tới mẫu MỚI NHẤT nên chỉ bảo vệ đuôi chuỗi — mẫu cũ hơn đã nhận rồi mất nằm giữa
    lịch sử DB cũ và mẫu Redis mới thì không còn dấu vết nào. Nên TRƯỚC khi dấu nhảy qua mẫu cũ, kiểm chứng Redis còn giữ mẫu
    đó; không còn (hoặc không kiểm chứng được) ⇒ ghi `metrics_lost_upto` = dấu cũ. Thứ tự quan trọng: ghi mốc mất mẫu TRƯỚC khi
    dấu tiến lên, nếu không chết giữa chừng làm mất luôn dấu vết."""
    prior = device.metrics_seen_at
    if prior is not None and prior < ts:
        try:
            if _redis_holds_previous_sample(device, ts, expected_if_ids) is not True:
                _record_lost_upto(device, prior)
        except Exception:
            logger.warning("Device %s: không kiểm chứng được Redis còn giữ mẫu cũ", device.name, exc_info=True)
    try:
        Device.objects.filter(pk=device.pk).filter(
            Q(metrics_seen_at__isnull=True) | Q(metrics_seen_at__lt=ts)
        ).update(metrics_seen_at=ts)
        if device.metrics_seen_at is None or device.metrics_seen_at < ts:
            device.metrics_seen_at = ts
    except Exception:
        logger.warning("Device %s: không ghi được metrics_seen_at", device.name, exc_info=True)


def _save_metrics_db(device: Device, data: NormalizedData) -> None:
    with transaction.atomic():
        _save_system_health(device, data)
        if data.interfaces:
            _save_interface_stats(device, data)
        if data.extra.get("vms"):
            _save_vm_stats(device, data)
        if data.extra.get("volumes"):
            _save_volume_stats(device, data)
        if data.extra.get("wifi_aps"):
            _save_wifi_ap_stats(device, data)
        if data.extra.get("wifi_clients"):
            _save_wifi_client_stats(device, data)


def _save_metrics_cache(device: Device, data: NormalizedData) -> bool:
    """Ghi snapshot + ring-buffer vào Redis; persist DB khi đổi trạng thái quan trọng.

    Trả True nếu ghi cache thành công; False → caller fallback ghi DB (Redis lỗi).
    """
    prev = metrics_cache.get_latest(device.id) or {}
    prev_ifaces = prev.get("interfaces") or {}
    prev_ts_epoch = prev.get("ts")

    # Interface inventory VẪN ghi DB (đổi thưa) — cần mapping name->Interface (có PK)
    # để khoá ring-buffer theo interface_id và ghi evidence.
    existing_ifaces = _sync_interface_inventory(device, data) if data.interfaces else {}

    ts_epoch = data.timestamp.timestamp()
    snap_ifaces: dict[str, dict] = {}
    if_samples: dict[int, dict] = {}
    changed_ifaces: list[tuple] = []  # (Interface, InterfaceData, in_mbps, out_mbps)

    for iface_data in data.interfaces:
        iface = existing_ifaces.get(_iface_key(iface_data.name))
        if not iface:
            continue
        prev_if = prev_ifaces.get(str(iface.id))
        in_mbps, out_mbps = _calc_mbps_from_snapshot(
            prev_if, prev_ts_epoch, iface_data, data.timestamp, device.collect_interval
        )
        snap_ifaces[str(iface.id)] = {
            "if_index": iface_data.if_index,
            "name": iface_data.name,
            "status": iface_data.status,
            "in_bytes": iface_data.in_bytes,
            "out_bytes": iface_data.out_bytes,
            "in_errors": iface_data.in_errors,
            "out_errors": iface_data.out_errors,
            "in_mbps": in_mbps,
            "out_mbps": out_mbps,
            "speed": iface_data.speed_mbps,
        }
        if_samples[iface.id] = {
            "ts": ts_epoch,
            "in_mbps": in_mbps,
            "out_mbps": out_mbps,
            "status": iface_data.status,
            "in_errors": iface_data.in_errors,
            "out_errors": iface_data.out_errors,
        }
        # Chỉ coi là "đổi trạng thái" khi có mẫu trước (tránh nhiễu ở poll đầu).
        if prev_if and prev_if.get("status") != iface_data.status:
            changed_ifaces.append((iface, iface_data, in_mbps, out_mbps))

    extra = {k: v for k, v in data.extra.items() if k not in _EXTRA_SKIP_KEYS}
    snapshot = {
        "ts": ts_epoch,
        "cpu": data.cpu_percent,
        "mem": data.mem_percent,
        "uptime": data.uptime_secs or None,
        "extra": extra,
        "interfaces": snap_ifaces,
        "vms": data.extra.get("vms", []),
        "volumes": data.extra.get("volumes", []),
        "wifi_aps": data.extra.get("wifi_aps", []),
        "wifi_clients": data.extra.get("wifi_clients", []),
        "cpu_hv_percent": data.cpu_hv_percent,
        "mem_available_mb": data.mem_available_mb,
        "disk_read_iops": data.disk_read_iops,
        "disk_write_iops": data.disk_write_iops,
        "disk_read_latency_ms": data.disk_read_latency_ms,
        "disk_write_latency_ms": data.disk_write_latency_ms,
        "net_mbps_total": data.net_mbps_total,
        "disk_read_throughput_mbps": data.disk_read_throughput_mbps,
        "disk_write_throughput_mbps": data.disk_write_throughput_mbps,
        "disk_queue_length": data.disk_queue_length,
        "avg_io_size_kb": data.avg_io_size_kb,
    }

    ok_latest = metrics_cache.set_latest(device.id, snapshot)
    ok_series = metrics_cache.push_series(
        device.id,
        _device_scalar_sample(data, ts_epoch),
        if_samples,
    )
    if not (ok_latest and ok_series):
        return False

    _persist_change_events(device, data, prev, changed_ifaces)
    return True


_REPL_HEALTHY = {"Normal", "NotConfigured"}


def _device_scalar_sample(data: NormalizedData, ts_epoch: float) -> dict:
    """Mẫu scalar cấp thiết bị cho ring-buffer sys — nguồn cho sustained alert.

    Ngoài cpu/mem còn gói các scalar/poll: vm running (vmr), vm repl unhealthy
    (vmu), wifi client (wc), wifi AP offline (wao) — chỉ thêm key khi thiết bị
    có dữ liệu tương ứng.
    """
    sample = {"ts": ts_epoch, "cpu": data.cpu_percent, "mem": data.mem_percent}
    vms = data.extra.get("vms")
    if vms:
        sample["vmr"] = sum(1 for v in vms if v.get("state") == "Running")
        sample["vmu"] = sum(1 for v in vms if (v.get("repl_health") or "") not in _REPL_HEALTHY)
    aps = data.extra.get("wifi_aps")
    if aps:
        sample["wao"] = sum(1 for a in aps if not a.get("is_online"))
        clients = data.extra.get("wifi_clients")
        sample["wc"] = len(clients) if clients else sum(int(a.get("client_count") or 0) for a in aps)
    # HyperV host performance counters (short-key ring-buffer m:series:sys) — chỉ thêm
    # key khi có giá trị (is not None), khác quy ước sentinel-0 của mem_percent.
    _HOST_PERF_SHORT_KEYS = (
        ("chv", data.cpu_hv_percent),
        ("mav", data.mem_available_mb),
        ("dri", data.disk_read_iops),
        ("dwi", data.disk_write_iops),
        ("drl", data.disk_read_latency_ms),
        ("dwl", data.disk_write_latency_ms),
        ("nmb", data.net_mbps_total),
        ("drt", data.disk_read_throughput_mbps),
        ("dwt", data.disk_write_throughput_mbps),
        ("dql", data.disk_queue_length),
        ("aio", data.avg_io_size_kb),
    )
    for short_key, value in _HOST_PERF_SHORT_KEYS:
        if value is not None:
            sample[short_key] = value
    return sample


def _vms_changed(prev_vms: list, new_vms: list) -> bool:
    """True nếu có VM đổi state/repl_health (hoặc thêm/bớt VM). Poll đầu (prev rỗng) → False."""
    if not prev_vms:
        return False

    def _idx(vms):
        return {v.get("name"): (v.get("state"), v.get("repl_health")) for v in vms}

    p, n = _idx(prev_vms), _idx(new_vms)
    if set(p) != set(n):
        return True
    return any(p[k] != n[k] for k in n)


def _persist_change_events(device: Device, data: NormalizedData, prev: dict, changed_ifaces: list) -> None:
    """Ghi evidence xuống Postgres khi đổi trạng thái quan trọng (event thưa).

    - Interface up/down → ghi InterfaceStats cho các cổng đổi trạng thái.
    - VM đổi state/replication → ghi snapshot VMStats.
    - Kèm 1 SystemHealth làm ngữ cảnh CPU/mem tại thời điểm sự cố.
    """
    vm_changed = _vms_changed(prev.get("vms") or [], data.extra.get("vms") or [])
    if not (changed_ifaces or vm_changed):
        return
    with transaction.atomic():
        _save_system_health(device, data, evidence=True)
        if changed_ifaces:
            InterfaceStats.objects.bulk_create([
                InterfaceStats(
                    interface=iface,
                    timestamp=data.timestamp,
                    status=idata.status,
                    in_bytes=idata.in_bytes,
                    out_bytes=idata.out_bytes,
                    in_errors=idata.in_errors,
                    out_errors=idata.out_errors,
                    in_mbps=in_mbps,
                    out_mbps=out_mbps,
                )
                for (iface, idata, in_mbps, out_mbps) in changed_ifaces
            ])
        if vm_changed:
            _save_vm_stats(device, data)
    logger.info(
        "Device %s: ghi evidence DB (if_changes=%d vm_changed=%s)",
        device.name, len(changed_ifaces), vm_changed,
    )


def _save_system_health(device: Device, data: NormalizedData, *, evidence: bool = False) -> None:
    """`evidence=True`: dòng bằng chứng/ngữ cảnh ở cache-mode (xem `EVIDENCE_KEY`) — không phải mẫu poll thường."""
    # Lưu extra vendor-specific (bỏ các key list lớn đã xử lý riêng).
    extra = {k: v for k, v in data.extra.items() if k not in _EXTRA_SKIP_KEYS}
    if evidence:
        extra[EVIDENCE_KEY] = True
    SystemHealth.objects.create(
        device=device,
        timestamp=data.timestamp,
        cpu_percent=data.cpu_percent,
        mem_percent=data.mem_percent,
        uptime_secs=data.uptime_secs or None,
        extra=extra,
        cpu_hv_percent=data.cpu_hv_percent,
        mem_available_mb=data.mem_available_mb,
        disk_read_iops=data.disk_read_iops,
        disk_write_iops=data.disk_write_iops,
        disk_read_latency_ms=data.disk_read_latency_ms,
        disk_write_latency_ms=data.disk_write_latency_ms,
        net_mbps_total=data.net_mbps_total,
        disk_read_throughput_mbps=data.disk_read_throughput_mbps,
        disk_write_throughput_mbps=data.disk_write_throughput_mbps,
        disk_queue_length=data.disk_queue_length,
        avg_io_size_kb=data.avg_io_size_kb,
    )


from datetime import timedelta


def _normalize_text(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_trunk_interface(device: Device, iface_data: InterfaceData) -> bool:
    # Dữ liệu mode thật từ SNMP (Q-BRIDGE) ưu tiên cao nhất — trunk/hybrid = uplink.
    if iface_data.port_mode in ("trunk", "hybrid"):
        return True

    # Manual cấu hình.
    normalized_manual = {_normalize_text(p) for p in (device.uplink_ports or [])}
    normalized_name = _normalize_text(iface_data.name)
    if normalized_name in normalized_manual:
        return True

    # port_mode == "access" THẬT → access, không để heuristic tên/speed ép thành uplink.
    if iface_data.port_mode == "access":
        return False

    normalized_desc = _normalize_text(iface_data.description)
    if normalized_desc and any(keyword in normalized_desc for keyword in TRUNK_DESC_KEYWORDS):
        return True

    if normalized_name.startswith(TRUNK_NAME_PREFIXES):
        return True

    # Uplink liên tầng thường là 10G+ (heuristic mềm, tránh phụ thuộc vendor).
    if float(iface_data.speed_mbps or 0) >= 10000:
        return True

    return False


def _iface_key(name: str | None) -> str:
    """Khoá định danh interface — theo TÊN (ổn định cho cả SNMP & SSH).

    SSH collector sinh if_index theo vị trí block CLI nên không ổn định giữa các poll;
    tên cổng mới là định danh bền vững. Chuẩn hoá strip + casefold để tránh lệch hoa/thường.
    """
    return (name or "").strip().casefold()


def _sync_interface_inventory(device: Device, data: NormalizedData) -> dict[str, Interface]:
    """Đồng bộ bảng Interface (inventory thiết bị) — khớp theo TÊN, bulk create/update.

    Trả về mapping {name_key: Interface} có PK (dùng cho cả DB-mode lẫn cache-mode).
    Đây là dữ liệu cấu hình đổi thưa → vẫn ghi Postgres ở cả hai mode.
    """
    existing_ifaces = {_iface_key(i.name): i for i in Interface.objects.filter(device=device)}
    new_ifaces = []
    update_ifaces = []
    seen_new_keys: set[str] = set()

    for iface_data in data.interfaces:
        key = _iface_key(iface_data.name)
        if key not in existing_ifaces:
            # Hai tên khác nhau chỉ khác phần đuôi sau khi cắt (hoặc ifDescr trùng) cùng key → chỉ
            # tạo 1 dòng, nếu không bulk_create vi phạm unique_together và hỏng cả lần ghi.
            if key in seen_new_keys:
                continue
            seen_new_keys.add(key)
        if key in existing_ifaces:
            iface = existing_ifaces[key]
            next_is_uplink = _is_trunk_interface(device, iface_data)
            # Không ghi đè VLAN bằng None khi nguồn (vd SSH) không thu được VLAN.
            next_access_vlan = (
                iface_data.access_vlan
                if iface_data.access_vlan is not None
                else iface.access_vlan
            )
            # Tương tự cho port_mode: nguồn không cấp (None/rỗng) → giữ giá trị cũ.
            next_port_mode = iface_data.port_mode or iface.port_mode
            if (
                iface.if_index != iface_data.if_index
                or iface.description != iface_data.description
                or iface.is_uplink != next_is_uplink
                or iface.access_vlan != next_access_vlan
                or iface.port_mode != next_port_mode
            ):
                iface.if_index = iface_data.if_index
                iface.description = iface_data.description
                iface.is_uplink = next_is_uplink
                iface.access_vlan = next_access_vlan
                iface.port_mode = next_port_mode
                update_ifaces.append(iface)
        else:
            new_ifaces.append(Interface(
                device=device,
                if_index=iface_data.if_index,
                name=iface_data.name,
                description=iface_data.description,
                is_uplink=_is_trunk_interface(device, iface_data),
                access_vlan=iface_data.access_vlan,
                port_mode=iface_data.port_mode or "",
            ))

    if new_ifaces:
        # bulk_create trả về objects đã có ID (nếu Postgres) hoặc không. Để an toàn, fetch lại:
        Interface.objects.bulk_create(new_ifaces)
        # Fetch lại để có PK
        existing_ifaces = {_iface_key(i.name): i for i in Interface.objects.filter(device=device)}

    if update_ifaces:
        Interface.objects.bulk_update(update_ifaces, ["if_index", "description", "is_uplink", "access_vlan", "port_mode"])

    return existing_ifaces


def _save_interface_stats(device: Device, data: NormalizedData) -> None:
    # 1. Đồng bộ Interface list (giảm N get_or_create thành bulk) — khớp theo TÊN.
    existing_ifaces = _sync_interface_inventory(device, data)

    # 2. Fetch "previous stats" cho tất cả interface bằng 1 query.
    # Cửa sổ tìm prev phải đủ rộng: nhịp poll thực do Celery beat quyết định (vd 300s)
    # và có thể LỚN hơn device.collect_interval. Nếu cửa sổ < nhịp poll thật,
    # prev luôn nằm ngoài → mbps=0 giả. Dùng floor METRIC_PREV_LOOKBACK_SECS.
    lookback = max(
        device.collect_interval * 3,
        int(getattr(settings, "METRIC_PREV_LOOKBACK_SECS", 900)),
    )
    cutoff = data.timestamp - timedelta(seconds=lookback)
    recent_stats = InterfaceStats.objects.filter(
        interface__device=device,
        timestamp__lt=data.timestamp,
        timestamp__gte=cutoff,
    ).order_by("interface_id", "-timestamp")

    prev_stats_dict = {}
    for stat in recent_stats:
        if stat.interface_id not in prev_stats_dict:
            prev_stats_dict[stat.interface_id] = stat

    # 3. Tính toán và bulk_create
    stats_to_create = []
    for iface_data in data.interfaces:
        iface = existing_ifaces.get(_iface_key(iface_data.name))
        if not iface:
            continue

        prev_stat = prev_stats_dict.get(iface.id)
        in_mbps, out_mbps = _calc_mbps(prev_stat, iface_data, data.timestamp, device.collect_interval)

        stats_to_create.append(InterfaceStats(
            interface=iface,
            timestamp=data.timestamp,
            status=iface_data.status,
            in_bytes=iface_data.in_bytes,
            out_bytes=iface_data.out_bytes,
            in_errors=iface_data.in_errors,
            out_errors=iface_data.out_errors,
            in_mbps=in_mbps,
            out_mbps=out_mbps,
        ))

    if stats_to_create:
        InterfaceStats.objects.bulk_create(stats_to_create)


def _counter_delta(prev: int, new: int, max_counter: int) -> int:
    """Tính delta của counter, phân biệt wrap (tràn) vs reset (reboot).

    - Delta dương: bình thường.
    - Delta âm + prev gần trần (> 90% max): counter tràn vòng → cộng max_counter.
    - Delta âm khác: counter bị reset (thiết bị reboot) → trả 0 (bỏ mẫu, tránh spike giả).
    """
    delta = new - prev
    if delta >= 0:
        return delta
    if prev > max_counter * 0.9:
        return delta + max_counter
    return 0


def _compute_mbps_core(
    prev_in: int,
    prev_out: int,
    prev_ts: datetime | None,
    new: InterfaceData,
    new_timestamp: datetime | None,
    fallback_interval_secs: int,
) -> tuple[float, float]:
    """Lõi tính Mbps từ delta bytes / delta time — dùng chung DB-mode & cache-mode."""
    try:
        # seconds between samples
        interval = float(fallback_interval_secs or 0)
        if new_timestamp and prev_ts:
            dt = (new_timestamp - prev_ts).total_seconds()
            if dt > 0:
                interval = float(dt)
        if not interval:
            return 0.0, 0.0

        max_counter = 2**64  # ifHCIn/OutOctets are 64-bit counters
        delta_in = _counter_delta(int(prev_in), int(new.in_bytes), max_counter)
        delta_out = _counter_delta(int(prev_out), int(new.out_bytes), max_counter)

        in_mbps  = (delta_in * 8) / (interval * 1_000_000)
        out_mbps = (delta_out * 8) / (interval * 1_000_000)

        # Sanity cap: nếu tốc độ tính ra vượt ~1.5× tốc độ cổng → coi là số rác
        # (counter nhảy bất thường) và bỏ mẫu thay vì lưu spike giả.
        speed = float(getattr(new, "speed_mbps", 0) or 0)
        if speed > 0:
            cap = speed * 1.5
            if in_mbps > cap or out_mbps > cap:
                logger.debug(
                    "_compute_mbps: bỏ mẫu vì vượt cap (in=%.1f out=%.1f cap=%.1f Mbps)",
                    in_mbps, out_mbps, cap,
                )
                return 0.0, 0.0

        return round(in_mbps, 3), round(out_mbps, 3)
    except Exception as exc:
        logger.debug("_compute_mbps failed: %s", exc)
        return 0.0, 0.0


def _calc_mbps(
    prev_stat: InterfaceStats | None,
    new: InterfaceData,
    new_timestamp: datetime | None,
    fallback_interval_secs: int,
) -> tuple[float, float]:
    """DB-mode: prev từ row InterfaceStats trước."""
    if not prev_stat:
        return 0.0, 0.0
    return _compute_mbps_core(
        prev_stat.in_bytes, prev_stat.out_bytes, prev_stat.timestamp,
        new, new_timestamp, fallback_interval_secs,
    )


def _calc_mbps_from_snapshot(
    prev_if: dict | None,
    prev_ts_epoch: float | None,
    new: InterfaceData,
    new_timestamp: datetime | None,
    fallback_interval_secs: int,
) -> tuple[float, float]:
    """Cache-mode: prev từ snapshot Redis trước (bytes trong prev_if, ts chung của snapshot)."""
    if not prev_if or prev_ts_epoch is None:
        return 0.0, 0.0
    prev_ts = metrics_cache.epoch_to_dt(prev_ts_epoch)
    return _compute_mbps_core(
        prev_if.get("in_bytes", 0), prev_if.get("out_bytes", 0), prev_ts,
        new, new_timestamp, fallback_interval_secs,
    )


def _save_vm_stats(device: Device, data: NormalizedData) -> None:
    vms_to_create = []
    for vm in data.extra.get("vms", []):
        try:
            vms_to_create.append(VMStats(
                device=device,
                timestamp=data.timestamp,
                vm_name=_fit(VMStats, "vm_name", vm.get("name")),
                state=_fit(VMStats, "state", vm.get("state")),
                cpu_percent=float(vm.get("cpu_percent") or 0),
                mem_assigned_mb=int(vm.get("mem_mb") or 0),
                repl_health=(vm.get("repl_health") or "")[:50],
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("Device %s: skip VM %r — bad data: %s",
                           device.name, vm.get("name"), exc)
    if vms_to_create:
        VMStats.objects.bulk_create(vms_to_create)


def _save_volume_stats(device: Device, data: NormalizedData) -> None:
    volumes_to_create = []
    for vol in data.extra.get("volumes", []):
        try:
            volumes_to_create.append(VolumeStats(
                device=device,
                timestamp=data.timestamp,
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
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("Device %s: skip volume %r — bad data: %s",
                           device.name, vol.get("name"), exc)
    if volumes_to_create:
        VolumeStats.objects.bulk_create(volumes_to_create)


def _save_wifi_ap_stats(device: Device, data: NormalizedData) -> None:
    aps_to_create = []
    for ap in data.extra.get("wifi_aps", []):
        try:
            aps_to_create.append(WifiApStats(
                device=device,
                timestamp=data.timestamp,
                ap_name=str(ap.get("name") or "")[:200],
                ap_mac=str(ap.get("mac") or "")[:32],
                ap_ip=str(ap.get("ip") or "")[:64],
                ap_group=str(ap.get("group") or "")[:128],
                is_online=bool(ap.get("is_online")),
                run_state=str(ap.get("run_state") or "")[:32],
                client_count=int(ap.get("client_count") or 0),
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("Device %s: skip AP %r — bad data: %s",
                           device.name, ap.get("name"), exc)
    if aps_to_create:
        WifiApStats.objects.bulk_create(aps_to_create)


def _save_wifi_client_stats(device: Device, data: NormalizedData) -> None:
    clients_to_create = []
    for c in data.extra.get("wifi_clients", []):
        try:
            rssi = c.get("rssi")
            clients_to_create.append(WifiClientStats(
                device=device,
                timestamp=data.timestamp,
                mac=str(c.get("mac") or "")[:32],
                ip=str(c.get("ip") or "")[:64],
                ssid=str(c.get("ssid") or "")[:128],
                ap_name=str(c.get("ap_name") or "")[:200],
                radio=str(c.get("radio") or "")[:32],
                rssi=int(rssi) if rssi not in (None, "") else None,
                online_secs=int(c.get("online_secs") or 0),
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("Device %s: skip WiFi client %r — bad data: %s",
                           device.name, c.get("mac"), exc)
    if clients_to_create:
        WifiClientStats.objects.bulk_create(clients_to_create)
