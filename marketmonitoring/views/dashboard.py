"""Dashboard (§14): таблица фитов с фильтрами/сортировками, узкие места, сводка.

UI обращается только к БД плагина и справочникам SDE через eveuniverse (§5.10) —
никаких запросов ESI из представлений.
"""

import logging
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Max
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _

from ..auth_hooks import has_edit, has_view
from ..calc import engine
from ..calc.engine import ComponentInput
from ..calc.service import get_ships_availability, resolve_scope_ids
from ..constants import ContractFitStatus, FitStatus, ScopeKind
from ..models import Fit, FitComponent, StockLevel, SyncRun
from ..tasks import SYNC_LOCK_KEY

logger = logging.getLogger("marketmonitoring")

# Допустимые сортировки (§14): поле → (asc, desc).
SORT_CHOICES = {
    "name": ("name", "-name"),
    "contracts": ("contracts_available", "-contracts_available"),
    "status": ("status_cache", "-status_cache"),
    "cost": ("total_cost", "-total_cost"),
    "k": ("fits_available", "-fits_available"),
}

BOTTLENECK_DISPLAY_LIMIT = 5


def _fit_scope(fit: Fit, component: FitComponent) -> tuple[str, int]:
    """Эффективная область позиции: override компонента, иначе область фита (§6.3)."""
    if component.scope_override_kind:
        return component.scope_override_kind, component.scope_override_ref
    return fit.scope_kind, fit.scope_ref


