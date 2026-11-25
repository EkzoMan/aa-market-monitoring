"""Тесты этапа 9: сводная доступность кораблей C+K (§6.13, §18.9).

Покрываются: чистые функции, эталон §6.11 с C=7, неизменность D_i/unit/cost/CSV,
исключение NO_DATA из сводок, HULL_MISSING, пер-фитная подпись K, дедупликация
``ships_deficit``, beat-расписание и отсутствие ESI-вызовов из view.

Сеть не используется: ESI-клиент и Discord HTTP патчатся через unittest.mock.
"""

import ast
import csv
import inspect
import io
import textwrap
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from .. import tasks
from ..auth_hooks import GROUP_EDIT, GROUP_VIEW
from ..calc import engine
from ..calc import service as calc_service
from ..calc.engine import ShipsAvailabilityResult
from ..constants import (
    ContractFitStatus,
    ContractSourceKind,
    FitStatus,
    LocationKind,
    NotificationEvent,
    ScopeKind,
    ShipsStatus,
    SyncRunStatus,
    SyncStatus,
    TriggerKind,
)
from ..esi import client as esi_client
from ..models import (
    ContractCache,
    ContractItemCache,
    ContractProvider,
    Fit,
    FitComponent,
    JitaOrderLevel,
    MarketLocation,
    NotificationLog,
    PluginSetting,
    StockLevel,
    SyncRun,
    WebhookConfig,
)
from ..notify import discord as discord_notify
from ..views.exports import CSV_HEADER
from .test_calc_engine import sde_ids  # noqa: F401 — справочники eveuniverse (SDE, без сети)

pytestmark = pytest.mark.django_db

D = Decimal
STATION_ID = 60003760
SYSTEM_ID = 30000142
REGION_ID = 10000002

ESI_FETCH_NAMES = (
    "fetch_jita_orders",
    "fetch_region_orders",
    "fetch_structure_orders",
    "fetch_structure_info",
    "fetch_corporation_contracts",
    "fetch_corporation_contract_items",
    "fetch_corporation_structures",
    "fetch_character_contracts",
    "fetch_character_contract_items",
)

# Канонические хэши старых событий и нового события (§13.1, решение №42).
EXPECTED_DEFICIT_HASH = "cc0b55729a81f2fc8b1b70dda21853a651eb9c797e6013f88514c0ddbda4df06"
EXPECTED_CONTRACTS_HASH = "22d40175e7b0705c772ecd9cd8c7c28bcbf15869e528752ff814147cd21f5934"
EXPECTED_SHIPS_HASH = "2b97c86b90fb7931cae40e082ea6c756f65486fbd6e9fc6bdd161dcd4341a4d9"


@pytest.fixture(autouse=True)
def _reset_packaged_volumes_cache():
    """Сброс модульного кеша packaged_volume между тестами (как в recalc_all)."""
    calc_service._packaged_volumes_cache = None
    yield
    calc_service._packaged_volumes_cache = None


@pytest.fixture
def viewer_client(db):
    """Клиент с правом _view."""
    user = User.objects.create_user(username="ships_viewer", password="x")
    user.groups.add(Group.objects.get(name=GROUP_VIEW))
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def editor_client(db):
    """Клиент с правом _edit."""
    user = User.objects.create_user(username="ships_editor", password="x")
    user.groups.add(Group.objects.get(name=GROUP_EDIT))
    client = Client()
    client.force_login(user)
    return client


@contextmanager
def _no_esi():
    """Любой ESI-вызов из view считается дефектом (§5.10)."""
    with ExitStack() as stack:
        for name in ESI_FETCH_NAMES:
            stack.enter_context(mock.patch.object(esi_client, name, side_effect=AssertionError(f"ESI {name} called")))
        yield


def _http_ok(*args, **kwargs):
    """Мок Discord: 204 No Content."""
    return SimpleNamespace(status_code=204, text="")


def _configure_reference_settings():
    """Настройки эталона §6.11: m=5%, c_del=1250, tau=25%."""
    settings = PluginSetting.load()
    settings.jita_markup_pct = D("5")
    settings.overprice_threshold_pct = D("25")
    settings.delivery_rate_isk_per_m3 = D("1250")
    settings.save()
    return settings


def _seed_reference_market(sde_ids, name="Reference fit", qty_target=30):
    """Рынок эталона §6.11: K=10, узкие места — корпус и заряды."""
    _configure_reference_settings()
    now = timezone.now()
    fit = Fit.objects.create(
        name=name,
        hull_type_id=1,
        qty_target=qty_target,
        scope_kind=ScopeKind.STATION,
        scope_ref=STATION_ID,
    )
    for type_id, qty in ((1, 1), (2, 3), (3, 1000)):
        FitComponent.objects.create(fit=fit, type_id=type_id, qty_per_fit=qty)
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
    for type_id, volume in ((1, 10), (2, 100), (3, 10000)):
        StockLevel.objects.create(
            location_id=STATION_ID,
            solar_system_id=SYSTEM_ID,
            region_id=REGION_ID,
            type_id=type_id,
            total_sell_volume=volume,
            updated_at=now,
        )
    for type_id, lots in (
        (1, (("40000000", 5), ("42000000", 15))),
        (2, (("100000", 100),)),
        (3, (("1000", 10000), ("1200", 10000))),
    ):
        for rank, (price, volume) in enumerate(lots, start=1):
            JitaOrderLevel.objects.create(
                type_id=type_id,
                rank=rank,
                price=D(price),
                volume=volume,
                fetched_at=now,
            )
    return fit


