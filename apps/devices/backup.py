"""Backup — Tự động sao lưu cấu hình running-config/current-configuration của Switch/Router."""
import os
import logging
from datetime import datetime, timezone
from django.conf import settings
try:
    from netmiko import ConnectHandler
except ImportError:
    ConnectHandler = None

logger = logging.getLogger(__name__)


def run_ssh_backup(device) -> str:
    """Kết nối SSH qua Netmiko, gửi lệnh show running-config và lấy cấu hình."""
    if ConnectHandler is None:
        raise ImportError("Thư viện 'netmiko' chưa được cài đặt trong môi trường Python.")
    if device.protocol != "ssh":
        raise ValueError("Thiết bị không sử dụng giao thức SSH")
    if not device.ssh_username or not device.ssh_password:
        raise ValueError("Thiết bị thiếu thông tin tài khoản hoặc mật khẩu SSH")

    # Bản đồ chuyển đổi hệ điều hành sang Netmiko device_type
    device_type_map = {
        "cisco_ios": "cisco_ios",
        "cisco_iosxe": "cisco_ios",
        "huawei_vrp": "huawei_vrp",
    }
    netmiko_type = device_type_map.get(device.os_family)
    if not netmiko_type:
        if device.vendor == "cisco":
            netmiko_type = "cisco_ios"
        elif device.vendor == "huawei":
            netmiko_type = "huawei"
        else:
            raise ValueError(f"Hệ điều hành {device.os_family} chưa được hỗ trợ sao lưu cấu hình tự động.")

    # Lệnh lấy cấu hình theo hãng
    backup_cmd = "show running-config" if "cisco" in netmiko_type else "display current-configuration"

    connection_params = {
        "device_type": netmiko_type,
        "host": device.ip_address,
        "username": device.ssh_username,
        "password": device.ssh_password,
        "timeout": 15,
    }

    try:
        with ConnectHandler(**connection_params) as net_connect:
            # Gửi lệnh enable nếu là cisco
            if "cisco" in netmiko_type:
                try:
                    net_connect.enable()
                except Exception:
                    pass  # Bỏ qua nếu không yêu cầu enable password riêng
            
            output = net_connect.send_command(backup_cmd)
            if not output or "Error" in output or "Invalid" in output:
                raise Exception(f"Lỗi khi thực thi lệnh sao lưu: {output[:100]}")
            return output
    except Exception as exc:
        logger.error("Lỗi sao lưu SSH cho %s (%s): %s", device.name, device.ip_address, exc)
        raise exc


def save_backup_file(device, content: str) -> str:
    """Lưu cấu hình vào file backups/ trong thư mục dự án và lưu DB."""
    from .models import DeviceBackup
    backup_dir = os.path.join(settings.BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"device_{device.id}_{timestamp}.txt"
    filepath = os.path.join(backup_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
        
    size_kb = round(os.path.getsize(filepath) / 1024, 2)
    
    # Save to Database
    DeviceBackup.objects.create(
        device=device,
        status="SUCCESS",
        file_path=filepath,
        file_size_kb=size_kb
    )

    return filepath


def get_device_backups(device) -> list[dict]:
    """Lấy danh sách các tệp cấu hình đã sao lưu từ DB."""
    from .models import DeviceBackup
    qs = DeviceBackup.objects.filter(device=device).order_by("-created_at")
    backups = []
    for record in qs:
        filename = os.path.basename(record.file_path) if record.file_path else ""
        backups.append({
            "id": record.id,
            "filename": filename,
            "filepath": record.file_path,
            "size_kb": record.file_size_kb,
            "status": record.status,
            "error_message": record.error_message,
            "created_at": record.created_at.strftime("%H:%M:%S %d/%m/%Y"),
        })
    return backups
