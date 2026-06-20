import pytest
from unittest.mock import patch, MagicMock
from apps.devices.models import Device, DeviceBackup
from apps.devices.tasks import auto_backup_all_devices

@pytest.fixture
def ssh_devices(db):
    # Device SSH hợp lệ, bật auto backup
    Device.objects.create(name="SSH Device 1", ip_address="1.1.1.1", device_type="switch", vendor="cisco", protocol="ssh", backup_enabled=True, ssh_username="u", ssh_password="p")
    # Device SSH lỗi mật khẩu, bật auto backup
    Device.objects.create(name="SSH Device Fail", ip_address="2.2.2.2", device_type="switch", vendor="cisco", protocol="ssh", backup_enabled=True, ssh_username="", ssh_password="")
    # Device không bật backup
    Device.objects.create(name="No Backup Device", ip_address="3.3.3.3", device_type="switch", vendor="cisco", protocol="ssh", backup_enabled=False)

@pytest.mark.django_db
class TestAutoBackup:
    @patch('apps.devices.tasks.run_ssh_backup')
    @patch('apps.devices.tasks.save_backup_file')
    def test_auto_backup_all_devices(self, mock_save, mock_run, ssh_devices):
        # Giả lập run_ssh_backup thành công
        mock_run.return_value = "config building..."
        mock_save.return_value = "/mock/path.txt"
        
        # Cái thứ 2 sẽ quăng ValueError vì thiếu username/password do mình viết logic trong run_ssh_backup?
        # Không, run_ssh_backup bị mock toàn bộ rồi, nên nó luôn trả về string.
        # Để test kỹ, ta cấu hình side_effect.
        
        def mock_run_ssh_backup(device):
            if not device.ssh_username:
                raise ValueError("Missing credentials")
            return "config..."
        
        mock_run.side_effect = mock_run_ssh_backup
        
        result = auto_backup_all_devices()
        
        assert result["success"] == 1
        assert result["failed"] == 1
        
        # Verify db records
        backups = DeviceBackup.objects.all()
        assert backups.count() == 1 # Cái thành công được lưu thông qua save_backup_file (bị mock nên ko lưu).
        # Ah wait, save_backup_file BỊ MOCK nên nó ko tạo record trong DB cho cái success!
        # Nhưng cái failed thì task.py CÓ gọi DeviceBackup.objects.create!
        
        failed_backups = DeviceBackup.objects.filter(status="FAILED")
        assert failed_backups.count() == 1
        assert "Missing credentials" in failed_backups.first().error_message
