"""Тесты EFT-парсера (§18.1, §7): английский и русский экспорт, заряды, дубликаты, ошибки.

Тесты без сети: нужные записи EveType создаются в тестовой БД напрямую.
"""

import pytest
from django.apps import apps

from ..eft.parser import normalize_name, parse_eft, resolve_type_name

EveType = apps.get_model("eveuniverse", "EveType")
EveGroup = apps.get_model("eveuniverse", "EveGroup")
EveCategory = apps.get_model("eveuniverse", "EveCategory")

# Стандартный английский экспорт (пример §7.2).
EN_EFT = """[Ferox, Fleet Ferox]
Damage Control II
Magnetic Field Stabilizer II
Magnetic Field Stabilizer II

10MN Afterburner II
Warp Scrambler II

Heavy Neutron Blaster II, Antimatter Charge M
Heavy Neutron Blaster II, Antimatter Charge M

Medium Core Defense Field Extender I

Hobgoblin II x5

Antimatter Charge M x1000"""

# Русский экспорт: корпус и модули на русском (имена создаются в БД тестом).
RU_EFT = """[Ферокс, Фарм Ферокс]
Дамаг-контроль II
Магнитный стабилизатор II x2

Хобгоблин II x5"""


@pytest.fixture
def eve_types(db):
    """Создание EveType-записей для обоих языков (без сети).

    Русские имена создаются как отдельные EveType с id=1000000+len(name):
    fallback-резолв по EveType.name находит их без модели локализованных имён
    (в установленной django-eveuniverse модели локализованных имён нет).
    """
    category, _ = EveCategory.objects.get_or_create(id=6, defaults={"name": "Ship", "published": True})
    group, _ = EveGroup.objects.get_or_create(
        id=1, defaults={"name": "Frigate", "published": True, "eve_category": category}
    )
    created = {}
    en = {
        643: "Ferox",
        2046: "Damage Control II",
        35659: "Magnetic Field Stabilizer II",
        438: "10MN Afterburner II",
        5246: "Warp Scrambler II",
        1196: "Heavy Neutron Blaster II",
        2411: "Antimatter Charge M",
        31362: "Medium Core Defense Field Extender I",
        2444: "Hobgoblin II",
    }
    ru = {
        643: "Ферокс",
        2046: "Дамаг-контроль II",
        35659: "Магнитный стабилизатор II",
        2444: "Хобгоблин II",
    }
    for type_id, name in en.items():
        created[name] = EveType.objects.create(id=type_id, name=name, published=True, eve_group=group)
    # Модель локализованных имён в установленной django-eveuniverse отсутствует,
    # поэтому русские имена — отдельные EveType-записи (fallback EveType.name).
    for name in ru.values():
        EveType.objects.create(id=1000000 + len(name), name=name, published=True, eve_group=group)
    return created


def _items_by_type(parsed):
    return {item.type_id: item for item in parsed.items}


@pytest.mark.django_db
def test_parse_english_export(eve_types):
    """Английский экспорт §7.2: заголовок, дубликаты суммируются, xN у дронов и зарядов."""
    parsed = parse_eft(EN_EFT)
    assert parsed.hull_type_id == 643
    assert parsed.fit_name == "Fleet Ferox"
    assert parsed.errors == []
    assert parsed.is_valid

    items = _items_by_type(parsed)
    # Дубликаты Magnetic Field Stabilizer II суммировались: 2 (§7.3.7).
    assert items[35659].qty_per_fit == 2
    # Дубликаты модулей с зарядами: модуль x2, заряд — отдельная позиция x2 (§7.3.4, §7.3.7).
    assert items[1196].qty_per_fit == 2
    assert items[1196].is_charge is False
    assert items[2411].qty_per_fit == 2 + 1000  # 2 от строк с зарядами + 1000 отдельной строкой
    assert items[2411].is_charge is True
    # Дрон с x5.
    assert items[2444].qty_per_fit == 5
    # Пустые строки/секции игнорируются (§7.3.3).
    assert all(item.type_id is not None for item in parsed.items)


