"""Страница «Components» (§14, §14.4): список, создание/правка/удаление, поиск типов по SDE.

UI обращается только к БД плагина и справочникам SDE через eveuniverse (§5.10).
"""

import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from ..auth_hooks import has_edit, has_view
from ..calc import service as calc_service
from ..forms import ComponentRequestForm
from ..models import ComponentRequest
from ._calc_view import build_component_request_rows

logger = logging.getLogger("marketmonitoring")


@login_required
def components(request):
    """Список компонентов вне фитов: тип, целевое количество, область, дефицит, стоимость (§14).

    GET только ЧИТАЕТ данные: дефицит/стоимость считаются лёгким способом из
    кэша БД пакетными запросами (views/_calc_view.py, чистые функции calc/engine)
    без пересчёта и записи в БД — кэш статуса записывает только recalc_all
    из Celery-задачи (§12.1, §5.10).
    """
    if not has_view(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    from eveuniverse.models import EveType

    requests = list(ComponentRequest.objects.all().order_by("pk"))
    type_names = dict(EveType.objects.filter(pk__in={r.type_id for r in requests}).values_list("pk", "name"))
    rows = []
    for row in build_component_request_rows(requests):
        request_obj = row["obj"]
        rows.append(
            {
                "obj": request_obj,
                "type_name": str(type_names.get(request_obj.type_id, f"type {request_obj.type_id}")),
                "status": row["status"],
                "deficit": row["deficit"],
                "cost": row["cost"],
            }
        )
    # Сводная строка (04-components.md): сколько компонентов с дефицитом и суммарная стоимость.
    deficit_rows = [row for row in rows if row["deficit"] is not None and row["deficit"] > 0]
    context = {
        "page_title": _("Components"),
        "rows": rows,
        "deficit_count": len(deficit_rows),
        "deficit_total": sum((row["cost"] for row in deficit_rows if row["cost"]), Decimal("0")),
        "can_edit": has_edit(request.user),
    }
    return render(request, "marketmonitoring/components.html", context)


@login_required
def component_create(request):
    """Создание компонента (edit, §14): SDE-селектор типа, количество, область, заметка."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    form = ComponentRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.created_by = request.user
        obj.save()
        calc_service.recalc_component_request(obj)
        messages.success(request, _("Component created."))
        return redirect("marketmonitoring:components")

    return render(
        request,
        "marketmonitoring/component_form.html",
        {"page_title": _("Add component"), "form": form},
    )


@login_required
def component_update(request, component_id: int):
    """Правка компонента (edit, §14); после сохранения — пересчёт (§6.10)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    obj = get_object_or_404(ComponentRequest, pk=component_id)
    form = ComponentRequestForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        calc_service.recalc_component_request(obj)
        messages.success(request, _("Component updated."))
        return redirect("marketmonitoring:components")

    return render(
        request,
        "marketmonitoring/component_form.html",
        {"page_title": _("Edit component"), "form": form, "obj": obj},
    )


@login_required
def component_delete(request, component_id: int):
    """Удаление компонента с подтверждением (edit, §14)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    obj = get_object_or_404(ComponentRequest, pk=component_id)
    if request.method == "POST":
        obj.delete()
        messages.success(request, _("Component deleted."))
        return redirect("marketmonitoring:components")

    from eveuniverse.models import EveType

    type_name = EveType.objects.filter(pk=obj.type_id).values_list("name", flat=True).first()
    return render(
        request,
        "marketmonitoring/component_delete.html",
        {"page_title": _("Delete component"), "obj": obj, "type_name": type_name or f"type {obj.type_id}"},
    )


@login_required
def type_search(request):
    """JSON-поиск имён типов по SDE (edit, §14): EveType iloc name, лимит 20, один запрос (без N+1)."""
    if not has_edit(request.user):
        return JsonResponse({"results": [], "error": "permission denied"}, status=403)

    from eveuniverse.models import EveType

    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    rows = EveType.objects.filter(name__icontains=query).order_by("name").values("id", "name")[:20]
    return JsonResponse({"results": [{"type_id": row["id"], "name": row["name"]} for row in rows]})
