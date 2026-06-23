from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.user_list, name="list"),
    path("add/", views.user_add, name="add"),
    path("<int:pk>/edit/", views.user_edit, name="edit"),
    path("<int:pk>/delete/", views.user_delete, name="delete"),
    path("password/", views.password_change_self, name="password_change"),
]