def _seed_contract_provider():
    """Свежий успешный провайдер контрактов (§6.12, без ESI)."""
    character = EveCharacter.objects.create(
        character_id=9101,
        character_name="Ships Pilot",
        corporation_id=9102,
        corporation_name="Ships Corp",
        corporation_ticker="SHP",
    )
    provider = ContractProvider.objects.create(
        character=character,
        source_kind=ContractSourceKind.CORPORATION,
        enabled=True,
        priority=10,
        sync_status=SyncStatus.OK,
    )
    provider.last_synced_at = timezone.now()
    provider.save(update_fields=["last_synced_at"])
    return provider


def _seed_full_kit_contracts(fit, count):
    """``count`` открытых контрактов, в каждом — один полный комплект фита."""
    _seed_contract_provider()
    components = list(fit.components.all().order_by("pk"))
    now = timezone.now()
    for index in range(count):
        contract_id = 8000 + index
        contract = ContractCache.objects.create(
            contract_id=contract_id,
            source_kind=ContractSourceKind.CORPORATION,
            source_ref=9102,
            issuer_id=9101,
            issuer_corporation_id=9102,
            assignee_id=9102,
            availability="corporation",
            contract_type="item_exchange",
            status="outstanding",
            start_location_id=fit.scope_ref,
            price=D("1"),
            date_issued=now,
            date_expired=now + timedelta(days=7),
            fetched_at=now,
        )
        ContractItemCache.objects.bulk_create(
            [
                ContractItemCache(
                    contract=contract,
                    record_id=contract_id * 100 + position,
                    type_id=component.type_id,
                    quantity=component.qty_per_fit,
                )
                for position, component in enumerate(components)
            ]
        )


def _set_cached_ships(
    fit,
    *,
    contracts_available,
    market_available,
    contracts_status,
    market_status,
    ships_status,
    total_cost=D("0"),
):
    """Прямая запись кэша §6.13 (UI читает только БД)."""
    fit.contracts_available = contracts_available
    fit.fits_available = market_available
    fit.contracts_status_cache = contracts_status
    fit.status_cache = market_status
    fit.ships_status_cache = ships_status
    fit.total_cost = total_cost
    fit.overprice_count = 0
    fit.save(
        update_fields=[
            "contracts_available",
            "fits_available",
            "contracts_status_cache",
            "status_cache",
            "ships_status_cache",
            "total_cost",
            "overprice_count",
        ]
    )
    return fit


# --- §18.9: чистые функции и эталон §6.11 с C=7 -----------------------------


def test_ships_pure_functions_counts_statuses_and_decimal(sde_ids):  # noqa: F811
    """C+K, агрегатный дефицит и приоритет статусов — чистые функции без float."""
    assert engine.calc_ships_total(3, 10) == 13
    assert engine.calc_ships_deficit(30, 3, 10) == 17
    assert engine.calc_ships_total(0, 0) == 0
    assert engine.calc_ships_deficit(30, 0, 0) == 30
    assert engine.calc_ships_total(None, 10) is None
    assert engine.calc_ships_total(3, None) is None
    assert engine.calc_ships_deficit(30, None, 10) is None

    result = engine.calc_ships_availability(30, 3, 10, ContractFitStatus.DEFICIT, FitStatus.DEFICIT)
    assert result == ShipsAvailabilityResult(3, 10, 13, 17, ShipsStatus.DEFICIT, False)

    partial_cases = (
        (None, 10, ContractFitStatus.NO_DATA, FitStatus.OK),
        (3, None, ContractFitStatus.OK, FitStatus.OK),
        (0, 0, None, FitStatus.DEFICIT),
        (0, 0, ContractFitStatus.DEFICIT, "no_data"),
        (0, 0, ContractFitStatus.DEFICIT, ""),
    )
    for contracts_available, market_available, contracts_status, market_status in partial_cases:
        partial = engine.calc_ships_availability(
            30, contracts_available, market_available, contracts_status, market_status
        )
        assert partial.ships_total is None
        assert partial.ships_deficit is None
        assert partial.ships_status == ShipsStatus.NO_DATA
        assert partial.is_partial is True

    # no_data имеет приоритет над дефицитом; deficit — над ok.
    priority = engine.calc_ships_availability(30, 0, 0, ContractFitStatus.NO_DATA, FitStatus.DEFICIT)
    assert priority.ships_status == ShipsStatus.NO_DATA
    deficit = engine.calc_ships_availability(30, 0, 0, ContractFitStatus.DEFICIT, FitStatus.DEFICIT)
    assert deficit.ships_status == ShipsStatus.DEFICIT
    # hull_missing рыночной части — известное значение, не no_data (решение №43).
    hull_missing = engine.calc_ships_availability(10, 0, 10, ContractFitStatus.DEFICIT, FitStatus.HULL_MISSING)
    assert hull_missing.ships_status == ShipsStatus.OK
    assert hull_missing.is_partial is False

    # В расчётах участвуют только int; денежные функции ядра не используют float (§22.4).
    for func in (engine.calc_ships_total, engine.calc_ships_deficit, engine.calc_ships_availability):
        source = textwrap.dedent(inspect.getsource(func))
        assert "float" not in source
        tree = ast.parse(source)
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]


