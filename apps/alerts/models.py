from django.db import models
from apps.devices.models import Device

CHANNEL_CHOICES = [
    ("email",    "Email"),
    ("telegram", "Telegram"),
    ("slack",    "Slack"),
    ("teams",    "MS Teams"),
]


class AlertRule(models.Model):
    CONDITION_CHOICES = [("gt", ">"), ("lt", "<"), ("eq", "="), ("ne", "≠"), ("gte", "≥"), ("lte", "≤")]
    SEVERITY_CHOICES  = [("WARNING", "Warning"), ("CRITICAL", "Critical")]
    DEVICE_TYPE_CHOICES = [
        ("all",             "Tất cả"),
        ("switch",          "Switch"),
        ("router",          "Router"),
        ("firewall",        "Firewall"),
        ("hyperv",          "HyperV Host"),
        ("wlan_controller", "WLAN Controller (AC)"),
        ("ap",              "Access Point"),
    ]

    name         = models.CharField(max_length=100, unique=True, verbose_name="Tên rule")
    device_type  = models.CharField(max_length=20, default="all",
                                    choices=DEVICE_TYPE_CHOICES, verbose_name="Loại thiết bị")
    metric       = models.CharField(max_length=100, verbose_name="Metric")
    condition    = models.CharField(max_length=5, choices=CONDITION_CHOICES, verbose_name="Điều kiện")
    threshold    = models.FloatField(verbose_name="Ngưỡng")
    severity     = models.CharField(max_length=20, choices=SEVERITY_CHOICES, verbose_name="Mức độ")
    duration_min = models.IntegerField(default=0, verbose_name="Kéo dài (phút)")
    channels     = models.JSONField(default=list, verbose_name="Kênh thông báo")
    enabled      = models.BooleanField(default=True, verbose_name="Kích hoạt")

    class Meta:
        verbose_name = "Alert Rule"
        ordering = ["severity", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.severity})"

    @property
    def metric_label(self) -> str:
        """Human-friendly label for metric key (for UI)."""
        labels = {
            "cpu_percent": "CPU (%)",
            "mem_percent": "RAM (%)",
            "if_status": "Uplink status (0=DOWN, 1=UP)",
            "uplink_in_mbps_max": "Uplink IN traffic max (Mbps)",
            "uplink_out_mbps_max": "Uplink OUT traffic max (Mbps)",
            "fw_session_count": "Firewall sessions (Fortinet)",
            "vm_count_running": "Số VM đang chạy",
            "vm_repl_unhealthy": "Số VM replication lỗi",
            "device_online": "Trạng thái online (0=OFFLINE, 1=ONLINE)",
            "wifi_client_count": "Số client WiFi (WLAN controller)",
        }
        return labels.get(self.metric, self.metric)

    @property
    def threshold_label(self) -> str:
        """Formatted threshold for UI display."""
        m = self.metric
        t = float(self.threshold)
        if m in ("cpu_percent", "mem_percent"):
            return f"{t:.1f}%"
        if m in ("uplink_in_mbps_max", "uplink_out_mbps_max"):
            return f"{t:.3f} Mbps"
        if m in ("vm_count_running", "vm_repl_unhealthy", "fw_session_count", "wifi_client_count"):
            return f"{t:.0f}"
        if m == "if_status":
            return "DOWN" if t == 0 else "UP"
        if m == "device_online":
            return "OFFLINE" if t == 0 else "ONLINE"
        return f"{t:.2f}"


class Alert(models.Model):
    device          = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="alerts")
    rule            = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="alerts")
    severity        = models.CharField(max_length=20)
    message         = models.TextField()
    metric_value    = models.FloatField()
    triggered_at    = models.DateTimeField(auto_now_add=True)
    resolved_at     = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.CharField(max_length=100, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    is_active       = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = "Alert"
        ordering = ["-triggered_at"]

    def __str__(self) -> str:
        return f"{self.severity}: {self.device.name} — {self.rule.name}"


class AlertNotification(models.Model):
    alert    = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="notifications")
    channel  = models.CharField(max_length=20)   # email | telegram
    sent_at  = models.DateTimeField(auto_now_add=True)
    status   = models.CharField(max_length=20)   # sent | failed
    error    = models.TextField(blank=True)

    class Meta:
        verbose_name = "Alert Notification"
        ordering = ["-sent_at"]
