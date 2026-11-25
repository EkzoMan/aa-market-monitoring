"""Тесты этапа 7: экспорт buyback (§14.3, §18.8).

Без сети: данные берутся из кэша БД плагина (StockLevel / JitaOrderLevel),
который сеется напрямую. Проверяются: multibuy (только D_i>0, английские
имена, табы, сортировка case-insensitive), CSV (заголовок, сумма cost ==
Fit.total_cost, UTF-8 без BOM), txt (Content-Type, имя файла), права доступа.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ..auth_hooks import GROUP_VIEW
from ..calc.service import recalc_fit
from ..constants import LocationKind, SyncStatus
from ..models import Fit, FitComponent, JitaOrderLevel, MarketLocation, StockLevel
from ..views.exports import calc_export_rows
from .test_eft_parser import eve_types  # noqa: F401 — фикстура EveType записей

pytestmark = pytest.mark.django_db

STATION_ID = 60003760
SYSTEM_ID = 30000142
REGION_ID = 10000002


@pytest.fixture
def viewer_client(db):
    """Клиент под пользователем с view-правом."""
    user = User.objects.create_user(username="viewer", password="x")
    user.groups.add(Group.objects.get(name=GROUP_VIEW))
    client = Client()
    client.force_login(user)
    return client


def _seed_market(db):
    """Свежий станционный кэш: MarketLocation + StockLevel + JitaOrderLevel."""
    now = timezone.now()
    MarketLocation.objects.create(
        name="Jita IV-4",
        kind=LocationKind.STATION,
        station_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        enabled=True,
        sync_status=SyncStatus.OK,
        last_synced_at=now,
    )
    # Damage Control II: доступно 1, требуется 2 → дефицит 1.
    StockLevel.objects.create(
        location_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=2046,
        total_sell_volume=1,
        best_sell=Decimal("100"),
        updated_at=now,
    )
    # Корпус Ferox: доступно 2, требуется 2 → дефицита нет.
    StockLevel.objects.create(
        location_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=643,
        total_sell_volume=2,
        best_sell=Decimal("10000000"),
        updated_at=now,
    )
    JitaOrderLevel.objects.create(type_id=2046, rank=1, price=Decimal("100"), volume=1000, fetched_at=now)


def _deficit_fit(db) -> Fit:
    """Фит с дефицитом по одному компоненту (scope: станция)."""
    fit = Fit.objects.create(
        name="Buyback test",
        hull_type_id=643,
        eft_raw="[Ferox, Buyback test]\nDamage Control II",
        qty_target=2,
        scope_kind="station",
        scope_ref=STATION_ID,
        status_cache="no_data",
    )
    FitComponent.objects.create(fit=fit, type_id=643, qty_per_fit=1)
    FitComponent.objects.create(fit=fit, type_id=2046, qty_per_fit=1)
    return fit


def test_export_txt_format(eve_types, viewer_client, db):  # noqa: F811
    """txt: только D_i>0, английские имена, табы, сортировка case-insensitive (§14.3)."""
    _seed_market(db)
    fit = _deficit_fit(db)
    # Второй дефицитный компонент с именем в нижнем регистре — для сортировки.
    FitComponent.objects.create(fit=fit, type_id=2444, qty_per_fit=1)
    StockLevel.objects.filter(location_id=STATION_ID, type_id=2444).delete()
    StockLevel.objects.create(
        location_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=2444,
        total_sell_volume=0,
        best_sell=None,
        updated_at=timezone.now(),
    )
    JitaOrderLevel.objects.create(type_id=2444, rank=1, price=Decimal("50"), volume=1000, fetched_at=timezone.now())

    response = viewer_client.get(reverse("marketmonitoring:export_txt", args=[fit.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert response["Content-Disposition"] == f'attachment; filename="fit_{fit.pk}_buyback.txt"'
    body = response.content.decode("utf-8")
    lines = body.strip().split("\n")
    # Только дефицитные позиции: корпус (D=0) отсутствует.
    assert all("\t" in line for line in lines)
    assert not any(line.startswith("Ferox") for line in lines)
    # Сортировка case-insensitive: Hobgoblin II < damage control ii → по lower().
    names = [line.split("\t")[0].lower() for line in lines]
    assert names == sorted(names)
    assert lines == ["Damage Control II\t1", "Hobgoblin II\t2"]


def test_export_csv_headers_and_total(eve_types, viewer_client, db):  # noqa: F811
    """CSV: заголовок, сумма cost == Fit.total_cost (та же формула §6.9), UTF-8 без BOM."""
    _seed_market(db)
    fit = _deficit_fit(db)
    recalc_fit(fit)
    fit.refresh_from_db()

    response = viewer_client.get(reverse("marketmonitoring:export_csv", args=[fit.pk]))
    assert response.status_code == 200
    assert response["Content-Disposition"] == f'attachment; filename="fit_{fit.pk}_buyback.csv"'
    # UTF-8 без BOM: первые байты не EF BB BF.
    assert not response.content.startswith(b"\xef\xbb\xbf")
    text = response.content.decode("utf-8")
    lines = text.strip().split("\n")
    assert lines[0] == "type_name,type_id,qty,unit_price,cost"
    rows = [line.split(",") for line in lines[1:]]
    assert len(rows) == 1
    assert rows[0][0] == "Damage Control II"
    assert rows[0][1] == "2046"
    assert rows[0][2] == "1"
    # Все денежные значения — целые ISK.
    int(rows[0][3])
    cost_sum = sum(int(row[4]) for row in rows)
    assert cost_sum == fit.total_cost


def test_export_csv_not_covered_shortfall_column(eve_types, viewer_client, db):  # noqa: F811
    """NOT_COVERED: qty = covered, отдельная колонка shortfall (§6.6, §14.3)."""
    _seed_market(db)
    fit = _deficit_fit(db)
    # Стакан покрывает только 5 из 10 требуемых → NOT_COVERED.
    JitaOrderLevel.objects.filter(type_id=2046).update(volume=5)
    StockLevel.objects.filter(type_id=2046).update(total_sell_volume=0)
    fit.qty_target = 10
    fit.save(update_fields=["qty_target"])

    response = viewer_client.get(reverse("marketmonitoring:export_csv", args=[fit.pk]))
    text = response.content.decode("utf-8")
    lines = text.strip().split("\n")
    assert lines[0] == "type_name,type_id,qty,unit_price,cost,shortfall"
    row = lines[1].split(",")
    assert row[2] == "5"  # qty = covered
    assert row[5] == "5"  # shortfall = D - covered


def test_export_multibuy_endpoint(eve_types, viewer_client, db):  # noqa: F811
    """JSON-эндпоинт multibuy: текст на сервере, табы; пустой список → empty=true."""
    _seed_market(db)
    fit = _deficit_fit(db)
    response = viewer_client.get(reverse("marketmonitoring:export_multibuy", args=[fit.pk]))
    data = response.json()
    assert data["empty"] is False
    assert data["text"] == "Damage Control II\t1\n"
    assert data["count"] == 1

    # Нет дефицита → пустой список и сообщение «нет позиций» на клиенте.
    fit2 = Fit.objects.create(
        name="Empty fit",
        hull_type_id=643,
        eft_raw="[Ferox, Empty fit]",
        qty_target=1,
        scope_kind="station",
        scope_ref=STATION_ID,
    )
    FitComponent.objects.create(fit=fit2, type_id=643, qty_per_fit=1)
    response = viewer_client.get(reverse("marketmonitoring:export_multibuy", args=[fit2.pk]))
    data = response.json()
    assert data["empty"] is True
    assert data["text"] == ""


def test_calc_export_rows_empty_when_jita_disabled(eve_types, db):  # noqa: F811
    """Выключенная Jita: закупка недоступна — calc_export_rows возвращает [].

    Локальный дефицит и StockLevel остаются свежими: пустой экспорт вызван
    именно отсутствием цены докупки, а не NO_DATA рыночных локаций.
    """
    _seed_market(db)
    fit = _deficit_fit(db)
    MarketLocation.objects.filter(station_id=STATION_ID).update(enabled=False)

    assert calc_export_rows(fit) == []


def test_export_empty_deficit_files(eve_types, viewer_client, db):  # noqa: F811
    """Пустой список дефицита: txt/csv с пустым списком (§14.3)."""
    _seed_market(db)
    fit = _deficit_fit(db)
    StockLevel.objects.filter(type_id=2046).update(total_sell_volume=100)
    response = viewer_client.get(reverse("marketmonitoring:export_txt", args=[fit.pk]))
    assert response.content.decode("utf-8") == ""
    response = viewer_client.get(reverse("marketmonitoring:export_csv", args=[fit.pk]))
    assert response.content.decode("utf-8").strip() == "type_name,type_id,qty,unit_price,cost"


def test_export_empty_scope_is_no_data(eve_types, viewer_client, db):  # noqa: F811
    """Пустой scope (нет станций/структур области) = NO_DATA — экспорт пуст (§6.1, §6.7).

    Единое поведение с fit_detail и recalc_fit: пустая область — нет данных,
    дефицит/стоимость не считаются (§6.4).
    """
    fit = Fit.objects.create(
        name="Empty scope fit",
        hull_type_id=643,
        eft_raw="[Ferox, Empty scope fit]\nDamage Control II",
        qty_target=2,
        scope_kind="system",
        scope_ref=30099999,  # системы нет в SDE → станций области нет, структур нет
        status_cache="no_data",
    )
    FitComponent.objects.create(fit=fit, type_id=643, qty_per_fit=1)
    FitComponent.objects.create(fit=fit, type_id=2046, qty_per_fit=1)

    response = viewer_client.get(reverse("marketmonitoring:export_txt", args=[fit.pk]))
    assert response.status_code == 200
    assert response.content.decode("utf-8") == ""
    response = viewer_client.get(reverse("marketmonitoring:export_csv", args=[fit.pk]))
    assert response.content.decode("utf-8").strip() == "type_name,type_id,qty,unit_price,cost"


def test_export_requires_view(eve_types, client, db):  # noqa: F811
    """Экспорт доступен только с view-правом; аноним → redirect на логин (§14)."""
    _seed_market(db)
    fit = _deficit_fit(db)
    response = client.get(reverse("marketmonitoring:export_txt", args=[fit.pk]))
    assert response.status_code == 302
    response = client.get(reverse("marketmonitoring:export_csv", args=[fit.pk]))
    assert response.status_code == 302