def test_reference_611_with_c7_keeps_position_calculation(sde_ids):  # noqa: F811
    """Эталон §6.11 с C=7: 7+10=17, дефицит 13; D_i/unit/cost/статусы эталонные."""
    fit = _seed_reference_market(sde_ids)
    _seed_full_kit_contracts(fit, 7)

    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()

    ships = result.ships_availability
    assert ships is not None
    assert ships.ships_in_contracts == 7
    assert ships.ships_from_market == 10
    assert ships.ships_total == 17
    assert ships.ships_deficit == 13
    assert ships.ships_status == ShipsStatus.DEFICIT
    assert ships.is_partial is False

    # Старые статусы и их кэш не подменяются общим статусом (§19.20).
    assert result.k == 10
    assert result.status == FitStatus.DEFICIT
    assert result.contracts_status == ContractFitStatus.DEFICIT
    assert fit.fits_available == 10
    assert fit.contracts_available == 7
    assert fit.status_cache == FitStatus.DEFICIT
    assert fit.contracts_status_cache == ContractFitStatus.DEFICIT
    assert fit.ships_status_cache == ShipsStatus.DEFICIT
    assert not hasattr(fit, "ships_deficit")  # решение №44: агрегатный дефицит не хранится

    # Позиционный расчёт эталона не изменился.
    assert [component.deficit for component in result.components] == [20, 0, 20000]
    assert result.components[0].unit == D("41500000")
    assert result.components[1].unit is None
    assert result.components[2].unit == D("1100")
    assert isinstance(result.components[0].unit, Decimal)
    assert isinstance(result.components[2].unit, Decimal)
    assert fit.total_cost == D("1269912500")
    assert result.total_cost == D("1269912500")
    reference_components = [
        engine.ComponentInput(type_id=1, qty_per_fit=1, availability=10),
        engine.ComponentInput(type_id=2, qty_per_fit=3, availability=100),
        engine.ComponentInput(type_id=3, qty_per_fit=1000, availability=10000),
    ]
    k, bottlenecks = engine.calc_fits([10, 100, 10000], reference_components)
    assert k == 10
    assert sorted(bottlenecks) == [0, 2]  # корпус и заряды


def test_reference_csv_unchanged_with_c7(viewer_client, sde_ids):  # noqa: F811
    """CSV эталона не меняется при C=7: те же позиции, цены и сумма cost."""
    fit = _seed_reference_market(sde_ids)
    _seed_full_kit_contracts(fit, 7)
    calc_service.recalc_fit(fit)

    with _no_esi():
        response = viewer_client.get(reverse("marketmonitoring:export_csv", args=[fit.pk]))
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
    assert rows[0] == CSV_HEADER
    assert len(rows) == 3  # корпус и заряды; у модуля D=0

    by_name = {row[0]: row for row in rows[1:]}
    assert by_name["Ferox Test Hull"][1:3] == ["1", "20"]
    assert by_name["Ferox Test Hull"][3:5] == ["62325000", "1246500000"]
    assert by_name["Charge M"][1:3] == ["3", "20000"]
    assert by_name["Charge M"][3:5] == ["1171", "23412500"]
    assert sum(int(row[4]) for row in rows[1:]) == 1269912500


# --- §18.9: HULL_MISSING, NO_DATA и сводные суммы ---------------------------


def test_hull_missing_market_keeps_contract_ships_in_total(sde_ids):  # noqa: F811
    """При K=0, C>0: общий дефицит считается от C, рыночный статус не перезаписывается."""
    fit = Fit.objects.create(
        name="Hull missing with contracts",
        hull_type_id=1,
        qty_target=3,
        scope_kind=ScopeKind.STATION,
        scope_ref=STATION_ID,
    )
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
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
    StockLevel.objects.create(
        location_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=1,
        total_sell_volume=0,
        updated_at=now,
    )
    _seed_full_kit_contracts(fit, 1)

    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()

    assert result.status == FitStatus.HULL_MISSING
    assert result.k == 0
    assert result.ships_availability == ShipsAvailabilityResult(1, 0, 1, 2, ShipsStatus.DEFICIT, False)
    assert fit.status_cache == FitStatus.HULL_MISSING
    assert fit.fits_available == 0
    assert fit.contracts_available == 1
    assert fit.ships_status_cache == ShipsStatus.DEFICIT

    pure = engine.calc_ships_availability(3, 1, 0, ContractFitStatus.DEFICIT, FitStatus.HULL_MISSING)
    assert pure == result.ships_availability
    assert pure.is_partial is False


