from django.urls import path
from . import api
from . import api_export

urlpatterns = [
    path("metrics/export/", api_export.export_metrics, name="export_metrics"),
    path("metrics/<int:device_id>/", api.device_metrics, name="device_metrics"),
    path("metrics/<int:device_id>/status/", api.device_status_timeline, name="device_status_timeline"),
    path("metrics/<int:device_id>/interfaces/", api.interface_metrics, name="interface_metrics"),
]
