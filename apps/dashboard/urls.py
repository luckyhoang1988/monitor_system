from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/poll-status/",     views.poll_status,     name="poll_status"),
    path("api/alerts-summary/",  views.alerts_summary,  name="alerts_summary"),
    path("switch/<int:pk>/",   views.switch_detail,   name="switch_detail"),
    path("router/<int:pk>/",   views.router_detail,   name="router_detail"),
    path("firewall/<int:pk>/", views.firewall_detail, name="firewall_detail"),
    path("nas/<int:pk>/",      views.nas_detail,      name="nas_detail"),
    path("hyperv/<int:pk>/",   views.hyperv_detail,   name="hyperv_detail"),
    path("wlan/<int:pk>/",     views.wlan_detail,     name="wlan_detail"),
    path("topology/",          views.topology,        name="topology"),
    path("api/topology/",      views.topology_data,   name="topology_data"),
]
