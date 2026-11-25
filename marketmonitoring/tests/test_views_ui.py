"""Тесты UI-слоя (дизайн-спека docs/design/): семантическая палитра, классы утилит, состояния, роли.

Тесты без сети: только БД плагина и справочники eveuniverse.
"""

import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

from ..auth_hooks import GROUP_EDIT, GROUP_VIEW
from ..models import Fit
from ..views import dashboard as dashboard_view
from .test_calc_engine import (  # noqa: F401 — справочники eveuniverse (система/станции)
    sde_ids,
)
from .test_eft_parser import eve_types  # noqa: F401 — записи EveType
from .test_ships_availability import (
    _no_esi,
    _seed_full_kit_contracts,
    _seed_reference_market,
)

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


def _dashboard_cell(html, column_index):
    """Ячейка <td> первой строки таблицы фитов; индекс — без колонки чекбокса."""
    tbody = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    first_row = tbody.split("<tr>", 1)[1].split("</tr>", 1)[0]
    return re.findall(r"<td[^>]*>(.*?)</td>", first_row, flags=re.S)[column_index + 1]


def test_dashboard_uses_semantic_bootstrap_badges(viewer_client, fit_with_component):
    """Палитра статусов — только семантические токены Bootstrap (README §1), без жёстких hex-классов."""
    response = viewer_client.get(reverse("marketmonitoring:index"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "text-bg-success" in content  # статус ok
    assert "mm-badge-ok" not in content  # старые hex-классы удалены
    assert "mm-progress" in content  # тонкий прогресс в колонках C/K и Collectability (C+K)
    assert "mm-num" in content  # числовая типографика
    assert "marketmonitoring/css/mm.css" in content  # utility-CSS подключён


def test_dashboard_hidden_edit_elements_for_view_role(viewer_client, fit_with_component):
    """Роль _view: кнопки edit скрыты — «Refresh now» и «+ New fit» (07-states.md §4)."""
    content = viewer_client.get(reverse("marketmonitoring:index")).content.decode()
    assert '<button type="submit" id="mm-refresh-btn"' not in content
    assert "New fit" not in content


def test_dashboard_collectability_is_c_plus_k(viewer_client, sde_ids):  # noqa: F811
    """Collectability показывает C+K и цвет общего статуса, а не рыночный K (решение №40)."""
    from ..calc import service as calc_service

    fit = _seed_reference_market(sde_ids, name="Collectability C+K", qty_target=20)
    _seed_full_kit_contracts(fit, 3)  # C=3, K=10 -> C+K=13
    calc_service.recalc_fit(fit)

    with _no_esi():
        html = viewer_client.get(reverse("marketmonitoring:index")).content.decode()

    assert "Collectability (C+K)" in html
    assert "Ships total (C+K)" not in html
    cell = _dashboard_cell(html, 1)
    cell_text = " ".join(re.sub(r"<[^>]+>", " ", cell).split())
    assert cell_text.startswith("13 of 20")
    assert "10 of 20" not in cell_text
    assert "width: 65%" in cell
    assert "bg-warning" in cell


def test_dashboard_collectability_no_data_is_dash(viewer_client, sde_ids):  # noqa: F811
    """Без данных контрактов Collectability — «—», без прогресса (решение №43)."""
    from ..calc import service as calc_service

    fit = _seed_reference_market(sde_ids, name="Collectability no data", qty_target=20)
    calc_service.recalc_fit(fit)  # нет ContractProvider -> C=None
    fit.refresh_from_db()
    assert fit.contracts_available is None

    with _no_esi():
        html = viewer_client.get(reverse("marketmonitoring:index")).content.decode()
    cell = _dashboard_cell(html, 1)
    assert "&mdash;" in cell
    assert "progress" not in cell
    assert "width:" not in cell


def test_dashboard_has_no_total_to_buy_or_overprice_columns(viewer_client, fit_with_component):
    """Столбцы «Total to buy» и «Overprice» удалены из таблицы дашборда."""
    content = viewer_client.get(reverse("marketmonitoring:index")).content.decode()
    assert "Total to buy" not in content
    assert "Overprice" not in content
    tbody = content.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    first_row = tbody.split("<tr>", 1)[1].split("</tr>", 1)[0]
    # Колонка чекбокса сводной закупки добавила одну ячейку к уже существующим девяти.
    assert len(re.findall(r"<td\b", first_row)) == 10


def test_dashboard_ships_availability_without_n_plus_one(viewer_client, sde_ids, monkeypatch):  # noqa: F811
    """Сводка C+K читается ровно один раз на фит, без повторных вызовов в цикле (§22.4)."""
    from ..constants import ScopeKind

    fits = [
        Fit.objects.create(
            name=f"N+1 fit {index}",
            hull_type_id=1,
            qty_target=5,
            scope_kind=ScopeKind.STATION,
            scope_ref=60003760,
        )
        for index in range(3)
    ]
    original = dashboard_view.get_ships_availability
    calls = []

    def counting_get_ships_availability(fit):
        calls.append(fit.pk)
        return original(fit)

    monkeypatch.setattr(dashboard_view, "get_ships_availability", counting_get_ships_availability)
    with _no_esi():
        response = viewer_client.get(reverse("marketmonitoring:index"))

    assert response.status_code == 200
    assert sorted(calls) == sorted(fit.pk for fit in fits)


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


def _enable_jita_location():
    """Явно включить локацию Jita: стакан читается только при enabled=True (§12.1)."""
    from django.utils import timezone

    from ..constants import LocationKind, SyncStatus
    from ..models import MarketLocation, PluginSetting

    settings_obj = PluginSetting.load()
    MarketLocation.objects.update_or_create(
        kind=LocationKind.STATION,
        station_id=settings_obj.jita_station_id,
        defaults={
            "name": "Jita IV-4",
            "solar_system_id": 30000142,
            "region_id": settings_obj.jita_region_id,
            "enabled": True,
            "sync_status": SyncStatus.OK,
            "last_synced_at": timezone.now(),
        },
    )


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
    _enable_jita_location()  # без включённой локации стакан не читается
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


def test_disabled_jita_shows_dash_instead_of_zero(viewer_client, fit_with_component):  # noqa: F811
    """Выключенная Jita: дашборд и карточка показывают &mdash;, а не «0 ISK»."""
    from ..calc import service as calc_service
    from ..constants import FitStatus

    result = calc_service.recalc_fit(fit_with_component)
    fit_with_component.refresh_from_db()
    assert result.status == FitStatus.OK
    assert result.k == 4
    assert result.total_cost is None and fit_with_component.total_cost is None

    index_content = viewer_client.get(reverse("marketmonitoring:index")).content.decode()
    detail_url = reverse("marketmonitoring:fit_detail", args=[fit_with_component.pk])
    detail_content = viewer_client.get(detail_url).content.decode()
    assert "&mdash;" in index_content
    assert "&mdash;" in detail_content
    assert "0 ISK" not in index_content
    assert "0 ISK" not in detail_content


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


def test_fit_detail_no_data_banner_for_empty_scope(viewer_client, eve_types, sde_ids):  # noqa: F811
    """NO_DATA: баннер alert-secondary только при пустой области расчёта (§6.4, решение №45)."""
    from eveuniverse.models import EveConstellation, EveSolarSystem

    empty_system = EveSolarSystem.objects.create(
        id=30000144,
        name="Empty system",
        eve_constellation=EveConstellation.objects.get(id=20000001),
        security_status=0.9,
    )
    fit = Fit.objects.create(
        name="Empty scope fit",
        hull_type_id=643,
        qty_target=2,
        scope_kind="system",
        scope_ref=empty_system.id,
        status_cache="no_data",
    )
    fit.components.create(type_id=643, qty_per_fit=1)

    response = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit.pk]))
    content = response.content.decode()
    assert response.context["status"] == "no_data"
    assert response.context["k"] is None
    assert "alert-secondary" in content
    assert "Calculation scope is empty" in content
    assert "mm-hull-row" in content  # состав остаётся виден с «—» (02-fit-detail.md §NO_DATA)


