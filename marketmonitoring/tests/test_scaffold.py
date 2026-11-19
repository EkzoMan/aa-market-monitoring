"""Smoke-тесты каркаса этапа 1: группы прав, модели, страница dashboard."""

# Приложение pytest-django отмечает этот модуль как Django TestCase-набор.
import pytest  # noqa: E402
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

from ..auth_hooks import GROUP_EDIT, GROUP_VIEW, has_edit, has_view
from ..models import Fit, PluginSetting


@pytest.mark.django_db
def test_groups_created():
    """Группы прав существуют после миграций."""
    assert Group.objects.filter(name=GROUP_VIEW).exists()
    assert Group.objects.filter(name=GROUP_EDIT).exists()


@pytest.mark.django_db
def test_has_view_and_edit():
    """has_view/has_edit: суперпользователь и члены групп; аноним — нет."""
    superuser = User.objects.create_superuser(username="admin", password="x", email="a@b.c")
    assert has_view(superuser)
    assert has_edit(superuser)

    viewer = User.objects.create_user(username="viewer", password="x")
    viewer.groups.add(Group.objects.get(name=GROUP_VIEW))
    assert has_view(viewer)
    assert not has_edit(viewer)

    editor = User.objects.create_user(username="editor", password="x")
    editor.groups.add(Group.objects.get(name=GROUP_EDIT))
    assert has_view(editor)
    assert has_edit(editor)

    anonymous = Client()
    from django.contrib.auth.models import AnonymousUser

    assert not has_view(AnonymousUser())
    assert not has_edit(AnonymousUser())


@pytest.mark.django_db
def test_dashboard_requires_login(client):
    """Dashboard требует логина (302 на login для анонима)."""
    response = client.get(reverse("marketmonitoring:index"))
    assert response.status_code == 302


@pytest.mark.django_db
def test_dashboard_for_viewer(client):
    """Viewer видит dashboard с 200."""
    user = User.objects.create_user(username="viewer2", password="x")
    user.groups.add(Group.objects.get(name=GROUP_VIEW))
    client.force_login(user)
    response = client.get(reverse("marketmonitoring:index"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_plugin_setting_singleton():
    """PluginSetting.load всегда возвращает одну строку с дефолтами."""
    setting = PluginSetting.load()
    assert setting.jita_region_id == 10000002
    assert setting.jita_station_id == 60003760
    again = PluginSetting.load()
    assert again.pk == setting.pk
    assert Fit.objects.count() == 0
