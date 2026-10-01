"""poll_all_ilo ghi/xoá lỗi poll iLO lên Device để UI hiện lý do (thay vì 'Chưa có dữ liệu' mãi)."""
import pytest

from apps.collectors.ilo_redfish import IloRedfishClient
from apps.collectors.tasks import poll_all_ilo
from apps.metrics.models import HardwareHealth
from tests.conftest import HyperVDeviceFactory


def _fake_status(mocker, status):
    resp = mocker.MagicMock(status_code=status)
    resp.json.return_value = {}
    mocker.patch("requests.Session.get", return_value=resp)


@pytest.mark.django_db
class TestLastError:
    def test_401_sets_reason(self, mocker):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
        _fake_status(mocker, 401)
        client = IloRedfishClient(host)
        assert client.collect_raw() is None
        assert "401" in client.last_error

    def test_poll_records_error_without_row(self, mocker):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
        _fake_status(mocker, 401)
        poll_all_ilo()
        host.refresh_from_db()
        assert "401" in host.ilo_last_error
        assert host.ilo_last_error_at is not None
        assert HardwareHealth.objects.count() == 0

    def test_exception_records_error(self, mocker):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
        mocker.patch.object(IloRedfishClient, "collect_raw", side_effect=RuntimeError("boom"))
        poll_all_ilo()
        host.refresh_from_db()
        assert "RuntimeError" in host.ilo_last_error

    def test_success_clears_error(self, mocker):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_last_error="401 Unauthorized")
        mocker.patch.object(IloRedfishClient, "collect_raw", return_value={"controllers": []})
        mocker.patch.object(IloRedfishClient, "normalize", return_value={"controller_health_code": 0})
        poll_all_ilo()
        host.refresh_from_db()
        assert host.ilo_last_error == ""
        assert host.ilo_last_error_at is None
        assert HardwareHealth.objects.filter(device=host).count() == 1
