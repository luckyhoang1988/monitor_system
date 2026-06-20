from django import forms
from .models import Device


class DeviceForm(forms.ModelForm):
    uplink_ports = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "GE0/0/1, GE0/0/2"}),
        label="Uplink/Trunk ports"
    )

    class Meta:
        model = Device
        fields = [
            "name", "device_type", "ip_address", "vendor",
            "protocol", "snmp_version", "snmp_community",
            "snmpv3_username", "snmpv3_auth_protocol", "snmpv3_auth_password",
            "snmpv3_priv_protocol", "snmpv3_priv_password",
            "ssh_username", "ssh_password",
            "collect_interval", "uplink_ports", "location", "notes",
            "enabled", "backup_enabled"
        ]
        widgets = {
            "snmp_version":    forms.Select(choices=Device.SNMP_VERSIONS),
            "ssh_password":    forms.PasswordInput(render_value=True),
            "snmp_community":  forms.PasswordInput(render_value=True),
            "snmpv3_auth_password": forms.PasswordInput(render_value=True),
            "snmpv3_priv_password": forms.PasswordInput(render_value=True),
            "uplink_ports":    forms.TextInput(attrs={"placeholder": "GE0/0/1, GE0/0/2"}),
            "notes":           forms.Textarea(attrs={"rows": 3}),
        }

    def clean_uplink_ports(self):
        value = self.cleaned_data.get("uplink_ports")
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        return value or []

    def clean(self):
        cleaned = super().clean()
        protocol = cleaned.get("protocol")
        snmp_version = cleaned.get("snmp_version")
        community = (cleaned.get("snmp_community") or "").strip()
        v3_user = (cleaned.get("snmpv3_username") or "").strip()
        v3_auth_proto = cleaned.get("snmpv3_auth_protocol") or ""
        v3_auth_pass = (cleaned.get("snmpv3_auth_password") or "").strip()
        v3_priv_proto = cleaned.get("snmpv3_priv_protocol") or ""
        v3_priv_pass = (cleaned.get("snmpv3_priv_password") or "").strip()

        if protocol != "snmp":
            return cleaned

        if snmp_version in ("v1", "v2c") and not community:
            self.add_error("snmp_community", "SNMP Community là bắt buộc với SNMP v1/v2c.")

        if snmp_version == "v3":
            if not v3_user:
                self.add_error("snmpv3_username", "SNMPv3 Username là bắt buộc.")
            if v3_auth_proto and not v3_auth_pass:
                self.add_error("snmpv3_auth_password", "Cần nhập SNMPv3 Auth Password.")
            if v3_auth_pass and not v3_auth_proto:
                self.add_error("snmpv3_auth_protocol", "Cần chọn SNMPv3 Auth Protocol.")
            if v3_priv_proto and not v3_priv_pass:
                self.add_error("snmpv3_priv_password", "Cần nhập SNMPv3 Privacy Password.")
            if v3_priv_pass and not v3_priv_proto:
                self.add_error("snmpv3_priv_protocol", "Cần chọn SNMPv3 Privacy Protocol.")
            if v3_priv_proto and not v3_auth_proto:
                self.add_error("snmpv3_auth_protocol", "SNMPv3 Privacy yêu cầu Auth Protocol.")

        return cleaned
