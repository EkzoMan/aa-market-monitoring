"""Лёгкие расчёты для UI (этап 5, §14): чтение кэша БД плагина + чистые функции calc/engine.

GET-страницы не пересчитывают записи БД и не пишут в БД: статус/дефицит/стоимость
считаются здесь из кэша StockLevel / JitaOrderLevel пакетными запросами
(без N+1, §22.4) и передаются в шаблон. Запись кэша статуса выполняет только
recalc_all из Celery-задачи (§12.1). UI к ESI не обращается (§5.10).
"""

import logging
from collections import defaultdict
from decimal import Decimal

from ..calc import engine
from ..calc.engine import ComponentInput, OrderBookRow
from ..calc.service import _freshness_details, resolve_scope_ids
from ..esi.selectors import is_jita_enabled
from ..models import JitaOrderLevel, PluginSetting, StockLevel

logger = logging.getLogger("marketmonitoring")


def _diagnostic_reasons(
    scope_ids: dict[tuple[str, int], set[int]],
    present_locations: set[int],
    freshness_reasons: list[str],
) -> list[str]:
    """Предупреждения с id локаций: пустая область, нет агрегата, не свежие данные (§6.7)."""
    reasons = list(freshness_reasons)
    for scope, ids in sorted(scope_ids.items()):
        if not ids:
            reasons.append(f"scope {scope[0]}/{scope[1]}: no locations resolved")
            continue
        missing = ids - present_locations
        if missing:
            reasons.append(f"scope {scope[0]}/{scope[1]}: no StockLevel aggregate for locations {sorted(missing)}")
    return reasons


def build_component_request_rows(requests) -> list[dict]:
    """Строки страницы «Components» (§14.4): статус/дефицит/стоимость без записи в БД.

    Один запрос настроек (PluginSetting.load), кеш резолва областей (§6.1),
    пакетные выборки StockLevel и JitaOrderLevel по всем типам; расчёт —
    чистые функции calc/engine (§22.3). Свежесть (§6.7) проверяется один раз
    по объединению локаций всех областей и используется только для предупреждений:
    локация без данных даёт нулевой вклад, ``NO_DATA`` — только при пустой
    области (решение №45). Компоненты вне фитов: HULL_MISSING не применяется (§6.10).
    Если Jita отключена, ценовые поля обнуляются чистой функцией engine.price_unavailable:
    дефицит и статус считаются по локальным рынкам, но cost становится None (не 0).
    Возвращает список словарей: obj, type_id, status, deficit, cost, reasons.
    """
    requests = list(requests)
    if not requests:
        return []
    settings_obj = PluginSetting.load()
    type_ids = {request_obj.type_id for request_obj in requests}
    scopes = {(request_obj.scope_kind, request_obj.scope_ref) for request_obj in requests}

    # Резолв областей — один вызов на уникальную область (§6.1).
    scope_ids = {scope: resolve_scope_ids(*scope) for scope in scopes}
    location_union: set[int] = set()
    for ids in scope_ids.values():
        location_union |= ids

    fresh_locations, freshness_reasons = _freshness_details(location_union, settings_obj.data_stale_after_min)

    # Две пакетные выборки StockLevel: объёмы по нужным типам и наличие
    # агрегата по локации (§6.7 — проверка на уровне локации, не типа).
    volume_by_key: dict[tuple[int, int], int] = {}
    for location_id, type_id, total in StockLevel.objects.filter(
        type_id__in=type_ids, location_id__in=location_union
    ).values_list("location_id", "type_id", "total_sell_volume"):
        volume_by_key[(location_id, type_id)] = total
    present_locations = set(
        StockLevel.objects.filter(location_id__in=location_union).values_list("location_id", flat=True).distinct()
    )
    reasons = _diagnostic_reasons(scope_ids, present_locations, freshness_reasons)

    # packaged_volume (§6.9) — из SDE через eveuniverse, один запрос.
    from eveuniverse.models import EveType

    packaged_volumes: dict[int, Decimal] = {}
    for type_id, packaged, volume in EveType.objects.filter(pk__in=type_ids).values_list(
        "pk", "packaged_volume", "volume"
    ):
        packaged_volumes[type_id] = Decimal(str(packaged)) if packaged else Decimal(str(volume or 0))

    # Один запрос стакана Jita по всем типам (§22.4); rank=1 — минимальная цена.
    # Отключённая Jita: стакан не читается даже при уцелевших строках кэша.
    jita_price_available = is_jita_enabled()
    books: dict[int, list[OrderBookRow]] = defaultdict(list)
    refs: dict[int, Decimal] = {}
    if jita_price_available:
        for level in JitaOrderLevel.objects.filter(type_id__in=type_ids).order_by("type_id", "rank"):
            if len(books[level.type_id]) < settings_obj.jita_book_depth:
                books[level.type_id].append(OrderBookRow(price=level.price, volume=level.volume))
    for type_id, book in books.items():
        refs[type_id] = book[0].price

    rows = []
    for request_obj in requests:
        scope = (request_obj.scope_kind, request_obj.scope_ref)
        ids = scope_ids[scope]
        has_no_data = not ids
        usable_ids = ids & fresh_locations
        component = ComponentInput(
            type_id=request_obj.type_id,
            qty_per_fit=1,  # компонент вне фитов: q=1 (§6.10)
            availability=sum(volume_by_key.get((location_id, request_obj.type_id), 0) for location_id in usable_ids),
            packaged_volume=packaged_volumes.get(request_obj.type_id, Decimal("0")),
        )
        if has_no_data:
            # engine.calc_fit_status — единственное место выбора NO_DATA (§6.4).
            status = engine.calc_fit_status(
                True, component.availability, component.availability, request_obj.qty_target, allow_hull_missing=False
            )
            rows.append(
                {
                    "obj": request_obj,
                    "type_id": request_obj.type_id,
                    "status": status,
                    "deficit": None,
                    "cost": None,
                    "reasons": reasons,
                }
            )
            continue
        result = engine.calc_component(
            component=component,
            qty_target=request_obj.qty_target,
            book=books.get(request_obj.type_id, []),
            ref_price=refs.get(request_obj.type_id),
            markup_pct=settings_obj.jita_markup_pct,
            delivery_rate=settings_obj.delivery_rate_isk_per_m3,
            overprice_threshold_pct=settings_obj.overprice_threshold_pct,
        )
        if not jita_price_available:
            result = engine.price_unavailable(result)
        # Компонент вне фитов: собираемость = доступность, HULL_MISSING не применяется (§6.10).
        status = engine.calc_fit_status(
            False, component.availability, component.availability, request_obj.qty_target, allow_hull_missing=False
        )
        rows.append(
            {
                "obj": request_obj,
                "type_id": request_obj.type_id,
                "status": status,
                "deficit": result.deficit,
                "cost": result.cost,
                "reasons": reasons,
            }
        )
    return rows


