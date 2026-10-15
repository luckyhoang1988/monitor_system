from django.core.exceptions import ValidationError
from django.db import models
from apps.devices.models import Device, Interface

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

    # Nguồn sự thật DUY NHẤT cho tên hiển thị của mọi metric — forms.py (METRIC_CHOICES cho
    # dropdown UI) đọc THẲNG từ dict này thay vì tự chép lại. Lý do: 2 bản sao (models.py +
    # forms.py) từng lệch nhau (forms.py thiếu 11 metric host-perf HyperV + 4 metric iLO) khiến
    # dropdown sửa rule không có option khớp giá trị đang lưu → HTML <select> tự chọn option ĐẦU
    # TIÊN trong list, bấm Lưu (kể cả chỉ để tắt/bật rule) âm thầm đổi metric của rule sang giá trị
    # sai (vd rule RAID Controller Critical bị đổi thành cpu_percent) mà không có dấu hiệu lỗi nào
    # (phát hiện khi review lại alert iLO 2026-09-29). Thêm metric mới cho engine → thêm đúng 1 chỗ
    # ở đây là đủ, không cần sửa forms.py.
    METRIC_LABELS = {
        "cpu_percent": "CPU (%)",
        "mem_percent": "RAM (%)",
        "if_status": "Uplink status (0=DOWN, 1=UP)",
        "uplink_in_mbps_max": "Uplink IN traffic max (Mbps)",
        "uplink_out_mbps_max": "Uplink OUT traffic max (Mbps)",
        "vm_count_running": "Số VM đang chạy",
        "vm_repl_unhealthy": "Số VM replication lỗi",
        "device_online": "Trạng thái online (0=OFFLINE, 1=ONLINE)",
        "wifi_client_count": "Số client WiFi (WLAN controller)",
        "wifi_ap_offline": "Số AP offline (WLAN controller)",
        "cpu_hv_percent": "CPU Hypervisor (%)",
        "mem_available_mb": "RAM available (MB)",
        "disk_read_iops": "Disk Read IOPS",
        "disk_write_iops": "Disk Write IOPS",
        "disk_read_latency_ms": "Disk Read Latency (ms)",
        "disk_write_latency_ms": "Disk Write Latency (ms)",
        "net_mbps_total": "Network Throughput (Mbps)",
        "disk_read_throughput_mbps": "Disk Read Throughput (MB/s)",
        "disk_write_throughput_mbps": "Disk Write Throughput (MB/s)",
        "disk_queue_length": "Disk Queue Length",
        "avg_io_size_kb": "Avg I/O Size (KB/IO)",
        "raid_controller_health": "RAID Controller Health (iLO)",
        "raid_logical_drive_health": "RAID Logical Drive Health (iLO)",
        "raid_missing_disk_count": "Số đĩa mất (iLO)",
        "raid_enclosure_mismatch": "Số enclosure bất thường (iLO)",
        "ilo_battery_health": "Smart Storage Battery Health (iLO)",
        "ilo_processor_health": "Processor Health (iLO)",
        "ilo_memory_health": "Memory Health (iLO)",
        "ilo_fan_health": "Fan Health (iLO)",
        "ilo_temperature_health": "Temperature Health (iLO)",
        "ilo_power_supply_health": "Power Supply Health (iLO)",
        "ilo_power_redundancy": "Power Redundancy (iLO)",
        "ilo_bios_hardware_health": "BIOS/Hardware Health (iLO)",
        "ilo_network_health": "Network Health (iLO)",
        "ilo_fan_redundancy": "Fan Redundancy (iLO)",
    }

    # Scale health code dùng chung cho mọi metric dạng OK/Warning/Critical (RAID controller/logical
    # drive + 6 metric iLO mở rộng 2026-09-29: Battery/Processor/Memory/Fan/Temperature/PowerSupply
    # — verify runtime iLO4 P440ar 2026-09-28/29, xem apps/metrics/models.py HardwareHealth). Đổi
    # tên từ RAID_HEALTH_NAMES (tên cũ chỉ đúng khi còn 2 metric RAID, nay dùng chung 8 metric nên
    # giữ tên cũ sẽ gây hiểu lầm cho người đọc sau). Dùng chung cho threshold_label ở đây và
    # _fmt_metric trong apps/alerts/engine.py — tránh lệch nếu ai chỉ sửa 1 chỗ.
    HEALTH_CODE_NAMES = {0: "OK", 1: "Warning", 2: "Critical"}

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

    # Hiện cho người dùng khi form/admin từ chối đổi phạm vi của rule đang bật (xem `clean`).
    SCOPE_LOCKED_MESSAGE = (
        "Rule đang bật nên chưa đổi được loại thiết bị: alert đang active của loại cũ sẽ không còn rule nào "
        "đánh giá và bị kẹt vĩnh viễn. Hãy tắt rule trước (bỏ tick “Kích hoạt” rồi lưu, hoặc dùng nút "
        "Tắt rule), sau đó đổi loại thiết bị và bật lại."
    )

    def __str__(self) -> str:
        return f"{self.name} ({self.severity})"

    def clean(self) -> None:
        """Rule đang BẬT không được đổi `device_type` (sau review thứ ba của hạng mục 1.4).

        Engine chỉ đánh giá rule `enabled=True` và chỉ cho thiết bị thuộc `device_type__in=[loại, "all"]`;
        `_resolve_alert` chỉ được gọi từ đó. Đổi "Tất cả" → "HyperV" trên rule đang bật làm alert đang active
        của Switch không còn rule nào đánh giá → `is_active=True` mãi mãi. Bắt buộc đi qua đường tắt rule
        (đóng alert active, ghi lý do) → đổi phạm vi → bật lại.

        * So với giá trị ĐÃ LƯU trong DB (form đã ghi giá trị mới lên instance trước khi gọi clean).
        * Cho phép: rule đã tắt; hoặc cùng lần lưu đó tắt rule (đường đóng alert sẽ lo alert của phạm vi cũ).
        * Đặt ở model để form UI và Django admin (cùng ModelForm) đi chung 1 quy tắc. KHÔNG chặn được thao
          tác ghi thẳng (`save()` từ code/CLI như `seed_alert_rules --overwrite`) — đó là hành động có chủ đích
          của người vận hành.
        """
        super().clean()
        if self.pk is None or not self.enabled:
            return
        stored = AlertRule.objects.filter(pk=self.pk).values("enabled", "device_type").first()
        if stored and stored["enabled"] and stored["device_type"] != self.device_type:
            raise ValidationError({"device_type": self.SCOPE_LOCKED_MESSAGE})

    @property
    def metric_label(self) -> str:
        """Human-friendly label for metric key (for UI)."""
        return self.METRIC_LABELS.get(self.metric, self.metric)

    @property
    def threshold_label(self) -> str:
        """Formatted threshold for UI display."""
        m = self.metric
        t = float(self.threshold)
        if m in ("cpu_percent", "mem_percent"):
            return f"{t:.1f}%"
        if m in ("uplink_in_mbps_max", "uplink_out_mbps_max"):
            return f"{t:.3f} Mbps"
        if m in ("vm_count_running", "vm_repl_unhealthy",
                 "wifi_client_count", "wifi_ap_offline"):
            return f"{t:.0f}"
        if m == "if_status":
            return "DOWN" if t == 0 else "UP"
        if m == "device_online":
            return "OFFLINE" if t == 0 else "ONLINE"
        if m == "cpu_hv_percent":
            return f"{t:.1f}%"
        if m == "mem_available_mb":
            return f"{t:.0f} MB"
        if m in ("disk_read_iops", "disk_write_iops"):
            return f"{t:.0f} IOPS"
        if m in ("disk_read_latency_ms", "disk_write_latency_ms"):
            return f"{t:.1f} ms"
        if m == "net_mbps_total":
            return f"{t:.1f} Mbps"
        if m in ("disk_read_throughput_mbps", "disk_write_throughput_mbps"):
            return f"{t:.1f} MB/s"
        if m == "disk_queue_length":
            return f"{t:.2f}"
        if m == "avg_io_size_kb":
            return f"{t:.1f} KB"
        if m in ("raid_controller_health", "raid_logical_drive_health",
                 "ilo_battery_health", "ilo_processor_health", "ilo_memory_health",
                 "ilo_fan_health", "ilo_temperature_health", "ilo_power_supply_health",
                 "ilo_bios_hardware_health", "ilo_network_health"):
            return self.HEALTH_CODE_NAMES.get(int(t), f"code={t:.0f}")
        if m in ("raid_missing_disk_count", "raid_enclosure_mismatch"):
            return f"{t:.0f}"
        if m in ("ilo_power_redundancy", "ilo_fan_redundancy"):
            return "OK" if t == 1 else "DEGRADED"
        return f"{t:.2f}"


