"""DRF API endpoint cho Devices."""
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from .permissions import IsAdminOrReadOnly
from .models import Device
from .serializers import DeviceSerializer

class DeviceViewSet(viewsets.ModelViewSet):
    """
    API endpoint cho phép quản lý (CRUD) Device.
    Cung cấp bộ lọc theo vendor, os_family, enabled.
    """
    queryset = Device.objects.prefetch_related("interfaces").all().order_by("name")
    serializer_class = DeviceSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["vendor", "os_family", "enabled", "device_type"]
