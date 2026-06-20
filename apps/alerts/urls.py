from django.urls import path
from . import views

app_name = "alerts"

urlpatterns = [
    path("", views.alert_list, name="list"),
    path("<int:pk>/ack/", views.alert_acknowledge, name="ack"),
    # AlertRule CRUD
    path("rules/", views.rule_list, name="rule_list"),
    path("rules/add/", views.rule_create, name="rule_create"),
    path("rules/<int:pk>/edit/", views.rule_edit, name="rule_edit"),
    path("rules/<int:pk>/delete/", views.rule_delete, name="rule_delete"),
]
