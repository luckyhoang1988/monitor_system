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

        return {"controllers": controllers}

    def _collect_controller(self, session: requests.Session, base: str, ac_uri: str) -> dict[str, Any]:
        device = self.device
        _, ac_detail = self._get(session, base, ac_uri)
        if not ac_detail:
            logger.warning("iLO %s: không lấy được controller detail %s", device.name, ac_uri)
            ac_detail = {}

        logical_drives: list[dict] = []
        expected_disk_uris: set[str] = set()
        ld_status, ld_list = self._get(session, base, ac_uri.rstrip("/") + "/LogicalDrives/")
        if ld_status == 200 and ld_list:
            for lm in ld_list.get("Members", []):
                ld_uri = lm.get("@odata.id")
                if not ld_uri:
                    continue
                _, ld_detail = self._get(session, base, ld_uri)
                if ld_detail:
                    logical_drives.append(ld_detail)
                dd_status, dd_list = self._get(session, base, ld_uri.rstrip("/") + "/DataDrives/")
                if dd_status == 200 and dd_list:
                    for dm in dd_list.get("Members", []):
                        d_uri = dm.get("@odata.id")
                        if d_uri:
                            expected_disk_uris.add(d_uri)

        # Đi qua đúng tập disk MÀ LD khai báo (DataDrives), không phải /DiskDrives/ collection —
        # collection chỉ liệt kê đĩa CÒN detect được, đĩa mất đơn giản KHÔNG xuất hiện trong đó.
        # Đi từng DataDrives URI mới bắt được 404 thật (verify runtime, xem docstring module).
        disks: list[dict] = []
        for d_uri in sorted(expected_disk_uris):
            d_status, d_detail = self._get(session, base, d_uri)
            if d_status == 404:
                disks.append({"@odata.id": d_uri, "is_missing": True})
            elif d_status == 200 and d_detail:
                disks.append(d_detail)
            else:
                logger.warning("iLO %s: disk %s trả HTTP %s không mong đợi (không phải 200/404)", device.name, d_uri, d_status)

        enclosures: list[dict] = []
        encl_status, encl_list = self._get(session, base, ac_uri.rstrip("/") + "/StorageEnclosures/")
        if encl_status == 200 and encl_list:
            for em in encl_list.get("Members", []):
                e_uri = em.get("@odata.id")
                if not e_uri:
                    continue
                _, e_detail = self._get(session, base, e_uri)
                if e_detail:
                    enclosures.append(e_detail)

        return {
            "detail": ac_detail,
            "logical_drives": logical_drives,
            "disks": disks,
            "enclosures": enclosures,
        }

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

        for controller in raw.get("controllers", []):
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

        return {
            "controller_health_code": max(controller_codes) if controller_codes else None,
            "logical_drive_worst_code": max(ld_codes) if ld_codes else None,
            "missing_disk_count": missing_count,
            "enclosure_mismatch_count": enclosure_mismatch,
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
