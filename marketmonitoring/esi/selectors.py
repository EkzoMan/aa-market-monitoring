"""Селекторы выборки данных для синков: типы расчёта и регионы синка (§9.4, §12)."""

from django.apps import apps

from ..constants import LocationKind
from ..models import ComponentRequest, Fit, FitComponent, MarketLocation, PluginSetting


def get_calc_type_ids() -> set[int]:
    """Все type_id, участвующие в расчёте: компоненты фитов, внеклубовые запросы и корпуса фитов."""
    type_ids: set[int] = set()
    type_ids.update(FitComponent.objects.values_list("type_id", flat=True))
    type_ids.update(ComponentRequest.objects.values_list("type_id", flat=True))
    type_ids.update(Fit.objects.values_list("hull_type_id", flat=True))
    return type_ids


def is_jita_enabled() -> bool:
    """Jita доступна для синков: включённая локация-станция с station_id из настроек.

    Явное указание заказчика: регион и стакан Jita не синкаются, если станция
    Jita удалена/отключена в Locations. Один запрос .exists(), без ESI.
    """
    settings_obj = PluginSetting.load()
    return MarketLocation.objects.filter(
        enabled=True,
        kind=LocationKind.STATION,
        station_id=settings_obj.jita_station_id,
    ).exists()


def get_sync_region_ids() -> set[int]:
    """Регионы для синка заказов (§9.4): включённые локации + области расчёта + регион Jita.

    Регион Jita добавляется только при включённой локации Jita (указание
    заказчика). При выключенной локации The Forge жёстко исключается из набора
    даже если он пришёл из явного scope region/system — иначе sync_region_orders
    всё равно сделал бы ESI-запрос к рынку Jita.
    """
    from ..constants import ScopeKind

    region_ids: set[int] = set()
    region_ids.update(MarketLocation.objects.filter(enabled=True).values_list("region_id", flat=True))
    region_ids.update(Fit.objects.filter(scope_kind=ScopeKind.REGION).values_list("scope_ref", flat=True))
    region_ids.update(
        FitComponent.objects.filter(scope_override_kind=ScopeKind.REGION).values_list("scope_override_ref", flat=True)
    )
    region_ids.update(ComponentRequest.objects.filter(scope_kind=ScopeKind.REGION).values_list("scope_ref", flat=True))
    # Регионы систем из scope system — через справочник SDE (eveuniverse), своей копии нет.
    system_refs = set()
    system_refs.update(Fit.objects.filter(scope_kind=ScopeKind.SYSTEM).values_list("scope_ref", flat=True))
    system_refs.update(
        FitComponent.objects.filter(scope_override_kind=ScopeKind.SYSTEM).values_list("scope_override_ref", flat=True)
    )
    system_refs.update(ComponentRequest.objects.filter(scope_kind=ScopeKind.SYSTEM).values_list("scope_ref", flat=True))
    if system_refs:
        EveSolarSystem = apps.get_model("eveuniverse", "EveSolarSystem")
        region_ids.update(
            EveSolarSystem.objects.filter(id__in=system_refs).values_list("eve_constellation__eve_region_id", flat=True)
        )
    jita_region_id = PluginSetting.load().jita_region_id
    if is_jita_enabled():
        region_ids.add(jita_region_id)
    else:
        # Жёсткое исключение после всех источников: явный scope_kind='region'
        # (Fit/FitComponent/ComponentRequest) и system → The Forge через SDE
        # не должны приводить к ESI-запросу к выключенному рынку Jita.
        region_ids.discard(jita_region_id)
    region_ids.discard(None)
    return set(region_ids)
