"""Представления фитов (§14): создание/правка/удаление с разбором EFT и предпросмотром."""

import logging
import re
from decimal import Decimal

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from ..auth_hooks import has_edit, has_view
from ..constants import FitStatus
from ..eft.parser import parse_eft
from ..forms import FitComponentRowForm, FitForm
from ..models import Fit, FitComponent

logger = logging.getLogger("marketmonitoring")

# ManagementForm-подобный формат строк предпросмотра: row<i>-<field> (§7.5).
_ROW_KEY_RE = re.compile(r"^row(\d+)-(\w+)$")
# Категория SDE "Charge" (id 16): is_charge добавленных строк наследуется от неё.
CHARGE_CATEGORY_ID = 16


def _save_components(fit: Fit, item_dicts: list[dict]) -> None:
    """Перезапись состава фита по строкам предпросмотра (атомарно, идемпотентно).

    Каждая строка — словарь с ключами type_id, qty_per_fit, is_charge, slot,
    scope_override_kind, scope_override_ref (§7.5, FR-4). Корпус включается в
    состав как позиция с qty_per_fit=1, is_charge=False (§6.3, §5.3 — HULL_MISSING
    через единый механизм); позиции уникальны по type_id (UNIQUE(fit, type_id)).
    """
    components = [
        FitComponent(
            fit=fit,
            type_id=item["type_id"],
            qty_per_fit=item["qty_per_fit"],
            is_charge=item["is_charge"],
            slot=item["slot"] or "",
            scope_override_kind=item["scope_override_kind"],
            scope_override_ref=item["scope_override_ref"],
        )
        for item in item_dicts
    ]
    with transaction.atomic():
        fit.components.all().delete()
        FitComponent.objects.bulk_create(components)


def _parse_preview_edits(post) -> tuple[dict[int, dict], list[str]]:
    """Правки строк предпросмотра из POST (§7.5, FR-3).

    Формат — ManagementForm-подобный: скрытые поля ``row<i>-type_id``,
    ``row<i>-qty_per_fit``, ``row<i>-delete``, ``row<i>-scope_override_kind``,
    ``row<i>-scope_override_ref`` (плюс служебные is_charge/raw_line). Каждая
    строка валидируется FitComponentRowForm; правки группируются по type_id
    (парсер агрегирует дубликаты, поэтому type_id уникален), количество при
    дубликатах суммируется. Возвращает (правки по type_id, ошибки строк).
    """
    raw: dict[int, dict] = {}
    for key in post.keys():
        match = _ROW_KEY_RE.match(key)
        if match:
            idx = int(match.group(1))
            raw.setdefault(idx, {})[f"row{idx}-{match.group(2)}"] = post.get(key)
    edits: dict[int, dict] = {}
    errors: list[str] = []
    for idx in sorted(raw):
        form = FitComponentRowForm(raw[idx], prefix=f"row{idx}")
        if not form.is_valid():
            errors.append(str(_("Row %s: invalid values.") % (idx + 1)))
            continue
        cleaned = form.cleaned_data
        type_id = cleaned.get("type_id")
        if type_id is None:
            continue
        if type_id in edits:
            # Дубликат type_id в предпросмотре запрещён: количество и override
            # уже заданы существующей строкой — молчаливое слияние теряло бы
            # override. Серверная проверка дублирует JS-проверку при добавлении.
            errors.append(str(_("Type %s is already in the fit; row ignored.") % type_id))
            continue
        edits[type_id] = {
            "qty": cleaned.get("qty_per_fit") or 0,
            "delete": bool(cleaned.get("delete")),
            "scope_kind": cleaned.get("scope_override_kind") or "",
            "scope_ref": cleaned.get("scope_override_ref"),
        }
    return edits, errors


