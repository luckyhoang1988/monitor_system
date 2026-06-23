from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

from apps.devices.api import DeviceViewSet
from apps.alerts.api import AlertViewSet
from apps.dashboard.views import health_check

# Khởi tạo Router cho REST API v1
api_router = DefaultRouter()
api_router.register(r'devices', DeviceViewSet, basename='device')
api_router.register(r'alerts', AlertViewSet, basename='alert')

urlpatterns = [
    path("health/", health_check, name="health_check"),
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("apps.dashboard.urls")),
    path("devices/", include("apps.devices.urls")),
    path("alerts/", include("apps.alerts.urls")),
    path("users/", include("apps.accounts.urls")),

    # API Router chính (DRF)
    path("api/v1/", include(api_router.urls)),
    
    # API cũ cho Metrics (Chart.js)
    path("api/", include("apps.metrics.urls")),
    
    # OpenAPI 3 Schema & Swagger UI
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
