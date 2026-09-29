"""Tests cho IloRedfishClient — dựa trên schema JSON thật đã verify runtime (Hyperv-02 P440ar
firmware 5.04 đang RAID Critical, Hyprver03 P440ar firmware 4.52 khoẻ mạnh, 2026-09-28, xem
scratchpad/ilo_probe_output*.txt), không cần thiết bị thật để chạy test.

⚠️ Field disk2/3 Location trong _raw_unhealthy_controller() là SUY DIỄN hợp lý (cùng tiền tố
"1I:3" như disk0/1 đã verify thật) để test được logic enclosure_mismatch một cách xác định —
KHÔNG phải giá trị đã tự tay verify trên thiết bị thật (probe gốc chỉ lấy mẫu disk 0/1). Disk
4/5 "is_missing" (404 thật, verify runtime) và mọi field khác (Status.Health, DriveBayCount,
SerialNumber, FirmwareVersion...) đều là giá trị thật đã dump.
"""
import pytest
import requests

from apps.collectors.ilo_redfish import IloRedfishClient
from tests.conftest import HyperVDeviceFactory


def _disk(disk_id: str, location: str, health: str = "OK") -> dict:
    return {
        "Id": disk_id,
        "Model": "EG1800JEMDB",
        "Location": location,
        "CapacityGB": 1800,
        "Status": {"Health": health, "State": "Enabled"},
    }


def _raw_unhealthy_controller() -> dict:
    """Mirror Hyperv-02 thật: controller Critical, LD2/RAID5 Warning, mất disk 4+5."""
    return {
        "controllers": [{
            "detail": {
                "Model": "Smart Array P440ar Controller",
                "SerialNumber": "PDNLH0BRH78FMU",
                "FirmwareVersion": {"Current": {"VersionString": "5.04"}},
                "Status": {"Health": "Critical", "State": "Enabled"},
            },
            "logical_drives": [
                {"Id": "1", "LogicalDriveNumber": 1, "Raid": "1", "CapacityMiB": 286070,
                 "LogicalDriveName": "01A6761F...", "Status": {"Health": "OK", "State": "Enabled"}},
                {"Id": "2", "LogicalDriveNumber": 2, "Raid": "5", "CapacityMiB": 3433850,
                 "LogicalDriveName": "060C1E21...", "Status": {"Health": "Warning", "State": "Enabled"}},
            ],
            "disks": [
                _disk("0", "1I:3:4"),
                _disk("1", "1I:3:3"),
                _disk("2", "1I:3:2"),  # suy diễn (xem docstring module) — cùng tiền tố đã verify
                _disk("3", "1I:3:1"),  # suy diễn
                {"@odata.id": "/redfish/v1/.../DiskDrives/4/", "is_missing": True},
                {"@odata.id": "/redfish/v1/.../DiskDrives/5/", "is_missing": True},
            ],
            "enclosures": [
                {"Id": "0", "Location": "1I:3", "DriveBayCount": 4, "Status": {"Health": "OK", "State": "Enabled"}},
                {"Id": "1", "Location": "2I:3", "DriveBayCount": 4, "Status": {"Health": "OK", "State": "Enabled"}},
            ],
        }],
    }


def _raw_healthy_controller() -> dict:
    """Mirror Hyprver03 thật: mọi Health=OK, đủ 6/6 disk."""
    return {
        "controllers": [{
            "detail": {
                "Model": "Smart Array P440ar Controller",
                "SerialNumber": "PDNLH0BRH6327D",
                "FirmwareVersion": {"Current": {"VersionString": "4.52"}},
                "Status": {"Health": "OK", "State": "Enabled"},
            },
            "logical_drives": [
                {"Id": "1", "LogicalDriveNumber": 1, "Raid": "1", "CapacityMiB": 286070,
                 "LogicalDriveName": "009275FA...", "Status": {"Health": "OK", "State": "Enabled"}},
                {"Id": "2", "LogicalDriveNumber": 2, "Raid": "5", "CapacityMiB": 5150775,
                 "LogicalDriveName": "063229AD...", "Status": {"Health": "OK", "State": "Enabled"}},
            ],
            "disks": [
                _disk("0", "1I:3:4"), _disk("1", "1I:3:3"),
                _disk("2", "1I:3:2"), _disk("3", "1I:3:1"),
                _disk("4", "2I:3:2"), _disk("5", "2I:3:1"),
            ],
            "enclosures": [
                {"Id": "0", "Location": "1I:3", "DriveBayCount": 4, "Status": {"Health": "OK", "State": "Enabled"}},
                {"Id": "1", "Location": "2I:3", "DriveBayCount": 4, "Status": {"Health": "OK", "State": "Enabled"}},
            ],
        }],
    }


