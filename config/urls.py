from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from api import throttle


class ThrottledLoginView(auth_views.LoginView):
    """Panelga kirish — login bo'yicha urinishlar cheklangan (audit I17)."""

    def post(self, request, *args, **kwargs):
        user = (request.POST.get("username") or "").strip().lower()
        if throttle.blocked("panel", user):
            form = self.get_form()
            form.add_error(None, throttle.MESSAGE)
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        throttle.failed("panel", (self.request.POST.get("username") or ""),
                        throttle.client_ip(self.request))
        return super().form_invalid(form)

    def form_valid(self, form):
        throttle.succeeded("panel", (self.request.POST.get("username") or ""))
        return super().form_valid(form)


urlpatterns = [
    path("kirish/", ThrottledLoginView.as_view(
        template_name="dashboard/login.html"), name="login"),
    path("chiqish/", auth_views.LogoutView.as_view(next_page="/kirish/"),
         name="logout"),
    path("admin/", admin.site.urls),
    path("api/v1/", include("api.urls")),
    path("", include("dashboard.urls")),
]
