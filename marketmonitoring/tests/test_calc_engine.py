"""Тесты расчётного ядра (этап 4): чистые функции §6 и сервис пересчёта (§18.2–18.4).

Чистые функции тестируются без БД; scope/статусные тесты — через сервис с
фикстурами eveuniverse (без сети).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from marketmonitoring.calc import engine
from marketmonitoring.calc import service as calc_service
from marketmonitoring.calc.engine import ComponentInput, OrderBookRow
from marketmonitoring.constants import FitStatus, LocationKind, ScopeKind, SyncStatus
from marketmonitoring.models import (
    ComponentRequest,
    Fit,
    FitComponent,
    JitaOrderLevel,
    MarketLocation,
    PluginSetting,
    StockLevel,
)

D = Decimal


@pytest.fixture(autouse=True)
def _reset_packaged_volumes_cache():
    """Сброс кеша packaged_volume между тестами (кеш модульный, recalc_all
    сбрасывает его в продакшене; в тестах сбрасываем на границе теста)."""
    calc_service._packaged_volumes_cache = None
    yield
    calc_service._packaged_volumes_cache = None


# ---------------------------------------------------------------------------
# Фикстуры SDE (eveuniverse) — без сети
# ---------------------------------------------------------------------------


@pytest.fixture
def sde_ids(db):
    """Минимальные справочники eveuniverse: система/регион/станции/тип (§6.1)."""
    from eveuniverse.models import (
        EveConstellation,
        EveRegion,
        EveSolarSystem,
        EveStation,
        EveType,
    )

    region = EveRegion.objects.create(id=10000002, name="The Forge")
    other_region = EveRegion.objects.create(id=10000001, name="Other Region")
    constellation = EveConstellation.objects.create(id=20000001, name="Const", eve_region=region)
    other_constellation = EveConstellation.objects.create(id=20000002, name="OtherConst", eve_region=other_region)
    system = EveSolarSystem.objects.create(
        id=30000142, name="Jita", eve_constellation=constellation, security_status=0.9
    )
    other_system = EveSolarSystem.objects.create(
        id=30000143, name="Perse", eve_constellation=other_constellation, security_status=0.5
    )
    group = None
    from eveuniverse.models import EveCategory, EveGroup

    category, _ = EveCategory.objects.get_or_create(id=6, defaults={"name": "Ship", "published": True})
    group, _ = EveGroup.objects.get_or_create(
        id=1, defaults={"name": "ShipGroup", "eve_category": category, "published": True}
    )
    # Имя не должно совпадать с «Ferox» из фикстуры eve_types (test_eft_parser):
    # иначе resolve_type_name находит дубликат и корпус фита резолвится в id=1.
    eve_type = EveType.objects.create(
        id=1,
        name="Ferox Test Hull",
        eve_group=group,
        published=True,
        packaged_volume=15000,
        volume=15000,
    )
    EveType.objects.create(id=2, name="Module II", eve_group=group, published=True, packaged_volume=5, volume=5)
    EveType.objects.create(
        id=3, name="Charge M", eve_group=group, published=True, packaged_volume=D("0.0125"), volume=D("0.0125")
    )
    station = EveStation.objects.create(
        id=60003760,
        name="Jita IV-4",
        eve_solar_system=system,
        eve_type=eve_type,
        max_dockable_ship_volume=0,
        office_rental_cost=0,
        reprocessing_efficiency=0,
        reprocessing_stations_take=0,
    )
    station2 = EveStation.objects.create(
        id=60003761,
        name="Other",
        eve_solar_system=system,
        eve_type=eve_type,
        max_dockable_ship_volume=0,
        office_rental_cost=0,
        reprocessing_efficiency=0,
        reprocessing_stations_take=0,
    )
    station3 = EveStation.objects.create(
        id=60003762,
        name="Far",
        eve_solar_system=other_system,
        eve_type=eve_type,
        max_dockable_ship_volume=0,
        office_rental_cost=0,
        reprocessing_efficiency=0,
        reprocessing_stations_take=0,
    )
    return {
        "region": region,
        "system": system,
        "other_system": other_system,
        "type": eve_type,
        "stations": [station, station2, station3],
    }


def _make_fit(n=30, scope_kind=ScopeKind.STATION, scope_ref=60003760, name="Fleet Ferox") -> Fit:
    return Fit.objects.create(name=name, hull_type_id=1, qty_target=n, scope_kind=scope_kind, scope_ref=scope_ref)


def _add_stock(location_id, type_id, volume, region_id=10000002, system_id=30000142):
    return StockLevel.objects.create(
        location_id=location_id,
        solar_system_id=system_id,
        region_id=region_id,
        type_id=type_id,
        total_sell_volume=volume,
        updated_at=timezone.now(),
    )


def _make_location_fresh(location_id, kind=LocationKind.STATION, system_id=30000142, region_id=10000002, synced=True):
    existing = MarketLocation.objects.filter(
        kind=kind,
        station_id=location_id if kind == LocationKind.STATION else None,
        structure_id=location_id if kind == LocationKind.STRUCTURE else None,
    ).first()
    if existing:
        return existing
    return MarketLocation.objects.create(
        name=f"Loc {location_id}",
        kind=kind,
        station_id=location_id if kind == LocationKind.STATION else None,
        structure_id=location_id if kind == LocationKind.STRUCTURE else None,
        solar_system_id=system_id,
        region_id=region_id,
        enabled=True,
        sync_status=SyncStatus.OK,
        last_synced_at=timezone.now() if synced else None,
    )


# ---------------------------------------------------------------------------
# §18.2 Чистые функции: эталон §6.11 целиком
# ---------------------------------------------------------------------------


def test_reference_example_611():
    """Эталон §6.11: K=10, узкие места корпус и заряды, unit/cost/Total ровно."""
    hull = ComponentInput(type_id=1, qty_per_fit=1, availability=10, packaged_volume=D("15000"), is_hull=True)
    module = ComponentInput(type_id=2, qty_per_fit=3, availability=100, packaged_volume=D("5"))
    ammo = ComponentInput(type_id=3, qty_per_fit=1000, availability=10000, packaged_volume=D("0.0125"))
    components = [hull, module, ammo]
    n = 30

    # K и узкие места (§6.3)
    k, bottlenecks = engine.calc_fits([c.availability for c in components], components)
    assert k == 10
    assert sorted(bottlenecks) == [0, 2]  # корпус и заряды

    # Дефициты (§6.5)
    assert engine.calc_deficit(n, 1, 10) == 20
    assert engine.calc_deficit(n, 3, 100) == 0
    assert engine.calc_deficit(n, 1000, 10000) == 20000

    # unit корпуса (§6.6): лоты (40M;5),(42M;15), D=20 → 41,500,000
    hull_book = [OrderBookRow(D("40000000"), 5), OrderBookRow(D("42000000"), 15)]
    price = engine.covering_price(hull_book, 20)
    assert price.unit == D("41500000")
    assert not price.is_not_covered

    # unit зарядов: (1000;10000),(1200;10000), D=20000 → 1100
    ammo_book = [OrderBookRow(D("1000"), 10000), OrderBookRow(D("1200"), 10000)]
    price_ammo = engine.covering_price(ammo_book, 20000)
    assert price_ammo.unit == D("1100")

    # Стоимость (§6.9), m=5%, c_del=1250, доставка без наценки
    cost_hull = engine.calc_cost(D("41500000"), False, 20, 0, D("5"), D("1250"), D("15000"))
    assert cost_hull == D("1246500000")
    cost_ammo = engine.calc_cost(D("1100"), False, 20000, 0, D("5"), D("1250"), D("0.0125"))
    assert cost_ammo == D("23412500")
    assert cost_hull + cost_ammo == D("1269912500")

    # Оверпрайс: ref=40M, τ=25% → порог 50M → оверпрайса нет (§6.8)
    assert not engine.is_overprice(D("41500000"), D("40000000"), D("25"))


def test_covering_price_single_lot():
    """Покрытие одним лотом: v >= D → цена лота (§6.6, случай 1)."""
    book = [OrderBookRow(D("100"), 50), OrderBookRow(D("200"), 50)]
    result = engine.covering_price(book, 50)
    assert result.unit == D("100")
    assert result.covered == 50 and result.shortfall == 0 and not result.is_not_covered
    # D=51: первый лот не покрывает (v=50<51), накопление до второго лота →
    # средневзвешенная по лотам 1..2 (§6.6, случай 2).
    result2 = engine.covering_price(book, 51)
    assert result2.unit == D("150")


def test_covering_price_multi_lot():
    """Покрытие несколькими лотами: средневзвешенная до покрытия (§6.6, случай 2)."""
    book = [OrderBookRow(D("100"), 30), OrderBookRow(D("200"), 30), OrderBookRow(D("300"), 30)]
    result = engine.covering_price(book, 50)
    # k=2 (накоплено 60>=50): unit = (100*30 + 200*30) / 60 = 150
    assert result.unit == D("150")
    assert result.covered == 60


def test_covering_price_not_covered():
    """NOT_COVERED: covered, shortfall, unit — средневзвешенная по всему стакану (§6.6)."""
    book = [OrderBookRow(D("100"), 10), OrderBookRow(D("200"), 10)]
    result = engine.covering_price(book, 100)
    assert result.is_not_covered
    assert result.covered == 20
    assert result.shortfall == 80
    assert result.unit == D("150")
    # Стоимость только за covered (§6.6).
    cost = engine.calc_cost(D("150"), True, 100, 20, D("5"), D("1250"), D("1"))
    assert cost == (D("150") * D("1.05") + D("1250")) * 20


def test_overprice_threshold():
    """Оверпрайс ровно на пороге — не бейдж; чуть выше — бейдж; τ=0 (§6.8)."""
    ref = D("100")
    # Ровно на пороге τ=25%: 125 → не больше порога → нет.
    assert not engine.is_overprice(D("125"), ref, D("25"))
    # Чуть выше: 125.01 → бейдж.
    assert engine.is_overprice(D("125.01"), ref, D("25"))
    # τ=0: любое превышение — бейдж; равенство — нет.
    assert not engine.is_overprice(D("100"), ref, D("0"))
    assert engine.is_overprice(D("100.01"), ref, D("0"))


def test_fit_status_priority():
    """Статусы §6.4: NO_DATA > HULL_MISSING > DEFICIT > OK."""
    assert engine.calc_fit_status(True, 0, 0, 1, allow_hull_missing=True) == "no_data"
    assert engine.calc_fit_status(False, 0, 10, 5, allow_hull_missing=True) == "hull_missing"
    assert engine.calc_fit_status(False, 10, 5, 30, allow_hull_missing=True) == "deficit"
    assert engine.calc_fit_status(False, 10, 30, 30, allow_hull_missing=True) == "ok"


def test_fit_status_no_hull_missing_for_component_request():
    """Компонент вне фитов (§6.10): allow_hull_missing=False исключает HULL_MISSING."""
    # hull_available=0, но HULL_MISSING не применяется: статус по K vs N.
    assert engine.calc_fit_status(False, 0, 0, 1, allow_hull_missing=False) == "deficit"
    assert engine.calc_fit_status(False, 0, 10, 5, allow_hull_missing=False) == "ok"
    assert engine.calc_fit_status(False, 0, 30, 30, allow_hull_missing=False) == "ok"


# ---------------------------------------------------------------------------
# §18.3 Scope через сервис/БД
# ---------------------------------------------------------------------------


def test_station_scope_strict(sde_ids):
    """station не учитывает заказы других станций системы/региона (§5.1)."""
    _make_location_fresh(60003760)
    _add_stock(60003760, 1, 10)
    _add_stock(60003761, 1, 50)  # другая станция той же системы
    _add_stock(60003762, 1, 50, system_id=30000143)  # станция другого региона
    availability, has_data = calc_service.get_availability(1, ScopeKind.STATION, 60003760)
    assert has_data
    assert availability == 10  # только эта станция


def test_system_scope_npc_and_enabled_structures(sde_ids):
    """system = NPC-станции системы + только включённые структуры (§6.1)."""
    _make_location_fresh(60003760)
    _make_location_fresh(60003761)
    _make_location_fresh(70000001, kind=LocationKind.STRUCTURE)
    # Включённая структура другой системы и выключенная — не учитываются.
    _make_location_fresh(70000002, kind=LocationKind.STRUCTURE)
    _make_location_fresh(70000003, kind=LocationKind.STRUCTURE, system_id=30000143)
    # Отключим структуру 70000002: найдём по structure_id.
    MarketLocation.objects.filter(structure_id=70000002).update(enabled=False)
    MarketLocation.objects.filter(structure_id=70000003).update(enabled=True)

    _add_stock(60003760, 1, 10)
    _add_stock(60003761, 1, 20)
    _add_stock(70000001, 1, 5)
    _add_stock(70000002, 1, 100)  # выключенная структура — не входит
    _add_stock(70000003, 1, 7)  # другая система — не входит (не в L(S))

    availability, has_data = calc_service.get_availability(1, ScopeKind.SYSTEM, 30000142)
    assert has_data
    assert availability == 10 + 20 + 5  # NPC-станции системы + включённая структура


def test_region_scope_npc_and_enabled_structures(sde_ids):
    """region = NPC-станции региона + включённые структуры региона (§6.1)."""
    _make_location_fresh(60003760)
    _make_location_fresh(60003762, system_id=30000143)  # станция другого региона
    _make_location_fresh(70000001, kind=LocationKind.STRUCTURE)
    _make_location_fresh(70000002, kind=LocationKind.STRUCTURE, system_id=30000143)
    MarketLocation.objects.filter(structure_id=70000002).update(enabled=False)

    _add_stock(60003760, 1, 10)
    _add_stock(60003762, 1, 99, system_id=30000143)
    _add_stock(70000001, 1, 5)
    _add_stock(70000002, 1, 88, system_id=30000143)

    availability, _ = calc_service.get_availability(1, ScopeKind.REGION, 10000002)
    assert availability == 10 + 5


def test_scope_override_component(sde_ids):
    """scope_override компонента меняет область расчёта позиции (§6.3, решение 11)."""
    fit = _make_fit(scope_kind=ScopeKind.STATION, scope_ref=60003760)
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)  # корпус, без override
    FitComponent.objects.create(
        fit=fit, type_id=2, qty_per_fit=1, scope_override_kind=ScopeKind.REGION, scope_override_ref=10000002
    )

    _make_location_fresh(60003760)
    _make_location_fresh(60003761)
    _make_location_fresh(60003762, system_id=30000143)
    _make_location_fresh(70000001, kind=LocationKind.STRUCTURE)
    _add_stock(60003760, 1, 10)
    _add_stock(60003760, 2, 10)
    _add_stock(60003761, 2, 20)
    _add_stock(60003762, 2, 40, system_id=30000143)
    _add_stock(70000001, 2, 5)

    calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    # Корпус — station scope: A=10 → K ограничен корпусом (fits=10).
    # Модуль — region scope: A=10+20+5=75 → fits=75. K=10.
    assert fit.fits_available == 10
    assert fit.status_cache == FitStatus.DEFICIT


# ---------------------------------------------------------------------------
# §18.4 Статусы через сервис
# ---------------------------------------------------------------------------


def _fit_with_components(n=30, station_id=60003760):
    """Фит N=30: корпус (q=1), модуль (q=3), заряды (q=1000) на станции."""
    fit = _make_fit(n=n)
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    FitComponent.objects.create(fit=fit, type_id=2, qty_per_fit=3)
    FitComponent.objects.create(fit=fit, type_id=3, qty_per_fit=1000)
    _make_location_fresh(station_id)
    return fit


def _add_jita_levels(type_id, lots):
    """Лоты стакана Jita с rank от 1."""
    for rank, (price, volume) in enumerate(lots, start=1):
        JitaOrderLevel.objects.create(
            type_id=type_id,
            rank=rank,
            price=D(str(price)),
            volume=volume,
            fetched_at=timezone.now(),
        )


@pytest.mark.django_db
def test_service_get_jita_book_disabled_location_ignores_cache(sde_ids):
    """Отключённая локация Jita: сервис не отдаёт stale-лоты даже при уцелевших строках кэша."""
    location = _make_location_fresh(60003760)
    _add_jita_levels(1, [("40000000", 5), ("42000000", 15)])
    depth = PluginSetting.load().jita_book_depth

    book, ref = calc_service.get_jita_book(1, depth)
    assert ref == D("40000000")
    assert len(book) == 2
    assert isinstance(book[0].price, Decimal)

    MarketLocation.objects.filter(pk=location.pk).update(enabled=False)
    assert calc_service.get_jita_book(1, depth) == ([], None)


@pytest.mark.django_db
def test_service_disabled_jita_keeps_market_calc_without_prices(sde_ids):  # noqa: F811
    """Выключенная Jita: K/D_i/статус остаются по локальным рынкам, цены — None."""
    settings = PluginSetting.load()
    settings.jita_markup_pct = D("5")
    settings.overprice_threshold_pct = D("25")
    settings.delivery_rate_isk_per_m3 = D("1250")
    settings.save()
    fit = _fit_with_components()
    _add_stock(60003760, 1, 10)
    _add_stock(60003760, 2, 100)
    _add_stock(60003760, 3, 10000)
    _add_jita_levels(1, [("40000000", 5), ("42000000", 15)])
    _add_jita_levels(3, [("1000", 10000), ("1200", 10000)])

    baseline = calc_service.recalc_fit(fit)
    assert baseline.total_cost == D("1269912500")

    MarketLocation.objects.filter(station_id=60003760).update(enabled=False)
    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()

    # Jita — только источник цен: возврат к локальным рынкам, не полный NO_DATA.
    assert result.status == FitStatus.DEFICIT
    assert result.k == 10
    assert [c.deficit for c in result.components] == [20, 0, 20000]
    assert fit.status_cache == FitStatus.DEFICIT
    assert fit.fits_available == 10
    assert result.total_cost is None and fit.total_cost is None
    assert result.overprice_count == 0
    assert {c.type_id for c in result.components} == {1, 2, 3}
    for component in result.components:
        assert component.unit is None
        assert component.ref is None
        assert component.cost is None
        assert component.shortfall is None
        assert component.is_not_covered is False
        assert component.is_overprice is False
        assert component.overprice_pct is None


@pytest.mark.django_db
def test_service_reference_full(sde_ids):
    """Эталон §6.11 через сервис: status=DEFICIT, K=10, Total ровно."""
    settings = PluginSetting.load()
    settings.jita_markup_pct = D("5")
    settings.overprice_threshold_pct = D("25")
    settings.delivery_rate_isk_per_m3 = D("1250")
    settings.save()
    fit = _fit_with_components()
    _add_stock(60003760, 1, 10)
    _add_stock(60003760, 2, 100)
    _add_stock(60003760, 3, 10000)
    _add_jita_levels(1, [("40000000", 5), ("42000000", 15)])
    _add_jita_levels(2, [("100000", 100)])
    _add_jita_levels(3, [("1000", 10000), ("1200", 10000)])

    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.DEFICIT
    assert fit.fits_available == 10
    assert fit.total_cost is not None
    # hull cost + ammo cost (эталон) = 1,269,912,500; модуль D=0 → cost 0.
    assert fit.total_cost == D("1269912500")
    assert fit.overprice_count == 0
    assert result.components[0].deficit == 20
    assert result.components[2].deficit == 20000


def test_service_no_data_missing_stock(sde_ids):
    """NO_DATA при отсутствии агрегата StockLevel по нужной локации (§18.4, §6.7)."""
    fit = _fit_with_components()
    _add_stock(60003760, 1, 10)
    _add_stock(60003760, 2, 100)
    # У локации нет ни одного агрегата — данные по ней не синкались.
    StockLevel.objects.filter(location_id=60003760).delete()
    _add_jita_levels(1, [("40000000", 5)])
    calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.NO_DATA
    assert fit.fits_available is None and fit.total_cost is None


def test_service_no_data_stale(sde_ids):
    """NO_DATA при протухших данных last_synced_at (§18.4, §6.7)."""
    fit = _fit_with_components()
    _add_stock(60003760, 1, 10)
    _add_stock(60003760, 2, 100)
    _add_stock(60003760, 3, 10000)
    MarketLocation.objects.filter(station_id=60003760).update(last_synced_at=timezone.now() - timedelta(minutes=600))
    calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.NO_DATA


def test_service_no_data_structure_bad_sync(sde_ids):
    """NO_DATA, если структура области имеет sync_status != ok (§6.7)."""
    fit = _make_fit(n=5, scope_kind=ScopeKind.SYSTEM, scope_ref=30000142)
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    _make_location_fresh(60003760)
    _make_location_fresh(70000001, kind=LocationKind.STRUCTURE)
    MarketLocation.objects.filter(structure_id=70000001).update(sync_status=SyncStatus.NO_PROVIDER)
    _add_stock(60003760, 1, 10)
    _add_stock(70000001, 1, 50)
    calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.NO_DATA


def test_service_hull_missing(sde_ids):
    """HULL_MISSING при A_hull=0 независимо от остальных (§18.4, §5.3)."""
    fit = _fit_with_components()
    _add_stock(60003760, 1, 0)
    _add_stock(60003760, 2, 100)
    _add_stock(60003760, 3, 10000)
    _add_jita_levels(1, [("40000000", 5)])
    calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.HULL_MISSING


def test_service_ok_when_k_equals_n(sde_ids):
    """OK при K=N (§18.4)."""
    fit = _fit_with_components(n=5)
    _add_stock(60003760, 1, 10)
    _add_stock(60003760, 2, 100)
    _add_stock(60003760, 3, 10000)
    _add_jita_levels(1, [("40000000", 5)])
    _add_jita_levels(2, [("100000", 100)])
    _add_jita_levels(3, [("1000", 10000)])
    calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.OK
    # K = min(10, 33, 10) = 10 — независимо от N=5.
    assert fit.fits_available == 10
    assert fit.total_cost == D("0")


def test_deficit_independent_between_fits(sde_ids):
    """Дефицит двух фитов с общим компонентом считается независимо (§5.5, §19.5)."""
    fit1 = _fit_with_components(n=30, station_id=60003760)
    fit1.name = "Fit 1"
    fit1.save()
    fit2 = _fit_with_components(n=5, station_id=60003760)
    fit2.name = "Fit 2"
    fit2.save()
    _add_stock(60003760, 1, 10)
    _add_stock(60003760, 2, 100)
    _add_stock(60003760, 3, 10000)
    _add_jita_levels(1, [("40000000", 5)])
    _add_jita_levels(2, [("100000", 100)])
    _add_jita_levels(3, [("1000", 10000)])
    calc_service.recalc_fit(fit1)
    calc_service.recalc_fit(fit2)
    fit1.refresh_from_db()
    fit2.refresh_from_db()
    # Общий компонент (заряды A=10000) не «схлопывается»: у fit1 D=20000, у fit2 D=0.
    assert fit1.status_cache == FitStatus.DEFICIT and fit1.fits_available == 10
    assert fit2.status_cache == FitStatus.OK and fit2.fits_available == 10


def test_component_request_calc(sde_ids):
    """Компонент вне фитов (§6.10): q=1, статусы OK/DEFICIT/NO_DATA без HULL_MISSING."""
    request = ComponentRequest.objects.create(
        type_id=9, qty_target=10, scope_kind=ScopeKind.STATION, scope_ref=60003760
    )
    _make_location_fresh(60003760)
    _add_jita_levels(9, [("500", 3), ("600", 3)])
    # A=4 → DEFICIT (D=6): unit = (500*3 + 600*3)/6 = 550
    _add_stock(60003760, 9, 4)
    result = calc_service.recalc_component_request(request)
    assert result.status == FitStatus.DEFICIT
    assert result.components[0].unit == D("550")
    assert result.components[0].deficit == 6

    # A >= N → OK
    StockLevel.objects.filter(type_id=9).update(total_sell_volume=50)
    result = calc_service.recalc_component_request(request)
    assert result.status == FitStatus.OK

    # Нет данных вообще → NO_DATA (HULL_MISSING не применяется)
    StockLevel.objects.filter(type_id=9).delete()
    result = calc_service.recalc_component_request(request)
    assert result.status == FitStatus.NO_DATA


def test_recalc_via_task(sde_ids):
    """Интеграция: recalc_stock_levels вызывает пересчёт фитов (§12.1)."""
    from marketmonitoring.tasks import recalc_stock_levels

    fit = _fit_with_components()
    _add_stock(60003760, 1, 10)
    _add_stock(60003760, 2, 100)
    _add_stock(60003760, 3, 10000)
    _add_jita_levels(1, [("40000000", 5)])
    _add_jita_levels(2, [("100000", 100)])
    _add_jita_levels(3, [("1000", 10000)])
    recalc_stock_levels(trigger="manual", location_ids=[])
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.DEFICIT
    assert fit.last_checked_at is not None


# ---------------------------------------------------------------------------
# §6.7 свежесть: ревью этапа 4 (M1/M2/m2/m3/m5)
# ---------------------------------------------------------------------------


def test_no_data_stale_region_for_npc_station_without_market_location(sde_ids):
    """M1: NPC-станция без записи MarketLocation проверяется регионально (§6.7).

    Регион станции резолвится через eveuniverse (EveStation -> EveRegion);
    протухшие данные региона → NO_DATA, свежие → расчёт выполняется.
    """
    fit = _make_fit(n=5, scope_kind=ScopeKind.REGION, scope_ref=10000002)
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    # Обе NPC-станции региона — БЕЗ записей MarketLocation.
    stale_time = timezone.now() - timedelta(minutes=600)
    _add_stock(60003760, 1, 10)
    _add_stock(60003761, 1, 20)
    StockLevel.objects.update(updated_at=stale_time)
    _add_jita_levels(1, [("40000000", 20)])

    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.NO_DATA
    assert fit.fits_available is None
    assert any("region 10000002" in reason for reason in result.reasons)

    # Свежие данные регионального синка → расчёт выполняется.
    StockLevel.objects.update(updated_at=timezone.now())
    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    # A = 10+20 = 30 >= N=5 → OK
    assert fit.status_cache == FitStatus.OK
    assert fit.fits_available == 30
    assert result.reasons == []


def test_no_data_when_aggregate_missing_at_one_location(sde_ids):
    """M2: NO_DATA, если агрегат StockLevel отсутствует у одной локации области (§6.7)."""
    fit = _make_fit(n=5, scope_kind=ScopeKind.SYSTEM, scope_ref=30000142)
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    _make_location_fresh(60003760)
    _make_location_fresh(60003761)
    # Агрегат есть только на одной из двух нужных станций системы.
    _add_stock(60003760, 1, 10)
    _add_jita_levels(1, [("40000000", 10)])

    availability, has_data = calc_service.get_availability(1, ScopeKind.SYSTEM, 30000142)
    assert availability == 10
    assert not has_data

    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.NO_DATA
    assert fit.fits_available is None and fit.total_cost is None
    assert any("no StockLevel aggregate" in reason for reason in result.reasons)


def test_new_structure_without_type_orders_does_not_void_all_fits(sde_ids):
    """Регресс: свежая структура без лотов нужного типа даёт A=0, а не NO_DATA (§6.7).

    До исправления проверка требовала агрегат StockLevel по каждой паре
    (локация, тип): добавление структуры в область превращало все компоненты
    в NO_DATA, даже если на старых локациях данные есть.
    """
    fit = _make_fit(n=1, scope_kind=ScopeKind.SYSTEM, scope_ref=30000142)
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    FitComponent.objects.create(fit=fit, type_id=3, qty_per_fit=1000)
    _make_location_fresh(60003760)
    _make_location_fresh(60003761)
    _make_location_fresh(70000001, kind=LocationKind.STRUCTURE)
    # У структуры есть данные (тип 1), но нет лотов зарядов (тип 3).
    _add_stock(60003760, 1, 10)
    _add_stock(60003760, 3, 10000)
    _add_stock(60003761, 1, 10)
    _add_stock(60003761, 3, 1000)
    _add_stock(70000001, 1, 10)

    availability, has_data = calc_service.get_availability(3, ScopeKind.SYSTEM, 30000142)
    assert has_data is True
    assert availability == 11000

    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.OK
    assert fit.fits_available == 11
    assert result.reasons == []


def test_regional_check_not_applied_to_structures(sde_ids):
    """m2: региональная проверка свежести — только NPC-станции, не структуры (§6.7).

    Структура свежа по last_synced_at/sync_status структурного синка; отсутствие
    региональных данных не делает её NO_DATA.
    """
    _make_location_fresh(60003760)
    _make_location_fresh(70000001, kind=LocationKind.STRUCTURE)
    _add_stock(60003760, 1, 10)

    # Структура + свежая NPC-станция со свежими региональными данными → fresh.
    fresh, reasons = calc_service.check_freshness({60003760, 70000001}, 180)
    assert fresh, reasons

    # Протухшая региональная станция → причина только по станции, не по структуре.
    MarketLocation.objects.filter(station_id=60003760).update(last_synced_at=timezone.now() - timedelta(minutes=600))
    StockLevel.objects.update(updated_at=timezone.now() - timedelta(minutes=600))
    fresh, reasons = calc_service.check_freshness({60003760, 70000001}, 180)
    assert not fresh
    assert any("60003760" in reason for reason in reasons)
    assert not any("70000001" in reason for reason in reasons)


def test_region_sync_freshness_uses_needed_locations_only(sde_ids):
    """M2: региональная проверка опирается на нужные станции региона (§6.7).

    Свежая строка StockLevel на посторонней станции региона не спасает,
    если у нужной станции региона свежих данных нет.
    """
    fit = _make_fit(n=5, scope_kind=ScopeKind.REGION, scope_ref=10000002)
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    _make_location_fresh(60003760)  # нужная станция региона
    _add_stock(60003760, 1, 10)
    _add_stock(60003761, 1, 0)  # вторая NPC-станция региона: агрегат есть, объём 0
    # Свежие данные другого региона на посторонней станции (60003762).
    _add_stock(60003762, 1, 99, system_id=30000143, region_id=10000001)
    _add_jita_levels(1, [("40000000", 10)])

    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    # Нужная станция 60003760 имеет свежие данные → NO_DATA нет.
    assert fit.status_cache == FitStatus.OK

    # Убираем свежие данные нужной станции → региональная проверка ловит.
    StockLevel.objects.filter(location_id=60003760).delete()
    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.NO_DATA
    assert any("region 10000002" in reason for reason in result.reasons)


# --- calc_overprice_pct (§6.8): процент превышения для tooltip бейджа ---


def test_calc_overprice_pct_basic():
    """unit/ref*100 - 100, Decimal без float, квантование до 0.01 (§22.4)."""
    assert engine.calc_overprice_pct(D("125"), D("100")) == D("25.00")
    assert engine.calc_overprice_pct(D("1"), D("3")) == D("-66.67")
    assert engine.calc_overprice_pct(D("4995.005994005994"), D("1")) == D("499400.60")


def test_calc_overprice_pct_none_inputs():
    """None при отсутствии unit/ref или ref == 0."""
    assert engine.calc_overprice_pct(None, D("100")) is None
    assert engine.calc_overprice_pct(D("100"), None) is None
    assert engine.calc_overprice_pct(D("100"), D("0")) is None
