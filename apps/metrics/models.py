from django.db import models
from apps.devices.models import Device, Interface


class InterfaceStats(models.Model):
    """Time-series: trạng thái và lưu lượng interface, ghi mỗi poll cycle."""
    interface  = models.ForeignKey(Interface, on_delete=models.CASCADE, related_name="stats")
    timestamp  = models.DateTimeField(db_index=True)
    status     = models.CharField(max_length=10)   # up | down | testing | unknown
    in_bytes   = models.BigIntegerField(default=0)  # raw SNMP counter (octets)
    out_bytes  = models.BigIntegerField(default=0)
    in_errors  = models.IntegerField(default=0)
    out_errors = models.IntegerField(default=0)
    in_mbps    = models.FloatField(default=0.0)    # tính từ delta / interval
    out_mbps   = models.FloatField(default=0.0)

    class Meta:
        indexes = [models.Index(fields=["interface", "-timestamp"])]
        ordering = ["-timestamp"]
        verbose_name = "Interface Stats"


class SystemHealth(models.Model):
    """Time-series: CPU và RAM của thiết bị, ghi mỗi poll cycle."""
    device      = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="health_stats")
    timestamp   = models.DateTimeField(db_index=True)
    cpu_percent = models.FloatField()
    mem_percent = models.FloatField()
    uptime_secs = models.BigIntegerField(null=True, blank=True)
    extra       = models.JSONField(default=dict, blank=True)  # vendor-specific: session_count, v.v.

    class Meta:
        indexes = [models.Index(fields=["device", "-timestamp"])]
        ordering = ["-timestamp"]
        verbose_name = "System Health"


class VMStats(models.Model):
    """Time-series: metrics VM trên HyperV host."""
    device      = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="vm_stats")
    timestamp   = models.DateTimeField(db_index=True)
    vm_name     = models.CharField(max_length=200)
    state       = models.CharField(max_length=20)  # Running | Stopped | Paused | Saved
    cpu_percent = models.FloatField(default=0.0)
    mem_assigned_mb = models.IntegerField(default=0)
    repl_health     = models.CharField(max_length=50, blank=True)  # Normal | Warning | Critical | NotConfigured | ...

    class Meta:
        indexes = [models.Index(fields=["device", "vm_name", "-timestamp"])]
        ordering = ["-timestamp"]
        verbose_name = "VM Stats"


class WifiApStats(models.Model):
    """Time-series: snapshot trạng thái AP do WLAN controller (Huawei AC) báo cáo.

    Ghi mỗi poll cycle cho từng AP đang được AC quản lý. Online/offline lấy theo
    hwWlanApRunState của AC (đáng tin hơn ping cho AP không có IP/không ping được).
    """
    device       = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="wifi_ap_stats",
                                      verbose_name="WLAN Controller")
    timestamp    = models.DateTimeField(db_index=True)
    ap_name      = models.CharField(max_length=200)
    ap_mac       = models.CharField(max_length=32, blank=True)
    ap_ip        = models.CharField(max_length=64, blank=True)
    ap_group     = models.CharField(max_length=128, blank=True)
    is_online    = models.BooleanField(default=False)
    run_state    = models.CharField(max_length=32, blank=True)
    client_count = models.IntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["device", "ap_name", "-timestamp"])]
        ordering = ["-timestamp"]
        verbose_name = "WiFi AP Stats"


class WifiClientStats(models.Model):
    """Time-series: snapshot client WiFi đang kết nối qua WLAN controller.

    Mỗi poll cycle ghi 1 row/ client (giống pattern VMStats). UI hiển thị snapshot
    mới nhất theo timestamp gần nhất.
    """
    device       = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="wifi_clients",
                                      verbose_name="WLAN Controller")
    timestamp    = models.DateTimeField(db_index=True)
    mac          = models.CharField(max_length=32, db_index=True)
    ip           = models.CharField(max_length=64, blank=True)
    ssid         = models.CharField(max_length=128, blank=True)
    ap_name      = models.CharField(max_length=200, blank=True)
    radio        = models.CharField(max_length=32, blank=True)   # 2.4G | 5G | ...
    rssi         = models.IntegerField(null=True, blank=True)    # dBm
    online_secs  = models.BigIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["device", "-timestamp"]),
            models.Index(fields=["device", "mac", "-timestamp"]),
        ]
        ordering = ["-timestamp"]
        verbose_name = "WiFi Client Stats"