class Alert(models.Model):
    # Lý do alert bị ĐÓNG mà KHÔNG phải vì sự cố đã hồi phục (xem `closed_reason`).
    CLOSED_RULE_DISABLED = "rule_disabled"
    # Alert `if_status` bị đóng vì cổng gây ra nó không còn nằm trong phạm vi giám sát (xem `MonitoredUplink`).
    CLOSED_SCOPE_CHANGED = "scope_changed"

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
    # Vì sao alert đã đóng mà KHÔNG phải do hồi phục. Rỗng = còn active, hoặc đã hồi phục thật (đường
    # `_resolve_alert`). `is_active=False` một mình KHÔNG có nghĩa "đã hồi phục": mọi cơ chế suy ra RECOVERED
    # từ trạng thái đóng (late-recovery trong engine) phải bỏ qua alert có giá trị ở đây — chưa có dữ liệu nào
    # cho thấy sự cố đã hết (hạng mục 1.4, review sau 30b09aa). `db_default` để mã cũ (khi rollback) vẫn
    # INSERT được dù không biết cột này.
    closed_reason   = models.CharField(max_length=20, blank=True, default="", db_default="")
    # Chỉ dùng cho rule `if_status`: pk các cổng (Interface) đã được thấy KHÔNG up trong lúc alert này active.
    # `None` = alert nổ theo ngữ nghĩa CŨ ("mọi cổng trunk/hybrid", trước khi có `MonitoredUplink`) hoặc không
    # phải if_status → chưa biết cổng nào gây ra nó. Engine dùng tập này để phân biệt "cổng đã hồi phục"
    # (RECOVERED) với "cổng gây ra alert đã bị loại khỏi phạm vi giám sát" (đóng, không RECOVERED).
    down_interface_ids = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = "Alert"
        ordering = ["-triggered_at"]

    def __str__(self) -> str:
        return f"{self.severity}: {self.device.name} — {self.rule.name}"

    @property
    def metric_value_label(self) -> str:
        """Use the same metric semantics as rule thresholds in the web UI."""
        rule = AlertRule(metric=self.rule.metric, threshold=self.metric_value)
        return rule.threshold_label


