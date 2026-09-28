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

    def test_no_controllers_returns_none_codes(self, ilo_device):
        client = IloRedfishClient(ilo_device)
        result = client.normalize({"controllers": []})

        assert result["controller_health_code"] is None
        assert result["logical_drive_worst_code"] is None
        assert result["missing_disk_count"] == 0
        assert result["enclosure_mismatch_count"] == 0


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

    def test_missing_disks_sorted_last(self, ilo_device):
        present = {"Location": "1I:3:1", "Status": {"Health": "OK"}}
        missing = {"@odata.id": "x", "is_missing": True}
        ordered = sorted([missing, present], key=IloRedfishClient._disk_sort_key)
        assert ordered == [present, missing]
