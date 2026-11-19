"""Представления фитов (§14): создание/правка/удаление с разбором EFT и предпросмотром."""

import logging
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
from ..forms import FitForm
from ..models import Fit, FitComponent

logger = logging.getLogger("marketmonitoring")


def _save_components(fit: Fit, items: list) -> None:
    """Перезапись состава фита по результатам разбора (атомарно, идемпотентно).

    Корпус включается в состав как позиция с qty_per_fit=1, is_charge=False
    (§6.3, §5.3 — HULL_MISSING через единый механизм). UNIQUE(fit, type_id):
    если тип корпуса встречается и в items (экзотика), количество корпуса
    суммируется при мерже в парсере — здесь позиции уже уникальны по type_id.
    """
    components = [
        FitComponent(
            fit=fit,
            type_id=item.type_id,
            qty_per_fit=item.qty_per_fit,
            is_charge=item.is_charge,
            slot=item.slot or "",
        )
        for item in items
    ]
    with transaction.atomic():
        fit.components.all().delete()
        FitComponent.objects.bulk_create(components)


@login_required
def fit_create(request):
    """Создание фита: POST action=parse -> предпросмотр без сохранения (§7.5)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    form = FitForm(request.POST or None)
    parsed = None
    parse_errors = []

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "parse":
            # Разбор без сохранения: предпросмотр позиций (§7.5).
            result = parse_eft(request.POST.get("eft_raw", ""))
            parsed = result.items
            parse_errors = result.errors
            if result.fit_name and not form.data.get("name"):
                form = FitForm(initial={**request.POST.dict(), "name": result.fit_name})
        elif action == "save" and form.is_valid():
            # Сохранение только при валидном составе (§7.5).
            result = parse_eft(form.cleaned_data["eft_raw"])
            if not result.is_valid:
                parse_errors = result.errors
                parsed = result.items
                messages.error(request, _("Fit composition is invalid; see parse errors."))
            else:
                fit = form.save(commit=False)
                fit.hull_type_id = result.hull_type_id
                fit.created_by = request.user
                fit.save()
                _save_components(fit, result.items)
                messages.success(request, _("Fit created."))
                return redirect("marketmonitoring:index")

    context = {
        "page_title": _("Add fit"),
        "form": form,
        "parsed": parsed,
        "parse_errors": parse_errors,
    }
    return render(request, "marketmonitoring/fit_form.html", context)


@login_required
def fit_update(request, fit_id: int):
    """Правка фита: повторный разбор EFT и обновление компонентов (§14)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    fit = get_object_or_404(Fit, pk=fit_id)
    form = FitForm(request.POST or None, instance=fit)
    parsed = None
    parse_errors = []

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "parse":
            result = parse_eft(request.POST.get("eft_raw", ""))
            parsed = result.items
            parse_errors = result.errors
        elif action == "save" and form.is_valid():
            result = parse_eft(form.cleaned_data["eft_raw"])
            if not result.is_valid:
                parse_errors = result.errors
                parsed = result.items
                messages.error(request, _("Fit composition is invalid; see parse errors."))
            else:
                fit = form.save(commit=False)
                fit.hull_type_id = result.hull_type_id
                fit.save()
                _save_components(fit, result.items)
                messages.success(request, _("Fit updated."))
                return redirect("marketmonitoring:index")

    context = {
        "page_title": _("Edit fit"),
        "fit": fit,
        "form": form,
        "parsed": parsed,
        "parse_errors": parse_errors,
    }
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
