"""Экспорт фитов (§14.3): buyback .txt/.csv и копирование multibuy в буфер.

Расчёт позиций — как в fit_detail (views/fits.py): чтение кэша БД плагина
(StockLevel / JitaOrderLevel) пакетными запросами (без N+1, §22.4) + чистые
функции calc/engine. UI к ESI не обращается (§5.10). Экспорт закупки всегда
на английском (§15): EveType.name всегда английский. Права — view (§14).
"""

import csv
import io
import json
import logging
import re
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from ..auth_hooks import has_view
from ..calc import engine
from ..calc.engine import ComponentInput, OrderBookRow
from ..calc.service import _freshness_details, resolve_scope_ids
from ..esi.selectors import is_jita_enabled
from ..models import Fit, FitComponent, JitaOrderLevel, PluginSetting, StockLevel
from ._calc_view import build_fit_component_rows

logger = logging.getLogger("marketmonitoring")

CSV_HEADER = ["type_name", "type_id", "qty", "unit_price", "cost"]


def _isk_int(value: Decimal) -> int:
    """Целое ISK: ROUND_HALF_UP до единицы (§5.7)."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calc_export_rows(fit: Fit) -> list[dict]:
    """Позиции buyback фита (§14.3): только компоненты с D_i > 0.

    Расчёт идентичен карточке фита: общий селектор build_fit_component_rows
    (views/_calc_view.py) + calc/engine. NO_DATA — только пустая область
    (§6.4, §6.7, решение №45): дефицит/стоимость не считаются, экспорт пуст.
    Локация без данных даёт нулевой вклад и warning; экспорт пропускает фиты
    только при реальном no_data.

    В список закупки попадают **все** позиции с D_i > 0, даже если цена Jita
    недоступна: пользователю нужен сам список недостающих модулей, а не только
    посчитанная стоимость. При выключенной Jita ``unit_price``/``cost`` равны
    None (в CSV — пустые ячейки), \"старых\" цен не показываем; NOT_COVERED
    в этом режиме не применяется, покупается полный D_i. При включённой Jita
    NOT_COVERED означает qty = covered, а shortfall возвращается отдельно;
    cost каждой позиции — тот же, что в recalc_all (формула §6.9), поэтому
    сумма cost == Fit.total_cost. Возвращает список словарей: type_id,
    type_name, qty, unit_price (int ISK или None), cost (int ISK или None), shortfall.
    """
    settings_obj = PluginSetting.load()
    data = build_fit_component_rows(fit)
    # NO_DATA (пустая область): дефицит/стоимость не считаются (§6.4) — экспорт пуст.
    # Локации без данных дают нулевой вклад и не блокируют экспорт (решение №45).
    if data.any_no_data:
        return []
    rows = []
    for component_input, calc_result in zip(data.component_inputs, data.calc_results):
        if calc_result.deficit <= 0:
            continue
        unit = calc_result.unit if calc_result.unit is not None else Decimal("0")
        # Эффективная цена единицы: unit*(1+m) + c_del*pvol — целое ISK (§6.9).
        m = settings_obj.jita_markup_pct / Decimal("100")
        unit_price = unit * (Decimal("1") + m) + settings_obj.delivery_rate_isk_per_m3 * component_input.packaged_volume
        qty = calc_result.deficit - calc_result.shortfall if calc_result.is_not_covered else calc_result.deficit
        if qty <= 0:
            # NOT_COVERED с covered=0: покупать нечего — позиция не экспортируется.
            continue
        rows.append(
            {
                "type_id": calc_result.type_id,
                "type_name": str(data.type_names.get(calc_result.type_id, f"type {calc_result.type_id}")),
                # NOT_COVERED: покупаем только covered, shortfall отдельно (§6.6).
                "qty": qty,
                "unit_price": _isk_int(unit_price) if data.jita_price_available else None,
                "cost": _isk_int(calc_result.cost) if data.jita_price_available else None,
                "shortfall": calc_result.shortfall if calc_result.is_not_covered else 0,
                "is_not_covered": calc_result.is_not_covered,
            }
        )
    return rows


def _multibuy_text(rows: list[dict]) -> str:
    """Текст multibuy (§14.3): строки TypeName<TAB>Qty, сортировка по имени
    типа case-insensitive; имена только английские (EveType.name, §15)."""
    ordered = sorted(rows, key=lambda row: row["type_name"].lower())
    return "".join(f"{row['type_name']}\t{row['qty']}\n" for row in ordered)


def _effective_scope(fit: Fit, component: FitComponent) -> tuple[str, int]:
    """Эффективная область позиции: override компонента, иначе область фита (§6.3)."""
    if component.scope_override_kind:
        return component.scope_override_kind, component.scope_override_ref
    return fit.scope_kind, fit.scope_ref


def parse_fit_ids(values) -> list[int]:
    """Привести выбранные id фитов к списку целых чисел без дублей (для экспорта)."""
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values)
    result: list[int] = []
    seen: set[int] = set()
    for value in values or ():
        try:
            fit_id = int(value)
        except (TypeError, ValueError):
            continue
        if fit_id > 0 and fit_id not in seen:
            seen.add(fit_id)
            result.append(fit_id)
    return result


def calc_bulk_export_rows(fit_ids) -> list[dict]:
    """Сводный buyback по выбранным фитам: одинаковые типы складываются (D_i по §6.5).

    Расчёт каждой позиции полностью совпадает с :func:`calc_export_rows` для
    одного фита: те же ``engine.calc_component`` и кэш БД, но все чтения —
    пакетные: один запрос компонентов, один резолв на уникальную область,
    одна выборка ``StockLevel``, ``JitaOrderLevel`` и ``EveType``, один вызов
    проверки свежести (§22.4, без N+1 при десятках фитов).

    Фиты с пустой областью любой позиции (``NO_DATA``, §6.4) пропускаются:
    у них дефицит и стоимость не рассчитываются. При выключенной Jita цена
    докупки недоступна, но позиции всё равно экспортируются с полным ``D_i``
    и ``cost=None``. ``NOT_COVERED`` добавляет в закупку только ``covered``;
    ``shortfall`` копится отдельно для диагностики.
    Возвращает строки ``type_id``, ``type_name``, ``qty``, ``shortfall``,
    ``cost`` (Decimal ISK или None), отсортированные по английскому имени типа.
    """
    ids = parse_fit_ids(fit_ids)
    if not ids:
        return []
    fits = list(Fit.objects.filter(pk__in=ids).order_by("pk"))
    if not fits:
        return []
    settings_obj = PluginSetting.load()
    # Цена нужна только для суммы/наценки: сам список дефицита экспортируем
    # и при выключенной Jita (требование заказчика).
    jita_price_available = is_jita_enabled()

    fit_by_id = {fit.pk: fit for fit in fits}
    components = list(FitComponent.objects.filter(fit_id__in=fit_by_id).order_by("fit_id", "pk"))
    if not components:
        return []
    components_by_fit: dict[int, list[FitComponent]] = defaultdict(list)
    scopes: set[tuple[str, int]] = set()
    type_ids: set[int] = set()
    for component in components:
        components_by_fit[component.fit_id].append(component)
        scopes.add(_effective_scope(fit_by_id[component.fit_id], component))
        type_ids.add(component.type_id)

    # Резолв и свежесть — по уникальным областям/локациям, без повторов на фит.
    scope_ids = {scope: resolve_scope_ids(*scope) for scope in scopes}
    location_union: set[int] = set()
    for scope_locations in scope_ids.values():
        location_union |= scope_locations
    fresh_locations, _reasons = _freshness_details(location_union, settings_obj.data_stale_after_min)

    volume_by_key: dict[tuple[int, int], int] = {}
    for location_id, type_id, total in StockLevel.objects.filter(
        type_id__in=type_ids, location_id__in=location_union
    ).values_list("location_id", "type_id", "total_sell_volume"):
        volume_by_key[(location_id, type_id)] = total

    # Имена всегда английские (§15); packaged_volume — из SDE через eveuniverse.
    from eveuniverse.models import EveType

    type_names: dict[int, str] = {}
    packaged_volumes: dict[int, Decimal] = {}
    for type_id, name, packaged, volume in EveType.objects.filter(pk__in=type_ids).values_list(
        "pk", "name", "packaged_volume", "volume"
    ):
        type_names[type_id] = str(name)
        packaged_volumes[type_id] = Decimal(str(packaged)) if packaged else Decimal(str(volume or 0))

    books: dict[int, list[OrderBookRow]] = defaultdict(list)
    if jita_price_available:
        for level in JitaOrderLevel.objects.filter(type_id__in=type_ids).order_by("type_id", "rank"):
            if len(books[level.type_id]) < settings_obj.jita_book_depth:
                books[level.type_id].append(OrderBookRow(price=level.price, volume=level.volume))
    refs = {type_id: book[0].price for type_id, book in books.items()}

    totals: dict[int, dict] = {}
    for fit in fits:
        scope_rows = [
            (component, scope_ids[_effective_scope(fit, component)]) for component in components_by_fit.get(fit.pk, [])
        ]
        if any(not locations for _component, locations in scope_rows):
            # Пустая область — NO_DATA (§6.4): фит целиком исключается из закупки.
            continue
        for component, locations in scope_rows:
            availability = sum(
                volume_by_key.get((location_id, component.type_id), 0) for location_id in locations & fresh_locations
            )
            component_input = ComponentInput(
                type_id=component.type_id,
                qty_per_fit=component.qty_per_fit,
                availability=availability,
                packaged_volume=packaged_volumes.get(component.type_id, Decimal("0")),
            )
            result = engine.calc_component(
                component=component_input,
                qty_target=fit.qty_target,
                book=books.get(component.type_id, []),
                ref_price=refs.get(component.type_id),
                markup_pct=settings_obj.jita_markup_pct,
                delivery_rate=settings_obj.delivery_rate_isk_per_m3,
                overprice_threshold_pct=settings_obj.overprice_threshold_pct,
            )
            if not jita_price_available:
                # Без стакана нет ни цены, ни NOT_COVERED: покупаем полный D_i.
                result = engine.price_unavailable(result)
            if result.deficit <= 0:
                continue
            qty = result.deficit - result.shortfall if result.is_not_covered else result.deficit
            if qty <= 0:
                continue
            row = totals.setdefault(
                component.type_id,
                {
                    "type_id": component.type_id,
                    "type_name": type_names.get(component.type_id, f"type {component.type_id}"),
                    "qty": 0,
                    "shortfall": 0,
                    "cost": Decimal("0"),
                },
            )
            row["qty"] += qty
            if result.is_not_covered:
                row["shortfall"] += result.shortfall or 0
            row["cost"] += result.cost or Decimal("0")
    if not jita_price_available:
        for row in totals.values():
            row["cost"] = None
    return sorted(totals.values(), key=lambda row: row["type_name"].lower())


def _check_view_access(request):
    """Общий доступ по view-правам; при отказе — redirect с сообщением."""
    if not has_view(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")
    return None


@login_required
def export_txt(request, fit_id: int) -> HttpResponse:
    """Экспорт фита в .txt (§14.3): multibuy-формат, text/plain; charset=utf-8,
    файл fit_<id>_buyback.txt, attachment."""
    denied = _check_view_access(request)
    if denied:
        return denied
    fit = get_object_or_404(Fit, pk=fit_id)
    rows = calc_export_rows(fit)
    response = HttpResponse(content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="fit_{fit.pk}_buyback.txt"'
    response.write(_multibuy_text(rows))
    return response


@login_required
def export_csv(request, fit_id: int) -> HttpResponse:
    """Экспорт фита в .csv (§14.3): заголовок type_name,type_id,qty,unit_price,cost
    (+ shortfall при NOT_COVERED), разделитель ",", UTF-8 без BOM,
    файл fit_<id>_buyback.csv. При включённой Jita сумма cost равна
    Fit.total_cost (та же формула §6.9, что и в recalc_all); при выключенной —
    ценовые колонки пустые, но дефицитные позиции остаются в файле."""
    denied = _check_view_access(request)
    if denied:
        return denied
    fit = get_object_or_404(Fit, pk=fit_id)
    rows = calc_export_rows(fit)
    has_shortfall = any(row["is_not_covered"] for row in rows)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=",", lineterminator="\n")
    header = CSV_HEADER + (["shortfall"] if has_shortfall else [])
    writer.writerow(header)
    for row in rows:
        values = [
            row["type_name"],
            row["type_id"],
            row["qty"],
            row["unit_price"],
            row["cost"],
        ]
        if has_shortfall:
            values.append(row["shortfall"])
        writer.writerow(values)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="fit_{fit.pk}_buyback.csv"'
    # UTF-8 без BOM: HttpResponse.write строки, кодировка utf-8.
    response.write(buffer.getvalue())
    return response


@login_required
def export_multibuy(request, fit_id: int) -> JsonResponse:
    """JSON-эндпоинт для копирования multibuy в буфер (§14.3): текст формируется
    на сервере, JS только копирует (Clipboard API). Пустой список — флаг empty,
    UI показывает «нет позиций»."""
    denied = _check_view_access(request)
    if denied:
        return denied
    fit = get_object_or_404(Fit, pk=fit_id)
    rows = calc_export_rows(fit)
    text = _multibuy_text(rows)
    return JsonResponse(
        {
            "empty": not rows,
            "text": text,
            "count": len(rows),
        }
    )


@login_required
def export_bulk_txt(request) -> HttpResponse:
    """Сводный multibuy выбранных фитов в .txt: `?fit_ids=1&fit_ids=2` (§14.3).

    Имена только английские, строки отсортированы по имени типа; фиты с пустой
    областью дают пустой файл. Без включённой Jita дефицит всё равно
    экспортируется (цена не нужна для multibuy). Права — view; ESI из view не вызывается.
    """
    denied = _check_view_access(request)
    if denied:
        return denied
    rows = calc_bulk_export_rows(request.GET.getlist("fit_ids"))
    response = HttpResponse(content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="fits_buyback.txt"'
    response.write(_multibuy_text(rows))
    return response


@login_required
@require_POST
def export_bulk_multibuy(request) -> JsonResponse:
    """JSON для копирования сводного multibuy выбранных фитов в буфер.

    POST body: ``{"fit_ids": [1, 2]}``; текст формируется на сервере, JS только
    копирует его через Clipboard API (как в карточке фита, §14.3). Пустой
    результат возвращается с ``empty=true`` — UI показывает «нет позиций».
    """
    denied = _check_view_access(request)
    if denied:
        return denied
    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Invalid payload."}, status=400)
    rows = calc_bulk_export_rows(payload.get("fit_ids", []))
    text = _multibuy_text(rows)
    return JsonResponse({"empty": not rows, "text": text, "count": len(rows)})
