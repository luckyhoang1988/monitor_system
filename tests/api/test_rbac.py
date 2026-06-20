import pytest
from rest_framework.test import APIClient
from django.contrib.auth.models import User, Group
from apps.devices.models import Device

@pytest.fixture
def rbac_users(db):
    # Tạo groups
    admin_group, _ = Group.objects.get_or_create(name='Network Admins')
    read_group, _ = Group.objects.get_or_create(name='Read-Only Operators')
    
    # Tạo users
    admin_user = User.objects.create_user(username="admin_rbac", password="123")
    admin_user.groups.add(admin_group)
    
    readonly_user = User.objects.create_user(username="readonly_rbac", password="123")
    readonly_user.groups.add(read_group)
    
    # Tạo thiết bị mẫu
    device = Device.objects.create(name="Test Switch", ip_address="192.168.1.100", device_type="switch", vendor="cisco", protocol="snmp")
    
    return admin_user, readonly_user, device

@pytest.mark.django_db
class TestRBAC:
    def test_readonly_user_can_read(self, rbac_users):
        _, readonly_user, device = rbac_users
        client = APIClient()
        client.force_authenticate(user=readonly_user)
        
        # Đọc danh sách thiết bị
        response = client.get("/api/v1/devices/")
        assert response.status_code == 200
        
    def test_readonly_user_cannot_delete(self, rbac_users):
        _, readonly_user, device = rbac_users
        client = APIClient()
        client.force_authenticate(user=readonly_user)
        
        # Thử xoá thiết bị (Write)
        response = client.delete(f"/api/v1/devices/{device.id}/")
        assert response.status_code == 403  # Forbidden
        
    def test_admin_user_can_delete(self, rbac_users):
        admin_user, _, device = rbac_users
        client = APIClient()
        client.force_authenticate(user=admin_user)
        
        # Thử xoá thiết bị (Write)
        response = client.delete(f"/api/v1/devices/{device.id}/")
        assert response.status_code == 204  # No content
