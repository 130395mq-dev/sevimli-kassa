from django.urls import path

from . import views

app_name = "kpi"

urlpatterns = [
    path("", views.bosh, name="bosh"),
    path("men/", views.men, name="men"),
    path("xodim/<int:pk>/", views.xodim, name="xodim"),
    path("minuslar/", views.minuslar, name="minuslar"),
    path("minus/<int:pk>/", views.minus, name="minus"),
    path("boglash/", views.boglash, name="boglash"),
    path("qoida/", views.qoida, name="qoida"),
    path("chop-etish/", views.chop, name="chop"),
]
