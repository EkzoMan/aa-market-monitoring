"""Сервис пересчёта (этап 4): мост БД ↔ calc/engine.

Собирает примитивы (доступность A_i, стакан Jita, ref, свежесть §6.7) из
StockLevel / JitaOrderLevel / MarketLocation и вызывает чистые функции
calc/engine.py. UI и задачи не содержат бизнес-логики расчётов (§22.3).
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.db.models import Q
from django.utils import timezone

from ..constants import (
    ContractFitStatus,
    LocationKind,
    ScopeKind,
    ShipsStatus,
    SyncStatus,
)
from ..esi.selectors import is_jita_enabled
from ..models import (
    ComponentRequest,
    ContractCache,
    ContractProvider,
    Fit,
    JitaOrderLevel,
    MarketLocation,
    PluginSetting,
    StockLevel,
)
from . import engine
from .engine import (
    ComponentCalcResult,
    ComponentInput,
    ContractInput,
    ContractItemInput,
    ContractsFitCalcResult,
    OrderBookRow,
    ShipsAvailabilityResult,
)

logger = logging.getLogger("marketmonitoring")

# Сентинел: отличает «аргумент не передан» от явного None при пересчёте фита.
_UNSET = object()


class FitCalcResult:
    """Итог пересчёта фита: статус, K, сумма, счётчик оверпрайса, строки, предупреждения.

    ``reasons`` — диагностика локаций без свежих данных (§6.7, решение №45):
    concrete id и причина. Эти причины НЕ меняют статус фита: локация без
    данных даёт нулевой вклад в доступность, ``NO_DATA`` остаётся только для
    пустой области. Причины НЕ сохраняются в модель — в §8 поля нет, и
    расширять модели запрещено; UI получает их из результата пересчёта.
    """

    def __init__(
        self,
        status: str,
        k: int | None,
        total_cost: Decimal | None,
        overprice_count: int | None,
        components: list[ComponentCalcResult],
        reasons: list[str] | None = None,
        contracts_status: str = ContractFitStatus.NO_DATA,
        contracts_available: int | None = None,
        contracts_deficit: int | None = None,
        contracts_reasons: list[str] | None = None,
        ships_availability: ShipsAvailabilityResult | None = None,
    ):
        self.status = status
        self.k = k
        self.total_cost = total_cost
        self.overprice_count = overprice_count
        self.components = components
        self.reasons = reasons or []
        self.contracts_status = contracts_status
        self.contracts_available = contracts_available
        self.contracts_deficit = contracts_deficit
        self.contracts_reasons = contracts_reasons or []
        self.ships_availability = ships_availability


def get_ships_availability(
    fit: Fit,
    *,
    contracts_available: int | None | object = _UNSET,
    market_available: int | None | object = _UNSET,
    contracts_status: str | None | object = _UNSET,
    market_status: str | None | object = _UNSET,
) -> ShipsAvailabilityResult:
    """Сводная доступность кораблей фита C+K (§6.13, решение №44).

    По умолчанию читает уже сохранённый кэш ``Fit`` (только БД плагина, без ESI).
    Необязательные аргументы позволяют передать ещё не сохранённые результаты
    текущего пересчёта — движок получает только примитивы и строки статусов.
    """
    return engine.calc_ships_availability(
        qty_target=fit.qty_target,
        contracts_available=fit.contracts_available if contracts_available is _UNSET else contracts_available,
        market_available=fit.fits_available if market_available is _UNSET else market_available,
        contracts_status=fit.contracts_status_cache if contracts_status is _UNSET else contracts_status,
        market_status=fit.status_cache if market_status is _UNSET else market_status,
    )


# ---------------------------------------------------------------------------
# Области (§6.1)
# ---------------------------------------------------------------------------


def _npc_station_ids(scope_kind: str, scope_ref: int) -> set[int]:
    """NPC-станции области (§6.1) — из справочника SDE через eveuniverse.

    Для scope=station — сама станция; system — станции системы; region —
    станции региона. Своих копий SDE нет (§8, решение 17).
    """
    EveSolarSystem = apps.get_model("eveuniverse", "EveSolarSystem")
    if scope_kind == ScopeKind.STATION:
        return {scope_ref}
    if scope_kind == ScopeKind.SYSTEM:
        # values_list по обратной FK — LEFT JOIN: система без станций даёт строку NULL.
        # None в локациях области ломает выборки (start_location_id__in=[None]) — исключаем.
        return {
            station_id
            for station_id in EveSolarSystem.objects.filter(id=scope_ref).values_list("eve_stations__id", flat=True)
            if station_id is not None
        }
    if scope_kind == ScopeKind.REGION:
        return {
            station_id
            for station_id in EveSolarSystem.objects.filter(eve_constellation__eve_region_id=scope_ref).values_list(
                "eve_stations__id", flat=True
            )
            if station_id is not None
        }
    logger.warning("Unknown scope kind: %s", scope_kind)
    return set()


def _enabled_structure_ids(scope_kind: str, scope_ref: int) -> set[int]:
    """Включённые структуры области (§6.1): только MarketLocation с enabled=true.

    Отключённая структура не учитывается (§6.1, §18.3).
    """
    if scope_kind == ScopeKind.STATION:
        return set()
    qs = MarketLocation.objects.filter(enabled=True, kind=LocationKind.STRUCTURE, structure_id__isnull=False)
    if scope_kind == ScopeKind.SYSTEM:
        qs = qs.filter(solar_system_id=scope_ref)
    elif scope_kind == ScopeKind.REGION:
        qs = qs.filter(region_id=scope_ref)
    else:
        logger.warning("Unknown scope kind: %s", scope_kind)
        return set()
    return set(qs.values_list("structure_id", flat=True))


def resolve_scope_ids(scope_kind: str, scope_ref: int) -> set[int]:
    """Множество локаций L(S) области (§6.1): NPC-станции ∪ включённые структуры."""
    return _npc_station_ids(scope_kind, scope_ref) | _enabled_structure_ids(scope_kind, scope_ref)


def _enabled_contract_system_ids(scope_kind: str, scope_ref: int) -> set[int]:
    """Включённые локации-системы внутри области (поиск контрактов, §6.12).

    Локация-система (MarketLocation kind=system) расширяет область поиска
    контрактов: если система входит в область фита (station — система этой
    станции; system — сама система; region — система региона), контракты
    учитываются по всем локациям выдачи этой системы.
    """
    qs = MarketLocation.objects.filter(enabled=True, kind=LocationKind.SYSTEM)
    if scope_kind == ScopeKind.SYSTEM:
        qs = qs.filter(solar_system_id=scope_ref)
    elif scope_kind == ScopeKind.REGION:
        qs = qs.filter(region_id=scope_ref)
    elif scope_kind == ScopeKind.STATION:
        EveStation = apps.get_model("eveuniverse", "EveStation")
        system_id = EveStation.objects.filter(pk=scope_ref).values_list("eve_solar_system_id", flat=True).first()
        if system_id is None:
            return set()
        qs = qs.filter(solar_system_id=system_id)
    else:
        logger.warning("Unknown scope kind: %s", scope_kind)
        return set()
    # solar_system_id nullable: NULL у некорректных записей не должен попадать в системы.
    return {system_id for system_id in qs.values_list("solar_system_id", flat=True) if system_id is not None}


def resolve_contract_location_ids(scope_kind: str, scope_ref: int) -> set[int]:
    """Локации выдачи контрактов для области фита (§6.12).

    L(S) области ∪ все локации выдачи включённых локаций-систем области:
    NPC-станции системы (SDE) + структуры системы из MarketLocation.
    Структуры без записи MarketLocation не резолвятся — их start_location_id
    остаётся за пределами поиска (неизвестна система выдачи).
    """
    location_ids = resolve_scope_ids(scope_kind, scope_ref)
    EveSolarSystem = apps.get_model("eveuniverse", "EveSolarSystem")
    for system_id in _enabled_contract_system_ids(scope_kind, scope_ref):
        # Тот же LEFT JOIN-NULL: система без станций не должна давать локацию None.
        location_ids |= {
            station_id
            for station_id in EveSolarSystem.objects.filter(id=system_id).values_list("eve_stations__id", flat=True)
            if station_id is not None
        }
        location_ids |= set(
            MarketLocation.objects.filter(
                enabled=True, kind=LocationKind.STRUCTURE, structure_id__isnull=False, solar_system_id=system_id
            ).values_list("structure_id", flat=True)
        )
    return location_ids


# ---------------------------------------------------------------------------
# Свежесть (§6.7)
# ---------------------------------------------------------------------------


def _freshness_details(location_ids: set[int], stale_after_min: int) -> tuple[set[int], list[str]]:
    """Локации со свежими данными и причины отсутствия данных (§6.7, решение №45).

    Возвращает ``(fresh_location_ids, reasons)``. Причины содержат конкретные
    id локаций: структура с ``sync_status != ok`` (в т.ч. ``no_provider``),
    протухший ``last_synced_at``, отсутствие свежего ``StockLevel``, региональный
    синк региона не завершался успешно за период. ``fresh_location_ids``
    используется для нулевого вклада проблемных локаций в ``A_t^l`` (§6.2):
    ``check_freshness`` остаётся только диагностикой и не блокирует расчёт.

    Региональная проверка применяется только к NPC-станциям: структуры
    синкаются структурным синком. NPC-станция без записи MarketLocation тоже
    проверяется: её регион резолвится через eveuniverse (SDE, §8). Пустой
    ``last_synced_at`` у NPC-станции не считается протухшим, если свеж
    региональный ``StockLevel``; протухшее значение даёт предупреждение.
    """
    if not location_ids:
        return set(), []
    stale_cutoff = timezone.now() - timedelta(minutes=stale_after_min)
    reasons: list[str] = []
    bad_locations: set[int] = set()
    locations = {}
    qs = MarketLocation.objects.filter(Q(station_id__in=location_ids) | Q(structure_id__in=location_ids))
    for loc in qs:
        location_id = loc.station_id if loc.kind == LocationKind.STATION else loc.structure_id
        locations[location_id] = loc

    # Регион каждой NPC-станции: из записи MarketLocation либо из SDE.
    region_by_station: dict[int, int] = {}
    stations_without_record: list[int] = []
    for location_id in location_ids:
        location = locations.get(location_id)
        if location is not None and location.kind == LocationKind.STATION:
            region_by_station[location_id] = location.region_id
        elif location is None:
            stations_without_record.append(location_id)
    if stations_without_record:
        EveStation = apps.get_model("eveuniverse", "EveStation")
        resolved = EveStation.objects.filter(pk__in=stations_without_record).values_list(
            "pk", "eve_solar_system__eve_constellation__eve_region_id"
        )
        unresolved: list[int] = []
        for station_id, region_id in resolved:
            if region_id is None:
                unresolved.append(station_id)
            else:
                region_by_station[station_id] = region_id
        for station_id in unresolved:
            bad_locations.add(station_id)
            reasons.append(f"station {station_id}: cannot resolve region from SDE reference")

    for location_id in sorted(location_ids):
        location = locations.get(location_id)
        if location is None:
            # NPC-станция без записи MarketLocation — данные приходят
            # региональным синком; свежесть оценивается проверкой ниже.
            continue
        if location.kind == LocationKind.STRUCTURE:
            # Структура: sync_status != ok или провайдера нет → вклад 0 (§6.7);
            # региональная проверка к структурам не применяется.
            if location.sync_status != SyncStatus.OK:
                bad_locations.add(location_id)
                reasons.append(f"structure {location_id}: sync_status={location.sync_status}")
                continue
            if location.last_synced_at is None or location.last_synced_at < stale_cutoff:
                bad_locations.add(location_id)
                reasons.append(f"structure {location_id}: stale")
        elif location.last_synced_at is not None and location.last_synced_at < stale_cutoff:
            # NPC-станция: протухшая запись даёт предупреждение. Отсутствие
            # last_synced_at проверяется региональным кэшем ниже.
            bad_locations.add(location_id)
            reasons.append(f"location {location_id}: stale")

    # Региональный синк региона должен завершаться успешно за период (§6.7):
    # для каждой нужной NPC-станции региона требуется свежая строка StockLevel.
    stations_by_region: dict[int, set[int]] = {}
    for station_id, region_id in region_by_station.items():
        stations_by_region.setdefault(region_id, set()).add(station_id)
    for region_id in sorted(stations_by_region):
        station_set = stations_by_region[region_id]
        fresh_stations = set(
            StockLevel.objects.filter(location_id__in=station_set, updated_at__gte=stale_cutoff).values_list(
                "location_id", flat=True
            )
        )
        missing = station_set - fresh_stations
        for station_id in missing:
            bad_locations.add(station_id)
        if missing:
            reasons.append(f"region {region_id}: no successful region sync in period (stations {sorted(missing)})")

    # Структура без единого свежего агрегата тоже даёт вклад 0 (§6.7, §5.12).
    # Для NPC-станций этот случай уже покрыт региональной проверкой выше.
    fresh_stock = set(
        StockLevel.objects.filter(location_id__in=location_ids, updated_at__gte=stale_cutoff).values_list(
            "location_id", flat=True
        )
    )
    missing_stock = {
        location_id
        for location_id in location_ids
        if location_id not in fresh_stock and location_id not in bad_locations
    }
    if missing_stock:
        reasons.append(f"locations {sorted(missing_stock)}: no fresh StockLevel aggregate")
        bad_locations |= missing_stock
    return location_ids - bad_locations, reasons


def check_freshness(location_ids: set[int], stale_after_min: int) -> tuple[bool, list[str]]:
    """Диагностика свежести нужных локаций (§6.7); не меняет статус фита.

    Возвращает ``(fresh, reasons)``, где ``reasons`` — конкретные id локаций
    без данных и причины. Результат ``fresh`` используется только в диагностике:
    согласно решению №45 локация без данных даёт нулевой вклад в доступность,
    а ``NO_DATA`` назначается только при пустой области.
    """
    if not location_ids:
        return True, []
    _fresh_ids, reasons = _freshness_details(location_ids, stale_after_min)
    return not reasons, reasons


def check_contracts_freshness(stale_after_min: int) -> tuple[bool, list[str]]:
    """Проверка свежести кэша контрактов (§6.12).

    Свежими данные считаются, если есть хотя бы один включённый ContractProvider
    с ``sync_status=ok`` и ``last_synced_at`` не старше ``stale_after_min``.
    """
    providers = list(ContractProvider.objects.filter(enabled=True).order_by("priority", "pk"))
    if not providers:
        return False, ["no enabled contract providers"]
    stale_cutoff = timezone.now() - timedelta(minutes=stale_after_min)
    provider_reasons: list[str] = []
    any_fresh_ok = False
    for provider in providers:
        if provider.sync_status != SyncStatus.OK:
            provider_reasons.append(
                f"contract provider {provider.character_id} ({provider.source_kind}): sync_status={provider.sync_status}"
            )
            continue
        if provider.last_synced_at is None or provider.last_synced_at < stale_cutoff:
            provider_reasons.append(f"contract provider {provider.character_id} ({provider.source_kind}): stale")
            continue
        any_fresh_ok = True
    if any_fresh_ok:
        return True, []
    return False, provider_reasons


def evaluate_fit_contracts(
    fit: Fit,
    settings_obj: PluginSetting | None = None,
    scope_cache: dict[tuple[str, int], set[int]] | None = None,
) -> tuple[ContractsFitCalcResult, list[tuple[ContractCache, int]], list[str]]:
    """Оценка достаточности готовых кораблей в фите по открытым контрактам (§6.12).

    Читает только кэш БД плагина (ContractCache + prefetch items, без N+1 и без ESI)
    и вызывает чистую функцию ``engine.calc_contracts_fit``. Возвращает
    ``(результат_ядра, [(ContractCache, ships_count), ...], причины_NO_DATA)``.
    """
    if settings_obj is None:
        settings_obj = PluginSetting.load()
    required_components = list(fit.components.all().order_by("pk").values_list("type_id", "qty_per_fit"))
    if not required_components:
        empty_res = engine.calc_contracts_fit([], [], fit.qty_target, has_no_data=True)
        return empty_res, [], ["fit has no components"]

    fresh, contract_reasons = check_contracts_freshness(settings_obj.data_stale_after_min)
    if not fresh:
        no_data_res = engine.calc_contracts_fit([], required_components, fit.qty_target, has_no_data=True)
        return no_data_res, [], contract_reasons

    # Локации выдачи контрактов: L(S) области ∪ локации выдачи включённых
    # локаций-систем области (§6.12, поиск контрактов по системам).
    if scope_cache is not None:
        cache_key = ("contracts", fit.scope_kind, fit.scope_ref)
        if cache_key not in scope_cache:
            scope_cache[cache_key] = resolve_contract_location_ids(fit.scope_kind, fit.scope_ref)
        location_ids = scope_cache[cache_key]
    else:
        location_ids = resolve_contract_location_ids(fit.scope_kind, fit.scope_ref)

    if not location_ids:
        no_scope_res = engine.calc_contracts_fit([], required_components, fit.qty_target, has_no_data=True)
        return no_scope_res, [], [f"scope {fit.scope_kind}/{fit.scope_ref}: no locations resolved"]

    now = timezone.now()
    contracts_list = list(
        ContractCache.objects.filter(
            status="outstanding",
            contract_type="item_exchange",
            date_expired__gt=now,
            start_location_id__in=location_ids,
        )
        .prefetch_related("items")
        .order_by("date_expired", "contract_id")
    )
    contract_by_id = {c.contract_id: c for c in contracts_list}
    contract_inputs = [
        ContractInput(
            contract_id=c.contract_id,
            items=[
                ContractItemInput(
                    type_id=item.type_id,
                    quantity=int(item.quantity),
                    is_included=item.is_included,
                    raw_quantity=item.raw_quantity,
                )
                for item in c.items.all()
            ],
        )
        for c in contracts_list
    ]
    calc_res = engine.calc_contracts_fit(
        contracts=contract_inputs,
        required_components=required_components,
        qty_target=fit.qty_target,
        has_no_data=False,
    )
    matched_rows = [
        (contract_by_id[m.contract_id], m.ships_count)
        for m in calc_res.matched_contracts
        if m.contract_id in contract_by_id
    ]
    return calc_res, matched_rows, []


# ---------------------------------------------------------------------------
# Доступность (§6.2)
# ---------------------------------------------------------------------------


def locations_with_stock(location_ids: set[int]) -> set[int]:
    """Локации, по которым есть хотя бы один агрегат StockLevel (§6.7).

    Один запрос без фильтра по типу: §6.7 требует наличия агрегата **по
    локации**, а не по каждой паре (локация, тип). Отсутствие лотов конкретного
    типа на свежей локации означает A=0, а не NO_DATA — иначе добавление новой
    структуры в область превращало бы все фиты в NO_DATA.
    """
    if not location_ids:
        return set()
    return set(StockLevel.objects.filter(location_id__in=location_ids).values_list("location_id", flat=True).distinct())


def get_availability(
    type_id: int,
    scope_kind: str,
    scope_ref: int,
    *,
    location_ids: set[int] | None = None,
    fresh_locations: set[int] | None = None,
) -> tuple[int, bool]:
    """Доступность A_t^S (§6.2) по агрегату StockLevel области.

    Строгий scope (§5.1): station — только заказы этой станции; system/region —
    NPC-станции + включённые структуры; order_range не используется (§5.8).
    Возвращает ``(A, scope_resolved)``: второй флаг означает только то, что
    область не пуста (§6.7), а не «данные есть по всем локациям». Локация без
    свежих данных даёт вклад 0 (решение №45): если передан ``fresh_locations``
    из :func:`_freshness_details`, её ``StockLevel`` не участвует в сумме.
    Отсутствие лотов нужного типа — это A=0, а не NO_DATA. При ``location_ids``
    резолв области не повторяется, без N+1 (§22.4).
    """
    if location_ids is None:
        location_ids = resolve_scope_ids(scope_kind, scope_ref)
    if not location_ids:
        return 0, False
    usable_ids = location_ids if fresh_locations is None else (location_ids & fresh_locations)
    if not usable_ids:
        return 0, True
    rows = tuple(
        StockLevel.objects.filter(type_id=type_id, location_id__in=usable_ids).values_list(
            "location_id", "total_sell_volume"
        )
    )
    total = sum(volume for _location_id, volume in rows)
    return int(total), True


# ---------------------------------------------------------------------------
# Стакан Jita (§6.6, §6.8)
# ---------------------------------------------------------------------------


def get_jita_book(type_id: int, depth: int) -> tuple[list[OrderBookRow], Decimal | None]:
    """Стакан Jita: топ-depth sell-лотов ascending по цене + ref (мин. цена).

    UI никогда не обращается к ESI — читается только кэш JitaOrderLevel (§5.10).
    Если локация Jita отключена/удалена, возвращает пустой стакан и ref=None:
    даже при забытых строках кэша отключённый рынок не даёт stale-цену
    (явное указание заказчика).
    """
    if not is_jita_enabled():
        return [], None
    rows = JitaOrderLevel.objects.filter(type_id=type_id).order_by("rank")[:depth]
    book = [OrderBookRow(price=row.price, volume=row.volume) for row in rows]
    ref = book[0].price if book else None
    return book, ref


# ---------------------------------------------------------------------------
# packaged_volume (§7.6) — из SDE через eveuniverse
# ---------------------------------------------------------------------------

_packaged_volumes_cache: dict[int, Decimal] | None = None


def _packaged_volumes(refresh: bool = False) -> dict[int, Decimal]:
    """packaged_volume типов расчёта из SDE (eveuniverse), §7.6.

    packaged_volume отсутствует/0 → volume; и он 0 → доставка по позиции 0.
    Кэш сбрасывается в начале recalc_all (каждый прогон — свежие данные SDE).
    """
    global _packaged_volumes_cache
    if _packaged_volumes_cache is not None and not refresh:
        return _packaged_volumes_cache
    EveType = apps.get_model("eveuniverse", "EveType")
    volumes: dict[int, Decimal] = {}
    for eve_type in EveType.objects.filter(pk__in=_calc_type_ids()):
        packaged = getattr(eve_type, "packaged_volume", None)
        if not packaged:
            packaged = getattr(eve_type, "volume", None)
        volumes[eve_type.pk] = Decimal(str(packaged)) if packaged else Decimal("0")
    _packaged_volumes_cache = volumes
    return volumes


def _calc_type_ids() -> set[int]:
    """type_id всех позиций расчёта (фиты + компоненты вне фитов)."""
    type_ids = set(Fit.objects.values_list("hull_type_id", flat=True))
    type_ids.update(Fit.objects.values_list("components__type_id", flat=True))
    type_ids.update(ComponentRequest.objects.values_list("type_id", flat=True))
    type_ids.discard(None)
    return type_ids


# ---------------------------------------------------------------------------
# Пересчёт фитов и компонентов вне фитов (§6, §6.10)
# ---------------------------------------------------------------------------


def _component_scopes(
    fit: Fit, scope_cache: dict[tuple[str, int], set[int]]
) -> list[tuple[object, str, int, set[int]]]:
    """Эффективные области позиций фита (§6.3, override компонента) с кешем резолва.

    Один резолв на уникальную область, без повторных запросов по компонентам (§22.4).
    """
    rows = []
    for component in fit.components.all().order_by("pk"):
        scope_kind = component.scope_override_kind or fit.scope_kind
        scope_ref = component.scope_override_ref if component.scope_override_kind else fit.scope_ref
        rows.append((component, scope_kind, scope_ref, _resolve_scope_cached(scope_cache, scope_kind, scope_ref)))
    return rows


def _component_inputs(
    fit: Fit,
    scope_cache: dict[tuple[str, int], set[int]],
    *,
    fresh_locations: set[int] | None = None,
    component_scopes: list[tuple[object, str, int, set[int]]] | None = None,
) -> tuple[list[tuple[ComponentInput, str, int, bool]], bool, dict[tuple[str, int], set[int]]]:
    """Примитивы позиций фита с эффективной областью (§6.3, override компонента).

    Возвращает ``((ComponentInput, scope_kind, scope_ref, scope_resolved), ...)``,
    признак «все области непусты» и словарь областей с id локаций без агрегата
    StockLevel / без зарезолвенных локаций — только для предупреждений (§6.7).
    Пустая область — единственный случай для NO_DATA (§6.4, решение №45);
    отсутствие данных по локации даёт нулевой вклад через ``fresh_locations``
    и в диагностических ``reasons`` фита, но не меняет статус. Область компонента —
    scope_override, иначе область фита (§6.3, решение 11). Проверка агрегатов —
    один запрос на объединение локаций всех компонентов, без N+1 (§22.4).
    """
    result = []
    packaged_volumes = _packaged_volumes(refresh=False)
    scope_flags: list[bool] = []
    missing_by_scope: dict[tuple[str, int], set[int]] = {}
    scope_rows = component_scopes if component_scopes is not None else _component_scopes(fit, scope_cache)
    needed_locations: set[int] = set()
    for _component, _scope_kind, _scope_ref, scope_ids in scope_rows:
        needed_locations |= scope_ids
        if not scope_ids:
            # Пустая область — причина NO_DATA (§6.4); отделяем её от отсутствия агрегата.
            missing_by_scope.setdefault((_scope_kind, _scope_ref), set())
    present_locations = locations_with_stock(needed_locations)
    for component, scope_kind, scope_ref, scope_ids in scope_rows:
        missing = scope_ids - present_locations
        if missing:
            missing_by_scope.setdefault((scope_kind, scope_ref), set()).update(missing)
        availability, scope_resolved = get_availability(
            component.type_id,
            scope_kind,
            scope_ref,
            location_ids=scope_ids,
            fresh_locations=fresh_locations,
        )
        scope_flags.append(scope_resolved)
        result.append(
            (
                ComponentInput(
                    type_id=component.type_id,
                    qty_per_fit=component.qty_per_fit,
                    availability=availability,
                    packaged_volume=packaged_volumes.get(component.type_id, Decimal("0")),
                    is_hull=component.type_id == fit.hull_type_id,
                ),
                scope_kind,
                scope_ref,
                scope_resolved,
            )
        )
    return result, all(scope_flags), missing_by_scope


def _resolve_scope_cached(scope_cache: dict[tuple[str, int], set[int]], scope_kind: str, scope_ref: int) -> set[int]:
    """resolve_scope_ids с кешем внутри одного пересчёта (устранение дублирования)."""
    key = (scope_kind, scope_ref)
    if key not in scope_cache:
        scope_cache[key] = resolve_scope_ids(scope_kind, scope_ref)
    return scope_cache[key]


def _save_fit_cache(
    fit: Fit,
    status: str,
    k: int | None,
    total_cost: Decimal | None,
    overprice_count: int | None,
    contracts_status: str = ContractFitStatus.NO_DATA,
    contracts_available: int | None = None,
    ships_status: str = ShipsStatus.NO_DATA,
) -> None:
    """Запись кэша расчёта в Fit (контракты + рынок + общий статус + last_checked_at)."""
    fit.status_cache = status
    fit.fits_available = k
    fit.total_cost = total_cost
    fit.overprice_count = overprice_count
    fit.contracts_status_cache = contracts_status
    fit.contracts_available = contracts_available
    fit.ships_status_cache = ships_status
    fit.last_checked_at = timezone.now()
    fit.save(
        update_fields=[
            "status_cache",
            "fits_available",
            "total_cost",
            "overprice_count",
            "contracts_status_cache",
            "contracts_available",
            "ships_status_cache",
            "last_checked_at",
        ]
    )


def recalc_fit(fit: Fit) -> FitCalcResult:
    """Пересчёт одного фита (§6, §6.12) и запись кэша статуса в Fit (§12.1).

    В первую очередь оценивается достаточность готовых кораблей в открытых
    контрактах (§6.12), затем рыночная собираемость компонентов (§6.1–§6.9).
    Свежесть локаций (§6.7) — только диагностика: локация без данных даёт
    нулевой вклад в доступность (§5.12, решение №45), а ``NO_DATA`` вызывается
    ``engine.calc_fit_status`` только при пустой эффективной области (§6.4).
    Дефицит — независимо по каждому фиту (§5.5). При выключенной Jita цена
    докупки недоступна (None), но статус, K и D_i сохраняются (решение №43).
    """
    settings_obj = PluginSetting.load()
    scope_cache: dict[tuple[str, int], set[int]] = {}
    contracts_res, _matched_contracts, contracts_reasons = evaluate_fit_contracts(
        fit, settings_obj=settings_obj, scope_cache=scope_cache
    )
    # Отдельный кеш: контрактная область (§6.12) и рыночная L(S) (§6.1) —
    # разные множества локаций, хотя ключ у них один и тот же.
    market_scope_cache: dict[tuple[str, int], set[int]] = {}
    component_scopes = _component_scopes(fit, market_scope_cache)
    needed_location_ids: set[int] = set()
    for _component, _scope_kind, _scope_ref, scope_ids in component_scopes:
        needed_location_ids |= scope_ids
    # Один проход по свежести на все локации фита: причины и set пригодных
    # локаций для нулевого вклада отсутствующих данных (§6.7, решение №45).
    fresh_locations, reasons = _freshness_details(needed_location_ids, settings_obj.data_stale_after_min)
    component_rows_data, all_components_have_scope, missing_by_scope = _component_inputs(
        fit, market_scope_cache, fresh_locations=fresh_locations, component_scopes=component_scopes
    )
    component_rows = component_rows_data

    # Причины с конкретными id локаций — предупреждения, а не основание для NO_DATA.
    for (scope_kind, scope_ref), missing in sorted(missing_by_scope.items()):
        if not missing:
            reasons.append(f"scope {scope_kind}/{scope_ref}: no locations resolved")
        else:
            reasons.append(f"scope {scope_kind}/{scope_ref}: no StockLevel aggregate for locations {sorted(missing)}")

    if not component_rows or not all_components_have_scope:
        # Пустая область (или нет состава — решение №43): собираемость неизвестна.
        # engine.calc_fit_status — единственное место выбора NO_DATA (§6.4).
        status = engine.calc_fit_status(True, 0, 0, fit.qty_target, allow_hull_missing=True)
        ships = get_ships_availability(
            fit,
            contracts_available=contracts_res.ships_available,
            market_available=None,
            contracts_status=contracts_res.status,
            market_status=status,
        )
        _save_fit_cache(
            fit,
            status,
            None,
            None,
            None,
            contracts_status=contracts_res.status,
            contracts_available=contracts_res.ships_available,
            ships_status=ships.ships_status,
        )
        return FitCalcResult(
            status,
            None,
            None,
            None,
            [],
            reasons,
            contracts_status=contracts_res.status,
            contracts_available=contracts_res.ships_available,
            contracts_deficit=contracts_res.deficit,
            contracts_reasons=contracts_reasons,
            ships_availability=ships,
        )

    # Стаканы и ref по каждому типу — из кэша JitaOrderLevel, без ESI (§5.10);
    # один запрос на все type_id (без N+1, §22.4).
    type_ids = [c.type_id for c, _k, _r, _d in component_rows]
    depth = settings_obj.jita_book_depth
    books: dict[int, list[OrderBookRow]] = {}
    refs: dict[int, Decimal] = {}
    # Один .exists() на пересчёт: отключённая Jita не даёт цен даже из уцелевших
    # строк кэша (stale-цена), но статус/K/D_i считаются по локальным рынкам.
    jita_price_available = is_jita_enabled()
    if jita_price_available:
        for level in JitaOrderLevel.objects.filter(type_id__in=type_ids).order_by("type_id", "rank"):
            book = books.setdefault(level.type_id, [])
            if len(book) < depth:
                book.append(OrderBookRow(price=level.price, volume=level.volume))
    for type_id, book in books.items():
        refs[type_id] = book[0].price

    # K по составу фита (§6.3); дефициты и стоимость по каждой позиции (§6.5–6.9).
    inputs = [c for c, _k, _r, _d in component_rows]
    k, _bottlenecks = engine.calc_fits([c.availability for c in inputs], inputs)
    results = [
        engine.calc_component(
            component=component_input,
            qty_target=fit.qty_target,
            book=books.get(component_input.type_id, []),
            ref_price=refs.get(component_input.type_id),
            markup_pct=settings_obj.jita_markup_pct,
            delivery_rate=settings_obj.delivery_rate_isk_per_m3,
            overprice_threshold_pct=settings_obj.overprice_threshold_pct,
        )
        for component_input, _k, _r, _d in component_rows
    ]

    if not jita_price_available:
        # Выключенная Jita: цена докупки недоступна, но статус/K/D_i не меняются.
        results = [engine.price_unavailable(result) for result in results]
    hull_available_calc = next((c.availability for c in inputs if c.is_hull), 0)
    # Фит: NO_DATA уже исключён, HULL_MISSING/DEFICIT/OK — в engine (§6.4).
    status = engine.calc_fit_status(False, hull_available_calc, k, fit.qty_target, allow_hull_missing=True)
    total_cost = None if not jita_price_available else sum((r.cost for r in results), Decimal("0"))
    overprice_count = 0 if not jita_price_available else sum(1 for r in results if r.is_overprice)
    ships = get_ships_availability(
        fit,
        contracts_available=contracts_res.ships_available,
        market_available=k,
        contracts_status=contracts_res.status,
        market_status=status,
    )
    _save_fit_cache(
        fit,
        status,
        k,
        total_cost,
        overprice_count,
        contracts_status=contracts_res.status,
        contracts_available=contracts_res.ships_available,
        ships_status=ships.ships_status,
    )
    logger.debug(
        "Fit %s recalculated: contracts=%s(%s) market=%s(%s) ships=%s(%s) warnings=%s",
        fit.pk,
        contracts_res.status,
        contracts_res.ships_available,
        status,
        k,
        ships.ships_status,
        ships.ships_total,
        len(reasons),
    )
    return FitCalcResult(
        status,
        k,
        total_cost,
        overprice_count,
        results,
        reasons,
        contracts_status=contracts_res.status,
        contracts_available=contracts_res.ships_available,
        contracts_deficit=contracts_res.deficit,
        contracts_reasons=contracts_reasons,
        ships_availability=ships,
    )


def recalc_component_request(request: ComponentRequest) -> FitCalcResult:
    """Компонент вне фитов (§6.10): тот же расчёт при q=1, без HULL_MISSING.

    Статусы: OK / DEFICIT / NO_DATA (HULL_MISSING к ним не применяется,
    §6.10 — ветвление статуса через engine.calc_fit_status
    с allow_hull_missing=False). ``NO_DATA`` — только при пустой области;
    локации без данных дают нулевой вклад (§6.7, решение №45) и оставляют
    причины в ``FitCalcResult.reasons`` (в модель не сохраняются — §8).
    """
    settings_obj = PluginSetting.load()
    location_ids = resolve_scope_ids(request.scope_kind, request.scope_ref)
    fresh_locations, reasons = _freshness_details(location_ids, settings_obj.data_stale_after_min)
    availability, scope_resolved = get_availability(
        request.type_id,
        request.scope_kind,
        request.scope_ref,
        location_ids=location_ids,
        fresh_locations=fresh_locations,
    )
    if not scope_resolved:
        reasons.append(f"scope {request.scope_kind}/{request.scope_ref}: no locations resolved")
        # engine.calc_fit_status — единственное место выбора NO_DATA (§6.4).
        status = engine.calc_fit_status(True, availability, 0, request.qty_target, allow_hull_missing=False)
        return FitCalcResult(status, None, None, None, [], reasons)
    book, ref = get_jita_book(request.type_id, settings_obj.jita_book_depth)
    packaged_volumes = _packaged_volumes(refresh=False)
    component = ComponentInput(
        type_id=request.type_id,
        qty_per_fit=1,
        availability=availability,
        packaged_volume=packaged_volumes.get(request.type_id, Decimal("0")),
    )
    result = engine.calc_component(
        component=component,
        qty_target=request.qty_target,
        book=book,
        ref_price=ref,
        markup_pct=settings_obj.jita_markup_pct,
        delivery_rate=settings_obj.delivery_rate_isk_per_m3,
        overprice_threshold_pct=settings_obj.overprice_threshold_pct,
    )
    k = availability  # q=1: собираемость = доступность
    # Компонент вне фитов: HULL_MISSING не применяется (§6.10).
    status = engine.calc_fit_status(False, availability, k, request.qty_target, allow_hull_missing=False)
    return FitCalcResult(status, k, result.cost, 1 if result.is_overprice else 0, [result], reasons)


def recalc_all() -> dict:
    """Пересчёт всех фитов и компонентов вне фитов (§12.1, вызов из recalc_stock_levels).

    Сбой одного объекта не прерывает пересчёт остальных (§16 Надёжность).
    """
    global _packaged_volumes_cache
    _packaged_volumes_cache = None
    fits_ok = fits_failed = components_ok = components_failed = 0
    for fit in Fit.objects.all().order_by("pk"):
        try:
            recalc_fit(fit)
            fits_ok += 1
        except Exception:  # noqa: BLE001 — фиксируем сбой и продолжаем
            logger.exception("Fit %s recalculation failed", fit.pk)
            fits_failed += 1
    for request in ComponentRequest.objects.all().order_by("pk"):
        try:
            recalc_component_request(request)
            components_ok += 1
        except Exception:  # noqa: BLE001
            logger.exception("ComponentRequest %s recalculation failed", request.pk)
            components_failed += 1
    return {
        "fits_ok": fits_ok,
        "fits_failed": fits_failed,
        "components_ok": components_ok,
        "components_failed": components_failed,
    }