def test_no_data_source_excluded_from_summary_and_shown_as_dash(viewer_client, sde_ids):  # noqa: F811
    """NO_DATA в контрактах: C+K не считается, сводка исключает фит, UI показывает «—»."""
    fit = _seed_reference_market(sde_ids, name="No contract data")
    # Провайдера контрактов нет: contracts_status=no_data (§6.12).
    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()

    ships = result.ships_availability
    assert ships.ships_in_contracts is None
    assert ships.ships_from_market == 10
    assert ships.ships_total is None
    assert ships.ships_deficit is None
    assert ships.ships_status == ShipsStatus.NO_DATA
    assert ships.is_partial is True

    # Рыночные D_i, unit, стоимость и статус остаются эталонными (§19.20).
    assert fit.status_cache == FitStatus.DEFICIT
    assert fit.fits_available == 10
    assert fit.total_cost == D("1269912500")
    assert result.components[0].deficit == 20
    assert fit.ships_status_cache == ShipsStatus.NO_DATA

    with _no_esi():
        index_response = viewer_client.get(reverse("marketmonitoring:index"))
        detail_response = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit.pk]))
    assert index_response.status_code == 200
    assert detail_response.status_code == 200

    context = index_response.context
    assert context["ships_in_contracts_sum"] == 0
    assert context["ships_from_market_sum"] == 0
    assert context["ships_total_sum"] == 0
    row = context["rows"][0]
    assert row["ships_in_contracts"] is None
    assert row["ships_total"] is None
    assert row["ships_status"] == ShipsStatus.NO_DATA
    assert "&mdash;" in index_response.content.decode("utf-8")

    detail = detail_response.context
    assert detail["ships_in_contracts"] is None
    assert detail["ships_total"] is None
    assert detail["ships_deficit"] is None
    assert detail["ships_status"] == ShipsStatus.NO_DATA
    assert "&mdash;" in detail_response.content.decode("utf-8")


def test_missing_market_data_counts_zero_and_included_in_summary(viewer_client, sde_ids):  # noqa: F811
    """Нет StockLevel → A=0; дефицит/стоимость/C+K считаются общим правилом (§19.21)."""
    fit = _seed_reference_market(sde_ids, name="No market data")
    _seed_full_kit_contracts(fit, 7)
    # Нет агрегатов StockLevel по локации — её вклад в A равен 0, не NO_DATA.
    StockLevel.objects.all().delete()

    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()

    ships = result.ships_availability
    assert result.status == FitStatus.HULL_MISSING
    assert result.k == 0
    assert result.total_cost is not None
    assert ships.ships_in_contracts == 7
    assert ships.ships_from_market == 0
    assert ships.ships_total == 7
    assert ships.ships_deficit == 23
    assert ships.ships_status == ShipsStatus.DEFICIT
    assert ships.is_partial is False
    assert fit.status_cache == FitStatus.HULL_MISSING
    assert fit.contracts_status_cache == ContractFitStatus.DEFICIT
    assert fit.ships_status_cache == ShipsStatus.DEFICIT
    assert any("60003760" in reason for reason in result.reasons)

    with _no_esi():
        index_response = viewer_client.get(reverse("marketmonitoring:index"))
        detail_response = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit.pk]))
    context = index_response.context
    assert context["ships_in_contracts_sum"] == 7
    assert context["ships_from_market_sum"] == 0
    assert context["ships_total_sum"] == 7
    assert context["rows"][0]["ships_total"] == 7
    detail = detail_response.context
    assert detail["ships_total"] == 7
    assert detail["ships_deficit"] == 23
    assert detail["data_warnings"] is True  # предупреждение о локации без данных, не NO_DATA
    assert "Calculation scope is empty" not in detail_response.content.decode("utf-8")  # баннер NO_DATA не показан