def _build_preview_rows(result, edits_by_type: dict[int, dict], edit_errors: list[str]) -> tuple[list[dict], list[str]]:
    """Строки предпросмотра с формами правки (§7.5, FR-3/FR-4): правки поверх разобранного.

    Правки (qty/delete/override) применяются к разобранным позициям; правки
    с type_id, которого нет в разборе, трактуются как добавленные строки и
    проверяются по справочнику SDE (eveuniverse, §8 — своих копий нет, один
    пакетный запрос, §22.4). Строки без распознанного типа (ошибки парсера)
    остаются read-only. Инвариант §5.3: override области на строке корпуса
    игнорируется — область корпуса всегда равна области фита. Добавление
    дубликата типа запрещено (явная ошибка): количество и override уже заданы
    существующей строкой, молчаливое слияние теряло бы override. is_charge
    распарсенных строк берётся из парсера, добавленных — из категории
    SDE-типа (Charge), а не из клиентских полей. Возвращает
    (строки для шаблона, ошибки правок).
    """
    from eveuniverse.models import EveType

    errors = list(edit_errors)
    parsed_type_ids = {item.type_id for item in result.items if item.type_id}
    names = dict(EveType.objects.filter(pk__in=parsed_type_ids).values_list("pk", "name"))
    rows: list[dict] = []
    for item in result.items:
        if item.type_id is None:
            rows.append({"type_id": None, "form": None, "raw_line": item.raw_line})
            continue
        edit = edits_by_type.pop(item.type_id, None)
        qty = item.qty_per_fit
        delete = False
        kind = ""
        ref = None
        if edit:
            if edit["qty"]:
                qty = edit["qty"]
            delete = edit["delete"]
            kind = edit["scope_kind"]
            ref = edit["scope_ref"]
        if delete and item.is_hull:
            # Корпус удалять нельзя (§7.5): состав обязан содержать корпус.
            errors.append(str(_("The hull row cannot be deleted.")))
            delete = False
        if item.is_hull:
            # Инвариант §5.3: область корпуса = область фита — override корпуса игнорируется.
            kind = ""
            ref = None
        rows.append(
            {
                "type_id": item.type_id,
                "type_name": str(names.get(item.type_id, f"type {item.type_id}")),
                "qty_per_fit": qty,
                "is_charge": item.is_charge,
                "is_hull": item.is_hull,
                "slot": item.slot or "",
                "raw_line": item.raw_line,
                "delete": delete,
                "scope_override_kind": kind or None,
                "scope_override_ref": ref,
                "form": FitComponentRowForm(
                    prefix=f"row{len(rows)}",
                    initial={
                        "type_id": item.type_id,
                        "qty_per_fit": qty,
                        "delete": delete,
                        "scope_override_kind": kind,
                        "scope_override_ref": ref,
                        "is_charge": item.is_charge,
                        "raw_line": item.raw_line,
                    },
                ),
            }
        )
        if item.is_hull:
            # Инвариант §5.3: у hull-строки override области недоступен и не отправляется.
            hull_form = rows[-1]["form"]
            for field_name in ("scope_override_kind", "scope_override_ref"):
                hull_form.fields[field_name].disabled = True
                hull_form.fields[field_name].widget.attrs["title"] = str(_("Hull scope follows the fit scope."))
    # Оставшиеся правки — добавленные через SDE-селектор строки (§7.5, FR-3).
    # Дубликат типа при добавлении запрещён (количество и override уже заданы
    # существующей строкой; молчаливое слияние теряло бы override) — явная ошибка.
    added_type_ids = sorted(set(edits_by_type))
    if added_type_ids:
        added_rows = EveType.objects.filter(pk__in=added_type_ids).values_list(
            "pk", "name", "eve_group__eve_category_id"
        )
        added_info = {row[0]: (str(row[1]), row[2] == CHARGE_CATEGORY_ID) for row in added_rows}
    else:
        added_info = {}
    for type_id in added_type_ids:
        edit = edits_by_type[type_id]
        if type_id not in added_info:
            errors.append(str(_("Unknown type %s; row ignored.") % type_id))
            continue
        if type_id in parsed_type_ids:
            errors.append(str(_("Type %s is already in the fit; added row ignored.") % type_id))
            continue
        type_name, is_charge = added_info[type_id]
        qty = edit["qty"] or 1
        rows.append(
            {
                "type_id": type_id,
                "type_name": type_name,
                "qty_per_fit": qty,
                # is_charge добавленной строки наследуется от категории SDE-типа
                # (Charge, category id 16), а не от клиентского поля.
                "is_charge": is_charge,
                "is_hull": False,
                "slot": "",
                "raw_line": "",
                "delete": edit["delete"],
                "scope_override_kind": edit["scope_kind"] or None,
                "scope_override_ref": edit["scope_ref"],
                "added": True,
                "form": FitComponentRowForm(
                    prefix=f"row{len(rows)}",
                    initial={
                        "type_id": type_id,
                        "qty_per_fit": qty,
                        "delete": edit["delete"],
                        "scope_override_kind": edit["scope_kind"],
                        "scope_override_ref": edit["scope_ref"],
                        "is_charge": is_charge,
                        "raw_line": "",
                    },
                ),
            }
        )
    return rows, errors