@pytest.fixture
def ilo_device(db):
    return HyperVDeviceFactory(
        name="hyperv-ilo-test",
        ilo_ip_address="10.0.198.254",
        ilo_username="administrator",
        ilo_password="secret",
    )


class TestNormalize:
    def test_unhealthy_controller_matches_real_hyperv02_case(self, ilo_device):
        client = IloRedfishClient(ilo_device)
        result = client.normalize(_raw_unhealthy_controller())

        assert result["controller_health_code"] == 2       # Critical
        assert result["logical_drive_worst_code"] == 1      # LD2/RAID5 = Warning, LD1 = OK -> worst=1
        assert result["missing_disk_count"] == 2            # disk 4 + 5 mất (404 thật)
        assert result["enclosure_mismatch_count"] == 1       # enclosure "2I:3" 0 đĩa present dù bay=4
        assert result["raw"]["controllers"][0]["detail"]["SerialNumber"] == "PDNLH0BRH78FMU"

    def test_healthy_controller_matches_real_hyprver03_case(self, ilo_device):
        client = IloRedfishClient(ilo_device)
        result = client.normalize(_raw_healthy_controller())

        assert result["controller_health_code"] == 0
        assert result["logical_drive_worst_code"] == 0
        assert result["missing_disk_count"] == 0
        assert result["enclosure_mismatch_count"] == 0

    def test_unknown_health_enum_defaults_to_critical_and_logs_warning(self, ilo_device, caplog):
        raw = _raw_healthy_controller()
        raw["controllers"][0]["detail"]["Status"]["Health"] = "SomethingNeverSeenBefore"
        client = IloRedfishClient(ilo_device)

        with caplog.at_level("WARNING"):
            result = client.normalize(raw)

        assert result["controller_health_code"] == 2  # an toàn: coi enum lạ là Critical
        assert "enum Health lạ" in caplog.text

    def test_no_controllers_returns_none_for_everything(self, ilo_device):
        """Regression 2026-09-29 vòng 3: trước đây `missing_disk_count`/`enclosure_mismatch_count`
        trả 0 khi controllers=[] (đúng identity toán học "cộng dồn tập rỗng" nhưng SAI về ý
        nghĩa -- 0 controller trên 1 host đã cấu hình iLO monitoring là "chưa đọc được gì", không
        phải "đã verify 0 vấn đề"). Nay cả 4 field đều None (không đủ bằng chứng)."""
        client = IloRedfishClient(ilo_device)
        result = client.normalize({"controllers": []})

        assert result["controller_health_code"] is None
        assert result["logical_drive_worst_code"] is None
        assert result["missing_disk_count"] is None
        assert result["enclosure_mismatch_count"] is None

    def test_disks_incomplete_returns_none_instead_of_false_zero(self, ilo_device):
        """Regression 2026-09-29: LogicalDrives/DataDrives fetch lỗi từng bị nuốt im lặng ->
        disks rỗng -> missing_disk_count=0 GIẢ (alert engine đọc 0 này tưởng "đã hồi phục" dù
        RAID thật chưa chắc đã hồi phục). Nay phải trả None (không đủ dữ liệu để kết luận)."""
        raw = _raw_unhealthy_controller()
        raw["controllers"][0]["disks_complete"] = False
        client = IloRedfishClient(ilo_device)

        result = client.normalize(raw)

        assert result["missing_disk_count"] is None
        assert result["enclosure_mismatch_count"] is None  # phụ thuộc disks_complete
        # Health code controller/logical-drive không phụ thuộc disks -> vẫn tính bình thường.
        assert result["controller_health_code"] == 2
        assert result["logical_drive_worst_code"] == 1

    def test_enclosures_incomplete_leaves_missing_disk_count_intact(self, ilo_device):
        """StorageEnclosures fetch lỗi riêng (disks vẫn đủ) -> chỉ enclosure_mismatch_count bị
        None; missing_disk_count không phụ thuộc enclosures nên vẫn tính bình thường."""
        raw = _raw_unhealthy_controller()
        raw["controllers"][0]["enclosures_complete"] = False
        client = IloRedfishClient(ilo_device)

        result = client.normalize(raw)

        assert result["missing_disk_count"] == 2
        assert result["enclosure_mismatch_count"] is None

    def test_logical_drives_incomplete_returns_none_worst_code(self, ilo_device):
        """Regression 2026-09-29 vòng 2: cờ logical_drives_complete=False (vd 1 LD detail fetch
        lỗi) -> logical_drive_worst_code phải None, KHÔNG được tính max() trên phần LD còn lại
        (có thể bỏ sót đúng cái LD Critical bị lỗi fetch). Các field khác không phụ thuộc LD
        detail nên vẫn tính bình thường."""
        raw = _raw_unhealthy_controller()
        raw["controllers"][0]["logical_drives_complete"] = False
        client = IloRedfishClient(ilo_device)

        result = client.normalize(raw)

        assert result["logical_drive_worst_code"] is None
        assert result["controller_health_code"] == 2
        assert result["missing_disk_count"] == 2
        assert result["enclosure_mismatch_count"] == 1


