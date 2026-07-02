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
    "apps.accounts",
    "apps.realtime",
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
                "apps.accounts.context_processors.user_role",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

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

# Realtime SSE pub/sub — Redis DB riêng (/2) tách khỏi Celery broker (/0).
# Mặc định suy từ REDIS_URL (đổi số DB) để dùng đúng host `redis` trong Docker.
REALTIME_REDIS_URL = env(
    "REALTIME_REDIS_URL",
    default=env("REDIS_URL", default="redis://localhost:6379/0").rsplit("/", 1)[0] + "/2",
)

# Cache metrics — Redis DB riêng (/1) tách khỏi Celery /0 & realtime /2.
# Lưu "latest snapshot" + ring-buffer time-series thay cho ghi raw mỗi poll.
CACHE_REDIS_URL = env(
    "CACHE_REDIS_URL",
    default=env("REDIS_URL", default="redis://localhost:6379/0").rsplit("/", 1)[0] + "/1",
)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

from celery.schedules import crontab  # noqa: E402

# Chu kỳ poll (giây) — override qua .env nếu cần. Mọi mục 60s trừ HyperV.
POLL_NETWORK_INTERVAL_SECS = env.int("POLL_NETWORK_INTERVAL_SECS", default=60)
POLL_PING_INTERVAL_SECS    = env.int("POLL_PING_INTERVAL_SECS", default=60)
POLL_HYPERV_INTERVAL_SECS  = env.int("POLL_HYPERV_INTERVAL_SECS", default=300)
ALERT_EVAL_INTERVAL_SECS   = env.int("ALERT_EVAL_INTERVAL_SECS", default=60)
TOPOLOGY_DISCOVER_INTERVAL_SECS = env.int("TOPOLOGY_DISCOVER_INTERVAL_SECS", default=1800)

# options.expires: bản điều phối tồn quá 1 chu kỳ trong queue sẽ tự rớt,
# tránh tích nhiều bản trùng (snowball) khi worker bị dồn.
CELERY_BEAT_SCHEDULE = {
    "poll-all-network-devices": {
        "task": "apps.collectors.tasks.poll_all_network_devices",
        "schedule": POLL_NETWORK_INTERVAL_SECS,  # switch, router, firewall (SNMP/SSH)
        "options": {"expires": POLL_NETWORK_INTERVAL_SECS},
    },
    "poll-all-ping-devices": {
        "task": "apps.collectors.tasks.poll_all_ping_devices",
        "schedule": POLL_PING_INTERVAL_SECS,  # devices using ping/icmp
        "options": {"expires": POLL_PING_INTERVAL_SECS},
    },
    "poll-all-hyperv": {
        "task": "apps.collectors.tasks.poll_all_hyperv",
        "schedule": POLL_HYPERV_INTERVAL_SECS,
        "options": {"expires": POLL_HYPERV_INTERVAL_SECS},
    },
    "evaluate-alert-rules": {
        "task": "apps.alerts.tasks.evaluate_alert_rules",
        "schedule": ALERT_EVAL_INTERVAL_SECS,
        "options": {"expires": ALERT_EVAL_INTERVAL_SECS},
    },
    "cleanup-old-metrics": {
        "task": "apps.metrics.tasks.cleanup_old_metrics",
        "schedule": crontab(hour=3, minute=0),  # daily 3AM
    },
    "rollup-hourly-metrics": {
        "task": "apps.metrics.tasks.rollup_hourly_metrics",
        "schedule": crontab(minute=5),  # mỗi giờ, phút thứ 5
    },
    "discover-topology-links": {
        "task": "apps.collectors.tasks.discover_topology_links",
        "schedule": TOPOLOGY_DISCOVER_INTERVAL_SECS,
        "options": {"expires": TOPOLOGY_DISCOVER_INTERVAL_SECS},
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

# Tự động xóa metrics cũ (cleanup_old_metrics + dọn raw đã rollup).
# False = TẮT auto-delete, quản lý dung lượng thủ công qua trang Cảnh báo → Dung lượng.
METRICS_AUTO_CLEANUP = env.bool("METRICS_AUTO_CLEANUP", default=False)

# ── Cache-first metrics ───────────────────────────────────────────────────────
# METRICS_WRITE_MODE:
#   "db"    = (mặc định) ghi raw InterfaceStats/SystemHealth/... mỗi poll như cũ.
#   "cache" = metrics thường xuyên vào Redis (latest snapshot + ring-buffer);
#             Postgres CHỈ ghi khi có sự cố (alert fire) hoặc đổi trạng thái quan
#             trọng (interface up/down, online/offline, VM/repl đổi trạng thái).
# Alert engine, tính Mbps, dashboard/chart raw-tier tự đọc từ cache khi mode="cache".
# Redis lỗi → tự degrade sang ghi DB (fallback) để không mất dữ liệu/cảnh báo.
METRICS_WRITE_MODE = env("METRICS_WRITE_MODE", default="db")

# TTL (giây) cho snapshot mới nhất — nên > vài chu kỳ poll để dashboard/alert đọc được.
METRICS_LATEST_TTL_SECS = env.int("METRICS_LATEST_TTL_SECS", default=1800)
# TTL (giây) cho ring-buffer time-series (chart ngắn hạn + sustained alert).
METRICS_SERIES_TTL_SECS = env.int("METRICS_SERIES_TTL_SECS", default=90000)  # ~25h
# Số mẫu tối đa giữ trong mỗi ring-buffer (~1500 mẫu ≈ 25h @60s → phủ chart raw-tier 24h).
METRICS_SERIES_MAX_SAMPLES = env.int("METRICS_SERIES_MAX_SAMPLES", default=1500)

# Đường dẫn để theo dõi dung lượng disk (trang Cảnh báo → Dung lượng).
# Mặc định "/" — trong container, overlay fs phản ánh disk host nơi đặt volume DB.
STORAGE_MONITOR_PATH = env("STORAGE_MONITOR_PATH", default="/")

# ── Collector / Alert / Aggregation tuning ────────────────────────────────────
# Auto-discovery scan limits
DISCOVERY_MAX_IPS       = 256   # max host IPs per subnet scan
DISCOVERY_PING_WORKERS  = 100   # thread pool size for ping sweep
DISCOVERY_SNMP_WORKERS  = 80    # thread pool size for SNMP probe

# Online determination — kết hợp ICMP + SNMP
# True: thiết bị mạng (switch/router/firewall) online chỉ khi CẢ ping ICMP thông
# VÀ SNMP trả về dữ liệu thật (>=1 interface). Tránh false-online khi SNMP rỗng.
ONLINE_REQUIRE_ICMP = env.bool("ONLINE_REQUIRE_ICMP", default=True)
PING_TIMEOUT_SECS   = env.int("PING_TIMEOUT_SECS", default=1)

# SNMP request timeout/retries (collector). Giữ nhỏ để thiết bị offline không treo
# worker khi chu kỳ poll ngắn (60s). 1 walk chết ≈ timeout×(retries+1).
SNMP_TIMEOUT_SECS = env.int("SNMP_TIMEOUT_SECS", default=5)
SNMP_RETRIES      = env.int("SNMP_RETRIES", default=1)

# Alert engine
ALERT_GRACE_PERIOD_SECS    = 90   # min seconds before "no data" is treated as offline (1.5× poll interval)
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
# Phải ≥ nhịp poll thực của Celery beat (poll-all-network-devices = 60s) để
# không bỏ lỡ mẫu trước khi device.collect_interval bị đặt nhỏ hơn nhịp poll.
METRIC_PREV_LOOKBACK_SECS = env.int("METRIC_PREV_LOOKBACK_SECS", default=900)
