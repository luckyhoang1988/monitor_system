from .base import *
import environ

env = environ.Env()

DEBUG = False
# ALLOWED_HOSTS bắt buộc phải set trong .env — không có default để tránh rủi ro bảo mật
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")

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

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST          = SMTP_HOST
EMAIL_PORT          = SMTP_PORT
EMAIL_HOST_USER     = SMTP_USER
EMAIL_HOST_PASSWORD = SMTP_PASSWORD
EMAIL_USE_TLS       = True
DEFAULT_FROM_EMAIL  = SMTP_FROM
