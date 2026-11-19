"""DRF API endpoint cho Alerts."""
from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from .models import Alert
from .serializers import AlertSerializer

class AlertViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint CHỈ ĐỌC cho Alerts (mọi user đã đăng nhập).
    Cung cấp bộ lọc theo device, severity, is_active.

    Cố ý KHÔNG cho ghi (review ngoài 2026-11-19, F03): vòng đời alert (fire/resolve/outbox/RECOVERED/đóng vì tắt
    rule) chỉ do engine và các view có tên điều khiển. Khi còn là ModelViewSet, admin PATCH `is_active=false`
    đóng alert mà không qua `_resolve_alert` (không RECOVERED, không khoá Device, lệch outbox), còn DELETE kéo theo
    xoá cả lịch sử thông báo.
    """
    queryset = Alert.objects.select_related("rule", "device").all().order_by("-triggered_at")
    serializer_class = AlertSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["device", "severity", "is_active", "rule__metric"]
