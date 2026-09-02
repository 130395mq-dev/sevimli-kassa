"""
Sevimli Kassa — Hub sozlamalari.

Railway uchun mo'ljallangan: hamma maxfiy qiymatlar muhit o'zgaruvchilaridan
o'qiladi. Kodda hech qanday token yoki parol bo'lmasligi kerak.
"""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, str(default)).lower() in {"1", "true", "yes", "ha"}


# ---------------------------------------------------------------- xavfsizlik

SECRET_KEY = env("SECRET_KEY", "dev-only-not-for-production")
DEBUG = env_bool("DEBUG", False)

ALLOWED_HOSTS = [h.strip() for h in env("ALLOWED_HOSTS", "*").split(",") if h.strip()]

# Railway HTTPS'ni proxy orqali beradi.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in env("CSRF_TRUSTED_ORIGINS", "https://*.up.railway.app").split(",")
    if o.strip()
]

# Panel MoySklad ichida iframe'da ochilishi kerak bo'lishi mumkin
# (moderatsiya talabi). Shuning uchun frame'ga ruxsat beramiz, lekin
# faqat MoySklad domenidan.
X_FRAME_OPTIONS = "SAMEORIGIN"
CSP_FRAME_ANCESTORS = env(
    "CSP_FRAME_ANCESTORS", "'self' https://online.moysklad.ru https://*.moysklad.ru"
)

# --------------------------------------------------------------- ilovalar

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "catalog",
    "sales",
    "api",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "dashboard.middleware.FrameAncestorsMiddleware",
]

ROOT_URLCONF = "config.urls"

# Panelga kirish
LOGIN_URL = "/kirish/"
LOGIN_REDIRECT_URL = "/"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ------------------------------------------------------------------- baza

DATABASES = {
    "default": dj_database_url.config(
        default=env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=600,
        conn_health_checks=True,
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ------------------------------------------------------------------ statik

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# ------------------------------------------------------------- til va vaqt

LANGUAGE_CODE = "ru"
TIME_ZONE = "Asia/Tashkent"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------- MoySklad

# Vaqtinchalik: alohida integratsiya foydalanuvchisining tokeni.
# Kelajakda «приватное решение» o'rnatilganda MoySklad tokenni o'zi beradi
# va bu o'zgaruvchi kerak bo'lmaydi.
MOYSKLAD_TOKEN = env("MOYSKLAD_TOKEN")

# Mijozsiz savdolar kimga yoziladi — MoySklad'dagi «Розничный покупатель»
# kontragentining ID'si. Busiz mijozsiz chek yozilmaydi.
MOYSKLAD_RETAIL_CUSTOMER_ID = env("MOYSKLAD_RETAIL_CUSTOMER_ID")

# Kassa ilovasining yangilanishi.
#
# Asosiy yo'l — panel: «Versiyalar» sahifasida yangi SevimliKassa.exe
# yuklanadi (sales.KassaRelease). Kassalar o'zi tekshirib, yuklab oladi.
# Quyidagi env'lar — zaxira yo'l: bazada versiya bo'lmasa shular ishlatiladi.
APP_VERSION = env("APP_VERSION", "1.0.0")
APP_DOWNLOAD_URL = env("APP_DOWNLOAD_URL", "")
APP_UPDATE_NOTES = env("APP_UPDATE_NOTES", "")

# Yuklangan exe fayllar shu yerda turadi. Railway'da bu doimiy disk
# (volume) bo'lishi kerak — aks holda har deploy'da o'chib ketadi.
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "media")))
MEDIA_URL = "/media/"

# Chek sarlavhasida chiqadigan nom
MARKET_NAME = env("MARKET_NAME", "Sevimli Market")

# Chek printeri kengligi (belgi soni). 80 mm = 48, 58 mm = 32.
RECEIPT_WIDTH = int(env("RECEIPT_WIDTH", "48"))

# Yuborilmagan chekni necha marta urinib ko'rish. Shundan keyin «stuck»
# holatiga o'tadi va panelda ko'rinadi — bu yerda odam kerak bo'ladi.
SYNC_MAX_ATTEMPTS = int(env("SYNC_MAX_ATTEMPTS", "12"))

# Sinxronizatsiya davrlari (daqiqada) — cron shu bo'yicha sozlanadi.
SYNC_INTERVALS = {
    "stock": 3,
    "products": 10,
    "customers": 15,
    "folders": 60,
    "retail_stores": 60,
}

# ------------------------------------------------------------------- loglar

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {asctime} {name} — {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "moysklad": {"level": "INFO", "propagate": True},
        "catalog": {"level": "INFO", "propagate": True},
    },
}
