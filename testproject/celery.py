"""Celery-приложение тестового проекта (по образцу AA-шаблона, упрощённое)."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings.local")

from django.conf import settings  # noqa: E402

app = Celery("testproject")

app.conf.broker_connection_retry_on_startup = True

app.config_from_object("django.conf:settings", namespace="")

app.conf.broker_transport_options = {
    "priority_steps": list(range(10)),
    "queue_order_strategy": "priority",
}
app.conf.task_default_priority = 5
app.conf.worker_prefetch_multiplier = 1

app.autodiscover_tasks(lambda: settings.INSTALLED_APPS)
