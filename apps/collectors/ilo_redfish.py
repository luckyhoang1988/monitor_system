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
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import requests
import urllib3
from django.conf import settings

if TYPE_CHECKING:
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

_HEALTH_CODE = {"OK": 0, "WARNING": 1, "DEGRADED": 1, "CRITICAL": 2, "FAILED": 2}

_REDFISH_ROOT = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"


class IloRedfishClient:
    """Đọc RAID controller/logical drive/disk/enclosure health qua iLO Redfish (đọc-only)."""

    def __init__(self, device: "Device") -> None:
        self.device = device

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
            return None
        if status == 404:
            logger.warning(
                "iLO %s (%s): 404 tại endpoint gốc SmartStorage — nghi ngờ không phải HPE iLO "
                "hoặc firmware không có Redfish SmartStorage extension", device.name, device.ilo_ip_address,
            )
            return None
        if status is None:
            logger.warning("iLO %s (%s): lỗi kết nối (timeout/network)", device.name, device.ilo_ip_address)
            return None
        if status != 200 or not ac_root:
            logger.warning("iLO %s (%s): HTTP %s không mong đợi tại ArrayControllers root", device.name, device.ilo_ip_address, status)
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
            return None

        return {"controllers": controllers}

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
        return {
            "controller_health_code": max(controller_codes) if controller_codes else None,
            "logical_drive_worst_code": max(ld_codes) if (logical_drives_complete and ld_codes) else None,
            "missing_disk_count": missing_count if disks_complete else None,
            "enclosure_mismatch_count": enclosure_mismatch if (disks_complete and enclosures_complete) else None,
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