class FitComponentRows:
    """Данные расчёта позиций одного фита для UI (fit_detail, exports).

    Общая оркестрация чтения кэша БД (§22.4): компоненты фита, примитивы
    ComponentInput, результаты engine.calc_component, флаги NO_DATA по пустой
    области (§6.4, §6.7), предупреждения о локациях без данных и имена типов.
    Формулы остаются в calc/engine (§22.3).
    """

    def __init__(
        self,
        components: list,
        component_inputs: list[ComponentInput],
        calc_results: list,
        component_no_data: list[bool],
        fresh: bool,
        reasons: list[str],
        type_names: dict[int, str],
        jita_price_available: bool = True,
    ):
        self.components = components
        self.component_inputs = component_inputs
        self.calc_results = calc_results
        # Цены Jita доступны только при включённой локации; при False карточка
        # фита не должна показывать сохранённый Fit.total_cost (stale-цена).
        self.jita_price_available = jita_price_available
        # NO_DATA только при пустой области позиции (§6.4, решение №45).
        self.component_no_data = component_no_data
        # fresh/reasons — диагностика: локация без данных даёт вклад 0, но не
        # меняет статус. UI показывает эти причины отдельным предупреждением.
        self.fresh = fresh
        self.reasons = reasons
        self.type_names = type_names

    @property
    def any_no_data(self) -> bool:
        """Хотя бы одна позиция с пустой областью: NO_DATA (§6.4, решение №45)."""
        return any(self.component_no_data)

    @property
    def has_data_warnings(self) -> bool:
        """Есть предупреждения о локациях без свежих данных (§6.7)."""
        return bool(self.reasons)


