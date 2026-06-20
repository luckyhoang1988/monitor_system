from django.urls import path
from . import views

app_name = "devices"

urlpatterns = [
    path("", views.device_list, name="list"),
    path("add/", views.device_add, name="add"),
    path("<int:pk>/edit/", views.device_edit, name="edit"),
    path("<int:pk>/delete/", views.device_delete, name="delete"),
    path("<int:pk>/test/", views.device_test_connection, name="test"),
    
    # Discovery
    path("discovery/", views.device_discovery, name="discovery"),
    path("discovery/scan/", views.device_discovery_scan, name="discovery_scan"),
    
    # Backup
    path("<int:pk>/backups/", views.device_backups, name="backups"),
    path("<int:pk>/backups/run/", views.device_run_backup, name="run_backup"),
    path("<int:pk>/backups/download/<str:filename>/", views.device_download_backup, name="download_backup"),
]
