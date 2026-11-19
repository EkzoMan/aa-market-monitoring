"""Тесты UI-слоя (дизайн-спека docs/design/): семантическая палитра, классы утилит, состояния, роли.

Тесты без сети: только БД плагина и справочники eveuniverse.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

from ..auth_hooks import GROUP_EDIT, GROUP_VIEW
from ..models import ComponentRequest, Fit
from .test_calc_engine import sde_ids  # noqa: F401 — справочники eveuniverse (система/станции)
from .test_eft_parser import eve_types  # noqa: F401 — записи EveType

pytestmark = pytest.mark.django_db


@pytest.fixture
def viewer_client(db):
    """Клиент под пользователем с правом только просмотра."""
    user = User.objects.create_user(username="ui_viewer", password="x")
    user.groups.add(Group.objects.get(name=GROUP_VIEW))
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def editor_client(db):
    """Клиент под пользователем с правом редактирования."""
    user = User.objects.create_user(username="ui_editor", password="x")
    user.groups.add(Group.objects.get(name=GROUP_EDIT))
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def fit_with_component(eve_types, sde_ids):  # noqa: F811
    """Фит с корпусом (тип 643) и компонентом (тип 2046) в области системы 30000142, статус кэша OK."""
    fit = Fit.objects.create(
        name="UI test fit",
        hull_type_id=643,
        qty_target=2,
        scope_kind="system",
        scope_ref=30000142,
        status_cache="ok",
        fits_available=1,
        total_cost=123456,
    )
    fit.components.create(type_id=643, qty_per_fit=1)  # корпус
    fit.components.create(type_id=2046, qty_per_fit=1)
    # Наличие по обеим станциям системы 30000142 → статус OK, таблица считается.
    _add_stock(60003760, 643, 2)
    _add_stock(60003761, 643, 2)
    _add_stock(60003760, 2046, 2)
    _add_stock(60003761, 2046, 2)
    return fit


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_uses_semantic_bootstrap_badges(viewer_client, fit_with_component):
    """Палитра статусов — только семантические токены Bootstrap (README §1), без жёстких hex-классов."""
    response = viewer_client.get(reverse("marketmonitoring:index"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "text-bg-success" in content  # статус ok
    assert "mm-badge-ok" not in content  # старые hex-классы удалены
    assert "mm-progress" in content  # тонкий прогресс «K из N»
    assert "mm-num" in content  # числовая типографика
    assert "marketmonitoring/css/mm.css" in content  # utility-CSS подключён


def test_dashboard_hidden_edit_elements_for_view_role(viewer_client, fit_with_component):
    """Роль _view: кнопки edit скрыты — «Refresh now» и «+ New fit» (07-states.md §4)."""
    content = viewer_client.get(reverse("marketmonitoring:index")).content.decode()
    assert '<button type="submit" id="mm-refresh-btn"' not in content
    assert "New fit" not in content


def test_dashboard_edit_controls_for_edit_role(editor_client, fit_with_component):
    """Роль _edit видит «Refresh now» (GET — форма не публикует состояние)."""
    content = editor_client.get(reverse("marketmonitoring:index")).content.decode()
    assert "Refresh now" in content


def test_dashboard_empty_state_without_fits(viewer_client, db):
    """Нет фитов: empty state с gettext-текстом (07-states.md §4)."""
    response = viewer_client.get(reverse("marketmonitoring:index"))
    content = response.content.decode()
    assert "No fits yet" in content


def _add_stock(location_id, type_id, volume):
    """Наличие на станции в системе 30000142 (свежее, чтобы данные считались не NO_DATA)."""
    from django.utils import timezone

    from ..models import StockLevel

    stock, _created = StockLevel.objects.update_or_create(
        location_id=location_id,
        type_id=type_id,
        defaults={
            "solar_system_id": 30000142,
            "region_id": 10000002,
            "total_sell_volume": volume,
            "updated_at": timezone.now(),
        },
    )
    return stock


def test_dashboard_deficit_component_counter(viewer_client, fit_with_component, eve_types):  # noqa: F811
    """Перекрёстная сводка (§14.4): счётчик компонентов с дефицитом со ссылкой на Components."""
    from ..models import ComponentRequest

    ComponentRequest.objects.create(type_id=2046, qty_target=99, scope_kind="system", scope_ref=30000142)
    # Область «system 30000142» = станции 60003760 и 60003761 — наличие на обеих (иначе NO_DATA).
    _add_stock(60003760, 2046, 1)
    _add_stock(60003761, 2046, 1)
    content = viewer_client.get(reverse("marketmonitoring:index")).content.decode()
    assert "component with deficit" in content
    assert reverse("marketmonitoring:components") in content


# ---------------------------------------------------------------------------
# Карточка фита
# ---------------------------------------------------------------------------


def test_fit_detail_hull_row_and_total(editor_client, fit_with_component):
    """Таблица компонентов: строка корпуса .mm-hull-row, итоговая строка tfoot, toast «Copied»."""
    content = editor_client.get(reverse("marketmonitoring:fit_detail", args=[fit_with_component.pk])).content.decode()
    assert "mm-hull-row" in content
    assert "text-bg-primary" in content  # чип HULL
    assert "Total" in content
    assert "mm-copied-toast" in content


def test_fit_detail_overprice_badge(editor_client, eve_types, sde_ids):  # noqa: F811
    """Бейдж OVERPRICE в строке позиции при unit > ref*(1+threshold) — данные только из БД (02-fit-detail.md).

    Сценарий: дефицит 4 (нужно 5, есть 1); стакан Jita — rank1 дешёвый, но объёма
    не хватает (volume=1), rank2 дорогой; покрывающая цена уходит на дорогой
    уровень → unit ≫ ref = цена rank1 → оверпрайс.
    """
    from django.utils import timezone

    from ..models import JitaOrderLevel

    fit = Fit.objects.create(
        name="Overprice fit",
        hull_type_id=643,
        qty_target=5,
        scope_kind="system",
        scope_ref=30000142,
        status_cache="ok",
    )
    fit.components.create(type_id=643, qty_per_fit=1)  # корпус — достаток, без флага
    fit.components.create(type_id=2046, qty_per_fit=1)
    _add_stock(60003760, 643, 3)
    _add_stock(60003761, 643, 3)  # 6 ≥ 5 — корпус не дефицитен
    _add_stock(60003760, 2046, 1)
    _add_stock(60003761, 2046, 0)
    now = timezone.now()
    # Стакан 2046: дешёвый уровень объёмом 1 (ref), дорогой — покрывает остаток дефицита.
    JitaOrderLevel.objects.create(type_id=2046, rank=1, price=Decimal("1"), volume=1, fetched_at=now)
    JitaOrderLevel.objects.create(type_id=2046, rank=2, price=Decimal("5000"), volume=1000, fetched_at=now)
    # Стакан корпуса — нейтральный, чтобы не влиять на проверяемый бейдж.
    JitaOrderLevel.objects.create(type_id=643, rank=1, price=Decimal("100"), volume=100, fetched_at=now)

    content = editor_client.get(reverse("marketmonitoring:fit_detail", args=[fit.pk])).content.decode()
    assert "OVERPRICE" in content


def test_dashboard_denied_without_view_group(db):
    """Аутентифицированный пользователь без группы _view: доступ к index запрещён (§14).

    Покрытие прав не должно держаться только на фикстуре main character:
    проверяется ветка has_view() внутри view.
    """
    user = User.objects.create_user(username="ui_noview", password="x")
    client = Client()
    client.force_login(user)
    response = client.get(reverse("marketmonitoring:index"))
    content = response.content.decode()
    # Дашборд с фитами не рендерится; отказ выглядит как redirect/403 (не 200 с данными).
    assert response.status_code != 200 or "No fits yet" not in content


def test_fit_detail_no_data_banner(viewer_client, fit_with_component):
    """NO_DATA: баннер alert-secondary с причиной вместо расчётов (07-states.md §2)."""
    from ..models import StockLevel

    StockLevel.objects.all().delete()  # данные отсутствуют → пересчёт даёт NO_DATA
    fit_with_component.status_cache = "no_data"
    fit_with_component.save()
    content = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit_with_component.pk])).content.decode()
    assert "alert-secondary" in content
    assert "mm-hull-row" in content  # состав остаётся виден с «—» (02-fit-detail.md §NO_DATA)


def test_fit_detail_edit_buttons_hidden_for_view(viewer_client, fit_with_component):
    """Роль _view: кнопки Edit/Delete не рендерятся вовсе."""
    content = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit_with_component.pk])).content.decode()
    assert reverse("marketmonitoring:fit_update", args=[fit_with_component.pk]) not in content
    assert reverse("marketmonitoring:fit_delete", args=[fit_with_component.pk]) not in content


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def test_components_empty_state_and_cta(viewer_client, editor_client, db):
    """Пустая страница Components: empty state, CTA только для _edit (04-components.md)."""
    viewer_content = viewer_client.get(reverse("marketmonitoring:components")).content.decode()
    assert "No standalone components yet." in viewer_content
    assert reverse("marketmonitoring:component_create") not in viewer_content

    editor_content = editor_client.get(reverse("marketmonitoring:components")).content.decode()
    assert reverse("marketmonitoring:component_create") in editor_content


def test_components_summary_strip(viewer_client, fit_with_component, eve_types):  # noqa: F811
    """Summary strip (04-components.md): сводка «N компонентов с дефицитом · сумма»."""
    from ..models import ComponentRequest

    ComponentRequest.objects.create(type_id=2046, qty_target=99, scope_kind="system", scope_ref=30000142)
    _add_stock(60003760, 2046, 1)
    _add_stock(60003761, 2046, 1)
    content = viewer_client.get(reverse("marketmonitoring:components")).content.decode()
    assert "component with deficit" in content


# ---------------------------------------------------------------------------
# Confirm-экраны
# ---------------------------------------------------------------------------


def test_fit_delete_confirm_screen(editor_client, fit_with_component):
    """Confirm-экран удаления фита: «Delete — cannot be undone» (07-states.md §5)."""
    content = editor_client.get(reverse("marketmonitoring:fit_delete", args=[fit_with_component.pk])).content.decode()
    assert "cannot be undone" in content
    assert "border-danger" in content


def test_component_delete_confirm_screen(editor_client, fit_with_component):
    """Confirm-экран удаления компонента: объяснение последствий (04-components.md)."""
    request = ComponentRequest.objects.create(type_id=2046, qty_target=5, scope_kind="system", scope_ref=30000142)
    content = editor_client.get(reverse("marketmonitoring:component_delete", args=[request.pk])).content.decode()
    assert "cannot be undone" in content
    assert "fits are not affected" in content


def test_location_delete_confirm_screen(editor_client, db):
    """Confirm-экран удаления локации — тот же паттерн (05-locations.md)."""
    from ..models import MarketLocation

    location = MarketLocation.objects.create(
        name="Test station", kind="station", solar_system_id=30000142, region_id=10000002
    )
    content = editor_client.get(reverse("marketmonitoring:location_delete", args=[location.pk])).content.decode()
    assert "cannot be undone" in content
    assert "border-danger" in content
