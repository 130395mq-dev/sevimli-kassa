from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.points, name="points"),
    path("smenalar/", views.shifts, name="shifts"),
    path("smena/<int:pk>/", views.shift_detail, name="shift-detail"),
    path("kassalar/", views.registers, name="registers"),
    path("kassa/<int:pk>/sozlash/", views.register_edit, name="register-edit"),
    path("narxlar/", views.prices, name="prices"),
    path("versiyalar/", views.releases, name="releases"),
    path("ornatish/", views.installer, name="installer"),
    path("ornatish/fayl/", views.installer_download, name="installer-download"),
    path("health/", views.health, name="health"),
]
