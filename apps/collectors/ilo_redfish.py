"""iLO Redfish client — RAID/disk health cho HyperV host, độc lập với WinRM collector.

Chỉ hỗ trợ HPE iLO 4/5 + Redfish SmartStorage extension (OEM-specific) — loại BMC duy nhất
đã verify thật (Hyperv-02/Hyprver03, 2026-09-28, xem CLAUDE.md "Phạm vi" + scratchpad
ilo_probe_output*.txt). KHÔNG build generic BMC layer cho Dell iDRAC/khác — chưa có thiết bị.

KHÔNG kế thừa BaseCollector — khác lifecycle (không test_connection()/NormalizedData, không
qua CollectorFactory), chạy task Celery riêng (`poll_all_ilo`) độc lập _poll_device_once/WinRM.

⚠️ Bẫy đã verify runtime (KHÔNG suy luận, đo thật trên Hyperv-02/Hyprver03 2026-09-28):
- HPE iLO4/5 embedded webserver đóng TCP connection sau ĐÚNG 1 request dù client gửi
  keep-alive. `requests.Session` tái dùng connection cũ đã bị server đóng -> ConnectionError
  xen kẽ đều đặn OK/ERR/OK/ERR. Fix: header `Connection: close` mỗi request + retry 1 lần khi
  ConnectionError (đóng session cũ, requests tự mở connection mới).
- Đĩa "mất" (RAID member không detect) = HTTP 404 thật tại `/DiskDrives/{id}/`
  (`MessageID: Base.0.10.ResourceMissingAtURI`) — KHÔNG phải chỉ biến mất khỏi list.
- `Status.Health` chỉ thấy 3 giá trị thật: "OK"/"Warning"/"Critical" (controller + logical
  drive). Enum lạ chưa từng thấy -> coi Critical (an toàn, không bỏ sót) + log rõ để verify sau.
- `StorageEnclosure` có field `DriveBayCount` + `Location` dạng "1I:3" (ControllerPort:Box);
  disk có `Location` dạng "1I:3:4" (…:Bay) — dùng tiền tố để nhóm đĩa hiện có theo enclosure.

⚠️ Bug đã fix 2026-09-29 (phát hiện qua review code, không phải verify runtime): fetch lỗi ở
`/LogicalDrives/`, `/DataDrives/` hoặc `/StorageEnclosures/` (khác thất bại toàn bộ ở root —
root fail thì `collect_raw()` trả `None` và `poll_all_ilo` bỏ qua, KHÔNG lưu) từng bị nuốt im
lặng: `disks`/`enclosures` list vẫn rỗng nhưng KHÔNG coi là lỗi, khiến `normalize()` cộng dồn ra
`missing_disk_count=0`/`enclosure_mismatch_count=0` GIẢ (trông như "vừa xác minh sạch" dù thực
ra chỉ là không lấy được dữ liệu) — alert engine đọc số 0 này để RESOLVE cảnh báo mất đĩa dù
RAID thật chưa chắc đã hồi phục. Fix: `_collect_controller()` tự theo dõi `disks_complete`/
`enclosures_complete`; `normalize()` trả `None` (không phải 0) cho 2 field này khi không đủ dữ
liệu con — `_latest_ilo`/`_sustained_ilo` (`apps/alerts/engine.py`) đã filter
`{field}__isnull=False` sẵn nên tự rơi về giá trị KHÔNG-null gần nhất, không suy diễn "đã hồi
phục" từ 1 poll thiếu dữ liệu.

⚠️ Bug đã fix 2026-09-29 vòng 2 (review lại đúng bản fix vòng 1 ở trên, phát hiện fix vòng 1 chưa
đủ): vòng 1 chỉ che lỗi ở tầng LIST (`/LogicalDrives/`, `/DataDrives/`, `/StorageEnclosures/`) —
lỗi fetch DETAIL của TỪNG MEMBER riêng lẻ (`LogicalDrives/{ld}/`, `StorageEnclosures/{n}/`) vẫn bị
`if detail:` nuốt im lặng, chỉ bỏ qua khỏi list mà KHÔNG hạ cờ complete. Hệ quả tinh vi hơn vòng
1: nếu member lỗi fetch chính là cái đang Critical/mismatch, "worst"/"mismatch" tính trên PHẦN CÒN
LẠI (đã fetch được) có thể ra kết quả tốt hơn thực tế — ví dụ LD đang Critical fetch lỗi, LD khác
vẫn OK, `logical_drive_worst_code` tính ra 0 thay vì phải là None. Fix: thêm cờ
`logical_drives_complete` (song song `disks_complete`/`enclosures_complete` đã có) theo dõi cả
tầng list LẪN tầng detail-từng-member; `enclosures_complete` mở rộng theo dõi thêm tầng detail
member (trước chỉ theo dõi tầng list). `normalize()` áp cờ `logical_drives_complete` vào
`logical_drive_worst_code` (trả `None` khi không đủ dữ liệu, giống 2 field count).

⚠️ Bug đã fix 2026-09-29 vòng 3 (review lại collect_raw() ở tầng GỐC, thay vì tầng per-controller
như 2 vòng trước): `ArrayControllers/` root trả HTTP 200 nhưng `Members` rỗng/thiếu (`ac_root` vẫn
là dict TRUTHY như `{"Members": []}` nên KHÔNG trúng nhánh `not ac_root` đã có) -> `controllers=[]`
lọt qua hết -> `normalize()` duyệt `raw["controllers"]` rỗng, vòng for không chạy lần nào -> mọi
counter/cờ giữ nguyên giá trị khởi tạo (`missing_count=0`, `enclosure_mismatch=0`, `*_complete=
True`) -> trông giống hệt "đã verify sạch, 0 controller nào có vấn đề" dù thực ra KHÔNG đọc được
controller nào — cùng họ bug với vòng 1/2 nhưng ở tầng cao hơn 1 bậc (thiếu HẲN controller, không
phải thiếu sub-data BÊN TRONG 1 controller đã có). Host đã cấu hình `ilo_ip_address` ngầm định có
RAID controller cần theo dõi nên 0 controller không phải "host không có RAID" mà là "chưa đọc được
gì" — coi như thất bại root giống hệt 401/404/connection-error đã xử lý. Fix 2 lớp: (1)
`collect_raw()` tự chặn tại nguồn — `controllers` rỗng sau vòng lặp Members thì trả `None` y hệt
các nhánh lỗi root khác, `poll_all_ilo` tự bỏ qua không lưu; (2) `normalize()` tự phòng thủ độc
lập (đề phòng raw dựng tay/gọi trực tiếp không qua `collect_raw()`) — `controllers_list` rỗng thì
hạ cả 3 cờ `disks_complete`/`enclosures_complete`/`logical_drives_complete` về `False`. Suy ra từ
đọc code, CHƯA verify trên iLO thật (chưa từng quan sát `Members` rỗng khi status=200).

Mở rộng ngoài RAID (2026-09-29, cùng ngày): thêm 3 nhóm dữ liệu ĐỘC LẬP với RAID/`controllers`
(mỗi nhóm = 1 GET phẳng, all-or-nothing — KHÁC hẳn pattern nested completeness-flag của RAID vì
Fans/Temperatures/PowerSupplies/Redundancy/Battery đã nhúng sẵn đầy đủ trong 1 document, không
cần fetch từng member riêng như LogicalDrives):
- `Systems/1/` → `Oem.Hp.Battery[]` (Smart Storage Battery), `Oem.Hp.DeviceDiscoveryComplete.
  AMSDeviceDiscovery` (Agentless Management Service, chỉ hiển thị — KHÔNG phải lỗi phần cứng nên
  không có alert rule), `ProcessorSummary`/`MemorySummary.Status.HealthRollUp`.
- `Chassis/1/Thermal/` → `Fans[]`, `Temperatures[]` (44 sensor trên DL380 Gen9, phần lớn `Absent`
  do server 1 CPU — bỏ qua sensor Absent khi tính "worst", không suy đoán trạng thái cho sensor
  không tồn tại).
- `Chassis/1/Power/` → `PowerSupplies[]`, `Redundancy[]`. ⚠️ Verify runtime 2026-09-29 (Hyprver03,
  đang có PSU Bay1 thật Critical/Offline `ACPowerLost`): `Redundancy[]` schema này
  (`PowerMetrics.0.11.0`, iLO4 cũ) KHÔNG có field `Status` ở tầng group — phải tự tính redundancy
  từ `MinNumNeeded` + đếm PSU "OK" (xem `_compute_power_redundancy`). `RedundancySet[]["@odata.id"]`
  dạng `".../Power#/PowerSupplies/<N>"` là JSON Pointer fragment (RFC 6901) — `<N>` là index mảng
  `PowerSupplies` theo đặc tả, verify khớp thật (index 0 = Bay1 Critical, 1 = Bay2 OK).

Giới hạn đã biết (ghi rõ, không thiết kế lại cho tình huống chưa có thiết bị thật):
- 3 nhóm mới vẫn phụ thuộc root `ArrayControllers/` đã xác thực OK trước đó (nếu root 401/404/lỗi
  thì `collect_raw()` trả `None` ngay từ đầu, không tới các nhóm mới) — 1 HyperV host có iLO nhưng
  KHÔNG có Smart Array/RAID controller sẽ không bao giờ lấy được 8 mục mới dù chúng không liên
  quan RAID. Vô hại với fleet hiện tại (cả 3 host đều P440ar).

Mở rộng ngoài RAID — iLO5 (2026-09-29, vòng 2, cùng ngày, ngay sau khi deploy vòng 1): verify sống
trên prod phát hiện **Hyperv-01 thực ra là ProLiant DL380 Gen10 + iLO5** (không phải Gen9/iLO4 như
2 host kia — chưa từng biết trước đó) → 4 field Battery/AMS/Processor/Memory null hoàn toàn ngay
sau deploy vì `Systems/1/` trên iLO5 dùng `Oem.Hpe`, không phải `Oem.Hp`. Verify runtime trực tiếp
trên chính Hyperv-01: iLO5 gộp SẴN mọi thứ vào 1 rollup `Oem.Hpe.AggregateHealthStatus` — khác hẳn
cấu trúc iLO4 (`_parse_system_summary_ilo4`):
- `SmartStorageBattery.Status.Health` (dict đơn, KHÔNG phải mảng nhiều battery như iLO4) →
  `_parse_system_summary_ilo5` bọc thành list 1 phần tử để dùng chung logic `normalize()`.
- `AgentlessManagementService` (string, thấy `"Unavailable"` — khác hẳn `"NoAMS"` của iLO4, chỉ
  là 2 cách diễn đạt khác nhau của cùng ý "chưa cài AMS", cả 2 đều lưu thẳng làm string hiển thị).
- `Processors`/`Memory`/`BiosOrHardwareHealth`/`Network` — đều đọc từ CHÍNH rollup này, KHÔNG phải
  `ProcessorSummary`/`MemorySummary` cấp root (2 field đó vẫn tồn tại trên iLO5 nhưng dùng key
  `HealthRollup` chữ **u thường** — khác iLO4 `HealthRollUp` chữ U hoa, 1 bẫy case-sensitivity
  thật giữa 2 firmware; tránh hẳn bằng cách không đọc từ `ProcessorSummary`/`MemorySummary` nữa).
- `FanRedundancy` (string enum, chỉ mới thấy `"Redundant"`) — 3 field bonus mới
  (`bios_hardware_health_code`, `network_health_code`, `fan_redundancy_ok`) chính là 3/12 mục
  "Health Summary" ban đầu tưởng "chưa có nguồn rõ" (BIOS/Hardware Health, Network, Fan Redundancy)
  — hoá ra CÓ nguồn, chỉ là nguồn đó chỉ tồn tại trên schema iLO5, không có trên iLO4. `Oem.Hp`
  (iLO4) đã soát toàn bộ key, KHÔNG có rollup tương đương — 3 field này **None vĩnh viễn trên host
  iLO4** (Hyperv-02/Hyprver03), chỉ có dữ liệu thật trên host iLO5 (Hyperv-01).
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import requests
import urllib3
from django.conf import settings

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

_HEALTH_CODE = {"OK": 0, "WARNING": 1, "DEGRADED": 1, "CRITICAL": 2, "FAILED": 2}

_REDFISH_ROOT = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"

# Path chính xác của document đang đọc PowerSupplies/Redundancy (khớp URL fetch thật trong
# _collect_power: "/redfish/v1/Chassis/1/Power/", literal "1" — hardcode giống nơi fetch, không
# suy đoán multi-chassis chưa verify). Tham chiếu RedundancySet HỢP LỆ phải trỏ vào ĐÚNG document
# này trước dấu "#" — xem bug 2026-09-29 vòng 5, docstring _compute_power_redundancy.
_POWER_RESOURCE_PATH = "/redfish/v1/Chassis/1/Power"

# JSON Pointer fragment (RFC 6901) đúng định dạng đã verify thật cho tham chiếu PowerSupplies
# trong Redundancy[].RedundancySet — bắt buộc TOÀN BỘ fragment phải là "/PowerSupplies/<N>",
# không chỉ lấy số cuối cùng (xem bug 2026-09-29 vòng 4, docstring _compute_power_redundancy).
_PSU_REF_RE = re.compile(r"^/PowerSupplies/(\d+)$")


class IloRedfishClient:
    """Đọc RAID controller/logical drive/disk/enclosure health qua iLO Redfish (đọc-only)."""

    def __init__(self, device: "Device") -> None:
        self.device = device
        # Lý do ngắn của lần collect_raw() trả None (lỗi gốc) — poll_all_ilo lưu lên Device để UI
        # hiện "iLO lỗi: 401" thay vì "Chưa có dữ liệu" mãi mãi.
        self.last_error: str | None = None

    def collect_raw(self) -> dict[str, Any] | None:
        device = self.device
        if not device.ilo_ip_address:
            return None

        base = f"https://{device.ilo_ip_address}"
        session = requests.Session()
        session.verify = getattr(settings, "ILO_CERT_VALIDATE", False)
        if not session.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        session.auth = (device.ilo_username, device.ilo_password)
        session.headers.update({"Accept": "application/json"})

        status, ac_root = self._get(session, base, _REDFISH_ROOT)
        if status == 401:
            logger.warning("iLO %s (%s): 401 Unauthorized — kiểm tra lại username/password", device.name, device.ilo_ip_address)
            self.last_error = "401 Unauthorized (sai username/password)"
            return None
        if status == 404:
            logger.warning(
                "iLO %s (%s): 404 tại endpoint gốc SmartStorage — nghi ngờ không phải HPE iLO "
                "hoặc firmware không có Redfish SmartStorage extension", device.name, device.ilo_ip_address,
            )
            self.last_error = "404 tại SmartStorage (không phải HPE iLO / thiếu Redfish extension)"
            return None
        if status is None:
            logger.warning("iLO %s (%s): lỗi kết nối (timeout/network)", device.name, device.ilo_ip_address)
            self.last_error = "Không kết nối được (timeout/network)"
            return None
        if status != 200 or not ac_root:
            logger.warning("iLO %s (%s): HTTP %s không mong đợi tại ArrayControllers root", device.name, device.ilo_ip_address, status)
            self.last_error = f"HTTP {status} không mong đợi tại ArrayControllers"
            return None

        controllers = []
        for member in ac_root.get("Members", []):
            ac_uri = member.get("@odata.id")
            if not ac_uri:
                continue
            controllers.append(self._collect_controller(session, base, ac_uri))

        if not controllers:
            # Bẫy 2026-09-29 vòng 3: `ac_root` là dict TRUTHY (vd {"Members": []} hoặc thiếu hẳn
            # key "Members") nên KHÔNG trúng nhánh `not ac_root` ở trên -> lọt xuống đây với
            # controllers=[]. normalize() sau đó duyệt raw["controllers"] rỗng -> vòng lặp không
            # chạy lần nào -> mọi bộ đếm giữ nguyên giá trị khởi tạo (missing_count=0,
            # enclosure_mismatch=0, *_complete=True) -> trông giống hệt "đã verify sạch" dù thực
            # ra KHÔNG đọc được controller nào. Coi như thất bại root (giống 401/404/connection
            # error ở trên) thay vì "host không có RAID" — device đã cấu hình ilo_ip_address ngầm
            # định có RAID controller cần theo dõi, Members rỗng chỉ có thể là glitch/phản hồi bất
            # thường của iLO. Trả None -> poll_all_ilo bỏ qua hẳn, KHÔNG lưu HardwareHealth, để
            # alert engine tự rơi về giá trị KHÔNG-null gần nhất thay vì suy diễn đã hồi phục.
            # Suy ra từ code, CHƯA verify trên iLO thật (chưa từng thấy Members rỗng khi status=200).
            logger.warning(
                "iLO %s (%s): ArrayControllers root trả HTTP 200 nhưng Members rỗng/thiếu — không "
                "đọc được controller nào, coi như thất bại (không đủ bằng chứng để lưu/kết luận)",
                device.name, device.ilo_ip_address,
            )
            self.last_error = "ArrayControllers trả 200 nhưng không có controller nào"
            return None

        return {
            "controllers": controllers,
            # 3 nhóm mở rộng ngoài RAID (xem docstring module "Mở rộng ngoài RAID") — độc lập với
            # controllers VÀ độc lập với nhau: 1 nhóm lỗi (None) không xoá dữ liệu nhóm khác đã
            # fetch thành công.
            "system_summary": self._collect_system_summary(session, base),
            "thermal": self._collect_thermal(session, base),
            "power": self._collect_power(session, base),
        }

    def _collect_system_summary(self, session: requests.Session, base: str) -> dict[str, Any] | None:
        """Battery/AMS/Processor/Memory — tất cả nằm chung 1 document `Systems/1/` (1 GET, không
        cần fetch từng member như ArrayControllers). Trả về dict trung gian THỐNG NHẤT bất kể
        iLO4/iLO5 (`battery_conditions`: list string Condition/Health — `normalize()` tự tính max,
        không cần biết nguồn nào) — xem `_parse_system_summary_ilo4`/`_parse_system_summary_ilo5`."""
        device = self.device
        status, body = self._get(session, base, "/redfish/v1/Systems/1/")
        if status != 200 or not isinstance(body, dict):
            logger.warning("iLO %s: không lấy được Systems/1/ (HTTP %s)", device.name, status)
            return None
        oem = body.get("Oem") or {}
        oem_hp = oem.get("Hp")
        if isinstance(oem_hp, dict):
            return self._parse_system_summary_ilo4(body, oem_hp)
        oem_hpe = oem.get("Hpe")
        if isinstance(oem_hpe, dict):
            return self._parse_system_summary_ilo5(device.name, oem_hpe)
        logger.warning(
            "iLO %s: Systems/1/ thiếu cả Oem.Hp lẫn Oem.Hpe — schema không nhận diện được",
            device.name,
        )
        return None

    @staticmethod
    def _parse_system_summary_ilo4(body: dict, oem_hp: dict) -> dict[str, Any]:
        """iLO4 (verify runtime Hyperv-02/Hyprver03, P440ar/DL380 Gen9): Battery là MẢNG (mỗi
        phần tử `Condition`), rollup Processor/Memory nằm ở `ProcessorSummary`/`MemorySummary`
        cấp root (KHÔNG trong `Oem`), key `HealthRollUp` (chữ U hoa — khác iLO5, xem vòng 2).
        KHÔNG có nguồn tương đương cho BIOS/Hardware Health, Network, Fan Redundancy trên iLO4
        (đã soát toàn bộ key `Oem.Hp`, không có rollup nào giống `AggregateHealthStatus` của
        iLO5) — 3 field đó None vĩnh viễn trên host iLO4."""
        return {
            "battery_conditions": [b.get("Condition") for b in (oem_hp.get("Battery") or [])],
            "ams_device_discovery": (oem_hp.get("DeviceDiscoveryComplete") or {}).get("AMSDeviceDiscovery") or "",
            "processor_health": ((body.get("ProcessorSummary") or {}).get("Status") or {}).get("HealthRollUp"),
            "memory_health": ((body.get("MemorySummary") or {}).get("Status") or {}).get("HealthRollUp"),
            "bios_hardware_health": None,
            "network_health": None,
            "fan_redundancy_raw": None,
        }

    @staticmethod
    def _parse_system_summary_ilo5(device_name: str, oem_hpe: dict) -> dict[str, Any] | None:
        """iLO5 (verify runtime 2026-09-29, Hyperv-01 — hoá ra là DL380 Gen10, KHÔNG phải Gen9
        như 2 host kia, phát hiện qua bug field None sau khi mở rộng ngoài RAID). Khác hẳn iLO4:
        mọi thứ gộp sẵn trong 1 rollup `Oem.Hpe.AggregateHealthStatus` — Battery là DICT đơn (1
        SmartStorageBattery.Status.Health, không phải mảng nhiều battery), Processor/Memory/
        BiosOrHardwareHealth/Network cũng đọc từ CHÍNH rollup này (không phải `ProcessorSummary`/
        `MemorySummary` cấp root — 2 field đó VẪN tồn tại trên iLO5 nhưng dùng key `HealthRollup`
        chữ u THƯỜNG, khác iLO4 `HealthRollUp` — tránh bẫy case-sensitivity bằng cách không đọc
        từ đó, đọc thẳng AggregateHealthStatus cho nhất quán). `FanRedundancy` là string enum
        (chỉ mới thấy `"Redundant"` — xem `_parse_redundancy_status`), KHÔNG có mảng Battery nên
        bọc thành list 1 phần tử để dùng chung logic `normalize()` với iLO4."""
        agg = oem_hpe.get("AggregateHealthStatus")
        if not isinstance(agg, dict):
            logger.warning("iLO %s: Systems/1/ Oem.Hpe thiếu AggregateHealthStatus", device_name)
            return None
        return {
            "battery_conditions": [((agg.get("SmartStorageBattery") or {}).get("Status") or {}).get("Health")],
            "ams_device_discovery": agg.get("AgentlessManagementService") or "",
            "processor_health": ((agg.get("Processors") or {}).get("Status") or {}).get("Health"),
            "memory_health": ((agg.get("Memory") or {}).get("Status") or {}).get("Health"),
            "bios_hardware_health": ((agg.get("BiosOrHardwareHealth") or {}).get("Status") or {}).get("Health"),
            "network_health": ((agg.get("Network") or {}).get("Status") or {}).get("Health"),
            "fan_redundancy_raw": agg.get("FanRedundancy"),
        }

    def _collect_thermal(self, session: requests.Session, base: str) -> dict[str, Any] | None:
        """Fans[]/Temperatures[] — 1 document phẳng `Chassis/1/Thermal/`, mỗi phần tử đã có sẵn
        Status.Health (khi State=Enabled) hoặc chỉ State=Absent (bỏ qua khi tính worst — verify
        runtime: DL380 Gen9 1-CPU có nhiều fan bay/sensor Absent theo thiết kế, không phải lỗi)."""
        device = self.device
        status, body = self._get(session, base, "/redfish/v1/Chassis/1/Thermal/")
        if status != 200 or not isinstance(body, dict):
            logger.warning("iLO %s: không lấy được Chassis/1/Thermal/ (HTTP %s)", device.name, status)
            return None
        return {"fans": body.get("Fans") or [], "temperatures": body.get("Temperatures") or []}

    def _collect_power(self, session: requests.Session, base: str) -> dict[str, Any] | None:
        """PowerSupplies[]/Redundancy[] — 1 document phẳng `Chassis/1/Power/`.

        ⚠️ Bug đã fix 2026-09-29 (review ngoài): `body.get("PowerSupplies") or []` từng coi JSON
        thiếu/sai kiểu `PowerSupplies` là "0 PSU" hợp lệ — nếu `Redundancy` vẫn có
        `MinNumNeeded>0`, `_compute_power_redundancy` tính `ok_count=0 < needed` ra `False` ->
        báo "mất redundancy" GIẢ dù thực ra chỉ thiếu dữ liệu PSU (không phải PSU thật sự down).
        Fix: `PowerSupplies` phải là list hợp lệ mới coi nhóm Power này đã fetch đủ; thiếu/sai
        kiểu -> cả nhóm trả `None` (all-or-nothing, giống 2 nhóm mở rộng khác)."""
        device = self.device
        status, body = self._get(session, base, "/redfish/v1/Chassis/1/Power/")
        if status != 200 or not isinstance(body, dict):
            logger.warning("iLO %s: không lấy được Chassis/1/Power/ (HTTP %s)", device.name, status)
            return None
        psu_list = body.get("PowerSupplies")
        if not isinstance(psu_list, list):
            logger.warning(
                "iLO %s: Power/ JSON thiếu PowerSupplies hoặc sai kiểu — không đủ dữ liệu để tính "
                "Power Supply/Redundancy, bỏ qua thay vì suy đoán rỗng (0 PSU)", device.name,
            )
            return None
        return {"power_supplies": psu_list, "redundancy": body.get("Redundancy") or []}

    @staticmethod
    def _members(payload: dict | None) -> list[dict] | None:
        """Trả `payload["Members"]` nếu là list hợp lệ; `None` nếu thiếu key/không phải list.

        Bẫy 2026-09-29 vòng 4 (review code, rủi ro CHƯA quan sát trên iLO thật): `.get("Members",
        [])` gộp chung 2 case KHÁC NHAU — "collection rỗng hợp lệ" (`{"Members": []}`, đúng cấu
        trúc Redfish) và "JSON bất thường, thiếu hẳn key Members hoặc sai kiểu" (lỗi/glitch) —
        thành cùng 1 kết quả `[]`, khiến caller tưởng "đã duyệt xong, 0 phần tử" trong khi thực ra
        chưa có gì để duyệt. Dùng helper này để 2 case tách bạch: caller nhận `None` phải hạ cờ
        complete (giống hệt lý do fix root `ArrayControllers/` ở `collect_raw()` phía trên), nhận
        `[]` thật thì vẫn coi là hợp lệ (0 phần tử đã xác nhận, không phải suy đoán)."""
        if not isinstance(payload, dict):
            return None
        members = payload.get("Members")
        return members if isinstance(members, list) else None

    def _collect_controller(self, session: requests.Session, base: str, ac_uri: str) -> dict[str, Any]:
        device = self.device
        _, ac_detail = self._get(session, base, ac_uri)
        if not ac_detail:
            logger.warning("iLO %s: không lấy được controller detail %s", device.name, ac_uri)
            ac_detail = {}

        logical_drives: list[dict] = []
        expected_disk_uris: set[str] = set()
        # False nếu LogicalDrives/DataDrives/1 disk detail nào đó fetch lỗi -> disks list KHÔNG
        # ĐẦY ĐỦ, không được dùng để tính missing_disk_count (xem normalize() — bẫy 2026-09-29:
        # trước đây fetch lỗi bị nuốt im lặng, disks rỗng -> missing_disk_count=0 giả, alert
        # engine đọc 0 này tưởng "đã hồi phục" dù thực ra chỉ là poll không lấy được dữ liệu).
        disks_complete = True
        # False nếu detail 1 logical drive nào đó fetch lỗi -> ld_codes KHÔNG ĐẦY ĐỦ, không được
        # dùng để tính logical_drive_worst_code (bẫy 2026-09-29 vòng 2, cùng loại disks_complete:
        # LD lỗi fetch trước đây chỉ bị bỏ qua khỏi list, "worst" tính trên phần còn lại có thể bỏ
        # sót đúng cái LD đang Critical mà fetch lỗi, trả về ngỡ tưởng "đã hồi phục" giả).
        logical_drives_complete = True
        ld_status, ld_list = self._get(session, base, ac_uri.rstrip("/") + "/LogicalDrives/")
        # Bẫy 2026-09-29 vòng 4: dùng `_members()` thay vì `.get("Members", [])` trực tiếp —
        # phân biệt "JSON đúng cấu trúc, Members=[] thật" với "JSON bất thường thiếu hẳn key
        # Members" (rủi ro suy luận, CHƯA quan sát trên iLO thật). Case sau phải hạ cờ complete,
        # KHÔNG được coi ngang hàng "0 phần tử đã xác nhận".
        ld_members = self._members(ld_list) if ld_status == 200 else None
        if ld_members is not None:
            for lm in ld_members:
                ld_uri = lm.get("@odata.id")
                if not ld_uri:
                    continue
                ld_detail_status, ld_detail = self._get(session, base, ld_uri)
                if ld_detail_status == 200 and ld_detail:
                    logical_drives.append(ld_detail)
                else:
                    logger.warning(
                        "iLO %s: không lấy được logical drive detail %s (HTTP %s) — poll này "
                        "KHÔNG đủ dữ liệu để tính logical_drive_worst_code, bỏ qua thay vì suy "
                        "đoán từ phần còn lại", device.name, ld_uri, ld_detail_status,
                    )
                    logical_drives_complete = False
                dd_status, dd_list = self._get(session, base, ld_uri.rstrip("/") + "/DataDrives/")
                dd_members = self._members(dd_list) if dd_status == 200 else None
                if dd_members is not None:
                    for dm in dd_members:
                        d_uri = dm.get("@odata.id")
                        if d_uri:
                            expected_disk_uris.add(d_uri)
                else:
                    logger.warning(
                        "iLO %s: không lấy được DataDrives %s (HTTP %s hoặc JSON thiếu Members) — "
                        "poll này KHÔNG đủ dữ liệu để tính missing_disk_count, bỏ qua thay vì suy "
                        "đoán 0", device.name, ld_uri, dd_status,
                    )
                    disks_complete = False
        else:
            logger.warning(
                "iLO %s: không lấy được LogicalDrives cho controller %s (HTTP %s hoặc JSON thiếu "
                "Members) — poll này KHÔNG đủ dữ liệu để tính missing_disk_count, bỏ qua thay vì "
                "suy đoán 0", device.name, ac_uri, ld_status,
            )
            disks_complete = False
            logical_drives_complete = False

        # Đi qua đúng tập disk MÀ LD khai báo (DataDrives), không phải /DiskDrives/ collection —
        # collection chỉ liệt kê đĩa CÒN detect được, đĩa mất đơn giản KHÔNG xuất hiện trong đó.
        # Đi từng DataDrives URI mới bắt được 404 thật (verify runtime, xem docstring module).
        # sorted(expected_disk_uris) chỉ để có thứ tự GỌI ỔN ĐỊNH (không phụ thuộc thứ tự set) —
        # KHÔNG dùng thứ tự này để hiển thị, xem _disk_sort_key bên dưới.
        disks: list[dict] = []
        for d_uri in sorted(expected_disk_uris):
            d_status, d_detail = self._get(session, base, d_uri)
            if d_status == 404:
                disks.append({"@odata.id": d_uri, "is_missing": True})
            elif d_status == 200 and d_detail:
                disks.append(d_detail)
            else:
                logger.warning("iLO %s: disk %s trả HTTP %s không mong đợi (không phải 200/404)", device.name, d_uri, d_status)
                disks_complete = False
        # ⚠️ Sắp theo VỊ TRÍ VẬT LÝ (Location "Port:Box:Bay"), KHÔNG theo Redfish `Id` (thứ tự
        # nội bộ iLO tự gán lúc detect, không nhất thiết khớp bay vật lý). Verify runtime
        # 2026-09-28 (đối chiếu dashboard thật với người dùng biết chắc layout máy): box "1I:3"
        # có Id TĂNG dần (0,1,2,3) nhưng bay lại GIẢM dần (4,3,2,1) — 2 ổ 300GB chạy OS (bay 1,2)
        # bị đẩy xuống cuối bảng thay vì đứng đầu, gây hiểu nhầm khi đối chiếu tay với máy thật.
        disks.sort(key=self._disk_sort_key)

        # Gắn `bay_number` để UI hiển thị "Bay N" dễ đọc thay vì Redfish `Id` (Id không theo thứ
        # tự bay vật lý — xem chú thích _disk_sort_key). Lấy thẳng từ Location đã verify runtime,
        # KHÔNG suy đoán. Đĩa missing không có Location -> bay_number=None (UI tự fallback về
        # "Disk N"); vẫn gắn `Id` hiển thị được bằng path segment cuối của @odata.id — theo đúng
        # quy ước Redfish (Id trùng segment cuối URI thành viên), không phải đoán.
        for d in disks:
            if d.get("is_missing"):
                d["bay_number"] = None
                uri = d.get("@odata.id") or ""
                if uri:
                    d.setdefault("Id", uri.rstrip("/").rsplit("/", 1)[-1])
            else:
                _, _, bay = self._parse_location(d.get("Location") or "")
                d["bay_number"] = bay or None

        enclosures: list[dict] = []
        encl_status, encl_list = self._get(session, base, ac_uri.rstrip("/") + "/StorageEnclosures/")
        # Cùng lý do disks_complete ở trên: StorageEnclosures fetch lỗi -> enclosures rỗng, KHÔNG
        # phải "0 enclosure" thật -> enclosure_mismatch_count không được tính từ list rỗng này.
        # Bẫy 2026-09-29 vòng 4: dùng `_members()` thay vì check `encl_list is not None` rồi
        # `.get("Members", [])` riêng — trước đây `encl_list={}` (200 OK, JSON thiếu key Members)
        # khiến `encl_list is not None` = True (enclosures_complete SAI thành True) nhưng
        # `elif encl_list:` lại falsy nên bỏ qua vòng lặp lặng lẽ, không log/không hạ cờ. `_members()`
        # gộp đúng 1 chỗ: trả None cho cả lỗi HTTP lẫn JSON thiếu/sai kiểu Members.
        encl_members = self._members(encl_list) if encl_status == 200 else None
        enclosures_complete = encl_members is not None
        if not enclosures_complete:
            logger.warning(
                "iLO %s: không lấy được StorageEnclosures cho controller %s (HTTP %s hoặc JSON "
                "thiếu Members) — poll này KHÔNG đủ dữ liệu để tính enclosure_mismatch_count, bỏ "
                "qua thay vì suy đoán 0", device.name, ac_uri, encl_status,
            )
        else:
            for em in encl_members:
                e_uri = em.get("@odata.id")
                if not e_uri:
                    continue
                e_status, e_detail = self._get(session, base, e_uri)
                if e_status == 200 and e_detail:
                    enclosures.append(e_detail)
                else:
                    # Bẫy 2026-09-29 vòng 2: list StorageEnclosures fetch OK (enclosures_complete
                    # đã True ở nhánh trên) nhưng detail 1 enclosure cụ thể lỗi -> trước đây chỉ bỏ
                    # qua khỏi list mà KHÔNG hạ enclosures_complete, khiến enclosure_mismatch_count
                    # vẫn được tính (thiếu đúng cái enclosure có thể đang mismatch) -> resolve giả.
                    logger.warning(
                        "iLO %s: không lấy được enclosure detail %s (HTTP %s) — poll này KHÔNG đủ "
                        "dữ liệu để tính enclosure_mismatch_count, bỏ qua thay vì suy đoán từ phần "
                        "còn lại", device.name, e_uri, e_status,
                    )
                    enclosures_complete = False

        return {
            "detail": ac_detail,
            "logical_drives": logical_drives,
            "disks": disks,
            "enclosures": enclosures,
            "disks_complete": disks_complete,
            "enclosures_complete": enclosures_complete,
            "logical_drives_complete": logical_drives_complete,
        }

    @staticmethod
    def _disk_sort_key(disk: dict) -> tuple:
        """Sort theo vị trí vật lý `Location` ("Port:Box:Bay", vd "1I:3:4") thay vì Redfish
        `Id` — xem chú thích chỗ gọi (`collect_raw`). Đĩa `is_missing` (404, không có
        `Location`) xếp CUỐI — không đủ dữ liệu để biết bay thật của nó, không đoán."""
        if disk.get("is_missing"):
            return (1, "", 0, 0)
        port, box, bay = IloRedfishClient._parse_location(disk.get("Location") or "")
        return (0, port, box, bay)

    @staticmethod
    def _parse_location(location: str) -> tuple[str, int, int]:
        """Parse `Location` dạng "Port:Box:Bay" (vd "1I:3:4") -> (port, box, bay). Thiếu phần
        nào trả rỗng/0 cho phần đó — chỉ parse đúng cấu trúc đã verify runtime, không suy đoán
        giá trị. Dùng chung cho `_disk_sort_key` (sort) và gắn `bay_number` hiển thị UI."""
        parts = (location or "").split(":")
        port = parts[0] if len(parts) > 0 else ""
        try:
            box = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            box = 0
        try:
            bay = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            bay = 0
        return port, box, bay

    @staticmethod
    def _get(session: requests.Session, base: str, path: str) -> tuple[int | None, dict | None]:
        """GET path, trả (status_code, json|None). Retry 1 lần khi ConnectionError (xem docstring
        module — iLO đóng connection sau đúng 1 request dù keep-alive)."""
        resp = None
        for attempt in (1, 2):
            try:
                resp = session.get(base + path, timeout=(5, 15), headers={"Connection": "close"})
                break
            except requests.exceptions.ConnectionError:
                if attempt == 2:
                    return None, None
                session.close()
            except requests.exceptions.RequestException:
                return None, None
        if resp is None:
            return None, None
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        device_name = self.device.name
        controller_codes: list[int] = []
        ld_codes: list[int] = []
        missing_count = 0
        enclosure_mismatch = 0
        disks_complete = True
        enclosures_complete = True
        logical_drives_complete = True

        controllers_list = raw.get("controllers", [])
        for controller in controllers_list:
            detail = controller.get("detail") or {}
            code = self._health_code((detail.get("Status") or {}).get("Health"), device_name, "controller")
            if code is not None:
                controller_codes.append(code)

            for ld in controller.get("logical_drives", []):
                ld_code = self._health_code(
                    (ld.get("Status") or {}).get("Health"), device_name, f"logical_drive {ld.get('Id')}",
                )
                if ld_code is not None:
                    ld_codes.append(ld_code)

            disks = controller.get("disks", [])
            missing_count += sum(1 for d in disks if d.get("is_missing"))
            enclosure_mismatch += self._count_enclosure_mismatch(disks, controller.get("enclosures", []))
            # .get(..., True): raw dựng tay (test cũ, không qua _collect_controller) không có các
            # cờ này -> mặc định coi là đầy đủ (đúng ý nghĩa "raw này đại diện 1 poll thành công").
            disks_complete = disks_complete and controller.get("disks_complete", True)
            enclosures_complete = enclosures_complete and controller.get("enclosures_complete", True)
            logical_drives_complete = logical_drives_complete and controller.get("logical_drives_complete", True)

        # Bẫy 2026-09-29 vòng 3: `controllers_list` RỖNG (vd `collect_raw()` bị bypass, hoặc raw
        # dựng tay không qua nó) khiến vòng for phía trên KHÔNG chạy lần nào -> 3 cờ complete vẫn
        # giữ nguyên `True` khởi tạo dù thực ra KHÔNG có controller nào để xác nhận — 0 controller
        # trên 1 host ĐÃ cấu hình iLO monitoring không phải "host không có RAID" (host đó ngầm
        # định có RAID controller cần theo dõi) mà là "chưa đọc được gì", cùng loại với sub-fetch
        # lỗi ở trên. `collect_raw()` đã tự chặn case này ở tầng root (trả None khi Members rỗng),
        # nhưng normalize() vẫn cần tự phòng thủ độc lập (raw dựng tay/gọi trực tiếp không qua
        # collect_raw()) — hạ cả 3 cờ để 2 field đếm trả None thay vì 0 giả.
        if not controllers_list:
            disks_complete = False
            enclosures_complete = False
            logical_drives_complete = False

        # missing_disk_count/enclosure_mismatch_count/logical_drive_worst_code chỉ tin được khi ĐỦ
        # dữ liệu con (list LẪN detail từng LogicalDrive/DataDrive/StorageEnclosure member, xem
        # _collect_controller) — thiếu 1 trong các fetch đó làm disks/enclosures/logical_drives
        # list KHÔNG ĐẦY ĐỦ: cộng dồn sẽ ra số THẤP GIẢ (đĩa mất/enclosure mismatch thật không nằm
        # trong danh sách đã duyệt thì không được đếm), còn max() sẽ bỏ sót đúng cái LD/enclosure
        # đang tệ nhất nếu chính nó là cái fetch lỗi. Trả None thay vì số/mã sai: _latest_ilo/
        # _sustained_ilo (apps/alerts/engine.py) đã filter `{field}__isnull=False` nên tự rơi về
        # giá trị KHÔNG-null gần nhất trước đó thay vì coi 1 poll không đầy đủ là "đã hồi phục"
        # (bug phát hiện qua review 2026-09-29, vòng 2 mở rộng sang detail-fetch của từng LD/
        # enclosure member — vòng 1 chỉ mới che lỗi ở tầng list).
        # 7 field mở rộng ngoài RAID (Battery/AMS/Processor/Memory/Fan/Temperature/PowerSupply +
        # Power Redundancy) — MỖI NHÓM (System Summary/Thermal/Power) có try/except RIÊNG, tách
        # khỏi phần tính RAID ở trên VÀ tách khỏi NHAU: 1 bug ở logic 1 nhóm (vd parse index
        # redundancy lỗi trong nhóm Power) không được phép làm mất field RAID ở trên LẪN field
        # của 2 nhóm còn lại (Battery/Processor/... ở System Summary, Fan/Temperature ở Thermal).
        # ⚠️ Bug đã fix 2026-09-29 (review ngoài, vòng 6): trước đây CẢ 3 nhóm dùng CHUNG 1 khối
        # try/except — exception ở nhóm Power (vd RedundancySet chứa phần tử null) xoá SẠCH luôn
        # kết quả Battery/Processor/Network dù 2 nhóm kia đã tính đúng TRƯỚC khi Power crash (biến
        # local đã gán giá trị Critical thật rồi bị except ghi đè về None) — cảnh báo phần cứng mở
        # rộng bị bỏ sót dù không liên quan gì tới lỗi Power. Xem docstring module "Mở rộng ngoài
        # RAID" + docstring _compute_power_redundancy.
        battery_code = processor_code = memory_code = None
        bios_hardware_code = network_code = fan_redundancy_ok = None
        ams_device_discovery = ""
        try:
            system_summary = raw.get("system_summary")
            if system_summary:
                battery_codes = [
                    c for c in (
                        self._health_code(cond, device_name, "battery")
                        for cond in system_summary.get("battery_conditions", [])
                    ) if c is not None
                ]
                battery_code = max(battery_codes) if battery_codes else None
                ams_device_discovery = system_summary.get("ams_device_discovery", "")
                processor_code = self._health_code(system_summary.get("processor_health"), device_name, "processor summary")
                memory_code = self._health_code(system_summary.get("memory_health"), device_name, "memory summary")
                # 3 field bonus — CHỈ có dữ liệu trên iLO5 (Oem.Hpe.AggregateHealthStatus); iLO4
                # trả None cho cả 3 key này (xem _parse_system_summary_ilo4) nên tự động None ở
                # đây, không cần if/else riêng theo phiên bản iLO.
                bios_hardware_code = self._health_code(system_summary.get("bios_hardware_health"), device_name, "bios/hardware health")
                network_code = self._health_code(system_summary.get("network_health"), device_name, "network health")
                fan_redundancy_ok = self._parse_redundancy_status(system_summary.get("fan_redundancy_raw"), device_name, "fan redundancy")
        except Exception:
            logger.exception(
                "iLO %s: lỗi tính hardware-health nhóm System Summary (Battery/AMS/Processor/"
                "Memory/BIOS-Hardware/Network/FanRedundancy) — không ảnh hưởng nhóm Thermal/Power "
                "hay RAID", device_name,
            )
            battery_code = processor_code = memory_code = None
            bios_hardware_code = network_code = fan_redundancy_ok = None
            ams_device_discovery = ""

        fan_code = temperature_code = None
        try:
            thermal = raw.get("thermal")
            if thermal:
                fan_codes = [
                    c for c in (
                        self._health_code((f.get("Status") or {}).get("Health"), device_name, f"fan {f.get('FanName')}")
                        for f in thermal.get("fans", [])
                    ) if c is not None
                ]
                fan_code = max(fan_codes) if fan_codes else None
                temp_codes = [
                    c for c in (
                        self._health_code((t.get("Status") or {}).get("Health"), device_name, f"temperature {t.get('Name')}")
                        for t in thermal.get("temperatures", [])
                    ) if c is not None
                ]
                temperature_code = max(temp_codes) if temp_codes else None
        except Exception:
            logger.exception(
                "iLO %s: lỗi tính hardware-health nhóm Thermal (Fan/Temperature) — không ảnh "
                "hưởng nhóm System Summary/Power hay RAID", device_name,
            )
            fan_code = temperature_code = None

        # PSU health (đọc trực tiếp Status.Health từng PSU) và Power Redundancy (parse tham chiếu
        # RedundancySet, xem _compute_power_redundancy) là 2 PHÉP TÍNH ĐỘC LẬP dùng chung 1 nguồn
        # `power` — try/except RIÊNG cho từng cái: lỗi ở 1 bên (vd @odata.id sai kiểu làm
        # _compute_power_redundancy crash) không được phép xoá bên kia đã tính đúng. Bug đã fix
        # 2026-09-29 (review ngoài, vòng 7): trước đây CẢ 2 dùng CHUNG 1 try/except như 3 nhóm mở
        # rộng khác từng dùng chung ở vòng 6 — cùng họ bug, lần này ở TRONG nội bộ 1 nhóm thay vì
        # giữa 3 nhóm. Tái hiện đúng theo báo cáo: PSU Health=Critical (tính đúng, không lỗi) +
        # 1 @odata.id sai kiểu (int 123, không phải chuỗi) khiến `_compute_power_redundancy` crash
        # AttributeError ('int' object has no attribute 'partition') -> except (khi còn gộp chung)
        # xoá SẠCH cả psu_code (đã tính đúng=Critical TRƯỚC khi crash) lẫn power_redundancy_ok ->
        # cảnh báo "PSU Critical" bị bỏ sót dù không liên quan gì tới lỗi parse redundancy. Fix 2
        # lớp: (1) type-check `isinstance(odata_id, str)` trong _compute_power_redundancy (chặn
        # crash từ gốc, xem docstring hàm đó); (2) tách try/except này làm lớp phòng thủ thứ 2 cho
        # bất kỳ lỗi tương tự chưa lường trước ở 1 trong 2 phép tính.
        power = raw.get("power")
        psu_list = power.get("power_supplies", []) if power else []

        psu_code = None
        try:
            if power:
                psu_codes = [
                    c for c in (
                        self._health_code((p.get("Status") or {}).get("Health"), device_name, "power supply")
                        for p in psu_list
                    ) if c is not None
                ]
                psu_code = max(psu_codes) if psu_codes else None
        except Exception:
            logger.exception(
                "iLO %s: lỗi tính hardware-health PowerSupply — không ảnh hưởng Power Redundancy "
                "hay nhóm System Summary/Thermal/RAID", device_name,
            )
            psu_code = None

        power_redundancy_ok = None
        try:
            if power:
                power_redundancy_ok = self._compute_power_redundancy(psu_list, power.get("redundancy", []), device_name)
        except Exception:
            logger.exception(
                "iLO %s: lỗi tính hardware-health Power Redundancy — không ảnh hưởng PowerSupply "
                "health hay nhóm System Summary/Thermal/RAID", device_name,
            )
            power_redundancy_ok = None

        return {
            "controller_health_code": max(controller_codes) if controller_codes else None,
            "logical_drive_worst_code": max(ld_codes) if (logical_drives_complete and ld_codes) else None,
            "missing_disk_count": missing_count if disks_complete else None,
            "enclosure_mismatch_count": enclosure_mismatch if (disks_complete and enclosures_complete) else None,
            "battery_health_code": battery_code,
            "ams_device_discovery": ams_device_discovery,
            "processor_health_code": processor_code,
            "memory_health_code": memory_code,
            "fan_worst_code": fan_code,
            "temperature_worst_code": temperature_code,
            "power_supply_worst_code": psu_code,
            "power_redundancy_ok": power_redundancy_ok,
            "bios_hardware_health_code": bios_hardware_code,
            "network_health_code": network_code,
            "fan_redundancy_ok": fan_redundancy_ok,
            "raw": raw,
        }

    @staticmethod
    def _count_enclosure_mismatch(disks: list[dict], enclosures: list[dict]) -> int:
        """Enclosure khai báo DriveBayCount>0 nhưng 0 đĩa hiện diện khớp tiền tố Location —
        dấu hiệu mất kết nối tới cả 1 cage đĩa (xem CLAUDE.md vụ Hyperv-02 2026-09-28).
        Đĩa "is_missing" (404, không có Location) không tính vào "hiện diện". Heuristic dựa trên
        field đã verify thật (`DriveBayCount`, `Location` "ControllerPort:Box[:Bay]") nhưng
        CHƯA verify exhaustive việc mọi đĩa present đều có Location đúng tiền tố enclosure —
        raw JSON vẫn giữ đủ để soi tay qua UI nếu nghi ngờ.
        """
        present_by_prefix: dict[str, int] = {}
        for d in disks:
            if d.get("is_missing"):
                continue
            location = d.get("Location") or ""
            prefix = location.rsplit(":", 1)[0] if ":" in location else location
            if prefix:
                present_by_prefix[prefix] = present_by_prefix.get(prefix, 0) + 1

        mismatch = 0
        for encl in enclosures:
            bay_count = encl.get("DriveBayCount") or 0
            prefix = encl.get("Location") or ""
            if bay_count > 0 and present_by_prefix.get(prefix, 0) == 0:
                mismatch += 1
        return mismatch

    @staticmethod
    def _compute_power_redundancy(
        psu_list: list[dict], redundancy_groups: list[dict], device_name: str = "",
    ) -> bool | None:
        """True nếu MỌI redundancy group đủ PSU "OK" >= MinNumNeeded; None nếu 0 group (không áp
        dụng, vd host 1 PSU). `RedundancySet[]["@odata.id"]` dạng ".../Power#/PowerSupplies/<N>"
        là JSON Pointer fragment (RFC 6901) — `<N>` là index mảng `PowerSupplies` theo đặc tả,
        verify khớp thật 2026-09-29 (Hyprver03: index 0=Bay1 Critical/Offline thật, 1=Bay2 OK,
        đúng thứ tự mảng). KHÔNG dùng `Redundancy[].Status` — schema `PowerMetrics.0.11.0` (iLO4
        cũ) không có field Status ở tầng group này (verify: JSON đầy đủ không có key "Status"
        trong Redundancy[0]).

        ⚠️ Bug đã fix 2026-09-29 (review ngoài): `group.get("MinNumNeeded") or 0` từng mặc định
        field THIẾU/`None` thành `0` — 0 PSU "OK" vẫn "đủ" so với ngưỡng giả `0` nên trả `True`
        dù PSU đang Critical thật (tái hiện được: group không có `MinNumNeeded`, PSU Critical ->
        code cũ trả `True`). `MinNumNeeded` là field bắt buộc theo schema `Redundancy[]` đã verify
        — thiếu nó là dữ liệu bất thường/không đủ để kết luận, phải trả `None` (chưa xác định),
        KHÔNG suy đoán `0` (dễ hiểu nhầm PSU lỗi vẫn "đủ redundancy").

        ⚠️ Bug đã fix 2026-09-29 (review ngoài, cùng họ bug): `group.get("RedundancySet", [])`
        từng mặc định field THIẾU/sai kiểu thành `[]` — tái hiện đúng theo báo cáo: 2 PSU đều
        `OK`, group có `MinNumNeeded=2` nhưng thiếu hẳn `RedundancySet` -> `ok_count=0 < 2` ra
        `False` (báo GIẢ "mất redundancy" dù PSU thật đều khoẻ). Field thiếu/sai kiểu -> `None`
        (không đủ dữ liệu để đếm PSU OK); `RedundancySet: []` HIỆN DIỆN (rỗng thật, group tham
        chiếu đúng 0 PSU) vẫn giữ hành vi cũ (tính `ok_count=0` bình thường) — 2 case khác nhau,
        không gộp chung.

        ⚠️ Bug đã fix 2026-09-29 (review ngoài, vòng 3 — cùng họ bug, lần này ở TỪNG PHẦN TỬ bên
        trong `RedundancySet` thay vì cả field): 1 tham chiếu `@odata.id` sai định dạng (không
        parse được thành index, vd thiếu số/URI rỗng) hoặc trỏ tới index ngoài phạm vi
        `PowerSupplies` từng bị `continue`/bỏ qua ÂM THẦM — `ok_count` chỉ cộng dồn từ các tham
        chiếu ĐỌC ĐƯỢC, rồi đem so `ok_count < needed` như thể đã đếm đủ. Tái hiện đúng theo báo
        cáo: 2 PSU đều `OK`, `MinNumNeeded=2`, nhưng 1 trong 2 tham chiếu `RedundancySet` sai định
        dạng -> chỉ đếm được 1 PSU OK -> `1 < 2` ra `False` (báo GIẢ "mất redundancy" dù cả 2 PSU
        thật đều khoẻ, chỉ là 1 tham chiếu không đọc được). Field `RedundancySet` mảng vẫn hợp lệ
        (đã qua check `isinstance(..., list)` ở trên) nhưng 1 PHẦN TỬ bên trong nó không đọc được
        vẫn là dữ liệu không đủ tin cậy để kết luận — trả `None` ngay khi gặp tham chiếu không
        resolve được, không tiếp tục đếm thiếu rồi so sánh.

        ⚠️ Bug đã fix 2026-09-29 (review ngoài, vòng 4 — 2 lỗi trong CHÍNH cách đọc `@odata.id` của
        vòng 3): (1) `int(uri.rstrip("/").rsplit("/", 1)[-1])` chỉ lấy SỐ CUỐI CÙNG của URI, không
        xác nhận toàn bộ path thực sự trỏ vào `PowerSupplies` — 1 tham chiếu dạng
        `".../Power#/Other/0"` (trỏ vào mảng KHÁC, không phải PowerSupplies) vẫn parse ra `idx=0`
        và bị tính nhầm thành PSU 0. Fix: dùng `_PSU_REF_RE` khớp TOÀN BỘ fragment sau `#` phải
        đúng dạng `/PowerSupplies/<N>` — sai path (trỏ mảng khác) hoặc thiếu `#` đều trả `None`
        (không đủ tin cậy), không chỉ khớp mỗi con số cuối. (2) KHÔNG có gì chặn 1 index bị tham
        chiếu TRÙNG LẶP nhiều lần trong cùng `RedundancySet` — `ok_count` cộng dồn theo SỐ THAM
        CHIẾU, không theo SỐ PSU PHÂN BIỆT, nên PSU 0 `OK` được tham chiếu 2 lần cộng ra
        `ok_count=2`, đạt `MinNumNeeded=2` dù PSU 1 thật đang `Critical` (chỉ có 1 PSU khoẻ thật,
        không phải 2). Fix: dùng `set()` theo dõi index ĐÃ đếm — tham chiếu trùng tới cùng 1 index
        chỉ tính 1 lần, không cộng dồn thêm.

        ⚠️ Bug đã fix 2026-09-29 (review ngoài, vòng 5 — `_PSU_REF_RE` của vòng 4 chỉ khớp PHẦN
        SAU dấu `#`, chưa xác thực phần TRƯỚC dấu `#` có đúng là document `Chassis/1/Power` đang
        đọc hay không): tham chiếu `"/redfish/v1/Chassis/2/Power#/PowerSupplies/0"` (trỏ sang
        Chassis KHÁC — 2, không phải 1) vẫn khớp `_PSU_REF_RE` vì regex chỉ nhìn fragment
        `/PowerSupplies/0`, nên bị tính nhầm thành PSU 0 của `psu_list` (thuộc Chassis 1) — 1
        dạng "phản hồi không nhất quán" của iLO (chưa quan sát thật, chỉ là rủi ro suy ra từ code
        chưa được chặn). Fix: xác thực CẢ URI — tách `uri.partition("#")`, phần trước `#` (sau khi
        bỏ dấu `/` cuối nếu có) phải khớp CHÍNH XÁC `_POWER_RESOURCE_PATH`
        (`"/redfish/v1/Chassis/1/Power"`, đúng URL literal mà `_collect_power` đã fetch — không
        suy đoán multi-chassis chưa verify) rồi mới khớp fragment bằng `_PSU_REF_RE`; sai document
        gốc (trỏ Chassis khác/resource khác) đều trả `None`.

        ⚠️ Bug đã fix 2026-09-29 (review ngoài, vòng 6 — 2 vấn đề: kiểu phần tử KHÔNG được kiểm
        tra + phạm vi try/except ở `normalize()` quá rộng): (1) 1 phần tử `RedundancySet` không
        phải object (vd `RedundancySet: [null]`) khiến `ref.get("@odata.id")` crash
        `AttributeError: 'NoneType' object has no attribute 'get'` — hàm này KHÔNG tự bắt lỗi, để
        crash lan lên `normalize()`. Fix: thêm `isinstance(ref, dict)` ngay đầu vòng lặp, phần tử
        sai kiểu trả `None` (không đủ tin cậy) giống các nhánh không-resolve-được khác, không dựa
        vào exception. (2) Hệ quả của (1) khi CHƯA có check này: `normalize()`
        (`apps/collectors/ilo_redfish.py`) từng bọc CẢ 3 nhóm mở rộng (System Summary/Thermal/
        Power) trong 1 khối `try/except` DUY NHẤT — exception ở nhóm Power (crash tại đây) xoá
        SẠCH cả kết quả Battery/Processor/Memory/BIOS/Network/Fan/Temperature dù các nhóm đó đã
        tính đúng TRƯỚC khi Power crash (Python chạy tuần tự trong cùng try, các biến local đã gán
        đúng giá trị Critical thật rồi bị except ghi đè về `None`). Tái hiện đúng theo báo cáo:
        `RedundancySet: [null]` → lỗi đọc tham chiếu Power → except xoá luôn Battery/Processor/
        Network đang Critical → cảnh báo phần cứng mở rộng bị BỎ SÓT dù RAID (tính TRƯỚC khối
        try/except này, không nằm trong nó) vẫn được giữ đúng. Fix: tách 1 khối try/except DUY
        NHẤT thành 3 khối ĐỘC LẬP (System Summary / Thermal / Power) — lỗi ở 1 nhóm chỉ reset field
        của CHÍNH nhóm đó, không đụng 2 nhóm còn lại. Kết hợp cả 2 fix: crash ở (2) giờ không còn
        xảy ra nhờ (1) (đã chặn từ gốc), nhưng vẫn giữ cô lập try/except làm lớp phòng thủ thứ 2
        cho bất kỳ lỗi tương tự chưa lường trước ở bất kỳ nhóm nào trong 3 nhóm này.

        ⚠️ Bug đã fix 2026-09-29 (review ngoài, vòng 7 — cùng họ bug với vòng 6 nhưng ở TRONG nội
        bộ nhóm Power, giữa PSU health và Power Redundancy, chứ không phải giữa 3 nhóm): (1)
        `uri = ref.get("@odata.id") or ""` không kiểm tra KIỂU của `@odata.id` trước khi gọi
        `.partition()` — nếu field này tồn tại nhưng KHÔNG PHẢI chuỗi (vd số nguyên `123`, do
        `x or ""` chỉ thay thế khi `x` falsy, số khác 0 vẫn giữ nguyên kiểu int), `uri.partition("#")`
        crash `AttributeError: 'int' object has no attribute 'partition'`. Fix: thêm
        `isinstance(odata_id, str)` ngay sau khi đọc field, sai kiểu trả `None` giống các nhánh
        không-resolve-được khác. (2) Hệ quả: trước đây `normalize()` tính `psu_code` (PSU health,
        đọc thẳng `Status.Health` từng PSU — KHÔNG phụ thuộc gì vào việc parse `RedundancySet`) và
        `power_redundancy_ok` (gọi hàm này) trong CÙNG 1 try/except của riêng nhóm Power — crash ở
        (1) xoá SẠCH cả `psu_code` dù đã tính đúng (vd Critical) TRƯỚC khi crash xảy ra ở bước tính
        redundancy ngay sau đó. Tái hiện đúng theo báo cáo: PSU Health=Critical (đúng, không lỗi
        gì) + 1 `@odata.id` sai kiểu (int `123`) trong `RedundancySet` -> except (khi 2 phép tính
        còn gộp chung) xoá cả `power_supply_worst_code` lẫn `power_redundancy_ok` về `None` ->
        cảnh báo PSU Critical bị bỏ sót dù PSU health tự nó tính đúng, không liên quan gì tới lỗi
        parse redundancy. Fix: tách try/except của nhóm Power (vốn đã tách khỏi 2 nhóm kia ở vòng
        6) thành 2 khối ĐỘC LẬP hơn nữa — 1 cho PSU health, 1 cho Power Redundancy — lỗi ở phép
        tính này không đụng phép tính kia. Xem `normalize()`."""
        if not redundancy_groups:
            return None
        for group in redundancy_groups:
            needed = group.get("MinNumNeeded")
            if needed is None:
                logger.warning(
                    "iLO %s: redundancy group thiếu MinNumNeeded — không đủ dữ liệu để kết luận "
                    "Power Redundancy, coi là chưa xác định (None) thay vì mặc định 0",
                    device_name,
                )
                return None
            redundancy_set = group.get("RedundancySet")
            if not isinstance(redundancy_set, list):
                logger.warning(
                    "iLO %s: redundancy group thiếu RedundancySet hoặc sai kiểu — không đủ dữ "
                    "liệu để đếm PSU OK, coi là chưa xác định (None) thay vì mặc định rỗng",
                    device_name,
                )
                return None
            ok_indexes: set[int] = set()
            for ref in redundancy_set:
                if not isinstance(ref, dict):
                    logger.warning(
                        "iLO %s: RedundancySet có phần tử không phải object (%r, vd null) — "
                        "không đủ dữ liệu để đếm PSU OK, coi là chưa xác định (None) thay vì "
                        "crash/bỏ qua",
                        device_name, ref,
                    )
                    return None
                odata_id = ref.get("@odata.id")
                if odata_id is not None and not isinstance(odata_id, str):
                    logger.warning(
                        "iLO %s: RedundancySet có @odata.id không phải chuỗi (%r, kiểu %s) — "
                        "không đủ dữ liệu để đếm PSU OK, coi là chưa xác định (None) thay vì "
                        "crash/suy đoán",
                        device_name, odata_id, type(odata_id).__name__,
                    )
                    return None
                uri = odata_id or ""
                resource_path, sep, fragment = uri.partition("#")
                if not sep or resource_path.rstrip("/") != _POWER_RESOURCE_PATH:
                    logger.warning(
                        "iLO %s: RedundancySet có tham chiếu @odata.id không trỏ vào chính "
                        "document Chassis/1/Power đang đọc (%r) — không đủ dữ liệu để đếm PSU "
                        "OK, coi là chưa xác định (None) thay vì suy đoán từ fragment",
                        device_name, uri,
                    )
                    return None
                match = _PSU_REF_RE.match(fragment)
                if not match:
                    logger.warning(
                        "iLO %s: RedundancySet có tham chiếu @odata.id không trỏ đúng vào "
                        "PowerSupplies (%r) — không đủ dữ liệu để đếm PSU OK, coi là chưa xác "
                        "định (None) thay vì suy đoán từ số cuối URI",
                        device_name, uri,
                    )
                    return None
                idx = int(match.group(1))
                if not (0 <= idx < len(psu_list)):
                    logger.warning(
                        "iLO %s: RedundancySet tham chiếu index %d ngoài phạm vi PowerSupplies "
                        "(len=%d) — không đủ dữ liệu để đếm PSU OK, coi là chưa xác định (None)",
                        device_name, idx, len(psu_list),
                    )
                    return None
                if idx in ok_indexes:
                    continue
                health = ((psu_list[idx].get("Status") or {}).get("Health") or "").upper()
                if health == "OK":
                    ok_indexes.add(idx)
            if len(ok_indexes) < needed:
                return False
        return True

    @staticmethod
    def _health_code(health_value: Any, device_name: str, context: str) -> int | None:
        if not health_value:
            return None
        code = _HEALTH_CODE.get(str(health_value).upper())
        if code is None:
            logger.warning(
                "iLO %s: enum Health lạ %r tại %s (chưa từng verify: OK/Warning/Critical) — "
                "coi là Critical để không bỏ sót, cần đối chiếu lại", device_name, health_value, context,
            )
            return 2
        return code

    @staticmethod
    def _parse_redundancy_status(value: Any, device_name: str, context: str) -> bool | None:
        """`None` khi không có dữ liệu (vd iLO4 không expose fan redundancy — không suy đoán).
        Verify runtime 2026-09-29 (iLO5 Hyperv-01): chỉ từng thấy giá trị `"Redundant"` — enum
        lạ khác (chưa biết tên thật của trạng thái "mất redundancy") coi là `False` (an toàn,
        không bỏ sót) + log warning để đối chiếu sau, cùng triết lý `_health_code`."""
        if not value:
            return None
        if str(value).strip().upper() == "REDUNDANT":
            return True
        logger.warning(
            "iLO %s: %s trả giá trị lạ %r (chưa từng verify, chỉ mới thấy 'Redundant') — coi là "
            "KHÔNG redundant để không bỏ sót, cần đối chiếu lại", device_name, context, value,
        )
        return False
