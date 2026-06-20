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
            "vendor", "os_family", "snmp_version", 
            "enabled", "created_at",
            "interfaces"
        ]
        # Không expose snmp_community hay credentials
