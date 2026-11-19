"""Data-миграция: создание групп прав market_monitoring_view / market_monitoring_edit (§3)."""

from django.db import migrations


def create_groups(apps, schema_editor):
    """Создание групп прав (перишены назначаются post_migrate-сигналом AppConfig)."""
    from ..auth_hooks import create_groups

    create_groups()


def remove_groups(apps, schema_editor):
    """Обратная операция: удаление групп прав плагина."""
    from django.contrib.auth.models import Group

    Group.objects.filter(name__in=["market_monitoring_view", "market_monitoring_edit"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("marketmonitoring", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_groups, remove_groups),
    ]
