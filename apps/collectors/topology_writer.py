"""Persist TopologyLink từ LLDP discovery hoặc bảng MAC (FDB)."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

from apps.collectors.topology_fdb import collect_fdb_ap_mappings
from apps.collectors.topology_lldp import collect_lldp_neighbors, normalize_mac
from apps.collectors.topology_walk import walk_memo
from apps.devices.topology_match import (
    get_default_ac_device,
    load_ac_ap_snapshot,
    match_lldp_to_ap,
)
from apps.devices.topology_switch_match import (
    build_switch_device_index,
    match_neighbor_to_switch,
)

if TYPE_CHECKING:
    from apps.devices.models import Device, TopologyLink

logger = logging.getLogger(__name__)

STALE_MISS_THRESHOLD = 3


def _monotonic() -> float:
    """Đồng hồ cho deadline — tách riêng để test thay bằng đồng hồ giả."""
    return time.monotonic()


def _count_miss(link: TopologyLink) -> None:
    """Đếm 1 lượt không thấy link; chạm STALE_MISS_THRESHOLD thì đánh stale.

    Link ĐÃ stale thì dừng đếm và không ghi nữa. Không đường đọc nào dùng `miss_count` ngoài ngưỡng
    stale, còn cột là PositiveSmallIntegerField (smallint, tối đa 32767): cộng mãi (≈39/ngày đo trên
    prod) thì tới 32768 mỗi lượt `save()` ném DataError và phần còn lại của switch đó bị bỏ qua. Link
    thấy lại được `_upsert_*` ghi `miss_count=0, is_stale=False` nên dừng đếm không cản hồi phục.
    """
    misses = link.miss_count or 0
    if link.is_stale and misses >= STALE_MISS_THRESHOLD:
        return
    link.miss_count = min(misses + 1, STALE_MISS_THRESHOLD)
    if link.miss_count >= STALE_MISS_THRESHOLD:
        link.is_stale = True
    # Bản `link` do worker nạp từ trước: admin có thể đã ghim dòng này thành thủ công trong lúc chờ. Điều
    # kiện nằm trong chính câu UPDATE (Postgres kiểm lại sau khi nhận khoá dòng) nên không đánh stale link thủ công.
    from apps.devices.models import TopologyLink

    TopologyLink.objects.filter(pk=link.pk).exclude(match_method="manual").update(
        miss_count=link.miss_count, is_stale=link.is_stale,
    )


class ApFdbWalkError(Exception):
    """Walk FDB bổ sung của pha AP lỗi SAU KHI pha LLDP của cùng switch đã đọc xong.

    Tách khỏi lỗi chung để `discover_all_switches` biết dữ liệu LLDP còn dùng được (bảng LLDP đã nằm
    trong bộ nhớ đệm của lượt) và vẫn chạy pha uplink LLDP — nếu không, 1 walk FDB hỏng làm switch
    mất luôn cả cập nhật/đánh stale uplink. Lỗi LLDP thì KHÔNG dùng lớp này: pha uplink đọc đúng bảng
    đó nên thử lại chỉ nhân đôi timeout trên switch chết.

    `links`/`confirmed`: số link AP LLDP đã ghi trước khi FDB lỗi (để thống kê lượt không bị hụt).
    """

    def __init__(self, cause: BaseException, links: int = 0, confirmed: int = 0) -> None:
        super().__init__(str(cause))
        self.links = links
        self.confirmed = confirmed


def _manual_ports(device: Device) -> set[str]:
    """Cổng của `device` đang có link THỦ CÔNG (mọi loại) — discovery KHÔNG được ghi đè.

    Link thủ công là khẳng định tường minh của admin (thường vì tự động không dò được). Link unique theo
    `(device, port)` nên `update_or_create` của collector sẽ đè luôn `match_method="manual"` thành
    "mac"/"name"/"lldp": link rời khỏi danh sách quản lý thủ công và sau đó bị đánh stale như link tự
    động. Chỉ API thủ công (`topology_links_api`) được ghi vào các cổng này.
    """
    from apps.devices.models import TopologyLink

    return set(
        TopologyLink.objects.filter(local_device=device, match_method="manual")
        .values_list("local_port", flat=True)
    )


def _resolve_interface(device: Device, port_name: str):
    from apps.devices.models import Interface

    return Interface.objects.filter(device=device, name=port_name).first()


def _write_link(device: Device, local_port: str, defaults: dict, *, match_method: str) -> bool:
    """Ghi `(device, port)` và trả True nếu đã ghi. Mọi đường ghi TopologyLink của collector và API đi qua đây.

    Link thủ công (`match_method="manual"`) chỉ bị ghi đè bởi một lần ghi cũng là thủ công (API của admin).
    Mọi lần ghi khác (discovery) KIỂM TRA RỒI GHI trong CÙNG transaction, dưới khoá chung với API:
      - dòng đã có: `SELECT … FOR UPDATE` — API (`update_or_create` cũng khoá dòng) và discovery tuần tự hoá, bên
        đến sau đọc lại bản đã commit của bên trước;
      - dòng chưa có: tranh nhau INSERT trên unique `(device, port)`. Bên thua nhận IntegrityError rồi đọc lại
        và áp dụng đúng quy tắc trên — KHÔNG được dùng `update_or_create` ở đây vì nhánh IntegrityError của
        nó tự `UPDATE` bản của bên thắng, tức đè link thủ công vừa được tạo.
    Tập `_manual_ports` đọc trước khi chờ mạng chỉ là bước lọc sớm; nó cũ ngay khi admin lưu giữa chừng (review
    ngoài 2026-10-21, tái hiện bằng 2 connection Postgres), nên quyết định cuối cùng luôn ở đây.
    """
    from django.db import IntegrityError, transaction

    from apps.devices.models import TopologyLink

    lookup = {"local_device": device, "local_port": local_port}
    overwrite_manual = match_method == "manual"
    with transaction.atomic():
        link = TopologyLink.objects.select_for_update().filter(**lookup).first()
        if link is None:
            try:
                with transaction.atomic():  # savepoint: IntegrityError không làm hỏng transaction ngoài
                    TopologyLink.objects.create(**lookup, **defaults)
                return True
            except IntegrityError:
                link = TopologyLink.objects.select_for_update().filter(**lookup).first()
                if link is None:  # không phải đụng khoá unique — lỗi khác
                    raise
        if link.match_method == "manual" and not overwrite_manual:
            return False
        for field, value in defaults.items():
            setattr(link, field, value)
        link.save()
        return True


def _upsert_ap_link(
    device: Device,
    local_port: str,
    *,
    mac: str,
    ap_name: str,
    protocol: str,
    match_method: str,
    is_confirmed: bool,
    remote_sys_name: str = "",
    remote_chassis_id: str = "",
    remote_port_id: str = "",
) -> bool:
    """Ghi link AP. Trả True khi đã ghi VÀ link đã xác nhận với AC (dùng để đếm `confirmed`)."""
    iface = _resolve_interface(device, local_port)
    written = _write_link(
        device,
        local_port,
        {
            "link_kind": "ap",
            "local_interface": iface,
            "remote_device": None,
            "remote_ap_mac": mac,
            "remote_ap_name": ap_name,
            "remote_sys_name": remote_sys_name,
            "remote_chassis_id": remote_chassis_id,
            "remote_port_id": remote_port_id,
            "protocol": protocol,
            "match_method": match_method,
            "is_confirmed": is_confirmed,
            "is_stale": False,
            "miss_count": 0,
            "last_seen": timezone.now(),
        },
        match_method=match_method,
    )
    return written and is_confirmed


def _upsert_switch_link(
    device: Device,
    local_port: str,
    remote: Device,
    *,
    remote_sys_name: str = "",
    remote_port_id: str = "",
    is_confirmed: bool = True,
    protocol: str = "lldp",
    match_method: str | None = None,
) -> bool:
    """Ghi link switch↔switch. Trả True nếu đã ghi (False: cổng đã bị admin ghim thủ công)."""
    iface = _resolve_interface(device, local_port)
    if match_method is None:
        match_method = "name" if is_confirmed else "lldp"
    return _write_link(
        device,
        local_port,
        {
            "link_kind": "switch",
            "local_interface": iface,
            "remote_device": remote,
            "remote_ap_mac": "",
            "remote_ap_name": remote.name,
            "remote_sys_name": remote_sys_name,
            "remote_port_id": remote_port_id,
            "protocol": protocol,
            "match_method": match_method,
            "is_confirmed": is_confirmed,
            "is_stale": False,
            "miss_count": 0,
            "last_seen": timezone.now(),
        },
        match_method=match_method,
    )


def discover_switch_links_fdb(switches: list[Device]) -> int:
    """Phát hiện cạnh switch↔switch qua FDB → upsert TopologyLink(link_kind=switch).

    Orient parent (gần core)→child bằng BFS để admin/đồ thị đọc tự nhiên; graph builder
    vẫn tự reorient theo BFS nên hướng lưu không bắt buộc.

    ALL-OR-NOTHING: thiếu MAC tự thân hoặc FDB của bất kỳ switch nào → ném `IncompleteSwitchDataError`,
    KHÔNG ghi và KHÔNG đánh stale gì (link cũ giữ nguyên). Suy luận cạnh trực tiếp cần biết mọi
    switch trung gian; bỏ sót 1 switch tạo cạnh ma giữa 2 switch hai bên.
    """
    from apps.collectors.topology_switch_fdb import (
        IncompleteSwitchDataError,
        build_switch_mac_registry,
        discover_switch_adjacency,
    )
    from apps.devices.models import TopologyLink
    from apps.devices.topology_hierarchy import bfs_depths, find_core_device

    switches = list(switches)
    by_id = {sw.id: sw for sw in switches}
    # Cổng có link thủ công (mọi switch) — discovery không ghi đè (xem `_manual_ports`).
    manual_keys = set(
        TopologyLink.objects.filter(match_method="manual")
        .values_list("local_device_id", "local_port")
    )
    missing: list[str] = []
    registry = build_switch_mac_registry(switches, missing=missing)
    if missing:
        raise IncompleteSwitchDataError(missing)
    pairs = discover_switch_adjacency(switches, registry, missing=missing)
    if missing:
        raise IncompleteSwitchDataError(missing)

    # BFS từ core để chọn parent (đầu nông hơn).
    adjacency: dict[int, set[int]] = {}
    for a, _pa, b, _pb in pairs:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    core = find_core_device()
    depths = bfs_depths(adjacency, core.id if core else None)

    def depth_of(dev_id: int) -> int:
        return depths.get(dev_id, 10_000)

    seen_keys: set[tuple[int, str]] = set()
    count = 0
    for a, port_a, b, port_b in pairs:
        if depth_of(a) <= depth_of(b):
            parent, parent_port, child = a, port_a, b
        else:
            parent, parent_port, child = b, port_b, a
        parent_dev = by_id.get(parent)
        child_dev = by_id.get(child)
        if not parent_dev or not child_dev:
            continue
        if (parent, parent_port) in manual_keys:
            continue
        if not _upsert_switch_link(
            parent_dev,
            parent_port,
            child_dev,
            remote_sys_name=child_dev.name,
            is_confirmed=True,
            protocol="fdb",
            match_method="mac",
        ):
            continue  # admin vừa ghim cổng này giữa chừng (manual_keys là ảnh chụp cũ)
        seen_keys.add((parent, parent_port))
        count += 1

    # Stale các link FDB không còn thấy (giữ manual).
    stale_qs = TopologyLink.objects.filter(
        link_kind="switch",
        protocol="fdb",
    ).exclude(match_method="manual")
    for link in stale_qs:
        if (link.local_device_id, link.local_port) in seen_keys:
            continue
        _count_miss(link)

    logger.info("Topology switch-FDB: %d cạnh switch↔switch", count)
    return count


def discover_switch_uplinks(
    device: Device,
    device_index: tuple[dict, dict],
) -> int:
    """LLDP neighbor là switch khác → TopologyLink link_kind=switch."""
    by_name, by_ip = device_index
    neighbors = collect_lldp_neighbors(device, ap_only=False, ap_macs=set())
    manual_ports = _manual_ports(device)
    seen_ports: set[str] = set()
    count = 0

    for neighbor in neighbors:
        if neighbor.is_ap_candidate:
            continue
        remote = match_neighbor_to_switch(neighbor, by_name, by_ip, device)
        if not remote:
            continue
        if neighbor.local_port in manual_ports:
            continue
        if not _upsert_switch_link(
            device,
            neighbor.local_port,
            remote,
            remote_sys_name=neighbor.remote_sys_name,
            remote_port_id=neighbor.remote_port_id,
        ):
            continue  # admin vừa ghim cổng này giữa chừng (manual_ports là ảnh chụp cũ)
        seen_ports.add(neighbor.local_port)
        count += 1

    from apps.devices.models import TopologyLink

    stale_qs = TopologyLink.objects.filter(
        local_device=device,
        link_kind="switch",
        protocol="lldp",
    ).exclude(local_port__in=seen_ports).exclude(match_method="manual")

    for link in stale_qs:
        _count_miss(link)

    if count:
        logger.info("Topology %s: %d switch uplink(s) via LLDP", device.name, count)
    return count


def upsert_switch_topology(device: Device, ac_device=None) -> tuple[int, int]:
    """Discovery AP của 1 switch: LLDP (ưu tiên) + FDB bổ sung → upsert TopologyLink AP.

    Hai nguồn chạy CẢ HAI mỗi lượt khi AC có danh sách AP. FDB chỉ thêm AP mà LLDP KHÔNG thấy (MAC
    chưa có link từ LLDP và cổng chưa bị LLDP chiếm — LLDP thắng khi tranh cổng, vì link unique theo
    `(device, port)`). Trước đây FDB chỉ chạy khi LLDP không thấy AP nào: switch có LLDP với 1 phần AP
    thì các AP còn lại không bao giờ được map, và link của nguồn không chạy lượt này đứng yên (không
    làm mới, không stale). Walk FDB dùng chung bộ nhớ đệm của lượt (`walk_memo`) với pha
    switch↔switch nên không tốn thêm walk.

    Đánh stale chỉ áp cho link thuộc nguồn ĐÃ chạy lượt này: FDB không chạy được (AC chưa có danh
    sách AP để đối chiếu) thì không đếm trượt link FDB — "không hỏi được" ≠ "không còn".
    """
    if ac_device is None:
        ac_device = get_default_ac_device()

    ap_snapshot = load_ac_ap_snapshot(ac_device)
    ap_macs = set(ap_snapshot.keys())

    neighbors = collect_lldp_neighbors(device, ap_only=True, ap_macs=ap_macs)
    manual_ports = _manual_ports(device)
    seen_ports: set[str] = set()
    lldp_macs: set[str] = set()
    confirmed = 0

    for neighbor in neighbors:
        match = match_lldp_to_ap(neighbor, ac_device)
        mac = normalize_mac(match.ap_mac or neighbor.remote_mac)
        if mac:
            lldp_macs.add(mac)  # LLDP đã cho biết vị trí AP này — FDB không đặt nó sang cổng khác
        if neighbor.local_port in manual_ports:
            continue  # admin đã ghim cổng này: không ghi đè
        seen_ports.add(neighbor.local_port)
        ok = _upsert_ap_link(
            device,
            neighbor.local_port,
            mac=mac,
            ap_name=match.ap_name or neighbor.remote_sys_name,
            protocol="lldp",
            match_method=match.match_method,
            is_confirmed=match.is_confirmed,
            remote_sys_name=neighbor.remote_sys_name,
            remote_chassis_id=neighbor.remote_chassis_id,
            remote_port_id=neighbor.remote_port_id,
        )
        if ok:
            confirmed += 1

    sources = ["lldp"]
    fdb_links = 0
    if ap_macs:
        sources.append("fdb")
        try:
            fdb_entries = collect_fdb_ap_mappings(device, ap_macs)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            # Không đánh stale gì (không biết AP còn hay mất qua FDB); LLDP đã ghi giữ nguyên.
            raise ApFdbWalkError(exc, links=len(seen_ports), confirmed=confirmed) from exc
        for entry in fdb_entries:
            if (
                entry.mac in lldp_macs
                or entry.local_port in seen_ports
                or entry.local_port in manual_ports
            ):
                continue  # LLDP đã cho AP này / đã chiếm cổng này / admin đã ghim cổng này
            seen_ports.add(entry.local_port)
            info = ap_snapshot.get(entry.mac, {})
            ok = _upsert_ap_link(
                device,
                entry.local_port,
                mac=entry.mac,
                ap_name=info.get("ap_name", ""),
                protocol="fdb",
                match_method="mac",
                is_confirmed=True,
            )
            if ok:  # FDB luôn is_confirmed=True nên ok == "đã ghi" (False: admin vừa ghim cổng giữa chừng)
                fdb_links += 1
                confirmed += 1
        logger.info(
            "Topology %s: FDB bổ sung — %d AP MAC match, %d link ngoài LLDP",
            device.name, len(fdb_entries), fdb_links,
        )

    from apps.devices.models import TopologyLink

    stale_qs = TopologyLink.objects.filter(
        local_device=device,
        link_kind="ap",
        protocol__in=sources,
    ).exclude(local_port__in=seen_ports).exclude(match_method="manual")

    for link in stale_qs:
        _count_miss(link)

    link_count = len(seen_ports)
    logger.info(
        "Topology %s: %d AP link(s) via %s (LLDP %d + FDB %d), %d confirmed with AC",
        device.name, link_count, "+".join(sources), len(neighbors), fdb_links, confirmed,
    )
    return link_count, confirmed


def discover_all_switches(deadline_secs: float | None = None) -> dict[str, int]:
    """Chạy discovery trên mọi switch SNMP enabled.

    `deadline_secs`: ngân sách (giây, tính từ lúc vào hàm) để BẮT ĐẦU việc mới — kiểm trước mỗi
    switch và trước pha switch↔switch. Quá hạn thì dừng sạch: switch chưa quét giữ nguyên link
    (không đếm trượt), pha switch↔switch bị bỏ qua nguyên khối (không đánh stale). Dừng bằng đồng
    hồ chứ không dựa vào SoftTimeLimitExceeded: exception đó chỉ bắn một lần và dễ bị nuốt
    (đo prod 2026-10-15: lượt chạy 149,8s, soft limit 120s bị nuốt).

    Một lượt dùng chung kết quả walk: bảng FDB/LLDP của mỗi switch chỉ walk 1 lần.
    """
    from apps.collectors.topology_switch_fdb import IncompleteSwitchDataError
    from apps.devices.models import Device

    started = _monotonic()

    def out_of_time() -> bool:
        return deadline_secs is not None and _monotonic() - started >= deadline_secs

    switches = Device.objects.filter(
        device_type="switch",
        enabled=True,
        protocol="snmp",
    )
    ac = get_default_ac_device()
    device_index = build_switch_device_index()
    total_links = 0
    total_confirmed = 0
    switch_links = 0
    errors = 0
    skipped: list[str] = []
    switch_fdb_skipped = 0

    # Switch lần poll trước lỗi (last_seen=None) quét SAU CÙNG: nếu lượt bị cắt thì cắt đúng switch
    # đang hỏng, không phải switch khoẻ (sort ổn định → giữ thứ tự mặc định trong từng nhóm).
    switch_list = sorted(switches, key=lambda sw: sw.last_seen is None)

    with walk_memo():
        for i, sw in enumerate(switch_list):
            if out_of_time():
                skipped = [s.name for s in switch_list[i:]]
                break
            # Hai pha TÁCH lỗi: walk FDB bổ sung của pha AP hỏng không được kéo theo mất pha uplink
            # LLDP (LLDP đã đọc xong, bảng nằm trong bộ nhớ đệm nên pha uplink không tốn walk).
            # Lỗi KHÁC ở pha AP (vd chính bảng LLDP không đọc được) thì bỏ qua pha uplink như trước:
            # nó đọc đúng bảng đó, thử lại chỉ nhân đôi timeout trên switch chết.
            try:
                n, c = upsert_switch_topology(sw, ac)
                total_links += n
                total_confirmed += c
            except SoftTimeLimitExceeded:
                raise
            except ApFdbWalkError as exc:
                errors += 1
                total_links += exc.links
                total_confirmed += exc.confirmed
                logger.warning(
                    "Topology discovery %s: walk FDB bổ sung lỗi (%s) — link AP giữ nguyên, không "
                    "đánh stale; uplink LLDP vẫn cập nhật", sw.name, exc,
                )
            except Exception as exc:
                errors += 1
                logger.warning("Topology discovery failed %s: %s", sw.name, exc)
                continue
            try:
                switch_links += discover_switch_uplinks(sw, device_index)
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                errors += 1
                logger.warning("Topology discovery (uplink) failed %s: %s", sw.name, exc)

        # Phát hiện cạnh switch↔switch qua FDB (1 lần/run — cần MAC toàn fleet). Cần dữ liệu đủ
        # cho cả fleet nên không chạy dở: hết giờ thì bỏ qua nguyên pha, link giữ nguyên.
        if skipped:
            switch_fdb_skipped = 1
            logger.warning(
                "Topology discovery dừng sớm theo deadline %.0fs: đã quét %d/%d switch, bỏ qua: %s",
                deadline_secs or 0, len(switch_list) - len(skipped), len(switch_list),
                ", ".join(skipped),
            )
        elif out_of_time():
            switch_fdb_skipped = 1
            logger.warning(
                "Topology switch-FDB bỏ qua vòng này: hết ngân sách %.0fs sau khi quét %d switch",
                deadline_secs or 0, len(switch_list),
            )
        else:
            # KHÔNG loại switch đã lỗi ở pha 1 khỏi pha này: lỗi 1 phần (vd chỉ cột LLDP) vẫn cho
            # MAC/FDB đọc được, còn thiếu thật thì pha tự bỏ nguyên khối (IncompleteSwitchDataError).
            try:
                switch_links += discover_switch_links_fdb(switch_list)
            except SoftTimeLimitExceeded:
                raise
            except IncompleteSwitchDataError as exc:
                switch_fdb_skipped = 1
                logger.warning(
                    "Topology switch-FDB bỏ qua vòng này: thiếu MAC/FDB của %s — không đủ cơ sở "
                    "suy ra cạnh trực tiếp (thiếu switch trung gian sẽ tạo cạnh ma); link giữ "
                    "nguyên, không đánh stale",
                    exc,
                )
            except Exception as exc:
                errors += 1
                logger.warning("Topology switch-FDB discovery failed: %s", exc)

    return {
        "switches": len(switch_list),
        "links": total_links,
        "confirmed": total_confirmed,
        "switch_links": switch_links,
        "errors": errors,
        "skipped": len(skipped),
        "switch_fdb_skipped": switch_fdb_skipped,
    }
