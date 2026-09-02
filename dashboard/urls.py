from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.points, name="points"),
    path("smenalar/", views.shifts, name="shifts"),
    path("smena/<int:pk>/", views.shift_detail, name="shift-detail"),
    path("kassirlar/", views.cashiers, name="cashiers"),
    path("kassalar/", views.registers, name="registers"),
    path("kassa/<int:pk>/sozlash/", views.register_edit, name="register-edit"),
    path("filiallar/", views.stores, name="stores"),
    path("versiyalar/", views.releases, name="releases"),
    path("health/", views.health, name="health"),
]
