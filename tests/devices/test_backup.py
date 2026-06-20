"""Tests for Switch/Router configuration backup via SSH."""
import pytest
import os
from django.contrib.auth.models import User
from django.urls import reverse
from django.conf import settings
from unittest.mock import patch, mock_open
from tests.conftest import CiscoSSHDeviceFactory, CiscoSNMPDeviceFactory
from apps.devices.backup import get_device_backups, save_backup_file


@pytest.fixture
def logged_in_client(client, db):
    user = User.objects.create_user(username="admin", password="password123")
    client.login(username="admin", password="password123")
    return client


@pytest.mark.django_db
class TestConfigBackup:
    def test_backups_list_view_requires_login(self, client):
        device = CiscoSSHDeviceFactory()
        response = client.get(reverse("devices:backups", args=[device.pk]))
        assert response.status_code == 302

    def test_backups_list_view_empty(self, logged_in_client):
        device = CiscoSSHDeviceFactory()
        response = logged_in_client.get(reverse("devices:backups", args=[device.pk]))
        assert response.status_code == 200
        assert "Chưa có bản sao lưu nào" in response.content.decode("utf-8")

    def test_run_backup_rejected_for_non_ssh(self, logged_in_client):
        # SNMP device is non-SSH
        device = CiscoSNMPDeviceFactory()
        response = logged_in_client.get(reverse("devices:run_backup", args=[device.pk]))
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "chỉ hỗ trợ giao thức SSH" in data["message"]

    @patch("apps.devices.views.run_ssh_backup")
    @patch("apps.devices.views.save_backup_file")
    def test_run_backup_success(self, mock_save, mock_run, logged_in_client):
        device = CiscoSSHDeviceFactory(ssh_username="admin", ssh_password="password")
        mock_run.return_value = "hostname sw-core-01\ninterface Gi0/1\n!"
        mock_save.return_value = "/mock/path/backups/device_1_20260526_224500.txt"

        response = logged_in_client.get(reverse("devices:run_backup", args=[device.pk]))
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["filename"] == "device_1_20260526_224500.txt"
        assert "Sao lưu cấu hình thành công" in data["message"]

        mock_run.assert_called_once_with(device)
        mock_save.assert_called_once_with(device, mock_run.return_value)

    @patch("apps.devices.views.run_ssh_backup")
    def test_run_backup_exception(self, mock_run, logged_in_client):
        device = CiscoSSHDeviceFactory(ssh_username="admin", ssh_password="password")
        mock_run.side_effect = Exception("Authentication failed")

        response = logged_in_client.get(reverse("devices:run_backup", args=[device.pk]))
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Sao lưu thất bại" in data["message"]

    def test_save_and_get_backups(self, db):
        device = CiscoSSHDeviceFactory()
        content = "interface Loopback0\nip address 1.1.1.1 255.255.255.255"
        
        filepath = save_backup_file(device, content)
        assert os.path.exists(filepath)

        backups = get_device_backups(device)
        assert len(backups) == 1
        assert backups[0]["filename"] == os.path.basename(filepath)
        assert backups[0]["size_kb"] > 0.0

        # Clean up
        os.remove(filepath)

    def test_download_backup_directory_traversal(self, logged_in_client):
        device = CiscoSSHDeviceFactory()
        response = logged_in_client.get(
            reverse("devices:download_backup", args=[device.pk, "..secret.txt"])
        )
        assert response.status_code == 404

    def test_download_backup_invalid_filename(self, logged_in_client):
        device = CiscoSSHDeviceFactory()
        response = logged_in_client.get(
            reverse("devices:download_backup", args=[device.pk, "other_device_1_2026.txt"])
        )
        assert response.status_code == 404
