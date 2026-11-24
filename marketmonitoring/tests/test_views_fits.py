"""Тесты сохранения фитов (§7.5, §14): корпус в составе, транзакция, права доступа."""

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

from ..auth_hooks import GROUP_EDIT, GROUP_VIEW
from ..models import Fit, FitComponent
from .test_calc_engine import sde_ids  # noqa: F401 — справочник eveuniverse (система 30000142)
from .test_eft_parser import eve_types  # noqa: F401 — фикстура EveType записей

pytestmark = pytest.mark.django_db

EFT = """[Ferox, Save test]
Damage Control II
Magnetic Field Stabilizer II x2
"""


@pytest.fixture
def editor_client(db):
    """Клиент под пользователем с правом редактирования."""
    user = User.objects.create_user(username="editor", password="x")
    user.groups.add(Group.objects.get(name=GROUP_EDIT))
    client = Client()
    client.force_login(user)
    return client


def _post_save(client, scope_ref: int = 30000142):
    """POST action=save с валидным EFT (scope: система 30000142)."""
    return client.post(
        reverse("marketmonitoring:fit_create"),
        {
            "action": "save",
            "name": "Save test",
            "eft_raw": EFT,
            "qty_target": 1,
            "scope_kind": "system",
            "scope_ref": scope_ref,
        },
    )


def test_fit_save_includes_hull_component(eve_types, sde_ids, editor_client):  # noqa: F811
    """При сохранении корпус попадает в FitComponent как позиция qty=1, is_charge=False (§6.3)."""
    response = _post_save(editor_client)
    assert response.status_code == 302
    fit = Fit.objects.get(name="Save test")
    hull = FitComponent.objects.get(fit=fit, type_id=643)
    assert hull.qty_per_fit == 1
    assert hull.is_charge is False


def test_fit_save_components(eve_types, sde_ids, editor_client):  # noqa: F811
    """Состав сохраняется по позициям с учётом дубликатов (§7.3.7)."""
    _post_save(editor_client)
    fit = Fit.objects.get(name="Save test")
    assert FitComponent.objects.filter(fit=fit).count() == 3  # корпус + 2 модуля
    mfs = FitComponent.objects.get(fit=fit, type_id=35659)
    assert mfs.qty_per_fit == 2


def test_fit_update_rewrites_components(eve_types, sde_ids, editor_client):  # noqa: F811
    """Правка фита идемпотентно перезаписывает состав, корпус сохраняется (§14)."""
    _post_save(editor_client)
    fit = Fit.objects.get(name="Save test")
    response = editor_client.post(
        reverse("marketmonitoring:fit_update", args=[fit.pk]),
        {
            "action": "save",
            "name": "Save test",
            "eft_raw": "[Ferox, Save test]\nDamage Control II x3",
            "qty_target": 1,
            "scope_kind": "system",
            "scope_ref": 30000142,
        },
    )
    assert response.status_code == 302
    fit.refresh_from_db()
    assert FitComponent.objects.filter(fit=fit).count() == 2  # корпус + 1 модуль
    assert FitComponent.objects.get(fit=fit, type_id=643).qty_per_fit == 1
    assert FitComponent.objects.get(fit=fit, type_id=2046).qty_per_fit == 3


def test_fit_create_denied_without_edit(eve_types, client):  # noqa: F811
    """Пользователь без права редактирования получает отказ (§14)."""
    user = User.objects.create_user(username="viewer3", password="x")
    user.groups.add(Group.objects.get(name=GROUP_VIEW))
    client.force_login(user)
    response = client.get(reverse("marketmonitoring:fit_create"))
    assert response.status_code == 302
    assert Fit.objects.count() == 0
