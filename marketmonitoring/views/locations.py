"""Локации мониторинга (§14, edit): список, вкл/выкл, добавление станций и структур.

Добавление структуры: ввод structure_id + желаемое имя; реальное имя подтянется
при первом синке через fetch_structure_info в задаче (UI к ESI не обращается, §5.10).
"""

import logging
import re
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from .. import tasks
from ..auth_hooks import has_edit
from ..constants import LocationKind, SyncStatus
from ..esi import client as esi_client
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
        if cleaned["kind"] == LocationKind.SYSTEM:
            # Локация-система: station_id/structure_id не заполняются.
            MarketLocation.objects.create(
                name=cleaned["name"],
                kind=LocationKind.SYSTEM,
                solar_system_id=cleaned["solar_system_id"],
                region_id=cleaned["region_id_resolved"],
                sync_status=SyncStatus.OK,
            )
        else:
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
        "kind_system": LocationKind.SYSTEM,
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


@login_required
def region_search(request):
    """JSON-поиск регионов SDE для редактора фитов (edit); лимит 20."""
    if not has_edit(request.user):
        return JsonResponse({"results": [], "error": "permission denied"}, status=403)

    from eveuniverse.models import EveRegion

    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    rows = EveRegion.objects.filter(name__icontains=query).order_by("name").values("id", "name")[:20]
    return JsonResponse({"results": [{"id": row["id"], "name": row["name"]} for row in rows]})


@login_required
def region_search(request):
    """JSON-поиск регионов SDE для редактора фитов (edit); лимит 20."""
    if not has_edit(request.user):
        return JsonResponse({"results": [], "error": "permission denied"}, status=403)

    from eveuniverse.models import EveRegion

    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    rows = EveRegion.objects.filter(name__icontains=query).order_by("name").values("id", "name")[:20]
    return JsonResponse({"results": [{"id": row["id"], "name": row["name"]} for row in rows]})


# Валидный job_id поиска структур: 32 hex-символа (uuid4().hex).
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _search_character_for_request(request) -> tuple[int | None, list[int]]:
    """Персонаж для поиска структур (§9): токены — из AA, плагин токены не хранит.

    Приоритет — явный character_id из GET-параметра (если у пользователя есть
    валидный токен этого персонажа с нужным скоупом), иначе первый доступный.
    Запрос к БД AA (Token) — не ESI (§5.10). Возвращает (character_id|None,
    список доступных character_id).
    """
    candidates = esi_client.list_search_characters(request.user)
    if not candidates:
        return None, []
    requested = request.GET.get("character_id") or ""
    if requested.isdigit() and int(requested) in candidates:
        return int(requested), candidates
    return candidates[0], candidates


@login_required
def structure_search_start(request):
    """Запуск Celery-задачи поиска структур (§9 шаг 1); возвращает job_id.

    Сам поиск выполняется только в задаче search_structures — UI к ESI
    не обращается (§5.10). Минимум ~3 символа (§9). Ответ:
    {"job_id", "character_id", "candidates"} либо ошибку no_token.
    """
    if not has_edit(request.user):
        return JsonResponse({"results": [], "error": "permission denied"}, status=403)
    query = (request.GET.get("q") or "").strip()
    if len(query) < 3:
        return JsonResponse({"results": [], "job_id": None})
    character_id, candidates = _search_character_for_request(request)
    if character_id is None:
        return JsonResponse(
            {
                "results": [],
                "error": "no_token",
                "scope": esi_client.SCOPE_SEARCH_STRUCTURES,
            },
            status=400,
        )
    job_id = uuid.uuid4().hex
    tasks.search_structures.delay(character_id, query, job_id)
    return JsonResponse({"job_id": job_id, "character_id": character_id, "candidates": candidates})


@login_required
def structure_search_result(request):
    """Результат задачи поиска структур из кэша Django (edit): status/results.

    pending — задача ещё не закончилась (или TTL истёк); done — results;
    error — текст сбоя. Ключ job_id строго 32 hex-символа.
    """
    if not has_edit(request.user):
        return JsonResponse({"results": [], "error": "permission denied"}, status=403)
    job_id = request.GET.get("job_id") or ""
    if not _JOB_ID_RE.fullmatch(job_id):
        return JsonResponse({"results": [], "error": "invalid job_id"}, status=400)
    data = cache.get(f"{tasks.SEARCH_CACHE_PREFIX}:{job_id}")
    if data is None:
        return JsonResponse({"status": "pending", "results": []})
    return JsonResponse(data)