class MonitoredUplink(models.Model):
    """Cổng uplink của 1 thiết bị mà rule `if_status` ("Switch Uplink Down") phải theo dõi trạng thái.

    Phạm vi giám sát được XÁC LẬP từ 2 nguồn rồi GIỮ LẠI (engine ghi, xem `_sync_uplink_scope`):
      * topology — cổng local của 1 `TopologyLink(link_kind="switch")` đang CÒN MỚI (chưa stale) lúc engine
        nhìn thấy nó. Dòng được giữ khi link sau đó down hoặc chuyển stale: nếu chỉ lấy link "chưa stale" thì
        uplink đứt sẽ biến mất khỏi phạm vi rồi bị coi là đã hồi phục. Link stale mà engine CHƯA TỪNG thấy
        còn mới thì KHÔNG được đưa vào (không tự nhập link cũ vào danh sách).
      * manual — tên cổng trong `Device.uplink_ports`. Dòng này bị bỏ khi tên bị xoá khỏi `uplink_ports`
        (người vận hành chủ động đổi cấu hình), trừ khi cổng cũng đã có link topology.
    Xoá 1 dòng (Django admin) = ngừng giám sát cổng đó — cách duy nhất để bỏ 1 cổng topology (vd cáp đã gỡ hẳn).
    KHÔNG liên quan `Interface.port_mode`/`is_uplink`: cổng trunk không tự trở thành đối tượng cảnh báo.
    """
    SOURCE_TOPOLOGY = "topology"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [(SOURCE_TOPOLOGY, "Topology switch↔switch"), (SOURCE_MANUAL, "uplink_ports thủ công")]

    device    = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="monitored_uplinks")
    interface = models.OneToOneField(Interface, on_delete=models.CASCADE, related_name="monitored_uplink")
    source    = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_TOPOLOGY)
    pinned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Cổng uplink giám sát"
        verbose_name_plural = "Cổng uplink giám sát"
        ordering = ["device__name", "interface__name"]

    def __str__(self) -> str:
        return f"{self.device.name}: {self.interface.name} ({self.source})"


