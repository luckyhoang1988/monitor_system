"""Serializers cho models Device."""
from rest_framework import serializers
from .models import Device, Interface

class InterfaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Interface
        fields = ["id", "if_index", "name", "description", "is_uplink"]

class DeviceSerializer(serializers.ModelSerializer):
    interfaces = InterfaceSerializer(many=True, read_only=True)

    class Meta:
        model = Device
        fields = [
            "id", "name", "ip_address", "device_type",
            "vendor", "os_family", "protocol", "snmp_version",
            "enabled", "created_at",
            "interfaces"
        ]
        # Không expose snmp_community hay credentials

    def validate_snmp_version(self, value: str) -> str:
        """Cùng quy tắc với `DeviceForm`: không lưu thiết bị SNMPv3 khi backend không làm được v3."""
        if value == "v3":
            from apps.collectors.snmp_client import SNMP_V3_UNSUPPORTED_MESSAGE, snmp_v3_supported
            if not snmp_v3_supported():
                raise serializers.ValidationError(SNMP_V3_UNSUPPORTED_MESSAGE)
        return value
