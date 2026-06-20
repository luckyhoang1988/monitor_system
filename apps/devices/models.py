from django.db import models

from .fields import EncryptedCharField


class Device(models.Model):
    DEVICE_TYPES = [
        ("switch",   "Switch"),
        ("router",   "Router"),
        ("firewall", "Firewall"),
        ("hyperv",   "HyperV Host"),
    ]
    VENDORS = [
        ("cisco",    "Cisco"),
        ("huawei",   "Huawei"),
        ("hp",       "HP/Aruba"),
        ("mikrotik", "MikroTik"),
        ("fortinet", "Fortinet"),
        ("microsoft","Microsoft"),
    ]
    PROTOCOLS    = [("snmp", "SNMP"), ("ssh", "SSH"), ("winrm", "WinRM"), ("ping", "Ping/ICMP")]
    SNMP_VERSIONS = [("v1", "v1"), ("v2c", "v2c"), ("v3", "v3")]
    SNMPV3_AUTH_PROTOCOLS = [("", "None"), ("md5", "MD5"), ("sha", "SHA")]
    SNMPV3_PRIV_PROTOCOLS = [("", "None"), ("des", "DES"), ("aes", "AES")]

    name             = models.CharField(max_length=100, unique=True, verbose_name="Tên thiết bị")
    device_type      = models.CharField(max_length=20, choices=DEVICE_TYPES, verbose_name="Loại")
    ip_address       = models.GenericIPAddressField(verbose_name="Địa chỉ IP")
    vendor           = models.CharField(max_length=50, choices=VENDORS, verbose_name="Hãng")
    os_family        = models.CharField(max_length=50, blank=True, verbose_name="OS Family")
    protocol         = models.CharField(max_length=10, choices=PROTOCOLS, verbose_name="Giao thức")
    snmp_version     = models.CharField(max_length=5, choices=SNMP_VERSIONS, default="v2c")
    snmp_community   = EncryptedCharField(blank=True, verbose_name="SNMP Community")
    snmpv3_username  = models.CharField(max_length=100, blank=True, verbose_name="SNMPv3 Username")
    snmpv3_auth_protocol = models.CharField(
        max_length=20,
        choices=SNMPV3_AUTH_PROTOCOLS,
        blank=True,
        default="",
        verbose_name="SNMPv3 Auth Protocol",
    )
    snmpv3_auth_password = EncryptedCharField(blank=True, verbose_name="SNMPv3 Auth Password")
    snmpv3_priv_protocol = models.CharField(
        max_length=20,
        choices=SNMPV3_PRIV_PROTOCOLS,
        blank=True,
        default="",
        verbose_name="SNMPv3 Privacy Protocol",
    )
    snmpv3_priv_password = EncryptedCharField(blank=True, verbose_name="SNMPv3 Privacy Password")
    ssh_username     = models.CharField(max_length=100, blank=True, verbose_name="SSH Username")
    ssh_password     = EncryptedCharField(blank=True, verbose_name="SSH Password")
    collect_interval = models.IntegerField(default=300, verbose_name="Chu kỳ quét (giây)")
    uplink_ports     = models.JSONField(default=list, verbose_name="Uplink/Trunk ports")
    location         = models.CharField(max_length=200, blank=True, verbose_name="Vị trí")
    notes            = models.TextField(blank=True, verbose_name="Ghi chú")
    enabled          = models.BooleanField(default=True, verbose_name="Kích hoạt")
    backup_enabled   = models.BooleanField(default=False, verbose_name="Sao lưu cấu hình")
    last_seen        = models.DateTimeField(null=True, blank=True, verbose_name="Lần poll cuối")
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Thiết bị"
        verbose_name_plural = "Danh sách thiết bị"
        ordering = ["device_type", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.ip_address})"

    @property
    def is_online(self) -> bool:
        from django.utils import timezone
        from django.conf import settings
        from datetime import timedelta
        if not self.last_seen:
            return False
        # Tránh false-offline khi collect_interval cấu hình nhỏ hơn chu kỳ poll thực tế.
        min_grace = int(getattr(settings, "DEVICE_ONLINE_MIN_GRACE_SECS", 300))
        grace_secs = max(int(self.collect_interval or 300) * 3, min_grace)
        threshold = timezone.now() - timedelta(seconds=grace_secs)
        return self.last_seen >= threshold


class Interface(models.Model):
    device      = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="interfaces")
    if_index    = models.IntegerField(verbose_name="Interface Index")
    name        = models.CharField(max_length=100, verbose_name="Tên port")
    description = models.CharField(max_length=200, blank=True, verbose_name="Mô tả")
    is_uplink   = models.BooleanField(default=False, verbose_name="Là uplink/trunk")

    class Meta:
        unique_together = ("device", "if_index")
        verbose_name = "Interface"
        ordering = ["if_index"]

    def __str__(self) -> str:
        return f"{self.device.name} - {self.name}"


class DiscoveredDevice(models.Model):
    ip_address    = models.GenericIPAddressField(unique=True, verbose_name="Địa chỉ IP")
    hostname      = models.CharField(max_length=200, blank=True, verbose_name="Hostname")
    snmp_status   = models.BooleanField(default=False, verbose_name="Hỗ trợ SNMP")
    sys_descr     = models.TextField(blank=True, verbose_name="Mô tả SNMP")
    discovered_at = models.DateTimeField(auto_now=True, verbose_name="Thời gian phát hiện")
    is_imported   = models.BooleanField(default=False, verbose_name="Đã import")

    class Meta:
        verbose_name = "Thiết bị phát hiện"
        verbose_name_plural = "Thiết bị phát hiện tự động"
        ordering = ["-discovered_at"]

    def __str__(self) -> str:
        return f"{self.ip_address} ({self.hostname or 'N/A'})"


class DeviceBackup(models.Model):
    STATUS_CHOICES = [
        ("SUCCESS", "Thành công"),
        ("FAILED", "Thất bại"),
    ]
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="backups", verbose_name="Thiết bị")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, verbose_name="Trạng thái")
    file_path = models.CharField(max_length=500, blank=True, verbose_name="Đường dẫn file")
    file_size_kb = models.FloatField(default=0, verbose_name="Dung lượng (KB)")
    error_message = models.TextField(blank=True, verbose_name="Lỗi (Nếu có)")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Thời gian chạy")

    class Meta:
        verbose_name = "Lịch sử sao lưu"
        verbose_name_plural = "Lịch sử sao lưu cấu hình"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Backup {self.device.name} - {self.status} - {self.created_at.strftime('%Y-%m-%d %H:%M:%S')}"

