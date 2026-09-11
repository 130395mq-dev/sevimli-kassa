from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.points, name="points"),
    path("smenalar/", views.shifts, name="shifts"),
    path("smena/<int:pk>/", views.shift_detail, name="shift-detail"),
    path("kassalar/", views.registers, name="registers"),
    path("kassa/<int:pk>/sozlash/", views.register_edit, name="register-edit"),
    path("bonus/", views.bonus, name="bonus"),
    path("bonus/mijoz/<int:pk>/", views.customer_bonus, name="customer-bonus"),
    path("narxlar/", views.prices, name="prices"),
    path("tolov-turlari/", views.payment_methods, name="payment-methods"),
    path("versiyalar/", views.releases, name="releases"),
    path("ornatish/", views.installer, name="installer"),
    path("ornatish/fayl/", views.installer_download, name="installer-download"),
    path("health/", views.health, name="health"),
    path("aloqa.json", views.aloqa_json, name="aloqa-json"),
]
