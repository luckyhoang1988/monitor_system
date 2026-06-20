import logging
from celery import shared_task
from apps.devices.models import Device, DeviceBackup
from apps.devices.backup import run_ssh_backup, save_backup_file

logger = logging.getLogger(__name__)

@shared_task
def auto_backup_all_devices():
    """Chạy ngầm vào ban đêm để tự động sao lưu tất cả thiết bị có bật backup."""
    devices = Device.objects.filter(backup_enabled=True, protocol="ssh")
    logger.info(f"Bắt đầu tự động sao lưu cấu hình cho {devices.count()} thiết bị.")
    
    success_count = 0
    failed_count = 0
    
    for device in devices:
        try:
            logger.info(f"Đang sao lưu {device.name}...")
            content = run_ssh_backup(device)
            save_backup_file(device, content)
            success_count += 1
        except Exception as exc:
            logger.error(f"Sao lưu thất bại cho {device.name}: {exc}")
            DeviceBackup.objects.create(
                device=device,
                status="FAILED",
                error_message=str(exc)
            )
            failed_count += 1
            
    logger.info(f"Hoàn tất sao lưu: {success_count} thành công, {failed_count} thất bại.")
    return {"success": success_count, "failed": failed_count}
