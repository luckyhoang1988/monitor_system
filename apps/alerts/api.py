"""DRF API endpoint cho Alerts."""
from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from apps.devices.permissions import IsAdminOrReadOnly
from .models import Alert
from .serializers import AlertSerializer

class AlertViewSet(viewsets.ModelViewSet):
    """
    API endpoint cho phép xem và quản lý Alerts.
    Cung cấp bộ lọc theo device, severity, is_active.
    """
    queryset = Alert.objects.select_related("rule", "device").all().order_by("-triggered_at")
    serializer_class = AlertSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["device", "severity", "is_active", "rule__metric"]