def _final_items_from_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Итоговый состав для сохранения из строк предпросмотра (§7.5).

    Удалённые строки и нераспознанные строки EFT отбрасываются; валидация
    сохранения: корпус + >= 1 позиции помимо корпуса, все количества >= 1
    (количества уже провалидированы FitComponentRowForm). is_charge берётся
    из результата разбора / категории SDE (см. _build_preview_rows), а не из
    клиентских полей. Override области для строки корпуса сбрасывается —
    инвариант §5.3 «область корпуса = область фита». Возвращает
    (словари для _save_components, ошибки валидации).
    """
    items: list[dict] = []
    errors: list[str] = []
    for row in rows:
        if row.get("form") is None or row["type_id"] is None:
            continue
        if row["delete"]:
            continue
        if row["qty_per_fit"] is None or row["qty_per_fit"] < 1:
            errors.append(str(_("Quantity per fit must be >= 1.")))
            continue
        is_hull = bool(row["is_hull"])
        items.append(
            {
                "type_id": row["type_id"],
                "qty_per_fit": row["qty_per_fit"],
                "is_charge": row["is_charge"],
                "slot": row["slot"],
                # Инвариант §5.3: корпус наследует область фита — override сбрасывается.
                "scope_override_kind": None if is_hull else row["scope_override_kind"],
                "scope_override_ref": None if is_hull else row["scope_override_ref"],
            }
        )
    has_hull = any(row.get("is_hull") and not row["delete"] for row in rows if row.get("form") is not None)
    non_hull_count = sum(1 for row in rows if row.get("form") is not None and not row["is_hull"] and not row["delete"])
    if not has_hull:
        errors.append(str(_("Hull row is required.")))
    if not non_hull_count:
        errors.append(str(_("At least one component besides the hull is required.")))
    return items, errors


def _process_fit_post(request, form: FitForm, fit: Fit | None = None):
    """Единая обработка POST fit_create/fit_update (§7.5, §14).

    action=parse: разбор EFT без сохранения; введённые правки состава
    (row<i>-поля POST) применяются поверх разбора и переживают повторный
    разбор. action=save: те же правки применяются к итоговому составу;
    валидация сохранения — корпус + >= 1 позиция, количества >= 1 (§7.5).
    Возвращает (response, context_updates) — response не None при редиректе.
    """
    action = request.POST.get("action")
    preview_rows: list[dict] = []
    parse_errors: list[str] = []
    result = None

    if action in ("parse", "save"):
        result = parse_eft(request.POST.get("eft_raw", ""))
        edits_by_type, edit_errors = _parse_preview_edits(request.POST)
        preview_rows, edit_all_errors = _build_preview_rows(result, edits_by_type, edit_errors)
        parse_errors = [f"{e.line_no}: {e.line} — {e.message}" for e in result.errors] + edit_all_errors
        if action == "parse" and result.fit_name and not form.data.get("name"):
            form = FitForm(initial={**request.POST.dict(), "name": result.fit_name})

    if action == "save":
        items, validation_errors = _final_items_from_rows(preview_rows)
        # Валидация сохранения (§7.5): форма, состав парсера, правки строк и итоговый состав.
        if form.is_valid() and result is not None and result.is_valid and not edit_all_errors and not validation_errors:
            fit_obj = form.save(commit=False)
            fit_obj.hull_type_id = result.hull_type_id
            if fit is None:
                fit_obj.created_by = request.user
            fit_obj.save()
            _save_components(fit_obj, items)
            messages.success(request, _("Fit created.") if fit is None else _("Fit updated."))
            return redirect("marketmonitoring:index"), None
        parse_errors = parse_errors + [str(e) for e in form.non_field_errors()] + validation_errors
        messages.error(request, _("Fit composition is invalid; see errors."))

    return None, {
        "form": form,
        "parsed": bool(preview_rows),
        "parse_errors": parse_errors,
        "preview_rows": preview_rows,
        # Прототип строки для JS-добавления: options областей рендерит Django,
        # JS клонирует <template> — choices не дублируются в JS.
        "row_proto": FitComponentRowForm(prefix="__proto__", initial={"qty_per_fit": 1}),
    }


@login_required
def fit_create(request):
    """Создание фита: POST action=parse -> предпросмотр с правками состава (§7.5)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    form = FitForm(request.POST or None)
    response, context = _process_fit_post(request, form)
    if response is not None:
        return response

    context.update({"page_title": _("Add fit")})
    return render(request, "marketmonitoring/fit_form.html", context)


@login_required
def fit_update(request, fit_id: int):
    """Правка фита: повторный разбор EFT, правки состава и обновление компонентов (§14)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    fit = get_object_or_404(Fit, pk=fit_id)
    form = FitForm(request.POST or None, instance=fit)
    response, context = _process_fit_post(request, form, fit=fit)
    if response is not None:
        return response

    context.update({"page_title": _("Edit fit"), "fit": fit})
    return render(request, "marketmonitoring/fit_form.html", context)


@login_required
def fit_delete(request, fit_id: int):
    """Удаление фита с подтверждением (§14)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    fit = get_object_or_404(Fit, pk=fit_id)
    if request.method == "POST":
        fit.delete()
        messages.success(request, _("Fit deleted."))
        return redirect("marketmonitoring:index")

    return render(
        request,
        "marketmonitoring/fit_delete.html",
        {"page_title": _("Delete fit"), "fit": fit},
    )