def test_dashboard_sums_keep_fit_with_stale_npc_station(viewer_client, sde_ids):  # noqa: F811
    """Свежий фит даёт ΣC/ΣK/Σ(C+K); протухшая локация даёт нулевой вклад K (§19.18).

    Дашборд продолжает суммировать оба фита: нулевой вклад не превращает
    расчёт в partial, а итог Σ(C+K) не меняется (решение №45).
    """
    fresh_fit = _seed_reference_market(sde_ids, name="Fresh for sums")
    _seed_full_kit_contracts(fresh_fit, 7)
    fresh_result = calc_service.recalc_fit(fresh_fit)
    assert fresh_result.status == FitStatus.DEFICIT
    assert fresh_result.ships_availability.ships_total == 17

    stale_fit = Fit.objects.create(
        name="Stale station fit",
        hull_type_id=1,
        qty_target=5,
        scope_kind=ScopeKind.STATION,
        scope_ref=60003761,
    )
    FitComponent.objects.create(fit=stale_fit, type_id=1, qty_per_fit=1)
    MarketLocation.objects.create(
        name="Other station",
        kind=LocationKind.STATION,
        station_id=60003761,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        enabled=True,
        sync_status=SyncStatus.OK,
        last_synced_at=timezone.now(),
    )
    StockLevel.objects.create(
        location_id=60003761,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=1,
        total_sell_volume=10,
        updated_at=timezone.now() - timedelta(minutes=600),
    )
    stale_result = calc_service.recalc_fit(stale_fit)
    assert stale_result.status == FitStatus.HULL_MISSING, stale_result.reasons
    assert stale_result.k == 0
    assert stale_result.ships_availability.is_partial is False
    assert stale_result.ships_availability.ships_total == 0

    with _no_esi():
        response = viewer_client.get(reverse("marketmonitoring:index"))
    assert response.status_code == 200
    context = response.context
    assert context["ships_in_contracts_sum"] == 7
    assert context["ships_from_market_sum"] == 10
    assert context["ships_total_sum"] == 17
    rows = {row["fit"].name: row for row in context["rows"]}
    assert rows["Fresh for sums"]["ships_total"] == 17
    assert rows["Stale station fit"]["ships_total"] == 0


def test_dashboard_keeps_bottlenecks_and_warns_for_partially_unsynced_fit(viewer_client, sde_ids):  # noqa: F811
    """Частично несинканная локация: узкие места и сводка сохраняются, NO_DATA не показывается (§19.21)."""
    fit = Fit.objects.create(
        name="Partially synced fit",
        hull_type_id=1,
        qty_target=30,
        scope_kind=ScopeKind.SYSTEM,
        scope_ref=SYSTEM_ID,
    )
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    FitComponent.objects.create(fit=fit, type_id=2, qty_per_fit=3)
    _seed_full_kit_contracts(fit, 0)  # контракты известны — фит участвует в сводке C+K
    stale_station_id = 60003761
    now = timezone.now()
    for station_id in (STATION_ID, stale_station_id):
        MarketLocation.objects.create(
            name=f"Station {station_id}",
            kind=LocationKind.STATION,
            station_id=station_id,
            solar_system_id=SYSTEM_ID,
            region_id=REGION_ID,
            enabled=True,
            sync_status=SyncStatus.OK,
            last_synced_at=now,
        )
    StockLevel.objects.create(
        location_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=1,
        total_sell_volume=10,
        updated_at=now,
    )
    StockLevel.objects.create(
        location_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=2,
        total_sell_volume=100,
        updated_at=now,
    )
    # Протухшие объёмы второй станции больше, чем свежие: без исключения
    # вклад был бы 60/100 и узкое место было бы модулем, а не корпусом.
    StockLevel.objects.create(
        location_id=stale_station_id,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=1,
        total_sell_volume=50,
        updated_at=now - timedelta(days=2),
    )
    StockLevel.objects.create(
        location_id=stale_station_id,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=2,
        total_sell_volume=0,
        updated_at=now - timedelta(days=2),
    )

    result = calc_service.recalc_fit(fit)
    assert result.status == FitStatus.DEFICIT
    assert result.k == 10
    assert any(str(stale_station_id) in reason for reason in result.reasons)

    with _no_esi():
        response = viewer_client.get(reverse("marketmonitoring:index"))
    rows = {row["fit"].name: row for row in response.context["rows"]}
    row = rows["Partially synced fit"]
    assert row["bottleneck_names"] == ["Ferox Test Hull"]
    assert row["fit"].status_cache != FitStatus.NO_DATA
    assert row["ships_from_market"] == 10
    assert any(str(stale_station_id) in warning for warning in row["data_warnings"])
    assert response.context["no_data_count"] == 0
    assert response.context["ships_from_market_sum"] == 10

    parts = response.content.decode().split("<tr")
    index = next(i for i, part in enumerate(parts) if "Partially synced fit" in part)
    # Предупреждение рендерится отдельной строкой сразу после строки фита.
    row_html = parts[index] + (parts[index + 1] if index + 1 < len(parts) else "")
    assert "alert-warning" in row_html
    assert str(stale_station_id) in row_html  # предупреждение с id локации в строке дашборда
    assert "Calculation scope is empty" not in row_html  # NO_DATA-баннер не показан