def build_fit_component_rows(fit) -> FitComponentRows:
    """Расчёт позиций фита для UI (§14.1, §14.3) — общий селектор fit_detail и экспорта.

    Чтение кэша БД плагина (StockLevel / JitaOrderLevel) пакетными запросами
    (без N+1, §22.4) + чистые функции calc/engine. UI к ESI не обращается (§5.10).
    Позиция NO_DATA только при пустой области (§6.4, решение №45). Локации без
    свежих данных (failed/partial/no_provider/stale/нет агрегата) дают вклад 0
    и отдельное предупреждение с id локации (§6.7). При выключенной Jita ценовые
    поля обнуляются чистой функцией engine.price_unavailable: K/D_i и статус
    остаются, цены — None.
    """
    from eveuniverse.models import EveType

    settings_obj = PluginSetting.load()
    components = list(fit.components.all().order_by("pk"))
    type_ids = {component.type_id for component in components}
    type_names = dict(EveType.objects.filter(pk__in=type_ids).values_list("pk", "name"))

    # packaged_volume (§6.9) из SDE по нужным типам.
    packaged_volumes: dict[int, Decimal] = {}
    for type_id, packaged, volume in EveType.objects.filter(pk__in=type_ids).values_list(
        "pk", "packaged_volume", "volume"
    ):
        packaged_volumes[type_id] = Decimal(str(packaged)) if packaged else Decimal(str(volume or 0))

    def _scope_of(component) -> tuple[str, int]:
        """Эффективная область позиции: override компонента, иначе область фита (§6.3)."""
        if component.scope_override_kind:
            return component.scope_override_kind, component.scope_override_ref
        return fit.scope_kind, fit.scope_ref

    scopes = {_scope_of(component) for component in components}
    scope_ids = {scope: resolve_scope_ids(*scope) for scope in scopes}
    location_union: set[int] = set()
    for ids in scope_ids.values():
        location_union |= ids

    fresh_locations, freshness_reasons = _freshness_details(location_union, settings_obj.data_stale_after_min)
    volume_by_key: dict[tuple[int, int], int] = {}
    for location_id, type_id, total in StockLevel.objects.filter(
        type_id__in=type_ids, location_id__in=location_union
    ).values_list("location_id", "type_id", "total_sell_volume"):
        volume_by_key[(location_id, type_id)] = total
    present_locations = set(
        StockLevel.objects.filter(location_id__in=location_union).values_list("location_id", flat=True).distinct()
    )
    reasons = _diagnostic_reasons(scope_ids, present_locations, freshness_reasons)
    fresh = not freshness_reasons

    # Один запрос стакана Jita по всем типам (§22.4), rank=1 — минимальная цена.
    # Gate по включённости локации: отключённая Jita не даёт stale-цен из кэша.
    jita_price_available = is_jita_enabled()
    books: dict[int, list[OrderBookRow]] = defaultdict(list)
    refs: dict[int, Decimal] = {}
    if jita_price_available:
        for level in JitaOrderLevel.objects.filter(type_id__in=type_ids).order_by("type_id", "rank"):
            if len(books[level.type_id]) < settings_obj.jita_book_depth:
                books[level.type_id].append(OrderBookRow(price=level.price, volume=level.volume))
    for type_id, book in books.items():
        refs[type_id] = book[0].price

    component_inputs = [
        ComponentInput(
            type_id=component.type_id,
            qty_per_fit=component.qty_per_fit,
            # Локация без свежих данных не даёт объём (§6.2, решение №45).
            availability=sum(
                volume_by_key.get((location_id, component.type_id), 0)
                for location_id in scope_ids[_scope_of(component)] & fresh_locations
            ),
            packaged_volume=packaged_volumes.get(component.type_id, Decimal("0")),
            is_hull=component.type_id == fit.hull_type_id,
        )
        for component in components
    ]
    calc_results = [
        engine.calc_component(
            component=component_input,
            qty_target=fit.qty_target,
            book=books.get(component_input.type_id, []),
            ref_price=refs.get(component_input.type_id),
            markup_pct=settings_obj.jita_markup_pct,
            delivery_rate=settings_obj.delivery_rate_isk_per_m3,
            overprice_threshold_pct=settings_obj.overprice_threshold_pct,
        )
        for component_input in component_inputs
    ]
    if not jita_price_available:
        calc_results = [engine.price_unavailable(result) for result in calc_results]
    component_no_data = [not scope_ids[_scope_of(component)] for component in components]
    return FitComponentRows(
        components=components,
        component_inputs=component_inputs,
        calc_results=calc_results,
        component_no_data=component_no_data,
        fresh=fresh,
        reasons=reasons,
        type_names=type_names,
        jita_price_available=jita_price_available,
    )