class TestGetRetry:
    """iLO4/5 đóng TCP connection sau ĐÚNG 1 request dù keep-alive (verify runtime 2026-09-28)
    -> _get() phải retry 1 lần bằng session mới khi ConnectionError."""

    def test_retries_once_on_connection_error_then_succeeds(self, mocker):
        session = mocker.MagicMock(spec=requests.Session)
        ok_response = mocker.MagicMock(status_code=200)
        ok_response.json.return_value = {"hello": "world"}
        session.get.side_effect = [requests.exceptions.ConnectionError("reset"), ok_response]

        status, body = IloRedfishClient._get(session, "https://10.0.198.254", "/redfish/v1/x/")

        assert status == 200
        assert body == {"hello": "world"}
        assert session.get.call_count == 2
        session.close.assert_called_once()  # đóng session cũ trước khi retry

    def test_returns_none_after_second_connection_error(self, mocker):
        session = mocker.MagicMock(spec=requests.Session)
        session.get.side_effect = [
            requests.exceptions.ConnectionError("reset"),
            requests.exceptions.ConnectionError("reset again"),
        ]

        status, body = IloRedfishClient._get(session, "https://10.0.198.254", "/redfish/v1/x/")

        assert status is None
        assert body is None
        assert session.get.call_count == 2

    def test_404_returns_status_with_no_retry(self, mocker):
        session = mocker.MagicMock(spec=requests.Session)
        response = mocker.MagicMock(status_code=404)
        response.json.return_value = {"error": "not found"}
        session.get.return_value = response

        status, body = IloRedfishClient._get(session, "https://10.0.198.254", "/redfish/v1/missing/")

        assert status == 404
        assert session.get.call_count == 1


