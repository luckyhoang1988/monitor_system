from .base import *
import environ
from django.core.exceptions import ImproperlyConfigured

env = environ.Env()

DEBUG = False
# ALLOWED_HOSTS bắt buộc phải set trong .env — không có default để tránh rủi ro bảo mật
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")

SECRET_KEY = env("SECRET_KEY")
if not SECRET_KEY or SECRET_KEY == "django-insecure-change-me-in-production":
    raise ImproperlyConfigured("SECRET_KEY phải được set trong .env production (không dùng default)")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME":     env("DB_NAME"),
        "USER":     env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST":     env("DB_HOST", default="localhost"),
        "PORT":     env.int("DB_PORT", default=5432),
        "CONN_MAX_AGE": 60,
    }
}

SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

# ── HTTPS sau reverse proxy (nginx termination) ──
# nginx set header X-Forwarded-Proto=https → Django coi request la secure
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# nginx da lo viec redirect 80→443, khong de Django redirect (tranh double redirect)
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Origin tin cay cho POST/CSRF qua HTTPS (Django 4+). Mac dinh suy ra tu ALLOWED_HOSTS.
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[f"https://{h}" for h in ALLOWED_HOSTS],
)

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST          = SMTP_HOST
EMAIL_PORT          = SMTP_PORT
EMAIL_HOST_USER     = SMTP_USER
EMAIL_HOST_PASSWORD = SMTP_PASSWORD
EMAIL_USE_TLS       = True
DEFAULT_FROM_EMAIL  = SMTP_FROM
