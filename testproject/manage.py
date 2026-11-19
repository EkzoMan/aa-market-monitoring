#!/usr/bin/env python
"""Тестовый manage.py для локальной проверки плагина (не входит в пакет)."""

import os
import sys

# Убираем каталог testproject из sys.path, чтобы не затенять пакет celery (python manage.py добавляет его первым).
_script_dir = os.path.dirname(os.path.abspath(__file__))
while _script_dir in sys.path:
    sys.path.remove(_script_dir)

# Корень репозитория в sys.path, чтобы были доступны testproject и marketmonitoring.
sys.path.insert(0, os.path.dirname(_script_dir))


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings.local")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and available on your PYTHONPATH?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