def test_disabled_jita_does_not_show_stale_prices(viewer_client, sde_ids):  # noqa: F811
    """Выключенная Jita: цены докупки недоступны, но K/D_i/статус считаются по локальным рынкам.

    Уточнённый контракт: unit/ref/cost/shortfall/overprice_pct = None,
    Fit.total_cost = None, overprice_count = 0; полный NO_DATA из-за Jita
    не наступает (§6.7 — только про свежесть рыночных локаций).
    """
    fit = _seed_reference_market(sde_ids)
    baseline = calc_service.recalc_fit(fit)
    assert baseline.total_cost == D("1269912500")

    # Локацию выключаем, строки JitaOrderLevel намеренно оставляем в БД.
    MarketLocation.objects.filter(station_id=STATION_ID).update(enabled=False)
    settings_obj = PluginSetting.load()
    assert calc_service.get_jita_book(1, settings_obj.jita_book_depth) == ([], None)

    result = calc_service.recalc_fit(fit)
    fit.refresh_from_db()
    assert result.status == FitStatus.DEFICIT
    assert result.k == 10
    assert [c.deficit for c in result.components] == [20, 0, 20000]
    assert fit.status_cache == FitStatus.DEFICIT
    assert result.total_cost is None and fit.total_cost is None
    assert result.overprice_count == 0
    for component in result.components:
        assert component.unit is None
        assert component.ref is None
        assert component.cost is None
        assert component.shortfall is None
        assert component.is_not_covered is False
        assert component.is_overprice is False
        assert component.overprice_pct is None

    with _no_esi():
        detail_response = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit.pk]))
    assert detail_response.status_code == 200
    context = detail_response.context
    assert context["total_cost"] is None
    assert "&mdash;" in detail_response.content.decode("utf-8")
    rows = {row["component"].type_id: row for row in context["rows"]}
    for type_id in (1, 3):
        assert rows[type_id]["ref"] is None
        assert rows[type_id]["unit"] is None
        assert rows[type_id]["cost"] is None


def test_dashboard_sum_counts_per_fit_k_not_unique_modules(viewer_client, sde_ids):  # noqa: F811
    """Sum C=5, Sum K=8, Sum(C+K)=13 — K суммируется per fit, без двойного учёта."""
    fit_a = Fit.objects.create(
        name="Sum fit A", hull_type_id=1, qty_target=8, scope_kind=ScopeKind.STATION, scope_ref=STATION_ID
    )
    _set_cached_ships(
        fit_a,
        contracts_available=4,
        market_available=6,
        contracts_status=ContractFitStatus.DEFICIT,
        market_status=FitStatus.DEFICIT,
        ships_status=ShipsStatus.OK,
    )
    fit_b = Fit.objects.create(
        name="Sum fit B", hull_type_id=1, qty_target=5, scope_kind=ScopeKind.STATION, scope_ref=STATION_ID
    )
    _set_cached_ships(
        fit_b,
        contracts_available=1,
        market_available=2,
        contracts_status=ContractFitStatus.DEFICIT,
        market_status=FitStatus.DEFICIT,
        ships_status=ShipsStatus.DEFICIT,
    )
    fit_no_data = Fit.objects.create(
        name="Sum fit no data", hull_type_id=1, qty_target=5, scope_kind=ScopeKind.STATION, scope_ref=STATION_ID
    )
    _set_cached_ships(
        fit_no_data,
        contracts_available=None,
        market_available=None,
        contracts_status=ContractFitStatus.NO_DATA,
        market_status=FitStatus.NO_DATA,
        ships_status=ShipsStatus.NO_DATA,
    )

    with _no_esi():
        response = viewer_client.get(reverse("marketmonitoring:index"))
    assert response.status_code == 200
    context = response.context
    assert context["ships_in_contracts_sum"] == 5
    assert context["ships_from_market_sum"] == 8
    assert context["ships_total_sum"] == 13
    assert context["ships_total_sum"] != 21

    rows = {row["fit"].name: row for row in context["rows"]}
    assert rows["Sum fit A"]["ships_in_contracts"] == 4
    assert rows["Sum fit A"]["ships_from_market"] == 6
    assert rows["Sum fit A"]["ships_total"] == 10
    assert rows["Sum fit B"]["ships_total"] == 3
    assert rows["Sum fit no data"]["ships_total"] is None

    html = response.content.decode("utf-8")
    assert "Ships from market (K/N, per fit)" in html
    assert "total (\u03a3(C+K))" in html
    assert "on contracts (\u03a3C)" in html


def test_ui_shows_ships_columns_and_edit_only_refresh(viewer_client, editor_client, sde_ids):  # noqa: F811
    """_view видит C/K/итог/общий статус, но не refresh; карточка показывает те же величины."""
    fit = _seed_reference_market(sde_ids, name="UI ships fit", qty_target=20)
    _seed_full_kit_contracts(fit, 3)
    calc_service.recalc_fit(fit)

    with _no_esi():
        viewer_index = viewer_client.get(reverse("marketmonitoring:index"))
        viewer_detail = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit.pk]))
        editor_index = editor_client.get(reverse("marketmonitoring:index"))
    assert viewer_index.status_code == 200
    assert viewer_detail.status_code == 200
    assert editor_index.status_code == 200

    index_html = viewer_index.content.decode("utf-8")
    assert "Ships on contracts" in index_html
    assert "Ships from market (K/N, per fit)" in index_html
    assert "Collectability (C+K)" in index_html
    assert "Ships total (C+K)" not in index_html  # дубль удалён
    assert "DEFICIT" in index_html
    assert 'id="mm-refresh-btn"' not in index_html
    assert 'id="mm-refresh-btn"' in editor_index.content.decode("utf-8")

    detail_html = viewer_detail.content.decode("utf-8")
    assert "C (contracts)" in detail_html
    assert "K (market, per fit)" in detail_html
    assert "DEFICIT" in detail_html
    detail = viewer_detail.context
    assert detail["ships_in_contracts"] == 3
    assert detail["ships_from_market"] == 10
    assert detail["ships_total"] == 13
    assert detail["ships_deficit"] == 7
    assert detail["ships_status"] == ShipsStatus.DEFICIT