@pytest.mark.django_db
def test_parse_russian_export(eve_types):
    """Русский экспорт: резолв имён по русским именам EveType (§7.3.8), корпус и состав."""
    parsed = parse_eft(RU_EFT)
    assert parsed.errors == []
    # Корпус «Ферокс» разрешён (русская запись EveType) и включён в состав
    # первой позицией с qty_per_fit=1, is_charge=False (§6.3, §5.3).
    hull_id = resolve_type_name("Ферокс")
    assert parsed.hull_type_id == hull_id
    assert parsed.is_valid
    assert parsed.items[0].type_id == hull_id
    assert parsed.items[0].qty_per_fit == 1
    assert parsed.items[0].is_charge is False
    assert parsed.items[0].is_hull is True
    dc_id = resolve_type_name("Дамаг-контроль II")
    mfs_id = resolve_type_name("Магнитный стабилизатор II")
    drone_id = resolve_type_name("Хобгоблин II")
    items_by_type = {item.type_id: item for item in parsed.items}
    assert items_by_type[dc_id].qty_per_fit == 1  # Дамаг-контроль II
    assert items_by_type[mfs_id].qty_per_fit == 2  # Магнитный стабилизатор II x2
    assert items_by_type[drone_id].qty_per_fit == 5  # Хобгоблин II x5
    assert all(not item.is_charge for item in parsed.items[1:])


@pytest.mark.django_db
def test_resolve_type_name_non_english_fallback(eve_types):
    """resolve_type_name находит тип по не-английскому имени через fallback EveType.name."""
    assert resolve_type_name("Ферокс") == 1000000 + len("Ферокс")
    assert resolve_type_name("Дамаг-контроль II") == 1000000 + len("Дамаг-контроль II")
    assert resolve_type_name("Хобгоблин II") == 1000000 + len("Хобгоблин II")
    assert resolve_type_name("  Магнитный   стабилизатор II ") == 1000000 + len("Магнитный стабилизатор II")
    assert resolve_type_name("Нет такого типа") is None


@pytest.mark.django_db
def test_parse_hull_merges_with_duplicate(eve_types):
    """Корпус, встречающийся и в items (экзотика), суммируется: 1 + qty (UNIQUE(fit, type_id))."""
    text = "[Ferox, Dup hull]\nFerox\nDamage Control II"
    parsed = parse_eft(text)
    assert parsed.errors == []
    items = {item.type_id: item for item in parsed.items}
    assert items[643].qty_per_fit == 2
    assert items[643].is_hull is True
    assert parsed.items[0].type_id == 643  # корпус — первая строка


@pytest.mark.django_db
def test_parse_empty_slots_ignored(eve_types):
    """Пустые слоты [Empty ...] игнорируются (§7.3.2)."""
    text = "[Ferox, Empty test]\n\n[Empty High slot]\n[Empty Low slot]\nDamage Control II\n[Empty Rig slot]"
    parsed = parse_eft(text)
    # Корпус — первая позиция состава (§6.3), плюс единственная модульная строка.
    assert len(parsed.items) == 2
    assert parsed.items[0].type_id == 643 and parsed.items[0].is_hull
    assert parsed.items[1].type_id == 2046
    assert parsed.errors == []


@pytest.mark.django_db
def test_unrecognized_line_error(eve_types):
    """Нераспознанная строка -> ошибка с текстом строки и её номером (§7.3.10)."""
    text = "[Ferox, Err fit]\nDamage Control II\nTotally Unknown Module Name"
    parsed = parse_eft(text)
    assert len(parsed.errors) == 1
    error = parsed.errors[0]
    assert error.line_no == 3
    assert "Totally Unknown Module Name" in error.line
    # Нераспознанный корпус -> отказ (парсинг продолжается, но is_valid False).
    text2 = "[Unknown Hull XYZ, Fit]\nDamage Control II"
    parsed2 = parse_eft(text2)
    assert parsed2.hull_type_id is None
    assert not parsed2.is_valid
    assert any("Unknown Hull XYZ" in e.message for e in parsed2.errors)


@pytest.mark.django_db
def test_missing_header_rejected(eve_types):
    """Без заголовка — отказ (§7.3.1)."""
    parsed = parse_eft("Damage Control II")
    assert parsed.hull_type_id is None
    assert parsed.errors
    assert parsed.fit_name == ""
    # Пустой текст тоже ошибка.
    parsed2 = parse_eft("")
    assert parsed2.errors


@pytest.mark.django_db
def test_normalize_name(eve_types):
    """Нормализация пробелов (§7.3.8)."""
    assert normalize_name("  Damage   Control  II ") == "Damage Control II"


@pytest.mark.django_db
def test_resolve_type_name_priority(eve_types):
    """Резолв точный без регистра, приоритет en (§7.3.8)."""
    assert resolve_type_name("ferox") == 643
    assert resolve_type_name("  ferox  ") == 643
    assert resolve_type_name("No Such Type") is None
