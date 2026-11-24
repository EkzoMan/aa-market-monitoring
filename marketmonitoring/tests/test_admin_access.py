"""Тесты Django-admin: разделы плагина доступны только суперпользователю (§3, кр.14 §19)."""

import django.utils.timezone
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from ..models import SyncRun

ADMIN_URLS = [
    "admin:marketmonitoring_syncrun_changelist",
    "admin:marketmonitoring_notificationlog_changelist",
    "admin:marketmonitoring_marketlocation_changelist",
    "admin:marketmonitoring_structureprovider_changelist",
    "admin:marketmonitoring_contractprovider_changelist",
    "admin:marketmonitoring_webhookconfig_changelist",
    "admin:marketmonitoring_pluginsetting_changelist",
]


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ADMIN_URLS)
def test_admin_section_denied_for_non_superuser(url_name):
    """Разделы admin плагина: не-суперпользователь (в т.ч. _edit) получает отказ."""
    editor = User.objects.create_user(username="editor", password="x", is_staff=True)
    editor.is_staff = True
    editor.save()
    client = Client()
    client.force_login(editor)
    response = client.get(reverse(url_name))
    assert response.status_code in (302, 403), f"{url_name} доступен не-суперпользователю"


@pytest.mark.django_db
def test_syncrun_admin_visible_for_superuser():
    """Суперпользователь видит журнал синков в admin (§3, кр.14 §19)."""
    SyncRun.objects.create(
        task_name="sync_jita_book",
        trigger="beat",
        status="ok",
        started_at=django.utils.timezone.now(),
    )
    User.objects.create_superuser(username="admin", password="x", email="a@b.c")
    client = Client()
    client.force_login(User.objects.get(username="admin"))
    response = client.get(reverse("admin:marketmonitoring_syncrun_changelist"))
    assert response.status_code == 200
    assert b"sync_jita_book" in response.content
