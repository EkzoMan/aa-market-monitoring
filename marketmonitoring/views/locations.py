"""Локации мониторинга (§14, edit): список, вкл/выкл, добавление станций и структур.

Добавление структуры: ввод structure_id + желаемое имя; реальное имя подтянется
при первом синке через fetch_structure_info в задаче (UI к ESI не обращается, §5.10).
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from ..auth_hooks import has_edit
from ..constants import LocationKind, SyncStatus
from ..forms import MarketLocationForm
from ..models import MarketLocation

logger = logging.getLogger("marketmonitoring")


@login_required
def locations(request):
    """Список локаций (edit, §14): статус синка, последняя синхронизация, вкл/выкл, добавление."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    items = list(MarketLocation.objects.all().order_by("kind", "name"))
    form = MarketLocationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        MarketLocation.objects.create(
            name=cleaned["name"],
            kind=cleaned["kind"],
            station_id=cleaned.get("station_id"),
            structure_id=cleaned.get("structure_id"),
            solar_system_id=cleaned["solar_system_id"],
            region_id=cleaned["region_id_resolved"],
            sync_status=SyncStatus.STALE,
        )
        messages.success(request, _("Location added."))
        return redirect("marketmonitoring:locations")

    context = {
        "page_title": _("Locations"),
        "locations": items,
        "form": form,
        "kind_station": LocationKind.STATION,
        "kind_structure": LocationKind.STRUCTURE,
    }
    return render(request, "marketmonitoring/locations.html", context)


@login_required
def location_toggle(request, location_id: int):
    """Вкл/выкл локации (POST, edit, §14)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    location = get_object_or_404(MarketLocation, pk=location_id)
    if request.method == "POST":
        location.enabled = not location.enabled
        location.save(update_fields=["enabled"])
        messages.success(request, _("Location updated."))
    return redirect("marketmonitoring:locations")


@login_required
def location_delete(request, location_id: int):
    """Удаление локации с подтверждением (POST, edit, §14)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    location = get_object_or_404(MarketLocation, pk=location_id)
    if request.method == "POST":
        location.delete()
        messages.success(request, _("Location deleted."))
        return redirect("marketmonitoring:locations")
    return render(
        request,
        "marketmonitoring/location_delete.html",
        {"page_title": _("Delete location"), "obj": location},
    )


@login_required
def system_search(request):
    """JSON-поиск систем SDE для формы локаций (edit); опциональный фильтр по региону, лимит 20."""
    if not has_edit(request.user):
        return JsonResponse({"results": [], "error": "permission denied"}, status=403)

    from eveuniverse.models import EveSolarSystem

    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    qs = EveSolarSystem.objects.filter(name__icontains=query)
    region_id = request.GET.get("region_id")
    if region_id and region_id.isdigit():
        qs = qs.filter(eve_constellation__eve_region_id=int(region_id))
    rows = qs.order_by("name").values("id", "name")[:20]
    return JsonResponse({"results": [{"id": row["id"], "name": row["name"]} for row in rows]})


@login_required
def station_search(request):
    """JSON-поиск NPC-станций SDE для формы локаций (edit); фильтр по системе, лимит 20."""
    if not has_edit(request.user):
        return JsonResponse({"results": [], "error": "permission denied"}, status=403)

    from eveuniverse.models import EveStation

    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    qs = EveStation.objects.filter(name__icontains=query)
    system_id = request.GET.get("system_id")
    if system_id and system_id.isdigit():
        qs = qs.filter(eve_solar_system_id=int(system_id))
    rows = qs.order_by("name").values("id", "name")[:20]
    return JsonResponse({"results": [{"id": row["id"], "name": row["name"]} for row in rows]})
