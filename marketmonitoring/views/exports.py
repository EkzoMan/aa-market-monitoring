"""Экспорт фитов (§14.3): buyback .txt/.csv и копирование multibuy в буфер.

Расчёт позиций — как в fit_detail (views/fits.py): чтение кэша БД плагина
(StockLevel / JitaOrderLevel) пакетными запросами (без N+1, §22.4) + чистые
функции calc/engine. UI к ESI не обращается (§5.10). Экспорт закупки всегда
на английском (§15): EveType.name всегда английский. Права — view (§14).
"""

import csv
import io
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _

from ..auth_hooks import has_view
from ..models import Fit, PluginSetting
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
    только при реальном no_data. При отключённой Jita
    цена докупки недоступна: общий селектор не читает стакан, позиции не
    экспортируются (ни stale-цен, ни нулевой стоимости).
    При NOT_COVERED qty = covered, дефицит возвращается отдельным полем shortfall. cost
    каждой позиции — тот же, что в recalc_all (формула §6.9), поэтому
    сумма cost == Fit.total_cost. Возвращает список словарей: type_id,
    type_name, qty, unit_price (int ISK), cost (int ISK), shortfall.
    """
    settings_obj = PluginSetting.load()
    data = build_fit_component_rows(fit)
    # NO_DATA (пустая область): дефицит/стоимость не считаются (§6.4) — экспорт пуст.
    # Локации без данных дают нулевой вклад и не блокируют экспорт (решение №45).
    # Выключенная Jita: нет цены докупки — докупка не экспортируется.
    if data.any_no_data or not data.jita_price_available:
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
                "unit_price": _isk_int(unit_price),
                "cost": _isk_int(calc_result.cost),
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
    файл fit_<id>_buyback.csv. Сумма cost равна Fit.total_cost (та же формула
    §6.9, что и в recalc_all)."""
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