# --- §18.9: хэши, уведомления и Celery -------------------------------------


def test_ships_deficit_hash_is_separate_and_old_hashes_are_stable(sde_ids):  # noqa: F811
    """Старые хэши deficit/contracts_deficit не изменились, ships_deficit — отдельный."""
    old_deficit_hash = discord_notify.canonical_state_hash(
        {
            "fit_id": 1,
            "k": 10,
            "deficits": {"1": 20, "3": 20000},
            "total": "1269912500",
        }
    )
    old_contracts_hash = discord_notify.canonical_state_hash(
        {
            "event": "contracts_deficit",
            "fit_id": 1,
            "contracts_available": 7,
            "qty_target": 30,
        }
    )
    ships_hash = discord_notify.ships_deficit_state_hash(fit_id=1, ships_status=ShipsStatus.DEFICIT, ships_deficit=13)
    assert old_deficit_hash == EXPECTED_DEFICIT_HASH
    assert old_contracts_hash == EXPECTED_CONTRACTS_HASH
    assert ships_hash == EXPECTED_SHIPS_HASH
    assert ships_hash != old_deficit_hash
    assert ships_hash != old_contracts_hash
    assert ships_hash != discord_notify.ships_deficit_state_hash(1, ShipsStatus.DEFICIT, 14)


def test_beat_schedule_has_market_syncs_with_five_minute_tick_and_guards(sde_ids):  # noqa: F811
    """Рыночные sync_* и notify_deficit в beat каждые 5 мин; guard блокирует частые запуски."""
    # sync_jita_book guard проверяется на включённой локации — иначе задача
    # пропускается по jita_disabled раньше проверки интервала.
    MarketLocation.objects.create(
        name="Jita IV-4",
        kind=LocationKind.STATION,
        station_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        enabled=True,
        sync_status=SyncStatus.OK,
        last_synced_at=timezone.now(),
    )
    schedule = tasks.build_beat_schedule()
    expected_tasks = (
        "marketmonitoring.sync_jita_book",
        "marketmonitoring.sync_region_orders",
        "marketmonitoring.sync_structure_orders",
        "marketmonitoring.sync_contracts",
        "marketmonitoring.verify_providers",
        "marketmonitoring.notify_deficit",
    )
    for task_name in expected_tasks:
        entry = next(entry for entry in schedule.values() if entry["task"] == task_name)
        assert entry["schedule"] == tasks.BEAT_SYNC_INTERVAL_SECONDS == 300
        assert entry["options"]["trigger"] == TriggerKind.BEAT

    for task_name, fetch_name in (
        ("marketmonitoring.sync_jita_book", "fetch_jita_orders"),
        ("marketmonitoring.sync_region_orders", "fetch_region_orders"),
        ("marketmonitoring.sync_structure_orders", "fetch_structure_orders"),
        ("marketmonitoring.sync_contracts", "fetch_corporation_contracts"),
    ):
        now = timezone.now()
        SyncRun.objects.create(
            task_name=task_name,
            trigger=TriggerKind.BEAT,
            started_at=now,
            finished_at=now,
            status=SyncRunStatus.OK,
        )
        with mock.patch.object(esi_client, fetch_name) as fetch:
            result = getattr(tasks, task_name.rsplit(".", 1)[1])(trigger=TriggerKind.BEAT)
        fetch.assert_not_called()
        assert result == {"skipped": True}


def test_chain_recalc_and_notify_respects_trigger(db):
    """Только beat запускает notify_deficit; manual — только пересчёт и снятие блокировки."""
    with (
        mock.patch.object(tasks.recalc_stock_levels, "delay") as recalc,
        mock.patch.object(tasks.notify_deficit, "delay") as notify,
    ):
        tasks._chain_recalc_and_notify(TriggerKind.MANUAL, [], lock_token="tok-1")
        recalc.assert_called_once_with(
            trigger=TriggerKind.MANUAL, location_ids=[], lock_token="tok-1", zero_location_ids=None
        )
        notify.assert_not_called()

        tasks._chain_recalc_and_notify(TriggerKind.BEAT, None)
        assert recalc.call_count == 2
        notify.assert_called_once_with(trigger=TriggerKind.BEAT)