def _scope_labels(scope_pairs: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """Человекочитаемые подписи областей из SDE (eveuniverse); массовые запросы, без N+1 (§22.4)."""
    from eveuniverse.models import EveRegion, EveSolarSystem, EveStation

    system_ids = {ref for kind, ref in scope_pairs if kind == ScopeKind.SYSTEM}
    region_ids = {ref for kind, ref in scope_pairs if kind == ScopeKind.REGION}
    station_ids = {ref for kind, ref in scope_pairs if kind == ScopeKind.STATION}
    names = {}
    for system_id, system_name in EveSolarSystem.objects.filter(pk__in=system_ids).values_list("pk", "name"):
        names[(ScopeKind.SYSTEM, system_id)] = str(system_name)
    for region_id, region_name in EveRegion.objects.filter(pk__in=region_ids).values_list("pk", "name"):
        names[(ScopeKind.REGION, region_id)] = str(region_name)
    for station_id, station_name in EveStation.objects.filter(pk__in=station_ids).values_list("pk", "name"):
        names[(ScopeKind.STATION, station_id)] = str(station_name)
    return {**names, **{pair: f"{pair[0]} {pair[1]}" for pair in scope_pairs if pair not in names}}


def _bottlenecks_by_fit(fits_list: list[Fit]) -> dict[int, list[int]]:
    """Узкие места по каждому фиту (§6.3) — без N+1 (§22.4).

    Один запрос компонентов, один резолв на уникальную область, одна выборка
    StockLevel по всем (type_id, location_id). Собираемость считается чистой
    функцией engine.calc_fits.
    """
    if not fits_list:
        return {}
    fit_ids = [fit.pk for fit in fits_list]
    components = list(FitComponent.objects.filter(fit_id__in=fit_ids).order_by("pk"))
    comps_by_fit: dict[int, list[FitComponent]] = defaultdict(list)
    scopes: set[tuple[str, int]] = set()
    type_ids: set[int] = set()
    fit_by_id = {fit.pk: fit for fit in fits_list}
    for component in components:
        comps_by_fit[component.fit_id].append(component)
        scopes.add(_fit_scope(fit_by_id[component.fit_id], component))
        type_ids.add(component.type_id)
    if not components:
        return {fit.pk: [] for fit in fits_list}

    # Резолв областей (§6.1) — один вызов на уникальную область.
    scope_ids_by_key: dict[tuple[str, int], set[int]] = {}
    for scope in scopes:
        scope_ids_by_key[scope] = resolve_scope_ids(*scope)
    location_union: set[int] = set()
    for ids in scope_ids_by_key.values():
        location_union |= ids

    # Одна выборка агрегатов StockLevel по всем типам и локациям областей.
    volume_by_key: dict[tuple[int, int], int] = {}
    present_by_type: dict[int, set[int]] = defaultdict(set)
    for location_id, type_id, total in StockLevel.objects.filter(
        type_id__in=type_ids, location_id__in=location_union
    ).values_list("location_id", "type_id", "total_sell_volume"):
        volume_by_key[(location_id, type_id)] = total
        present_by_type[type_id].add(location_id)

    bottlenecks: dict[int, list[int]] = {}
    for fit in fits_list:
        inputs: list[ComponentInput] = []
        for component in comps_by_fit.get(fit.pk, []):
            scope = _fit_scope(fit, component)
            ids = scope_ids_by_key[scope]
            availability = sum(volume_by_key.get((location_id, component.type_id), 0) for location_id in ids)
            has_data = bool(ids) and present_by_type.get(component.type_id, set()) == ids
            inputs.append(
                ComponentInput(type_id=component.type_id, qty_per_fit=component.qty_per_fit, availability=availability)
            )
            if not has_data:
                # NO_DATA: узкие места по фиту не имеют смысла (§6.7).
                inputs = []
                break
        if not inputs:
            bottlenecks[fit.pk] = []
            continue
        _k, bottleneck_indices = engine.calc_fits([c.availability for c in inputs], inputs)
        bottlenecks[fit.pk] = [inputs[i].type_id for i in bottleneck_indices]
    return bottlenecks


@login_required
def index(request):
    """Dashboard: список фитов (фильтры: статус/область/узкое место; сортировки), сводка «итого докупить» (§14)."""
    if not has_view(request.user):
        # Пользователь аутентифицирован, но права просмотра нет — отказ (§14).
        # redirect на marketmonitoring:index невозможен (это сам index) —
        # стандартный выход: страница входа (login_required-семантика).
        # AA5 не регистрирует namespace «auth» — имя страницы входа: auth_login_user.
        messages.warning(request, _("Permission denied."))
        return redirect("auth_login_user")

    from eveuniverse.models import EveType

    fits = Fit.objects.all()
    status_filter = request.GET.get("status") or ""
    contracts_status_filter = request.GET.get("contracts_status") or ""
    scope_filter = request.GET.get("scope") or ""
    bottleneck_filter = request.GET.get("bottleneck") or ""
    sort_param = request.GET.get("sort") or "name"

    if status_filter in FitStatus.values:
        fits = fits.filter(status_cache=status_filter)
    if contracts_status_filter in ContractFitStatus.values:
        fits = fits.filter(contracts_status_cache=contracts_status_filter)
    if ":" in scope_filter:
        scope_kind, _sep, scope_ref = scope_filter.partition(":")
        if scope_kind in ScopeKind.values and scope_ref.isdigit():
            fits = fits.filter(scope_kind=scope_kind, scope_ref=int(scope_ref))

    sort_key = sort_param.lstrip("-")
    sort_asc, sort_desc = SORT_CHOICES.get(sort_key, SORT_CHOICES["name"])
    reverse = sort_param.startswith("-")
    fits = list(fits.order_by(sort_desc if reverse else sort_asc))

    # Узкие места по всем фитам сразу (массовые выборки, §22.4).
    bottlenecks_by_fit = _bottlenecks_by_fit(fits)
    all_bottleneck_type_ids = {type_id for ids in bottlenecks_by_fit.values() for type_id in ids}
    type_names = dict(EveType.objects.filter(pk__in=all_bottleneck_type_ids).values_list("pk", "name"))

    # Фильтр по узкому месту применяется после расчёта (§14).
    bottleneck_type_id = int(bottleneck_filter) if bottleneck_filter.isdigit() else None
    # Сводка C+K (§6.13): только примитивы из кэша фитов, без запросов ESI и без N+1.
    ships_by_fit = {fit.pk: get_ships_availability(fit) for fit in fits}
    rows = []
    for fit in fits:
        bottleneck_type_ids = bottlenecks_by_fit.get(fit.pk, [])
        if bottleneck_type_id is not None and bottleneck_type_id not in bottleneck_type_ids:
            continue
        names = [str(type_names.get(type_id, f"type {type_id}")) for type_id in bottleneck_type_ids]
        ships = ships_by_fit[fit.pk]
        rows.append(
            {
                "fit": fit,
                "bottleneck_names": names,
                "bottleneck_extra": max(0, len(names) - BOTTLENECK_DISPLAY_LIMIT),
                "ships_in_contracts": ships.ships_in_contracts,
                "ships_from_market": ships.ships_from_market,
                "ships_total": ships.ships_total,
                "ships_status": ships.ships_status,
            }
        )

    # Сводная панель (§14): контракты в первую очередь; стоимость из блока
    # убрана — главное число: фиты с дефицитом контрактов.
    contracts_deficit_fits_count = sum(1 for fit in fits if fit.contracts_status_cache == ContractFitStatus.DEFICIT)
    contracts_missing_ships_total = sum(
        max(0, fit.qty_target - (fit.contracts_available or 0))
        for fit in fits
        if fit.contracts_status_cache == ContractFitStatus.DEFICIT
    )
    contracts_no_data_count = sum(1 for fit in fits if fit.contracts_status_cache == ContractFitStatus.NO_DATA)
    no_data_count = sum(1 for fit in fits if fit.status_cache == FitStatus.NO_DATA)
    hull_missing_count = sum(1 for fit in fits if fit.status_cache == FitStatus.HULL_MISSING)
    # ΣC / ΣK / Σ(C+K) только для фитов, где оба значения известны (решение №41).
    known_ships = [ships for ships in ships_by_fit.values() if not ships.is_partial]
    ships_in_contracts_sum = sum(ships.ships_in_contracts or 0 for ships in known_ships)
    ships_from_market_sum = sum(ships.ships_from_market or 0 for ships in known_ships)
    ships_total_sum = sum(ships.ships_total or 0 for ships in known_ships)

    scope_pairs = list(Fit.objects.values_list("scope_kind", "scope_ref").distinct())
    scope_labels = _scope_labels(scope_pairs)
    scope_choices = sorted({f"{kind}:{ref}" for kind, ref in scope_pairs})
    scope_choice_data = [
        {"value": value, "label": scope_labels.get((value.split(":")[0], int(value.split(":")[1])), value)}
        for value in scope_choices
    ]
    bottleneck_choices = [
        {"type_id": type_id, "name": str(type_names.get(type_id, f"type {type_id}"))}
        for type_id in sorted(all_bottleneck_type_ids)
    ]

    context = {
        "page_title": "Market Monitoring",
        "rows": rows,
        "fit_count": len(rows),
        "status_filter": status_filter,
        "contracts_status_filter": contracts_status_filter,
        "scope_filter": scope_filter,
        "bottleneck_filter": bottleneck_filter,
        "sort_param": sort_param,
        "status_choices": FitStatus.values,
        "contracts_status_choices": ContractFitStatus.values,
        "scope_choices": scope_choice_data,
        "bottleneck_choices": bottleneck_choices,
        "sort_choices": SORT_CHOICES.keys(),
        "contracts_deficit_fits_count": contracts_deficit_fits_count,
        "contracts_missing_ships_total": contracts_missing_ships_total,
        "contracts_no_data_count": contracts_no_data_count,
        "ships_in_contracts_sum": ships_in_contracts_sum,
        "ships_from_market_sum": ships_from_market_sum,
        "ships_total_sum": ships_total_sum,
        "no_data_count": no_data_count,
        "hull_missing_count": hull_missing_count,
        "has_any_fits": Fit.objects.exists(),
        "in_progress": cache.get(SYNC_LOCK_KEY) is not None,
        "last_checked_at": Fit.objects.aggregate(Max("last_checked_at"))["last_checked_at__max"],
        "last_run": SyncRun.objects.order_by("-started_at").first(),
        "can_edit": has_edit(request.user),
    }
    return render(request, "marketmonitoring/dashboard.html", context)
