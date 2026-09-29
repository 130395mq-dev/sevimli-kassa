from django.contrib import admin

from .models import LoginThrottle


@admin.register(LoginThrottle)
class LoginThrottleAdmin(admin.ModelAdmin):
    """Yopilgan loginni qo'lda ochish: yozuvni o'chirish kifoya (audit I17)."""

    list_display = ("key", "failures", "blocked_until", "window_start", "updated_at")
    search_fields = ("key",)
    ordering = ("-updated_at",)
    readonly_fields = ("key", "failures", "window_start", "blocked_until", "updated_at")

    def has_add_permission(self, request):
        return False
