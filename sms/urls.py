from django.urls import path

from . import views

app_name = "sms"

urlpatterns = [
    path("", views.bosh, name="bosh"),
]
