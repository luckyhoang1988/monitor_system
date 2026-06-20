from rest_framework import serializers
from .models import Alert, AlertRule

class AlertRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertRule
        fields = ["id", "name", "metric", "severity", "threshold", "duration_min", "enabled"]

class AlertSerializer(serializers.ModelSerializer):
    rule = AlertRuleSerializer(read_only=True)
    device_name = serializers.CharField(source="device.name", read_only=True)

    class Meta:
        model = Alert
        fields = [
            "id", "rule", "device", "device_name", 
            "severity", "message", "is_active", "metric_value", 
            "triggered_at", "resolved_at"
        ]
