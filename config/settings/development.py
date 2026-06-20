from .base import *

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME":     env("DB_NAME", default="monitor_db"),
        "USER":     env("DB_USER", default="monitor_user"),
        "PASSWORD": env("DB_PASSWORD", default=""),
        "HOST":     env("DB_HOST", default="localhost"),
        "PORT":     env.int("DB_PORT", default=5432),
    }
}

# Tắt email thật khi dev — in ra console
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
