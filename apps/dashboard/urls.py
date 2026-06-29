from django.urls import path
from . import views
from . import topology_links_api

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
    # Quản lý link topology thủ công (switch↔switch, AP↔switch)
    path("api/topology/ports/",            topology_links_api.ports_for_device, name="topology_ports"),
    path("api/topology/aps/",              topology_links_api.aps_for_ac,       name="topology_aps"),
    path("api/topology/links/",            topology_links_api.links_collection, name="topology_links"),
    path("api/topology/links/<int:pk>/",   topology_links_api.link_detail,      name="topology_link_detail"),
]
