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

from ..constants import ContractFitStatus, FitStatus, LocationKind, ScopeKind, SyncStatus
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
)

logger = logging.getLogger("marketmonitoring")


class FitCalcResult:
    """Итог пересчёта фита: статус, K, сумма, счётчик оверпрайса, строки, причины NO_DATA.

    ``reasons`` — причины NO_DATA (§6.7). Решение: причины НЕ сохраняются
    в модель — в §8 соответствующего поля нет, и расширять модели запрещено;
    UI получает причины на этапе 5 из результата пересчёта (объект
    FitCalcResult возвращается recalc_fit / recalc_component_request).
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
        return set(EveSolarSystem.objects.filter(id=scope_ref).values_list("eve_stations__id", flat=True))
    if scope_kind == ScopeKind.REGION:
        return set(
            EveSolarSystem.objects.filter(eve_constellation__eve_region_id=scope_ref).values_list(
                "eve_stations__id", flat=True
            )
        )
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


# ---------------------------------------------------------------------------
# Свежесть (§6.7)
# ---------------------------------------------------------------------------


def check_freshness(location_ids: set[int], stale_after_min: int) -> tuple[bool, list[str]]:
    """NO_DATA-проверка нужных локаций (§6.7).

    Возвращает (fresh, список причин). Причины NO_DATA: структура с
    sync_status != ok (в т.ч. без доступного провайдера — no_provider);
    отсутствие агрегата StockLevel по локации; last_synced_at старше
    data_stale_after_min; региональный синк региона не завершался успешно
    за тот же период (оценивается по свежим данным нужных NPC-станций
    региона, а не по факту любой строки StockLevel в регионе).

    Региональная проверка применяется только к NPC-станциям: структуры
    синкаются структурным синком, их свежесть определяется last_synced_at /
    sync_status, региональная проверка к ним не применяется. NPC-станция
    без записи MarketLocation тоже проверяется: её регион резолвится через
    eveuniverse (EveStation -> EveSolarSystem -> EveRegion; своих копий SDE
    нет — §8). Если регион резолвить не из чего (нет станции в справочнике),
    это легитимный NO_DATA — нехватка справочника SDE.
    """
    if not location_ids:
        return True, []
    stale_cutoff = timezone.now() - timedelta(minutes=stale_after_min)
    reasons: list[str] = []
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
            # Справочника не хватает для региональной проверки — NO_DATA (§6.7).
            reasons.append(f"station {station_id}: cannot resolve region from SDE reference")

    for location_id in sorted(location_ids):
        location = locations.get(location_id)
        if location is None:
            # NPC-станция без записи MarketLocation — данные приходят
            # региональным синком; свежесть оценивается региональной
            # проверкой ниже (пропуска без проверки нет).
            continue
        if location.kind == LocationKind.STRUCTURE:
            # Структура: sync_status != ok или провайдера нет → NO_DATA (§6.7);
            # региональная проверка к структурам не применяется.
            if location.sync_status != SyncStatus.OK:
                reasons.append(f"structure {location_id}: sync_status={location.sync_status}")
                continue
        if location.last_synced_at is None or location.last_synced_at < stale_cutoff:
            reasons.append(f"location {location_id}: stale")

    # Региональный синк региона должен завершаться успешно за период (§6.7):
    # для каждой нужной NPC-станции региона требуется свежая строка StockLevel
    # (данные NPC-станций поставляет региональный кэш).
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
        if missing:
            reasons.append(f"region {region_id}: no successful region sync in period (stations {sorted(missing)})")
    return (len(reasons) == 0, reasons)


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

    if scope_cache is not None:
        location_ids = _resolve_scope_cached(scope_cache, fit.scope_kind, fit.scope_ref)
    else:
        location_ids = resolve_scope_ids(fit.scope_kind, fit.scope_ref)

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


def get_availability(type_id: int, scope_kind: str, scope_ref: int) -> tuple[int, bool]:
    """Доступность A_t^S (§6.2) по агрегату StockLevel области.

    Строгий scope (§5.1): station — только заказы этой станции; system/region —
    NPC-станции + включённые структуры; order_range не используется (§5.8).
    Возвращает (A, есть_ли_агрегат_StockLevel_по_ВСЕМ нужным локациям
    области): NO_DATA, если агрегат отсутствует хотя бы по одной (§6.7).
    Один запрос на компонент: строки читаются одним проходом.
    """
    location_ids = resolve_scope_ids(scope_kind, scope_ref)
    if not location_ids:
        return 0, False
    rows = tuple(
        StockLevel.objects.filter(type_id=type_id, location_id__in=location_ids).values_list(
            "location_id", "total_sell_volume"
        )
    )
    present = {location_id for location_id, _volume in rows}
    has_data = present == location_ids
    total = sum(volume for _location_id, volume in rows)
    return int(total), has_data


# ---------------------------------------------------------------------------
# Стакан Jita (§6.6, §6.8)
# ---------------------------------------------------------------------------


def get_jita_book(type_id: int, depth: int) -> tuple[list[OrderBookRow], Decimal | None]:
    """Стакан Jita: топ-depth sell-лотов ascending по цене + ref (мин. цена).

    UI никогда не обращается к ESI — читается только кэш JitaOrderLevel (§5.10).
    """
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


def _component_inputs(
    fit: Fit, scope_cache: dict[tuple[str, int], set[int]]
) -> tuple[list[tuple[ComponentInput, str, int, bool]], bool]:
    """Примитивы позиций фита с эффективной областью (§6.3, override компонента).

    Возвращает ((ComponentInput, scope_kind, scope_ref, has_data), ...); область
    компонента — scope_override, иначе область фита (§6.3, решение 11).
    resolve_scope_ids кешируется в scope_cache (dict область → ids) — один
    резолв на уникальную область фита; get_availability — один запрос на
    компонент (без N+1, §22.4).
    """
    result = []
    packaged_volumes = _packaged_volumes(refresh=False)
    has_data_flags = []
    for component in fit.components.all().order_by("pk"):
        scope_kind = component.scope_override_kind or fit.scope_kind
        scope_ref = component.scope_override_ref if component.scope_override_kind else fit.scope_ref
        availability, has_data = get_availability(component.type_id, scope_kind, scope_ref)
        has_data_flags.append(has_data)
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
                has_data,
            )
        )
    return result, all(has_data_flags)


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
) -> None:
    """Запись кэша расчёта в Fit (контракты + рынок + last_checked_at)."""
    fit.status_cache = status
    fit.fits_available = k
    fit.total_cost = total_cost
    fit.overprice_count = overprice_count
    fit.contracts_status_cache = contracts_status
    fit.contracts_available = contracts_available
    fit.last_checked_at = timezone.now()
    fit.save(
        update_fields=[
            "status_cache",
            "fits_available",
            "total_cost",
            "overprice_count",
            "contracts_status_cache",
            "contracts_available",
            "last_checked_at",
        ]
    )


def recalc_fit(fit: Fit) -> FitCalcResult:
    """Пересчёт одного фита (§6, §6.12) и запись кэша статуса в Fit (§12.1).

    В первую очередь оценивается достаточность готовых кораблей в открытых
    контрактах (§6.12), затем рыночная собираемость компонентов (§6.1–§6.9).
    Нужные локации рынка — объединение областей всех компонентов с учётом override;
    при NO_DATA (§6.7) дефицит и стоимость не рассчитываются (§6.4). Дефицит —
    независимо по каждому фиту (§5.5).
    """
    settings_obj = PluginSetting.load()
    scope_cache: dict[tuple[str, int], set[int]] = {}
    contracts_res, _matched_contracts, contracts_reasons = evaluate_fit_contracts(
        fit, settings_obj=settings_obj, scope_cache=scope_cache
    )
    component_rows_data, all_components_have_data = _component_inputs(fit, scope_cache)
    component_rows = component_rows_data
    if not component_rows:
        _save_fit_cache(
            fit,
            FitStatus.NO_DATA,
            None,
            None,
            None,
            contracts_status=contracts_res.status,
            contracts_available=contracts_res.ships_available,
        )
        return FitCalcResult(
            FitStatus.NO_DATA,
            None,
            None,
            None,
            [],
            contracts_status=contracts_res.status,
            contracts_available=contracts_res.ships_available,
            contracts_deficit=contracts_res.deficit,
            contracts_reasons=contracts_reasons,
        )

    # Нужные локации — объединение областей всех компонентов (в т.ч. override);
    # резолв области кешируется на время пересчёта фита.
    needed_location_ids: set[int] = set()
    for _component_input, scope_kind, scope_ref, _has_data in component_rows:
        needed_location_ids |= _resolve_scope_cached(scope_cache, scope_kind, scope_ref)
    fresh, reasons = check_freshness(needed_location_ids, settings_obj.data_stale_after_min)

    if not all_components_have_data:
        for component_input, scope_kind, scope_ref, has_data in component_rows:
            if not has_data:
                # Агрегат StockLevel отсутствует хотя бы по одной нужной
                # локации области (§6.7).
                reasons.append(
                    f"type {component_input.type_id}: no StockLevel aggregate "
                    f"in scope {scope_kind}/{scope_ref} for all needed locations"
                )
    if not fresh or not all_components_have_data:
        # NO_DATA: локация не свежа (§6.7) или агрегат StockLevel отсутствует
        # по нужной локации (§6.7); дефицит и стоимость не считаются (§6.4).
        status = engine.STATUS_NO_DATA
        _save_fit_cache(
            fit,
            status,
            None,
            None,
            None,
            contracts_status=contracts_res.status,
            contracts_available=contracts_res.ships_available,
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
        )

    # Стаканы и ref по каждому типу — из кэша JitaOrderLevel, без ESI (§5.10);
    # один запрос на все type_id (без N+1, §22.4).
    type_ids = [c.type_id for c, _k, _r, _d in component_rows]
    depth = settings_obj.jita_book_depth
    books: dict[int, list[OrderBookRow]] = {}
    refs: dict[int, Decimal] = {}
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

    hull_available_calc = next((c.availability for c in inputs if c.is_hull), 0)
    # Фит: ветвление с HULL_MISSING (§6.4).
    status = engine.calc_fit_status(False, hull_available_calc, k, fit.qty_target, allow_hull_missing=True)
    total_cost = sum((r.cost for r in results), Decimal("0"))
    overprice_count = sum(1 for r in results if r.is_overprice)
    _save_fit_cache(
        fit,
        status,
        k,
        total_cost,
        overprice_count,
        contracts_status=contracts_res.status,
        contracts_available=contracts_res.ships_available,
    )
    logger.debug(
        "Fit %s recalculated: contracts=%s(%s) market=%s(%s) total=%s",
        fit.pk,
        contracts_res.status,
        contracts_res.ships_available,
        status,
        k,
        total_cost,
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
    )


def recalc_component_request(request: ComponentRequest) -> FitCalcResult:
    """Компонент вне фитов (§6.10): тот же расчёт при q=1, без HULL_MISSING.

    Статусы: OK / DEFICIT / NO_DATA (HULL_MISSING к ним не применяется,
    §6.10 — ветвление статуса через engine.calc_fit_status
    с allow_hull_missing=False). Причины NO_DATA — в FitCalcResult.reasons
    (в модель не сохраняются — в §8 поля нет, расширять модели запрещено).
    """
    settings_obj = PluginSetting.load()
    location_ids = resolve_scope_ids(request.scope_kind, request.scope_ref)
    fresh, reasons = check_freshness(location_ids, settings_obj.data_stale_after_min)
    availability, has_data = get_availability(request.type_id, request.scope_kind, request.scope_ref)
    if not has_data:
        reasons.append(
            f"type {request.type_id}: no StockLevel aggregate "
            f"in scope {request.scope_kind}/{request.scope_ref} for all needed locations"
        )
    if not fresh or not has_data:
        # NO_DATA при протухших данных или отсутствии агрегата (§6.7).
        return FitCalcResult(FitStatus.NO_DATA, None, None, None, [], reasons)
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
