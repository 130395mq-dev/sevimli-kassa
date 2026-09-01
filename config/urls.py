from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("kirish/", auth_views.LoginView.as_view(
        template_name="dashboard/login.html"), name="login"),
    path("chiqish/", auth_views.LogoutView.as_view(next_page="/kirish/"),
         name="logout"),
    path("admin/", admin.site.urls),
    path("api/v1/", include("api.urls")),
    path("", include("dashboard.urls")),
]