class TestCollectRaw:
    def test_returns_none_when_no_ilo_ip_configured(self, db, mocker):
        device = HyperVDeviceFactory(ilo_ip_address=None)
        spy = mocker.patch("requests.Session.get")

        result = IloRedfishClient(device).collect_raw()

        assert result is None
        spy.assert_not_called()

    def test_returns_none_on_401(self, ilo_device, mocker):
        response = mocker.MagicMock(status_code=401)
        response.json.return_value = {}
        mocker.patch("requests.Session.get", return_value=response)

        result = IloRedfishClient(ilo_device).collect_raw()

        assert result is None

    def test_returns_none_when_root_200_but_members_empty(self, ilo_device, mocker):
        """Regression 2026-09-29 vòng 3: root ArrayControllers trả HTTP 200 (không phải 401/404/
        lỗi kết nối) nhưng body {"Members": []} -- ac_root vẫn TRUTHY nên lọt qua check `not
        ac_root`, controllers=[] sau vòng lặp Members rỗng. Trước đây collect_raw() trả
        {"controllers": []} bình thường -> normalize() cộng dồn ra 0 GIẢ cho mọi counter. Nay
        collect_raw() phải tự chặn, trả None y hệt các nhánh lỗi root khác -- poll_all_ilo bỏ qua
        không lưu, KHÔNG để lại HardwareHealth row nào coi như "đã verify 0 vấn đề"."""
        root = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"
        response = mocker.MagicMock(status_code=200)
        response.json.return_value = {"Members": []}
        mocker.patch("requests.Session.get", return_value=response)

        result = IloRedfishClient(ilo_device).collect_raw()

        assert result is None

    def test_returns_none_when_root_200_but_no_members_key(self, ilo_device, mocker):
        """Biến thể khác của cùng bug: body 200 hoàn toàn thiếu key "Members" (không phải rỗng
        rõ ràng) -- `.get("Members", [])` cũng ra [] giống hệt case trên, cùng 1 nhánh chặn."""
        response = mocker.MagicMock(status_code=200)
        response.json.return_value = {"SomeOtherField": "x"}
        mocker.patch("requests.Session.get", return_value=response)

        result = IloRedfishClient(ilo_device).collect_raw()

        assert result is None

    def test_full_flow_with_one_missing_disk(self, ilo_device, mocker):
        """1 controller / 1 LD / 2 disk khai báo (1 present, 1 missing 404) / 1 enclosure —
        verify collect_raw() đi đúng endpoint và gắn cờ _missing đúng chỗ."""
        root = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"
        ac_uri = root + "0/"

        def fake_get(url, timeout=None, headers=None):
            path = url.replace("https://10.0.198.254", "")
            resp = mocker.MagicMock()
            if path == root:
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri}]}
            elif path == ac_uri:
                resp.status_code = 200
                resp.json.return_value = {"Status": {"Health": "Warning"}, "Model": "Test Controller"}
            elif path == ac_uri + "LogicalDrives/":
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri + "LogicalDrives/1/"}]}
            elif path == ac_uri + "LogicalDrives/1/":
                resp.status_code = 200
                resp.json.return_value = {"Id": "1", "Raid": "1", "Status": {"Health": "OK"}}
            elif path == ac_uri + "LogicalDrives/1/DataDrives/":
                resp.status_code = 200
                resp.json.return_value = {"Members": [
                    {"@odata.id": ac_uri + "DiskDrives/0/"},
                    {"@odata.id": ac_uri + "DiskDrives/1/"},
                ]}
            elif path == ac_uri + "DiskDrives/0/":
                resp.status_code = 200
                resp.json.return_value = {"Id": "0", "Status": {"Health": "OK"}, "Location": "1I:3:4"}
            elif path == ac_uri + "DiskDrives/1/":
                resp.status_code = 404
                resp.json.return_value = {"error": "Base.0.10.ResourceMissingAtURI"}
            elif path == ac_uri + "StorageEnclosures/":
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri + "StorageEnclosures/0/"}]}
            elif path == ac_uri + "StorageEnclosures/0/":
                resp.status_code = 200
                resp.json.return_value = {"Id": "0", "DriveBayCount": 4, "Location": "1I:3", "Status": {"Health": "OK"}}
            else:
                raise AssertionError(f"Unexpected path probed in test: {path}")
            return resp

        mocker.patch("requests.Session.get", side_effect=fake_get)

        raw = IloRedfishClient(ilo_device).collect_raw()

        assert raw is not None
        controller = raw["controllers"][0]
        assert controller["detail"]["Status"]["Health"] == "Warning"
        assert len(controller["logical_drives"]) == 1
        assert len(controller["disks"]) == 2
        missing = [d for d in controller["disks"] if d.get("is_missing")]
        assert len(missing) == 1
        assert missing[0]["@odata.id"] == ac_uri + "DiskDrives/1/"
        # Missing disk không có Location -> không đoán bay: bay_number=None, UI tự fallback về
        # "Disk N" — nhưng vẫn cần Id hiển thị được, lấy từ path segment cuối @odata.id (quy ước
        # Redfish, không phải đoán).
        assert missing[0]["bay_number"] is None
        assert missing[0]["Id"] == "1"
        present = [d for d in controller["disks"] if not d.get("is_missing")][0]
        assert present["bay_number"] == 4  # Location "1I:3:4" -> bay 4

        normalized = IloRedfishClient(ilo_device).normalize(raw)
        assert normalized["missing_disk_count"] == 1

    def test_disks_sorted_by_physical_bay_not_by_id(self, ilo_device, mocker):
        """Regression: verify runtime 2026-09-28 (đối chiếu dashboard thật với người dùng biết
        chắc layout máy) — box "1I:3" có Redfish Id TĂNG dần (0,1,2,3) nhưng bay vật lý lại
        GIẢM dần (4,3,2,1); 2 ổ 300GB chạy OS thật sự nằm bay 1-2 (đứng đầu vật lý) nhưng cũ
        sort theo Id đẩy chúng xuống cuối bảng. Mirror ĐÚNG layout thật Hyprver03: LD1(RAID1)
        = disk2,3 (300GB, bay 2,1) — LD2(RAID5) = disk0,1,4,5 (1800GB, bay 4,3 + bay 5,6)."""
        root = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"
        ac_uri = root + "0/"

        disk_specs = {
            "0": ("1I:3:4", 1800),
            "1": ("1I:3:3", 1800),
            "2": ("1I:3:2", 300),
            "3": ("1I:3:1", 300),
            "4": ("2I:3:5", 1800),
            "5": ("2I:3:6", 1800),
        }

        def fake_get(url, timeout=None, headers=None):
            path = url.replace("https://10.0.198.254", "")
            resp = mocker.MagicMock()
            if path == root:
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri}]}
            elif path == ac_uri:
                resp.status_code = 200
                resp.json.return_value = {"Status": {"Health": "OK"}}
            elif path == ac_uri + "LogicalDrives/":
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri + "LogicalDrives/1/"}]}
            elif path == ac_uri + "LogicalDrives/1/":
                resp.status_code = 200
                resp.json.return_value = {"Id": "1", "Raid": "1", "Status": {"Health": "OK"}}
            elif path == ac_uri + "LogicalDrives/1/DataDrives/":
                resp.status_code = 200
                resp.json.return_value = {
                    "Members": [{"@odata.id": ac_uri + f"DiskDrives/{i}/"} for i in disk_specs]
                }
            elif path == ac_uri + "StorageEnclosures/":
                resp.status_code = 200
                resp.json.return_value = {"Members": []}
            else:
                for disk_id, (location, capacity_gb) in disk_specs.items():
                    if path == ac_uri + f"DiskDrives/{disk_id}/":
                        resp.status_code = 200
                        resp.json.return_value = {
                            "Id": disk_id, "Location": location, "CapacityGB": capacity_gb,
                            "Status": {"Health": "OK"},
                        }
                        return resp
                raise AssertionError(f"Unexpected path probed in test: {path}")
            return resp

        mocker.patch("requests.Session.get", side_effect=fake_get)

        raw = IloRedfishClient(ilo_device).collect_raw()

        disks = raw["controllers"][0]["disks"]
        locations_in_order = [d["Location"] for d in disks]
        # Bay tăng dần xuyên suốt cả 2 box (1,2,3,4 rồi 5,6) — KHÔNG theo Id (vốn sẽ ra
        # 1I:3:4, 1I:3:3, 1I:3:2, 1I:3:1, 2I:3:5, 2I:3:6 nếu sort theo Id như code cũ).
        assert locations_in_order == ["1I:3:1", "1I:3:2", "1I:3:3", "1I:3:4", "2I:3:5", "2I:3:6"]
        # 2 ổ 300GB (chạy OS, bay 1-2) phải đứng đầu bảng.
        assert [d["CapacityGB"] for d in disks[:2]] == [300, 300]
        # bay_number hiển thị UI ("Bay N") phải tăng dần khớp thứ tự hiển thị, xuyên suốt cả 2 box.
        assert [d["bay_number"] for d in disks] == [1, 2, 3, 4, 5, 6]

    def test_logical_drives_fetch_failure_marks_disks_incomplete(self, ilo_device, mocker):
        """Regression 2026-09-29: LogicalDrives trả lỗi thật (HTTP 500, khác 401/404 ở ROOT —
        root fail thì collect_raw() trả None và poll_all_ilo bỏ qua hẳn) từng bị nuốt im lặng,
        để disks=[] rồi normalize() cộng dồn ra missing_disk_count=0 GIẢ. Nay collect_raw() phải
        đánh dấu disks_complete=False, normalize() phải trả None thay vì 0."""
        root = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"
        ac_uri = root + "0/"

        def fake_get(url, timeout=None, headers=None):
            path = url.replace("https://10.0.198.254", "")
            resp = mocker.MagicMock()
            if path == root:
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri}]}
            elif path == ac_uri:
                resp.status_code = 200
                resp.json.return_value = {"Status": {"Health": "Critical"}}
            elif path == ac_uri + "LogicalDrives/":
                resp.status_code = 500  # lỗi thật -- không phải 200 lẫn 404
                resp.json.side_effect = ValueError("not json")
            elif path == ac_uri + "StorageEnclosures/":
                resp.status_code = 200
                resp.json.return_value = {"Members": []}
            else:
                raise AssertionError(f"Unexpected path probed in test: {path}")
            return resp

        mocker.patch("requests.Session.get", side_effect=fake_get)

        raw = IloRedfishClient(ilo_device).collect_raw()

        controller = raw["controllers"][0]
        assert controller["disks_complete"] is False
        assert controller["disks"] == []

        normalized = IloRedfishClient(ilo_device).normalize(raw)
        # Controller health không phụ thuộc LogicalDrives -> vẫn đọc được Critical bình thường.
        assert normalized["controller_health_code"] == 2
        # Đây chính là bug: code cũ trả 0 (missing_count khởi tạo 0, không ai increment vì
        # disks=[]) -- alert engine đọc 0 này tưởng "đã hồi phục". Nay phải là None.
        assert normalized["missing_disk_count"] is None
        assert normalized["enclosure_mismatch_count"] is None

    def test_enclosure_detail_fetch_failure_marks_enclosures_incomplete(self, ilo_device, mocker):
        """Regression 2026-09-29 vòng 2: /StorageEnclosures/ (list) trả 200 OK, nhưng GET detail
        của 1 enclosure member cụ thể lỗi (HTTP 500) -- trước đây bị nuốt im lặng (chỉ bỏ qua
        khỏi list `enclosures`, KHÔNG hạ enclosures_complete) khiến enclosure_mismatch_count vẫn
        được tính trên phần còn lại (thiếu đúng enclosure có thể đang mismatch). Nay phải đánh
        dấu enclosures_complete=False, normalize() phải trả None."""
        root = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"
        ac_uri = root + "0/"

        def fake_get(url, timeout=None, headers=None):
            path = url.replace("https://10.0.198.254", "")
            resp = mocker.MagicMock()
            if path == root:
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri}]}
            elif path == ac_uri:
                resp.status_code = 200
                resp.json.return_value = {"Status": {"Health": "OK"}}
            elif path == ac_uri + "LogicalDrives/":
                resp.status_code = 200
                resp.json.return_value = {"Members": []}
            elif path == ac_uri + "StorageEnclosures/":
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri + "StorageEnclosures/0/"}]}
            elif path == ac_uri + "StorageEnclosures/0/":
                resp.status_code = 500  # detail fetch lỗi thật -- khác list root ở trên (200)
                resp.json.side_effect = ValueError("not json")
            else:
                raise AssertionError(f"Unexpected path probed in test: {path}")
            return resp

        mocker.patch("requests.Session.get", side_effect=fake_get)

        raw = IloRedfishClient(ilo_device).collect_raw()

        controller = raw["controllers"][0]
        assert controller["enclosures_complete"] is False
        assert controller["enclosures"] == []

        normalized = IloRedfishClient(ilo_device).normalize(raw)
        # missing_disk_count không phụ thuộc enclosures -> vẫn tính bình thường (0, không LD nào).
        assert normalized["missing_disk_count"] == 0
        # Đây chính là bug: code cũ trả 0 (mismatch khởi tạo 0, enclosures=[] nên không đếm được
        # gì) -- alert engine đọc 0 này tưởng "đã hồi phục". Nay phải là None.
        assert normalized["enclosure_mismatch_count"] is None

    def test_logical_drive_detail_fetch_failure_marks_logical_drives_incomplete(self, ilo_device, mocker):
        """Regression 2026-09-29 vòng 2: /LogicalDrives/ (list) trả 200 OK với 2 member, nhưng
        GET detail của 1 LD cụ thể lỗi (HTTP 500) -- trước đây bị nuốt im lặng (chỉ bỏ qua khỏi
        list `logical_drives`), khiến logical_drive_worst_code tính max() trên LD còn lại (OK),
        bỏ sót đúng LD lỗi fetch (có thể đang Critical) -> trả 0 thay vì None."""
        root = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"
        ac_uri = root + "0/"

        def fake_get(url, timeout=None, headers=None):
            path = url.replace("https://10.0.198.254", "")
            resp = mocker.MagicMock()
            if path == root:
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri}]}
            elif path == ac_uri:
                resp.status_code = 200
                resp.json.return_value = {"Status": {"Health": "OK"}}
            elif path == ac_uri + "LogicalDrives/":
                resp.status_code = 200
                resp.json.return_value = {"Members": [
                    {"@odata.id": ac_uri + "LogicalDrives/1/"},
                    {"@odata.id": ac_uri + "LogicalDrives/2/"},
                ]}
            elif path == ac_uri + "LogicalDrives/1/":
                resp.status_code = 200
                resp.json.return_value = {"Id": "1", "Status": {"Health": "OK"}}
            elif path == ac_uri + "LogicalDrives/2/":
                resp.status_code = 500  # detail fetch lỗi thật -- LD này có thể đang Critical
                resp.json.side_effect = ValueError("not json")
            elif path == ac_uri + "LogicalDrives/1/DataDrives/":
                resp.status_code = 200
                resp.json.return_value = {"Members": []}
            elif path == ac_uri + "LogicalDrives/2/DataDrives/":
                resp.status_code = 200
                resp.json.return_value = {"Members": []}
            elif path == ac_uri + "StorageEnclosures/":
                resp.status_code = 200
                resp.json.return_value = {"Members": []}
            else:
                raise AssertionError(f"Unexpected path probed in test: {path}")
            return resp

        mocker.patch("requests.Session.get", side_effect=fake_get)

        raw = IloRedfishClient(ilo_device).collect_raw()

        controller = raw["controllers"][0]
        assert controller["logical_drives_complete"] is False
        assert len(controller["logical_drives"]) == 1  # chỉ LD1 fetch được
        # DataDrives của cả 2 LD vẫn fetch OK -> disks_complete KHÔNG bị ảnh hưởng bởi LD detail lỗi.
        assert controller["disks_complete"] is True

        normalized = IloRedfishClient(ilo_device).normalize(raw)
        # Đây chính là bug: code cũ trả 0 (ld_codes=[0] từ LD1 OK, LD2 lỗi bị bỏ qua hẳn) -- alert
        # engine đọc 0 này tưởng "đã hồi phục" dù LD2 (fetch lỗi) có thể đang Critical. Nay None.
        assert normalized["logical_drive_worst_code"] is None
        assert normalized["missing_disk_count"] == 0  # không phụ thuộc LD detail

    def test_logical_drives_json_missing_members_key_marks_incomplete(self, ilo_device, mocker):
        """Regression 2026-09-29 vòng 4 (rủi ro suy luận, CHƯA quan sát trên iLO thật): /LogicalDrives/
        trả HTTP 200 nhưng JSON KHÔNG có key "Members" (vd {"Oem": {...}} bất thường, khác hẳn
        {"Members": []} rỗng hợp lệ) -- trước đây `.get("Members", [])` gộp chung 2 case này thành
        [] như nhau, khiến vòng lặp không chạy lần nào mà KHÔNG hạ disks_complete/
        logical_drives_complete. Nay `_members()` phải trả None cho case JSON thiếu Members, khác
        hẳn case Members=[] thật (xem test_healthy_controller_matches_real_hyprver03_case)."""
        root = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"
        ac_uri = root + "0/"

        def fake_get(url, timeout=None, headers=None):
            path = url.replace("https://10.0.198.254", "")
            resp = mocker.MagicMock()
            if path == root:
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri}]}
            elif path == ac_uri:
                resp.status_code = 200
                resp.json.return_value = {"Status": {"Health": "Critical"}}
            elif path == ac_uri + "LogicalDrives/":
                resp.status_code = 200  # OK thật -- KHÔNG phải lỗi HTTP
                resp.json.return_value = {"Oem": {"Hpe": {}}}  # JSON hợp lệ nhưng thiếu "Members"
            elif path == ac_uri + "StorageEnclosures/":
                resp.status_code = 200
                resp.json.return_value = {"Members": []}
            else:
                raise AssertionError(f"Unexpected path probed in test: {path}")
            return resp

        mocker.patch("requests.Session.get", side_effect=fake_get)

        raw = IloRedfishClient(ilo_device).collect_raw()

        controller = raw["controllers"][0]
        assert controller["disks_complete"] is False
        assert controller["logical_drives_complete"] is False
        assert controller["disks"] == []
        assert controller["logical_drives"] == []

        normalized = IloRedfishClient(ilo_device).normalize(raw)
        assert normalized["controller_health_code"] == 2  # không phụ thuộc LogicalDrives
        assert normalized["missing_disk_count"] is None
        assert normalized["logical_drive_worst_code"] is None

    def test_data_drives_json_missing_members_key_marks_disks_incomplete(self, ilo_device, mocker):
        """Cùng loại bug trên nhưng ở /DataDrives/ (tầng con của 1 LD cụ thể, khác /LogicalDrives/
        ở test trên) -- JSON 200 thiếu key Members phải hạ disks_complete, KHÔNG được coi "0 disk
        khai báo cho LD này". CHƯA quan sát trên iLO thật."""
        root = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"
        ac_uri = root + "0/"

        def fake_get(url, timeout=None, headers=None):
            path = url.replace("https://10.0.198.254", "")
            resp = mocker.MagicMock()
            if path == root:
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri}]}
            elif path == ac_uri:
                resp.status_code = 200
                resp.json.return_value = {"Status": {"Health": "OK"}}
            elif path == ac_uri + "LogicalDrives/":
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri + "LogicalDrives/1/"}]}
            elif path == ac_uri + "LogicalDrives/1/":
                resp.status_code = 200
                resp.json.return_value = {"Id": "1", "Status": {"Health": "OK"}}
            elif path == ac_uri + "LogicalDrives/1/DataDrives/":
                resp.status_code = 200  # OK thật -- KHÔNG phải lỗi HTTP
                resp.json.return_value = {}  # JSON hợp lệ nhưng thiếu "Members"
            elif path == ac_uri + "StorageEnclosures/":
                resp.status_code = 200
                resp.json.return_value = {"Members": []}
            else:
                raise AssertionError(f"Unexpected path probed in test: {path}")
            return resp

        mocker.patch("requests.Session.get", side_effect=fake_get)

        raw = IloRedfishClient(ilo_device).collect_raw()

        controller = raw["controllers"][0]
        assert controller["disks_complete"] is False
        # LD detail fetch OK bình thường -- chỉ DataDrives (sub-collection RIÊNG) lỗi.
        assert controller["logical_drives_complete"] is True
        assert controller["disks"] == []

        normalized = IloRedfishClient(ilo_device).normalize(raw)
        assert normalized["missing_disk_count"] is None
        assert normalized["logical_drive_worst_code"] == 0  # LD detail vẫn đọc được OK bình thường

    def test_storage_enclosures_json_missing_members_key_marks_incomplete(self, ilo_device, mocker):
        """Cùng loại bug trên nhưng ở /StorageEnclosures/ -- JSON 200 thiếu key Members từng bị
        `encl_list is not None`=True (SAI, coi enclosures_complete=True) rồi `elif encl_list:`
        falsy bỏ qua lặng lẽ, không log/không hạ cờ. CHƯA quan sát trên iLO thật."""
        root = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/"
        ac_uri = root + "0/"

        def fake_get(url, timeout=None, headers=None):
            path = url.replace("https://10.0.198.254", "")
            resp = mocker.MagicMock()
            if path == root:
                resp.status_code = 200
                resp.json.return_value = {"Members": [{"@odata.id": ac_uri}]}
            elif path == ac_uri:
                resp.status_code = 200
                resp.json.return_value = {"Status": {"Health": "OK"}}
            elif path == ac_uri + "LogicalDrives/":
                resp.status_code = 200
                resp.json.return_value = {"Members": []}
            elif path == ac_uri + "StorageEnclosures/":
                resp.status_code = 200  # OK thật -- KHÔNG phải lỗi HTTP
                resp.json.return_value = {}  # JSON hợp lệ (dict) nhưng thiếu "Members"
            else:
                raise AssertionError(f"Unexpected path probed in test: {path}")
            return resp

        mocker.patch("requests.Session.get", side_effect=fake_get)

        raw = IloRedfishClient(ilo_device).collect_raw()

        controller = raw["controllers"][0]
        assert controller["enclosures_complete"] is False
        assert controller["enclosures"] == []

        normalized = IloRedfishClient(ilo_device).normalize(raw)
        assert normalized["missing_disk_count"] == 0  # LogicalDrives Members=[] hợp lệ thật
        assert normalized["enclosure_mismatch_count"] is None

    def test_missing_disks_sorted_last(self, ilo_device):
        present = {"Location": "1I:3:1", "Status": {"Health": "OK"}}
        missing = {"@odata.id": "x", "is_missing": True}
        ordered = sorted([missing, present], key=IloRedfishClient._disk_sort_key)
        assert ordered == [present, missing]
