import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("monitor_system")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
# All beat schedules are defined in settings/base.py (CELERY_BEAT_SCHEDULE)
# to keep a single source of truth.