@login_required
def fit_detail(request, fit_id: int):
    """Карточка фита (§14, view): шапка K/N/статус/сумма, таблица компонентов §14.1.

    Пер-компонентные данные пересчитываются на лету из кэша БД плагина
    (StockLevel / JitaOrderLevel) чистыми функциями calc/engine — без ESI
    (§5.10) и без N+1: одна выборка StockLevel по всем компонентам, один
    запрос JitaOrderLevel по всем типам (§22.4).
    """
    if not has_view(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    from django.apps import apps

    from ..calc import engine
    from ..constants import ScopeKind
    from ..models import FitComponent
    from ._calc_view import build_fit_component_rows

    fit = get_object_or_404(Fit, pk=fit_id)
    data = build_fit_component_rows(fit)
    components = data.components

    # Строки компонентов §14.1: доступность, дефицит, unit, ref, стоимость, бейджи.
    # Единое поведение с recalc_fit (calc/service): пустая область или отсутствие
    # агрегата StockLevel хотя бы по одной позиции — NO_DATA (§6.4, §6.7).
    no_data = not data.fresh or any(data.component_no_data)
    k, bottleneck_indices = engine.calc_fits([c.availability for c in data.component_inputs], data.component_inputs)

    # Статус фита (§6.4): NO_DATA > HULL_MISSING > DEFICIT > OK.
    hull_available = next((c.availability for c in data.component_inputs if c.is_hull), 0)
    if no_data:
        status = FitStatus.NO_DATA
    else:
        status = engine.calc_fit_status(False, hull_available, k, fit.qty_target, allow_hull_missing=True)

    def _scope_of(component: FitComponent) -> tuple[str, int]:
        """Эффективная область позиции: override компонента, иначе область фита (§6.3)."""
        if component.scope_override_kind:
            return component.scope_override_kind, component.scope_override_ref
        return fit.scope_kind, fit.scope_ref

    scope_names = {}
    kind_to_model = {
        ScopeKind.SYSTEM: ("eveuniverse", "EveSolarSystem"),
        ScopeKind.REGION: ("eveuniverse", "EveRegion"),
        ScopeKind.STATION: ("eveuniverse", "EveStation"),
    }
    for scope_kind, scope_ref in {_scope_of(component) for component in components}:
        model_info = kind_to_model.get(scope_kind)
        if model_info:
            model = apps.get_model(*model_info)
            name = model.objects.filter(pk=scope_ref).values_list("name", flat=True).first()
            scope_names[(scope_kind, scope_ref)] = str(name) if name else f"{scope_kind} {scope_ref}"

    rows = []
    for i, (component, calc_result) in enumerate(zip(components, data.calc_results)):
        component_input = data.component_inputs[i]
        row_no_data = data.component_no_data[i]
        rows.append(
            {
                "component": component,
                "type_name": str(data.type_names.get(component.type_id, f"type {component.type_id}")),
                "scope_label": scope_names.get(
                    _scope_of(component), f"{_scope_of(component)[0]} {_scope_of(component)[1]}"
                ),
                "required": fit.qty_target * component.qty_per_fit,
                "available": component_input.availability,
                "deficit": calc_result.deficit,
                "unit": calc_result.unit,
                "ref": calc_result.ref,
                "cost": calc_result.cost,
                "is_not_covered": calc_result.is_not_covered,
                "shortfall": calc_result.shortfall,
                "is_overprice": calc_result.is_overprice,
                "overprice_pct": calc_result.overprice_pct,
                "is_hull": component.type_id == fit.hull_type_id,
                "no_data": row_no_data,
                "is_bottleneck": component.type_id in {data.component_inputs[j].type_id for j in bottleneck_indices},
            }
        )

    display_total = (
        fit.total_cost if fit.status_cache == status else sum((r.cost for r in data.calc_results), Decimal("0"))
    )

    context = {
        "page_title": fit.name,
        "fit": fit,
        "status": status,
        "k": None if no_data else k,
        "total_cost": display_total,
        "reasons": data.reasons if no_data else [],
        "rows": rows,
        # Счётчики шапки (02-fit-detail.md): оверпрайс-позиции и строки с дефицитом.
        "overprice_count": fit.overprice_count or 0,
        "deficit_lines": sum(
            1 for row in rows if not row["no_data"] and row["deficit"] is not None and row["deficit"] > 0
        ),
        "can_edit": has_edit(request.user),
        "export_txt_url": reverse("marketmonitoring:export_txt", args=[fit.pk]),
        "export_csv_url": reverse("marketmonitoring:export_csv", args=[fit.pk]),
        "export_multibuy_url": reverse("marketmonitoring:export_multibuy", args=[fit.pk]),
    }
    return render(request, "marketmonitoring/fit_detail.html", context)
