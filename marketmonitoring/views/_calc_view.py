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
from ..calc.service import check_freshness, resolve_scope_ids
from ..esi.selectors import is_jita_enabled
from ..models import JitaOrderLevel, PluginSetting, StockLevel

logger = logging.getLogger("marketmonitoring")


def build_component_request_rows(requests) -> list[dict]:
    """Строки страницы «Components» (§14.4): статус/дефицит/стоимость без записи в БД.

    Один запрос настроек (PluginSetting.load), кеш резолва областей (§6.1),
    пакетные выборки StockLevel и JitaOrderLevel по всем типам; расчёт —
    чистые функции calc/engine (§22.3). Свежесть (§6.7) проверяется один раз
    по объединению локаций всех областей — если хотя бы одна нужная локация
    не свежа, все строки NO_DATA (дефицит/стоимость не считаются, §6.4).
    Компоненты вне фитов: HULL_MISSING не применяется (§6.10).
    Если Jita отключена, ценовые поля обнуляются чистой функцией engine.price_unavailable:
    дефицит и статус считаются по локальным рынкам, но cost становится None (не 0).
    Возвращает список словарей: obj, type_id, status, deficit, cost.
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

    fresh, _reasons = check_freshness(location_union, settings_obj.data_stale_after_min)

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
        ids = scope_ids[(request_obj.scope_kind, request_obj.scope_ref)]
        # Агрегат StockLevel должен быть по всем нужным локациям области (§6.7).
        # Нет лотов нужного типа на свежей локации → A=0, но не NO_DATA.
        has_data = bool(ids) and ids <= present_locations
        if not fresh or not has_data:
            rows.append(
                {
                    "obj": request_obj,
                    "type_id": request_obj.type_id,
                    "status": engine.STATUS_NO_DATA,
                    "deficit": None,
                    "cost": None,
                }
            )
            continue
        component = ComponentInput(
            type_id=request_obj.type_id,
            qty_per_fit=1,  # компонент вне фитов: q=1 (§6.10)
            availability=sum(volume_by_key.get((location_id, request_obj.type_id), 0) for location_id in ids),
            packaged_volume=packaged_volumes.get(request_obj.type_id, Decimal("0")),
        )
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
            }
        )
    return rows


class FitComponentRows:
    """Данные расчёта позиций одного фита для UI (fit_detail, exports).

    Общая оркестрация чтения кэша БД (§22.4): компоненты фита, примитивы
    ComponentInput, результаты engine.calc_component, флаги NO_DATA по позициям
    (§6.4, §6.7), имена типов. Формулы остаются в calc/engine (§22.3).
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
        # Флаг NO_DATA по каждой позиции: протухшие данные (§6.7), пустая
        # область или отсутствие агрегата StockLevel по нужной локации (§6.7).
        self.component_no_data = component_no_data
        self.fresh = fresh
        self.reasons = reasons
        self.type_names = type_names

    @property
    def any_no_data(self) -> bool:
        """Хотя бы одна позиция NO_DATA — дефицит/стоимость не считаются (§6.4)."""
        return not self.fresh or any(self.component_no_data)


def build_fit_component_rows(fit) -> FitComponentRows:
    """Расчёт позиций фита для UI (§14.1, §14.3) — общий селектор fit_detail и экспорта.

    Чтение кэша БД плагина (StockLevel / JitaOrderLevel) пакетными запросами
    (без N+1, §22.4) + чистые функции calc/engine. UI к ESI не обращается (§5.10).
    Позиция считается NO_DATA, если данные не свежи (§6.7), область пуста или
    агрегат StockLevel отсутствует хотя бы по одной нужной локации (§6.7).
    При выключенной Jita ценовые поля обнуляются чистой функцией
    engine.price_unavailable: K/D_i и статус остаются, цены — None.
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

    fresh, reasons = check_freshness(location_union, settings_obj.data_stale_after_min)
    volume_by_key: dict[tuple[int, int], int] = {}
    for location_id, type_id, total in StockLevel.objects.filter(
        type_id__in=type_ids, location_id__in=location_union
    ).values_list("location_id", "type_id", "total_sell_volume"):
        volume_by_key[(location_id, type_id)] = total
    # §6.7: NO_DATA только если у локации нет агрегата StockLevel вообще;
    # отсутствие лотов конкретного типа даёт A=0, а не NO_DATA.
    present_locations = set(
        StockLevel.objects.filter(location_id__in=location_union).values_list("location_id", flat=True).distinct()
    )

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
            availability=sum(
                volume_by_key.get((location_id, component.type_id), 0)
                for location_id in scope_ids[_scope_of(component)]
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
    component_no_data = [
        not scope_ids[_scope_of(component)] or not scope_ids[_scope_of(component)] <= present_locations
        for component in components
    ]
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
