from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("connect", views.connect, name="connect"),
    path("login", views.login, name="login"),
    path("hello", views.hello, name="hello"),
    path("version", views.version, name="version"),
    path("catalog", views.catalog, name="catalog"),
    path("catalog/refresh", views.catalog_refresh, name="catalog-refresh"),
    path("customers", views.customers, name="customers"),
    path("customers/create", views.create_customer, name="customer-create"),
    path("shift/open", views.shift_open, name="shift-open"),
    path("shift/close", views.shift_close, name="shift-close"),
    path("shift/report", views.shift_report, name="shift-report"),
    path("cash", views.cash_operation, name="cash"),
    path("sales", views.create_sale, name="sales"),
    path("sales/returnable", views.returnable_sales, name="returnable"),
]
