"""WSGI-приложение тестового проекта."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings.local")

application = get_wsgi_application()
