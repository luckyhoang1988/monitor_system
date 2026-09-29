from django import forms
from .models import AlertRule, AlertConfig, CHANNEL_CHOICES

# Đọc THẲNG từ AlertRule.METRIC_LABELS (nguồn sự thật duy nhất) — KHÔNG tự liệt kê lại ở đây.
# Trước đây list này tự chép tay và lệch với models.py (thiếu 11 metric host-perf HyperV + 4
# metric iLO) → dropdown sửa rule cho các metric đó không có option khớp, HTML <select> tự chọn
# option đầu tiên, bấm Lưu âm thầm đổi sai metric của rule (xem models.py AlertRule.METRIC_LABELS).
METRIC_CHOICES = list(AlertRule.METRIC_LABELS.items())

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


class AlertConfigForm(forms.ModelForm):
    class Meta:
        model  = AlertConfig
        fields = ["telegram_enabled", "telegram_chat_id"]
        widgets = {
            "telegram_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "telegram_chat_id": forms.TextInput(attrs={"class": "form-control",
                                                       "placeholder": "vd -100123456789"}),
        }

    def clean_telegram_chat_id(self):
        return (self.cleaned_data.get("telegram_chat_id") or "").strip()
