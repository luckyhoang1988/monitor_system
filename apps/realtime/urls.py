from django.urls import path

from . import views

app_name = "realtime"

urlpatterns = [
    path("fleet/", views.fleet_stream, name="fleet_stream"),
    path("device/<int:device_id>/", views.device_stream, name="device_stream"),
]
