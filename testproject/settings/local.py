"""Локальные настройки тестового проекта: SQLite-БД."""

from .base import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "testproject" / "db.sqlite3",
    }
}
