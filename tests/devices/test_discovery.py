"""Tests for Auto-Discovery Scanner."""
import time

import pytest
from django.urls import reverse
from unittest.mock import patch
from apps.devices.models import Device, DiscoveredDevice


@pytest.mark.django_db
class TestDiscoveryViews:
    def test_discovery_page_requires_login(self, client):
        response = client.get(reverse("devices:discovery"))
        assert response.status_code == 302

    def test_discovery_page_view(self, logged_in_client):
        # Create a mock DiscoveredDevice
        DiscoveredDevice.objects.create(
            ip_address="192.168.1.50",
            hostname="sw-discovered",
            snmp_status=True,
            sys_descr="Cisco IOS Switch",
        )
        response = logged_in_client.get(reverse("devices:discovery"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "192.168.1.50" in content
        assert "sw-discovered" in content

    def test_scan_invalid_method(self, logged_in_client):
        response = logged_in_client.get(reverse("devices:discovery_scan"))
        assert response.status_code == 405

    def test_scan_empty_subnet(self, logged_in_client):
        response = logged_in_client.post(reverse("devices:discovery_scan"), data={"subnet": ""})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Vui lòng nhập dải mạng" in data["message"]

    def test_scan_invalid_subnet_format(self, logged_in_client):
        response = logged_in_client.post(reverse("devices:discovery_scan"), data={"subnet": "invalid-ip-range"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Dải mạng không hợp lệ" in data["message"]

    def test_scan_too_large_subnet(self, logged_in_client):
        # /16 is way larger than /24
        response = logged_in_client.post(reverse("devices:discovery_scan"), data={"subnet": "10.0.0.0/16"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "256" in data["message"]

    def test_scan_huge_subnet_rejected_fast(self, logged_in_client):
        # Regression: code cũ materialize list(network.hosts()) TRƯỚC khi so max_ips
        # → nhập nhầm /8 (16.7 triệu IP) tạo ngần ấy string trước khi kịp từ chối,
        # có thể treo/OOM worker. Fix kiểm tra network.num_addresses (O(1)) trước khi
        # duyệt — request phải trả lời gần như tức thì thay vì phải build xong list khổng lồ.
        start = time.monotonic()
        response = logged_in_client.post(
            reverse("devices:discovery_scan"), data={"subnet": "10.0.0.0/8"}
        )
        elapsed = time.monotonic() - start
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "256" in data["message"]
        assert elapsed < 2.0, f"Bị từ chối nhưng mất {elapsed:.2f}s — nghi ngờ vẫn materialize list lớn"

    def test_scan_ipv6_subnet_rejected_fast(self, logged_in_client):
        # IPv6 /64 = 2**64 địa chỉ — cùng nhóm rủi ro với /8, đảm bảo không đặc biệt
        # hoá riêng IPv4 mà bỏ sót IPv6.
        start = time.monotonic()
        response = logged_in_client.post(
            reverse("devices:discovery_scan"), data={"subnet": "2001:db8::/64"}
        )
        elapsed = time.monotonic() - start
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "256" in data["message"]
        assert elapsed < 2.0, f"Bị từ chối nhưng mất {elapsed:.2f}s — nghi ngờ vẫn materialize list lớn"

    @patch("apps.devices.views._ping_ip")
    @patch("apps.devices.views._probe_snmp")
    def test_scan_success(self, mock_probe, mock_ping, logged_in_client):
        # We will scan a tiny subnet /30 containing 2 host IPs (e.g. 192.168.1.1 and 192.168.1.2)
        mock_ping.side_effect = lambda ip: (ip, ip == "192.168.1.1")  # only .1 is alive
        mock_probe.return_value = (True, "Cisco IOS-XE switch")

        response = logged_in_client.post(
            reverse("devices:discovery_scan"),
            data={"subnet": "192.168.1.0/30", "community": "public"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["count"] == 1  # 1 alive device
        
        device_data = data["devices"][0]
        assert device_data["ip_address"] == "192.168.1.1"
        assert device_data["snmp_status"] is True
        assert device_data["sys_descr"] == "Cisco IOS-XE switch"

        # Verify DiscoveredDevice was saved in the db
        assert DiscoveredDevice.objects.filter(ip_address="192.168.1.1").exists()
