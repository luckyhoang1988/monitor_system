from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("switch/<int:pk>/",   views.switch_detail,   name="switch_detail"),
    path("router/<int:pk>/",   views.router_detail,   name="router_detail"),
    path("firewall/<int:pk>/", views.firewall_detail, name="firewall_detail"),
    path("hyperv/<int:pk>/",   views.hyperv_detail,   name="hyperv_detail"),
]
