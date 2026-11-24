"""Тесты правок состава в предпросмотре фита (§7.5, FR-3/FR-4) и tooltip-данных (§6.8)."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

from ..auth_hooks import GROUP_EDIT
from ..models import Fit, FitComponent
from .test_calc_engine import sde_ids  # noqa: F401 — справочник eveuniverse (система 30000142)
from .test_eft_parser import eve_types  # noqa: F401 — фикстура EveType записей

pytestmark = pytest.mark.django_db

EFT = """[Ferox, Edit test]
Damage Control II
Magnetic Field Stabilizer II x2
Antimatter Charge M x1000
"""


@pytest.fixture
def editor_client(db):
    """Клиент под пользователем с правом редактирования."""
    user = User.objects.create_user(username="edit_ui", password="x")
    user.groups.add(Group.objects.get(name=GROUP_EDIT))
    client = Client()
    client.force_login(user)
    return client


def _post(eft_raw: str = EFT, action: str = "parse", **extra) -> dict:
    """POST fit_create action=parse/save с базовыми параметрами (§7.5)."""
    data = {
        "action": action,
        "name": "Edit test",
        "eft_raw": eft_raw,
        "qty_target": 1,
        "scope_kind": "system",
        "scope_ref": 30000142,
        # Строки предпросмотра в порядке разбора: 0=корпус 643, 1=2046, 2=35659, 3=заряд 2411.
        "row0-type_id": "643",
        "row0-qty_per_fit": "1",
        "row1-type_id": "2046",
        "row1-qty_per_fit": "1",
        "row2-type_id": "35659",
        "row2-qty_per_fit": "2",
        "row3-type_id": "2411",
        "row3-qty_per_fit": "1000",
    }
    data.update(extra)
    return data


def test_preview_renders_row_forms(eve_types, editor_client):  # noqa: F811
    """Предпросмотр рендерит правимые строки row<i>-<field> (§7.5, FR-3)."""
    response = editor_client.post(reverse("marketmonitoring:fit_create"), _post())
    content = response.content.decode()
    assert "row0-type_id" in content
    assert "row0-qty_per_fit" in content
    assert "row0-scope_override_kind" in content
    assert "row1-delete" in content


def test_parse_keeps_edits_between_parses(eve_types, editor_client):  # noqa: F811
    """action=parse сохраняет введённые правки между разборами (§7.5, FR-3)."""
    data = _post()
    data["row1-qty_per_fit"] = "7"
    response = editor_client.post(reverse("marketmonitoring:fit_create"), data)
    content = response.content.decode()
    assert 'value="7"' in content  # правка qty пережила повторный разбор
    assert Fit.objects.count() == 0  # parse не сохраняет


def test_save_applies_edits_and_scope_override(eve_types, editor_client):  # noqa: F811
    """action=save применяет правки qty, удаление строки и override области (§7.5, FR-4)."""
    data = _post(action="save")
    data["row1-qty_per_fit"] = "9"  # Damage Control II: qty 1 -> 9
    data["row1-scope_override_kind"] = "system"
    data["row1-scope_override_ref"] = "30000142"
    data["row3-delete"] = "on"  # заряд удалён
    response = editor_client.post(reverse("marketmonitoring:fit_create"), data)
    assert response.status_code == 302, response.content.decode()
    fit = Fit.objects.get(name="Edit test")
    dc = FitComponent.objects.get(fit=fit, type_id=2046)
    assert dc.qty_per_fit == 9
    assert dc.scope_override_kind == "system"
    assert dc.scope_override_ref == 30000142
    assert not FitComponent.objects.filter(fit=fit, type_id=2411).exists()  # строка удалена
    hull = FitComponent.objects.get(fit=fit, type_id=643)
    assert hull.qty_per_fit == 1
    assert FitComponent.objects.get(fit=fit, type_id=35659).qty_per_fit == 2  # без правок


def test_save_rejects_composition_without_components(eve_types, editor_client):  # noqa: F811
    """Валидация сохранения (§7.5): все позиции помимо корпуса удалены — отказ."""
    data = _post(action="save")
    data["row1-delete"] = "on"
    data["row2-delete"] = "on"
    data["row3-delete"] = "on"
    response = editor_client.post(reverse("marketmonitoring:fit_create"), data)
    assert response.status_code == 200
    assert not Fit.objects.filter(name="Edit test").exists()
    assert "At least one component besides the hull is required." in response.content.decode()


def test_save_rejects_zero_qty(eve_types, editor_client):  # noqa: F811
    """Валидация сохранения (§7.5): количество < 1 — отказ."""
    data = _post(action="save")
    data["row1-qty_per_fit"] = "0"
    response = editor_client.post(reverse("marketmonitoring:fit_create"), data)
    assert response.status_code == 200
    assert not Fit.objects.filter(name="Edit test").exists()


def test_save_adds_row_from_type_search(eve_types, editor_client):  # noqa: F811
    """Добавление типа через SDE-селектор (row4-поля): строка попадает в состав (§7.5, FR-3)."""
    data = _post(action="save")
    data["row4-type_id"] = "438"
    data["row4-qty_per_fit"] = "4"
    data["row4-is_charge"] = "false"
    data["row4-raw_line"] = ""
    response = editor_client.post(reverse("marketmonitoring:fit_create"), data)
    assert response.status_code == 302, response.content.decode()
    fit = Fit.objects.get(name="Edit test")
    assert FitComponent.objects.get(fit=fit, type_id=438).qty_per_fit == 4


def test_save_rejects_unknown_added_type(eve_types, editor_client):  # noqa: F811
    """Добавленный тип должен существовать в SDE (eveuniverse, §8)."""
    data = _post(action="save")
    data["row4-type_id"] = "999999"
    data["row4-qty_per_fit"] = "1"
    data["row4-is_charge"] = "false"
    data["row4-raw_line"] = ""
    response = editor_client.post(reverse("marketmonitoring:fit_create"), data)
    assert response.status_code == 200
    assert not Fit.objects.filter(name="Edit test").exists()


def test_save_drops_hull_scope_override(eve_types, editor_client):  # noqa: F811
    """Инвариант §5.3: override области на hull-строке игнорируется при сохранении."""
    data = _post(action="save")
    data["row0-scope_override_kind"] = "system"
    data["row0-scope_override_ref"] = "30000143"
    response = editor_client.post(reverse("marketmonitoring:fit_create"), data)
    assert response.status_code == 302, response.content.decode()
    fit = Fit.objects.get(name="Edit test")
    hull = FitComponent.objects.get(fit=fit, type_id=643)
    assert hull.scope_override_kind is None
    assert hull.scope_override_ref is None


def test_preview_disables_hull_override_fields(eve_types, editor_client):  # noqa: F811
    """UI: у hull-строки поля override disabled с подсказкой (§5.3)."""
    response = editor_client.post(reverse("marketmonitoring:fit_create"), _post())
    content = response.content.decode()
    assert "row0-scope_override_kind" in content
    assert "disabled" in content
    assert "Hull scope follows the fit scope." in content


def test_save_rejects_duplicate_added_type(eve_types, editor_client):  # noqa: F811
    """Добавление типа, который уже есть в предпросмотре, — явная ошибка (без молчаливого слияния)."""
    data = _post(action="save")
    data["row4-type_id"] = "2046"
    data["row4-qty_per_fit"] = "1"
    data["row4-is_charge"] = "false"
    data["row4-raw_line"] = ""
    response = editor_client.post(reverse("marketmonitoring:fit_create"), data)
    assert response.status_code == 200
    assert not Fit.objects.filter(name="Edit test").exists()
    assert "already in the fit" in response.content.decode()


def test_save_added_row_is_charge_from_sde_category(eve_types, editor_client):  # noqa: F811
    """is_charge добавленной строки наследуется от категории SDE-типа (Charge = 16), не от POST."""
    from eveuniverse.models import EveCategory, EveGroup, EveType

    category, _ = EveCategory.objects.get_or_create(id=16, defaults={"name": "Charge", "published": True})
    group, _ = EveGroup.objects.get_or_create(
        id=99, defaults={"name": "Fusion Charge", "published": True, "eve_category": category}
    )
    EveType.objects.get_or_create(
        id=421,
        defaults={"name": "Void M", "published": True, "eve_group": group},
    )
    data = _post(action="save")
    data["row4-type_id"] = "421"
    data["row4-qty_per_fit"] = "500"
    data["row4-is_charge"] = "false"  # клиентская подделка — сервер берёт категорию из SDE
    data["row4-raw_line"] = ""
    response = editor_client.post(reverse("marketmonitoring:fit_create"), data)
    assert response.status_code == 302, response.content.decode()
    fit = Fit.objects.get(name="Edit test")
    assert FitComponent.objects.get(fit=fit, type_id=421).is_charge is True


def test_fit_update_applies_edits(eve_types, editor_client):  # noqa: F811
    """Правка существующего фита: override области сохраняется в FitComponent (§14)."""
    response = editor_client.post(reverse("marketmonitoring:fit_create"), _post(action="save"))
    assert response.status_code == 302
    fit = Fit.objects.get(name="Edit test")
    data = _post(action="save")
    data["row1-scope_override_kind"] = "system"
    data["row1-scope_override_ref"] = "30000142"
    response = editor_client.post(reverse("marketmonitoring:fit_update", args=[fit.pk]), data)
    assert response.status_code == 302, response.content.decode()
    assert FitComponent.objects.get(fit=fit, type_id=2046).scope_override_kind == "system"


def test_fit_detail_multibuy_data_url_and_bottleneck(editor_client, eve_types, sde_ids):  # noqa: F811
    """Карточка фита: data-url у кнопки multibuy и бейдж узкого места (FR-5)."""
    from .test_views_ui import _add_stock

    fit = Fit.objects.create(
        name="Detail fit",
        hull_type_id=643,
        qty_target=2,
        scope_kind="system",
        scope_ref=30000142,
        status_cache="ok",
    )
    fit.components.create(type_id=643, qty_per_fit=1)
    fit.components.create(type_id=2046, qty_per_fit=1)
    _add_stock(60003760, 643, 2)
    _add_stock(60003761, 643, 2)
    _add_stock(60003760, 2046, 2)
    _add_stock(60003761, 2046, 2)
    content = editor_client.get(reverse("marketmonitoring:fit_detail", args=[fit.pk])).content.decode()
    assert f'data-url="{reverse("marketmonitoring:export_multibuy", args=[fit.pk])}"' in content
    assert "Bottleneck" in content


def test_fit_detail_overprice_tooltip_pct(editor_client, eve_types, sde_ids):  # noqa: F811
    """Tooltip бейджа OVERPRICE содержит unit, ref и процент превышения (§6.8)."""
    from django.utils import timezone

    from ..models import JitaOrderLevel
    from .test_views_ui import _add_stock

    fit = Fit.objects.create(
        name="Overprice pct fit",
        hull_type_id=643,
        qty_target=5,
        scope_kind="system",
        scope_ref=30000142,
        status_cache="ok",
    )
    fit.components.create(type_id=643, qty_per_fit=1)
    fit.components.create(type_id=2046, qty_per_fit=1)
    _add_stock(60003760, 643, 3)
    _add_stock(60003761, 643, 3)
    _add_stock(60003760, 2046, 1)
    _add_stock(60003761, 2046, 0)
    now = timezone.now()
    JitaOrderLevel.objects.create(type_id=2046, rank=1, price=Decimal("1"), volume=1, fetched_at=now)
    JitaOrderLevel.objects.create(type_id=2046, rank=2, price=Decimal("5000"), volume=1000, fetched_at=now)
    JitaOrderLevel.objects.create(type_id=643, rank=1, price=Decimal("100"), volume=100, fetched_at=now)

    content = editor_client.get(reverse("marketmonitoring:fit_detail", args=[fit.pk])).content.decode()
    assert "OVERPRICE" in content
    assert "+499900.00%" in content  # unit = 5000 (лот rank2 покрывает дефицит целиком), ref = 1


def test_components_scope_label(eve_types, sde_ids, editor_client):  # noqa: F811
    """Страница Components: человекочитаемое имя области вместо сырого kind+ref (§14)."""
    from ..models import ComponentRequest

    ComponentRequest.objects.create(type_id=2046, qty_target=5, scope_kind="system", scope_ref=30000142)
    content = editor_client.get(reverse("marketmonitoring:components")).content.decode()
    assert "system 30000142" not in content  # подпись резолвится из SDE через eveuniverse
