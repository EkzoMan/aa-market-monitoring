"""Базовые настройки тестового проекта AA 5.x (упрощённые, только для локальной проверки плагина)."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = "testproject-secret-key-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # allianceauth
    "allianceauth",
    "allianceauth.framework",
    "allianceauth.notifications",
    "allianceauth.services",
    "allianceauth.eveonline",
    "allianceauth.authentication",
    "allianceauth.groupmanagement",
    # Тест-окружение AA5: меню (MenuItemHook) и темы нужны, чтобы базовые
    # шаблоны AA рендерились без TemplateDoesNotExist/NoneType в theme-тегах.
    "allianceauth.menu",
    "allianceauth.thirdparty.navhelper",
    "allianceauth.theme",
    "allianceauth.theme.bootstrap",
    "allianceauth.theme.darkly",
    "allianceauth.theme.flatly",
    "allianceauth.theme.materia",
    "allianceauth.custom_css",
    "django_celery_beat",
    "bootstrapform",
    "sri",
    "esi",
    "eveuniverse",
    # plugin
    "marketmonitoring",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allianceauth.authentication.middleware.UserSettingsMiddleware",
]

try:
    # Celery app testproject (по образцу AA-проекта) — нужен для checks allianceauth.
    from testproject import celery as _celery_app  # noqa: F401
except ImportError:
    pass

ROOT_URLCONF = "testproject.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "allianceauth.context_processors.auth_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "testproject.wsgi.application"

# БД задаётся в local.py (SQLite для тестов).
DATABASES = {}

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]

LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "static"

LOGIN_URL = "auth_login_user"
LOGIN_REDIRECT_URL = "authentication:dashboard"
LOGOUT_REDIRECT_URL = "authentication:dashboard"

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Celery: синхронное выполнение для тестов.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERYBEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Кэш: локальный-memory, чтобы AA не требовал Redis.
# test_env_compat подменяет django_redis.get_redis_connection, чтобы AA
# корректно использовал собственный stub в тестовом окружении.
import testproject.test_env_compat  # noqa: F401,E402

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Django ESI
ESI_USER_AGENT = "aa-market-monitoring test"
ESI_MAX_RETRIES = 3
ESI_TIMEOUT = 10
ESI_USER_CONTACT_EMAIL = "test@example.com"
ESI_SSO_CLIENT_ID = "test-client-id"
ESI_SSO_CLIENT_SECRET = "test-client-secret"
ESI_SSO_CALLBACK_URL = "http://localhost:8000/sso/callback"

SITE_URL = "http://localhost"
SITE_NAME = "Test AA"
DEFAULT_THEME = "allianceauth.theme.bootstrap.auth_hooks.BootstrapThemeHook"
CSRF_TRUSTED_ORIGINS = ["http://localhost"]
LOGIN_TOKEN_SCOPES = ["publicData"]
ESI_SSO_MANAGE_TOKENS = False

# Celery priorities (AA checks)
CELERY_DEFAULT_QUEUE = "default"
CELERY_TASK_DEFAULT_PRIORITY = 6
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "priority_steps": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    "queue_order_strategy": "priority",
}
BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "marketmonitoring": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