def test_fit_detail_data_warning_is_not_no_data(viewer_client, fit_with_component):
    """Локация без данных: отдельное предупреждение, статус/дефицит считаются (§19.21)."""
    from ..models import StockLevel

    StockLevel.objects.all().delete()
    response = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit_with_component.pk]))
    content = response.content.decode()
    assert response.context["status"] != "no_data"
    assert response.context["data_warnings"] is True
    assert response.context["k"] == 0
    assert "alert-warning" in content
    assert "Calculation scope is empty" not in content  # NO_DATA-баннер не показан
    assert "no StockLevel aggregate" in content  # причина с id локации остаётся видимой


def test_fit_detail_edit_buttons_hidden_for_view(viewer_client, fit_with_component):
    """Роль _view: кнопки Edit/Delete не рендерятся вовсе."""
    content = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit_with_component.pk])).content.decode()
    assert reverse("marketmonitoring:fit_update", args=[fit_with_component.pk]) not in content
    assert reverse("marketmonitoring:fit_delete", args=[fit_with_component.pk]) not in content


# ---------------------------------------------------------------------------
# Confirm-экраны
# ---------------------------------------------------------------------------


def test_fit_delete_confirm_screen(editor_client, fit_with_component):
    """Confirm-экран удаления фита: «Delete — cannot be undone» (07-states.md §5)."""
    content = editor_client.get(reverse("marketmonitoring:fit_delete", args=[fit_with_component.pk])).content.decode()
    assert "cannot be undone" in content
    assert "border-danger" in content


def test_location_delete_confirm_screen(editor_client, db):
    """Confirm-экран удаления локации — тот же паттерн (05-locations.md)."""
    from ..models import MarketLocation

    location = MarketLocation.objects.create(
        name="Test station", kind="station", solar_system_id=30000142, region_id=10000002
    )
    content = editor_client.get(reverse("marketmonitoring:location_delete", args=[location.pk])).content.decode()
    assert "cannot be undone" in content
    assert "border-danger" in content