class AlertConfig(models.Model):
    """Singleton (pk=1): cấu hình kênh thông báo chỉnh qua UI."""
    telegram_enabled = models.BooleanField(default=True, verbose_name="Bật gửi Telegram")
    telegram_chat_id = models.CharField(
        max_length=64, blank=True, default="",
        verbose_name="Telegram Chat ID",
        help_text="Chat/group ID nhận cảnh báo (vd -100123456789). Để trống = dùng giá trị trong .env.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Cấu hình cảnh báo"

    def __str__(self) -> str:
        return "Cấu hình cảnh báo"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ChannelCooldown(models.Model):
    """Thời gian chờ (flood control) dùng chung cho 1 kênh gửi — theo bot/chat, KHÔNG theo từng tiến trình.

    Telegram trả 429 kèm `retry_after`; mọi lần gửi trong khoảng đó (từ tác vụ poll khác, từ vòng sweep
    kế tiếp) đều bị từ chối và đốt lượt thử vô ích. Trước đây thời hạn chỉ sống trong biến cục bộ của
    1 vòng quét (review 2026-10-15). Xem apps/alerts/cooldown.py. Một dòng cho mỗi khoá, chỉ tăng `until`.
    """
    key = models.CharField(max_length=100, unique=True)
    until = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Channel Cooldown"

    def __str__(self) -> str:
        return f"{self.key} until {self.until:%Y-%m-%d %H:%M:%S}"


class AlertNotification(models.Model):
    KIND_CHOICES = [("fire", "Fire"), ("recovery", "Recovery")]

    alert    = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="notifications")
    channel  = models.CharField(max_length=20)   # email | telegram | slack | teams
    # "fire" (alert vừa nổ) hay "recovery" (đã hồi phục) — cần để phân biệt khi retry 1 row
    # "pending" bị kẹt (xem apps/alerts/engine.py _dispatch_notifications): is_active của
    # Alert có thể đã đổi lần nữa giữa lúc ghi pending và lúc sweep retry, nên KHÔNG thể suy
    # ra loại thông báo cần gửi từ is_active tại thời điểm retry — phải lưu tường minh lúc tạo.
    kind     = models.CharField(max_length=10, choices=KIND_CHOICES, default="fire")
    sent_at  = models.DateTimeField(auto_now_add=True)
    # status: pending (đã ghi ý định, chưa gửi) | processing (đang có 1 tiến trình claim để
    # gửi — xem apps/alerts/engine.py retry_pending_alert_notifications/_dispatch_notifications)
    # | sent | failed.
    status    = models.CharField(max_length=20)
    error     = models.TextField(blank=True)
    # Cập nhật MỖI LẦN đổi status (auto_now — KHÁC sent_at auto_now_add chỉ set lúc tạo).
    # Dùng để: (1) claim nguyên tử pending/processing→processing có điều kiện
    # (.filter(status=...).update(status="processing", updated_at=now())), (2) phát hiện row
    # "processing" bị bỏ rơi (worker chết giữa chừng, không kịp chuyển sent/failed) — quá lâu
    # không đổi status coi như kẹt, cho retry lại.
    updated_at = models.DateTimeField(auto_now=True)
    # Token ngẫu nhiên ghi lại MỖI LẦN claim (pending/processing→processing). Khi finalize
    # (sent/failed) phải match ĐÚNG token này mới được ghi — chặn 1 tiến trình bị coi "kẹt" và
    # bị reclaim (xem stale_processing_secs) nhưng thực ra vẫn đang gửi hợp lệ (SMTP treo lâu)
    # ghi đè kết quả của tiến trình đã reclaim sau nó. Xem apps/alerts/engine.py
    # _dispatch_notifications/retry_pending_alert_notifications.
    claim_token = models.CharField(max_length=36, blank=True, default="")
    # Số lần gửi ĐÃ BẮT ĐẦU (tăng nguyên tử lúc claim, kể cả lần bị SIGKILL giữa chừng) — trần
    # ALERT_NOTIFY_MAX_ATTEMPTS chặn thử lại vô hạn (review 2026-10-15, hạng mục 1.2).
    attempts = models.PositiveSmallIntegerField(default=0)
    # Lịch gửi lại khi lỗi TẠM THỜI (429 theo retry_after của Telegram, 5xx, mất kết nối): row quay về
    # status="pending" kèm mốc này; sweep chỉ nhặt khi đã đến hạn. NULL = chưa từng lên lịch (gửi lần đầu,
    # hoặc row kẹt chờ sweep nhặt sau grace_secs). Lưu trong DB nên không mất khi worker restart.
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Alert Notification"
        ordering = ["-sent_at"]
        indexes = [
            # Query định kỳ (mỗi 120s, retry_pending_alert_notifications) lọc theo status +
            # mốc thời gian — không có index này là full-table scan lặp lại khi lịch sử
            # notification lớn dần theo thời gian.
            models.Index(fields=["status", "sent_at"]),
            models.Index(fields=["status", "updated_at"]),
        ]
