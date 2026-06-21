from pathlib import Path
import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="django-insecure-change-me-in-production")

# Encryption key cho mã hóa credentials trong DB (SSH password, SNMP community)
ENCRYPTION_KEY = env("ENCRYPTION_KEY", default="")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_beat",
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "apps.devices",
    "apps.collectors",
    "apps.metrics",
    "apps.alerts",
    "apps.dashboard",
]

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Monitor System API",
    "DESCRIPTION": "REST API for Devices, Alerts, and Metrics data export",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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

WSGI_APPLICATION = "config.wsgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "vi"
TIME_ZONE = "Asia/Ho_Chi_Minh"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# Celery
CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

from celery.schedules import crontab  # noqa: E402
CELERY_BEAT_SCHEDULE = {
    "poll-all-network-devices": {
        "task": "apps.collectors.tasks.poll_all_network_devices",
        "schedule": 120,  # mỗi 120s — switch, router, firewall (SNMP/SSH)
    },
    "poll-all-ping-devices": {
        "task": "apps.collectors.tasks.poll_all_ping_devices",
        "schedule": 180,  # every 3 min — devices using ping/icmp
    },
    "poll-all-hyperv": {
        "task": "apps.collectors.tasks.poll_all_hyperv",
        "schedule": 300,
    },
    "evaluate-alert-rules": {
        "task": "apps.alerts.tasks.evaluate_alert_rules",
        "schedule": 300,
    },
    "cleanup-old-metrics": {
        "task": "apps.metrics.tasks.cleanup_old_metrics",
        "schedule": crontab(hour=3, minute=0),  # daily 3AM
    },
    "rollup-hourly-metrics": {
        "task": "apps.metrics.tasks.rollup_hourly_metrics",
        "schedule": crontab(minute=5),  # mỗi giờ, phút thứ 5
    },
    "rollup-daily-metrics": {
        "task": "apps.metrics.tasks.rollup_daily_metrics",
        "schedule": crontab(hour=3, minute=30),  # daily 3:30AM (sau cleanup)
    },
}

# Alert notification channels
SMTP_HOST     = env("SMTP_HOST", default="")
SMTP_PORT     = env.int("SMTP_PORT", default=587)
SMTP_USER     = env("SMTP_USER", default="")
SMTP_PASSWORD = env("SMTP_PASSWORD", default="")
SMTP_FROM     = env("SMTP_FROM", default="monitor@company.local")
ALERT_EMAIL_RECIPIENTS = env.list("ALERT_EMAIL_RECIPIENTS", default=[])

# Django email backend wiring (SMTP_* → EMAIL_*)
EMAIL_BACKEND       = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST          = SMTP_HOST
EMAIL_PORT          = SMTP_PORT
EMAIL_HOST_USER     = SMTP_USER
EMAIL_HOST_PASSWORD = SMTP_PASSWORD
EMAIL_USE_TLS       = True
DEFAULT_FROM_EMAIL  = SMTP_FROM

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHAT_ID   = env("TELEGRAM_CHAT_ID", default="")

# Public base URL for building links in notifications (optional).
# Example: https://monitor.company.com
SITE_URL = env("SITE_URL", default="").rstrip("/")

# OID profiles directory
OID_PROFILES_DIR = BASE_DIR / "oids"

# WinRM certificate validation cho HyperV collector
# "validate" = đúng chuẩn (production)
# "ignore"   = bỏ qua cert — chỉ dùng khi Hyper-V host dùng self-signed cert nội bộ
WINRM_CERT_VALIDATE = env("WINRM_CERT_VALIDATE", default="validate")

# Metrics retention (ngày)
METRICS_RETENTION_DAYS = env.int("METRICS_RETENTION_DAYS", default=90)

# ── Collector / Alert / Aggregation tuning ────────────────────────────────────
# Auto-discovery scan limits
DISCOVERY_MAX_IPS       = 256   # max host IPs per subnet scan
DISCOVERY_PING_WORKERS  = 100   # thread pool size for ping sweep
DISCOVERY_SNMP_WORKERS  = 80    # thread pool size for SNMP probe

# Alert engine
ALERT_GRACE_PERIOD_SECS    = 120  # min seconds before "no data" is treated as offline
ALERT_EVAL_WINDOW_MINUTES  = 10   # look-back window for alert task
DEVICE_ONLINE_MIN_GRACE_SECS = 300  # min grace for Device.is_online to avoid flapping

# Hysteresis: vùng đệm ngưỡng phục hồi (% so với threshold) để tránh dao động quanh ngưỡng.
# Vd 0.1 → rule gt 90% chỉ resolve khi value < 81%.
ALERT_HYSTERESIS_PCT  = env.float("ALERT_HYSTERESIS_PCT", default=0.1)
# Flapping: nếu một (device, rule) fire ≥ THRESHOLD lần trong WINDOW phút → bỏ qua notification.
ALERT_FLAP_WINDOW_MIN = env.int("ALERT_FLAP_WINDOW_MIN", default=30)
ALERT_FLAP_THRESHOLD  = env.int("ALERT_FLAP_THRESHOLD", default=4)

# Chart API: giới hạn số điểm trả về cho series raw (downsample phía server).
CHART_MAX_POINTS = env.int("CHART_MAX_POINTS", default=500)

# Metric aggregation buffers (avoid rolling up incomplete time buckets)
HOURLY_ROLLUP_BUFFER_HOURS = 2
DAILY_ROLLUP_BUFFER_DAYS   = 1

# Cửa sổ (giây) tìm mẫu InterfaceStats trước để tính delta Mbps.
# Phải ≥ nhịp poll thực của Celery beat (poll-all-network-devices = 300s) để
# không bỏ lỡ mẫu trước khi device.collect_interval bị đặt nhỏ hơn nhịp poll.
METRIC_PREV_LOOKBACK_SECS = env.int("METRIC_PREV_LOOKBACK_SECS", default=900)
