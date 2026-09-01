"""
MoySklad ichida iframe'da ochilishga ruxsat beruvchi middleware.

Moderatsiya talabi: «настройка должна выполняться на стороне МоегоСклада,
в главном окне». Ya'ni panel MoySklad ichida iframe'da ko'rinishi kerak.

Django standart holda X-Frame-Options: DENY qo'yadi va iframe ishlamaydi.
Bu middleware CSP frame-ancestors bilan almashtiradi — faqat MoySklad
domenlariga ruxsat beradi, qolganlariga yo'q. Bu DENY dan xavfsizroq emas,
lekin ochiq iframe'dan ancha xavfsizroq.
"""

from django.conf import settings


class FrameAncestorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.policy = getattr(settings, "CSP_FRAME_ANCESTORS", "'self'")

    def __call__(self, request):
        response = self.get_response(request)
        response.headers["Content-Security-Policy"] = f"frame-ancestors {self.policy}"
        # X-Frame-Options CSP bilan ziddiyat qilmasligi uchun olib tashlaymiz.
        response.headers.pop("X-Frame-Options", None)
        return response
