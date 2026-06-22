from django import forms
from .models import AlertRule, CHANNEL_CHOICES

METRIC_CHOICES = [
    ("cpu_percent",       "CPU (%)"),
    ("mem_percent",       "RAM (%)"),
    ("if_status",         "Uplink status (0=DOWN, 1=UP)"),
    ("uplink_in_mbps_max",  "Uplink IN traffic max (Mbps)"),
    ("uplink_out_mbps_max", "Uplink OUT traffic max (Mbps)"),
    ("fw_session_count",  "Firewall sessions (Fortinet)"),
    ("vm_count_running",  "Số VM đang chạy"),
    ("vm_repl_unhealthy", "Số VM replication lỗi"),
    ("device_online",     "Trạng thái online (0=OFFLINE, 1=ONLINE)"),
    ("wifi_client_count", "Số client WiFi (WLAN controller)"),
]

DEVICE_TYPE_CHOICES = [
    ("all",             "Tất cả"),
    ("switch",          "Switch"),
    ("router",          "Router"),
    ("firewall",        "Firewall"),
    ("hyperv",          "HyperV"),
    ("wlan_controller", "WLAN Controller (AC)"),
    ("ap",              "Access Point"),
]


class AlertRuleForm(forms.ModelForm):
    channels = forms.MultipleChoiceField(
        choices=CHANNEL_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Kênh thông báo",
    )

    class Meta:
        model  = AlertRule
        fields = ["name", "device_type", "metric", "condition",
                  "threshold", "severity", "duration_min", "channels", "enabled"]
        widgets = {
            "name":        forms.TextInput(attrs={"class": "form-control"}),
            "device_type": forms.Select(attrs={"class": "form-select"},
                                        choices=DEVICE_TYPE_CHOICES),
            "metric":      forms.Select(attrs={"class": "form-select"},
                                        choices=METRIC_CHOICES),
            "condition":   forms.Select(attrs={"class": "form-select"}),
            "threshold":   forms.NumberInput(attrs={"class": "form-control", "step": "0.1"}),
            "severity":    forms.Select(attrs={"class": "form-select"}),
            "duration_min": forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
            "enabled":     forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and isinstance(self.instance.channels, list):
            self.initial["channels"] = self.instance.channels

    def clean_channels(self):
        return list(self.cleaned_data.get("channels", []))