def test_notify_deficit_sends_ships_event_on_beat_and_dedupes(db):
    """beat: ships_deficit отправляется; повтор с тем же хэшем не отправляется."""
    WebhookConfig.objects.create(url="https://discord.example/ships", enabled=True)
    fit = Fit.objects.create(
        name="Ships notify fit",
        hull_type_id=1,
        qty_target=15,
        scope_kind=ScopeKind.STATION,
        scope_ref=STATION_ID,
    )
    result_stub = SimpleNamespace(
        status=FitStatus.OK,
        k=10,
        contracts_status=ContractFitStatus.OK,
        contracts_available=0,
        components=[],
        total_cost=D("0"),
        overprice_count=0,
    )
    ships_stub = ShipsAvailabilityResult(0, 10, 10, 5, ShipsStatus.DEFICIT, False)

    with (
        mock.patch("marketmonitoring.calc.service.recalc_fit", return_value=result_stub),
        mock.patch("marketmonitoring.calc.service.get_ships_availability", return_value=ships_stub) as get_ships,
        mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok) as post,
    ):
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
        assert result["ok"] is True
        assert result["sent"] == 1
        log = NotificationLog.objects.get(event=NotificationEvent.SHIPS_DEFICIT)
        assert log.state_hash == discord_notify.ships_deficit_state_hash(fit.pk, ShipsStatus.DEFICIT, 5)
        embed = post.call_args.kwargs["json"]["embeds"][0]
        assert embed["title"] == "SHIPS DEFICIT: Ships notify fit"
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        assert fields["Ships available (C+K)"] == "10/15"
        assert fields["Missing ships"] == "5"
        assert fields["On contracts (C)"] == "0"
        assert fields["From market (K)"] == "10"

        first_calls = post.call_count
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
        assert result["sent"] == 0
        assert post.call_count == first_calls
        assert NotificationLog.objects.filter(event=NotificationEvent.SHIPS_DEFICIT).count() == 1

        # Изменение состояния (дефицит 5 -> 6) отправляет сразу, без кулдауна.
        get_ships.return_value = ShipsAvailabilityResult(0, 10, 10, 6, ShipsStatus.DEFICIT, False)
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
        assert result["sent"] == 1
        assert NotificationLog.objects.filter(event=NotificationEvent.SHIPS_DEFICIT).count() == 2

        # Тот же хэш, но кулдаун 2 ч истёк — отправка возобновляется.
        settings_obj = PluginSetting.load()
        last_log = NotificationLog.objects.filter(event=NotificationEvent.SHIPS_DEFICIT).order_by("-id").first()
        last_log.sent_at = timezone.now() - timedelta(minutes=settings_obj.notification_deficit_cooldown_min + 1)
        last_log.save(update_fields=["sent_at"])
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
        assert result["sent"] == 1
        assert NotificationLog.objects.filter(event=NotificationEvent.SHIPS_DEFICIT).count() == 3


def test_manual_notify_does_not_emit_ships_deficit_or_change_cache(db):
    """manual-запуск не меняет ships_status_cache и не пишет лог уведомлений."""
    fit = Fit.objects.create(
        name="Manual ships fit",
        hull_type_id=1,
        qty_target=10,
        scope_kind=ScopeKind.STATION,
        scope_ref=STATION_ID,
        ships_status_cache=ShipsStatus.DEFICIT,
    )
    with mock.patch("marketmonitoring.notify.discord.requests.post") as post:
        result = tasks.notify_deficit(trigger=TriggerKind.MANUAL)
    assert result == {"skipped": True, "reason": "manual trigger"}
    post.assert_not_called()
    assert NotificationLog.objects.count() == 0
    fit.refresh_from_db()
    assert fit.ships_status_cache == ShipsStatus.DEFICIT


def test_recalc_all_is_idempotent_for_ships_cache(sde_ids):  # noqa: F811
    """Повторный recalc_all даёт те же кэши и сводку (без накопления состояния)."""
    fit = _seed_reference_market(sde_ids, name="Idempotent fit")
    _seed_full_kit_contracts(fit, 7)

    stats_first = calc_service.recalc_all()
    fit.refresh_from_db()
    snapshot_first = (
        fit.status_cache,
        fit.contracts_status_cache,
        fit.ships_status_cache,
        fit.fits_available,
        fit.contracts_available,
        fit.total_cost,
        fit.overprice_count,
    )
    ships_first = calc_service.get_ships_availability(fit)

    stats_second = calc_service.recalc_all()
    fit.refresh_from_db()
    snapshot_second = (
        fit.status_cache,
        fit.contracts_status_cache,
        fit.ships_status_cache,
        fit.fits_available,
        fit.contracts_available,
        fit.total_cost,
        fit.overprice_count,
    )
    ships_second = calc_service.get_ships_availability(fit)

    assert stats_first == stats_second == {"fits_ok": 1, "fits_failed": 0, "components_ok": 0, "components_failed": 0}
    assert snapshot_first == snapshot_second
    assert ships_first == ships_second
    assert ships_second.ships_total == 17
    assert ships_second.ships_deficit == 13