# ---------------------------------------------------------------------------
# Aggregated models — rollup data theo giờ/ngày cho query nhanh hơn
# ---------------------------------------------------------------------------

class SystemHealthHourly(models.Model):
    """Aggregated hourly: avg/max CPU và RAM theo giờ."""
    device       = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="health_hourly")
    hour         = models.DateTimeField(verbose_name="Đầu giờ (truncated)")
    cpu_avg      = models.FloatField()
    cpu_max      = models.FloatField()
    mem_avg      = models.FloatField()
    mem_max      = models.FloatField()
    sample_count = models.IntegerField(default=0, verbose_name="Số mẫu raw")

    class Meta:
        unique_together = ("device", "hour")
        indexes = [models.Index(fields=["device", "-hour"])]
        ordering = ["-hour"]
        verbose_name = "System Health (Hourly)"


class SystemHealthDaily(models.Model):
    """Aggregated daily: avg/max CPU và RAM theo ngày."""
    device       = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="health_daily")
    day          = models.DateField(verbose_name="Ngày")
    cpu_avg      = models.FloatField()
    cpu_max      = models.FloatField()
    mem_avg      = models.FloatField()
    mem_max      = models.FloatField()
    sample_count = models.IntegerField(default=0, verbose_name="Số mẫu hourly")

    class Meta:
        unique_together = ("device", "day")
        indexes = [models.Index(fields=["device", "-day"])]
        ordering = ["-day"]
        verbose_name = "System Health (Daily)"


class InterfaceStatsHourly(models.Model):
    """Aggregated hourly: avg/max traffic (Mbps) theo giờ."""
    interface    = models.ForeignKey(Interface, on_delete=models.CASCADE, related_name="stats_hourly")
    hour         = models.DateTimeField(verbose_name="Đầu giờ (truncated)")
    in_mbps_avg  = models.FloatField(default=0.0)
    in_mbps_max  = models.FloatField(default=0.0)
    out_mbps_avg = models.FloatField(default=0.0)
    out_mbps_max = models.FloatField(default=0.0)
    in_errors    = models.IntegerField(default=0, verbose_name="Tổng in errors")
    out_errors   = models.IntegerField(default=0, verbose_name="Tổng out errors")
    sample_count = models.IntegerField(default=0)

    class Meta:
        unique_together = ("interface", "hour")
        indexes = [models.Index(fields=["interface", "-hour"])]
        ordering = ["-hour"]
        verbose_name = "Interface Stats (Hourly)"


class InterfaceStatsDaily(models.Model):
    """Aggregated daily: avg/max traffic (Mbps) theo ngày."""
    interface    = models.ForeignKey(Interface, on_delete=models.CASCADE, related_name="stats_daily")
    day          = models.DateField(verbose_name="Ngày")
    in_mbps_avg  = models.FloatField(default=0.0)
    in_mbps_max  = models.FloatField(default=0.0)
    out_mbps_avg = models.FloatField(default=0.0)
    out_mbps_max = models.FloatField(default=0.0)
    in_errors    = models.IntegerField(default=0)
    out_errors   = models.IntegerField(default=0)
    sample_count = models.IntegerField(default=0)

    class Meta:
        unique_together = ("interface", "day")
        indexes = [models.Index(fields=["interface", "-day"])]
        ordering = ["-day"]
        verbose_name = "Interface Stats (Daily)"

