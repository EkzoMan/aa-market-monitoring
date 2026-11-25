"""Тесты этапа 3: ESI-клиент, синк задач, перезапись кэша, guard-интервалы, SyncRun (§18).

ESI полностью замокан на уровне marketmonitoring.esi.client — сеть не используется.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from allianceauth.eveonline.models import EveCharacter
from django.utils import timezone

from .. import tasks
from ..constants import LocationKind, ScopeKind, SourceKind, SyncRunStatus, SyncStatus, TriggerKind
from ..esi import client as esi_client
from ..esi import selectors as esi_selectors
from ..models import (
    ComponentRequest,
    Fit,
    FitComponent,
    JitaOrderLevel,
    MarketLocation,
    MarketOrderCache,
    PluginSetting,
    StockLevel,
    StructureProvider,
    SyncRun,
)
from .test_calc_engine import sde_ids  # noqa: F401 — справочник eveuniverse: система 30000142 → регион 10000002


def _order(type_id=34, price=Decimal("5.50"), volume=100, location_id=60003760, system_id=30000142, region_id=10000002):
    """Сырой sell-заказ в формате ESI (старый мок: есть system_id/region_id)."""
    return {
        "order_id": 1,
        "type_id": type_id,
        "price": price,
        "volume_remain": volume,
        "is_buy_order": False,
        "location_id": location_id,
        "system_id": system_id,
        "region_id": region_id,
    }


STRUCTURE_ID = 10221127
STRUCTURE_SYSTEM_ID = 30002187
STRUCTURE_REGION_ID = 10000043


def _structure_order(type_id=34, price=Decimal("5.50"), volume=100, location_id=STRUCTURE_ID):
    """Реалистичный ордер структуры: в ESI-ответе нет system_id/solar_system_id/region_id."""
    return {
        "order_id": 1,
        "type_id": type_id,
        "price": price,
        "volume_remain": volume,
        "is_buy_order": False,
        "location_id": location_id,
    }


def _structure_location():
    """Включённая структура-локация с координатами из MarketLocation (не из ордера ESI)."""
    return MarketLocation.objects.create(
        name=f"Structure {STRUCTURE_ID}",
        kind=LocationKind.STRUCTURE,
        structure_id=STRUCTURE_ID,
        solar_system_id=STRUCTURE_SYSTEM_ID,
        region_id=STRUCTURE_REGION_ID,
        enabled=True,
    )


def _structure_provider(character_id=99003001):
    """Провайдер структур без сети: fetch_* в тестах замоканы на уровне esi.client."""
    char = EveCharacter.objects.create(
        character_id=character_id,
        character_name=f"Provider {character_id}",
        corporation_id=98000001,
        corporation_name="Corp",
        corporation_ticker="CRP",
    )
    return StructureProvider.objects.create(character=char, priority=10)


@pytest.fixture(autouse=True)
def _eager_celery(settings):
    """Тесты без сети: Celery-задачи в eager-режиме (без брокера)."""
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = False
    from celery import current_app

    old = current_app.conf.task_always_eager
    old_prop = current_app.conf.task_eager_propagates
    current_app.conf.task_always_eager = True
    current_app.conf.task_eager_propagates = False
    yield
    current_app.conf.task_always_eager = old
    current_app.conf.task_eager_propagates = old_prop


@pytest.fixture
def settings_row(db):
    """Singleton-настройки."""
    return PluginSetting.load()


def _set_jita_location(settings_row, *, enabled: bool):
    """Создать/обновить локацию-станцию Jita с нужным флагом enabled (без ESI)."""
    location, _ = MarketLocation.objects.update_or_create(
        kind=LocationKind.STATION,
        station_id=settings_row.jita_station_id,
        defaults={
            "name": "Jita IV-4",
            "solar_system_id": 30000142,
            "region_id": settings_row.jita_region_id,
            "enabled": enabled,
            "sync_status": SyncStatus.OK if enabled else SyncStatus.STALE,
            "last_synced_at": timezone.now() if enabled else None,
        },
    )
    return location


@pytest.fixture
def jita_enabled(settings_row):
    """Позитивные сценарии: локация Jita включена в Locations."""
    return _set_jita_location(settings_row, enabled=True)


@pytest.fixture
def jita_disabled(settings_row):
    """Негативные сценарии: локация Jita отключена в Locations."""
    return _set_jita_location(settings_row, enabled=False)


def _called_region_ids(fetch_mock) -> list[int]:
    """Регионы, с которыми вызывался мок fetch_region_orders."""
    return [call.args[0] for call in fetch_mock.call_args_list if call.args]


@pytest.mark.django_db
def test_get_sync_region_ids_respects_jita_toggle(settings_row, sde_ids):  # noqa: F811
    """Jita-регион добавляется только при включённой локации; остальные источники не ломаются."""
    jita = _set_jita_location(settings_row, enabled=False)
    MarketLocation.objects.create(
        name="Amarr",
        kind=LocationKind.STATION,
        station_id=60008494,
        solar_system_id=30002187,
        region_id=10000043,
        enabled=True,
    )
    Fit.objects.create(
        name="Other region fit",
        hull_type_id=1,
        scope_kind=ScopeKind.REGION,
        scope_ref=10000043,
    )

    region_ids = esi_selectors.get_sync_region_ids()
    assert settings_row.jita_region_id not in region_ids
    assert 10000043 in region_ids

    jita.enabled = True
    jita.save(update_fields=["enabled"])
    region_ids = esi_selectors.get_sync_region_ids()
    assert settings_row.jita_region_id in region_ids
    assert 10000043 in region_ids


@pytest.mark.django_db
def test_get_sync_region_ids_excludes_jita_region_from_explicit_region_scope_when_disabled(
    settings_row, sde_ids
):  # noqa: F811
    """Выключенная Jita: явный region-scope на The Forge не приводит к ESI-синку её рынка."""
    _set_jita_location(settings_row, enabled=False)
    MarketLocation.objects.create(
        name="Amarr",
        kind=LocationKind.STATION,
        station_id=60008494,
        solar_system_id=30002187,
        region_id=10000043,
        enabled=True,
    )
    fit = Fit.objects.create(
        name="Forge region fit",
        hull_type_id=1,
        scope_kind=ScopeKind.REGION,
        scope_ref=settings_row.jita_region_id,
    )
    FitComponent.objects.create(
        fit=fit,
        type_id=1,
        qty_per_fit=1,
        scope_override_kind=ScopeKind.REGION,
        scope_override_ref=settings_row.jita_region_id,
    )
    ComponentRequest.objects.create(
        type_id=1,
        scope_kind=ScopeKind.REGION,
        scope_ref=settings_row.jita_region_id,
    )

    assert settings_row.jita_region_id not in esi_selectors.get_sync_region_ids()
    with mock.patch.object(esi_client, "fetch_region_orders", return_value=([], False)) as fetch:
        result = tasks.sync_region_orders(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert settings_row.jita_region_id not in _called_region_ids(fetch)
    assert 10000043 in _called_region_ids(fetch)


@pytest.mark.django_db
def test_get_sync_region_ids_excludes_jita_region_from_system_scope_when_disabled(settings_row, sde_ids):  # noqa: F811
    """Выключенная Jita: scope на систему 30000142 не резолвится в регион The Forge для синка."""
    _set_jita_location(settings_row, enabled=False)
    MarketLocation.objects.create(
        name="Amarr",
        kind=LocationKind.STATION,
        station_id=60008494,
        solar_system_id=30002187,
        region_id=10000043,
        enabled=True,
    )
    fit = Fit.objects.create(
        name="Jita system fit",
        hull_type_id=1,
        scope_kind=ScopeKind.SYSTEM,
        scope_ref=30000142,
    )
    FitComponent.objects.create(
        fit=fit,
        type_id=1,
        qty_per_fit=1,
        scope_override_kind=ScopeKind.SYSTEM,
        scope_override_ref=30000142,
    )
    ComponentRequest.objects.create(type_id=1, scope_kind=ScopeKind.SYSTEM, scope_ref=30000142)

    assert settings_row.jita_region_id not in esi_selectors.get_sync_region_ids()
    with mock.patch.object(esi_client, "fetch_region_orders", return_value=([], False)) as fetch:
        result = tasks.sync_region_orders(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert settings_row.jita_region_id not in _called_region_ids(fetch)
    assert 10000043 in _called_region_ids(fetch)


@pytest.mark.parametrize("scope_kind,scope_ref", [(ScopeKind.REGION, 10000002), (ScopeKind.SYSTEM, 30000142)])
@pytest.mark.django_db
def test_get_sync_region_ids_keeps_jita_region_from_scope_when_enabled(
    settings_row, sde_ids, scope_kind, scope_ref
):  # noqa: F811
    """Регрессия: включённая Jita оставляет The Forge в наборе для region/system-scope."""
    _set_jita_location(settings_row, enabled=True)
    Fit.objects.create(
        name=f"Jita {scope_kind} fit",
        hull_type_id=1,
        scope_kind=scope_kind,
        scope_ref=scope_ref,
    )
    assert settings_row.jita_region_id in esi_selectors.get_sync_region_ids()


@pytest.mark.django_db
def test_replace_source_cache_atomic(settings_row):
    """Перезапись кэша по источнику идемпотентна: старые строки удаляются, новые вставляются."""
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=10000002,
        location_id=60003760,
        solar_system_id=30000142,
        region_id=10000002,
        type_id=34,
        price=1,
        volume=1,
        fetched_at=timezone.now(),
    )
    rows = [
        {
            "source_kind": SourceKind.REGION,
            "source_ref": 10000002,
            "location_id": 60003760,
            "solar_system_id": 30000142,
            "region_id": 10000002,
            "type_id": 35,
            "price": 2,
            "volume": 5,
            "fetched_at": timezone.now(),
        },
    ]
    n = tasks._replace_source_cache(SourceKind.REGION, 10000002, rows)
    assert n == 1
    qs = MarketOrderCache.objects.filter(source_kind=SourceKind.REGION, source_ref=10000002)
    assert qs.count() == 1
    assert qs.first().type_id == 35
    # Повторный прогон — то же состояние.
    tasks._replace_source_cache(SourceKind.REGION, 10000002, rows)
    assert MarketOrderCache.objects.filter(source_kind=SourceKind.REGION, source_ref=10000002).count() == 1


@pytest.mark.django_db
def test_sync_jita_book_writes_cache_and_levels(settings_row, jita_enabled):  # noqa: F811
    """sync_jita_book: запись кэша по источнику + JitaOrderLevel + SyncRun ok."""
    with mock.patch.object(esi_client, "fetch_jita_orders") as fetch:
        fetch.return_value = (
            [
                _order(type_id=34, price=Decimal("10.00"), volume=50),
                _order(type_id=34, price=Decimal("9.00"), volume=20),
            ],
            False,
        )
        result = tasks.sync_jita_book(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert MarketOrderCache.objects.filter(source_kind=SourceKind.REGION, source_ref=10000002).count() == 2
    levels = JitaOrderLevel.objects.filter(type_id=34).order_by("rank")
    assert levels.count() == 2
    assert levels.first().rank == 1 and levels.first().price == Decimal("9.00")
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_jita_book").latest("started_at")
    assert run.status == SyncRunStatus.OK
    assert run.trigger == TriggerKind.MANUAL
    assert run.finished_at is not None


@pytest.mark.django_db
def test_sync_jita_book_guard_interval(settings_row, jita_enabled):  # noqa: F811
    """Guard-интервал: beat-запуск сразу после прогона пропускается, manual — выполняется."""
    SyncRun.objects.create(
        task_name="marketmonitoring.sync_jita_book",
        trigger=TriggerKind.BEAT,
        started_at=timezone.now(),
        status=SyncRunStatus.OK,
    )
    with mock.patch.object(esi_client, "fetch_jita_orders") as fetch:
        fetch.return_value = ([_order()], False)
        result = tasks.sync_jita_book(trigger=TriggerKind.BEAT)
        assert result.get("skipped") is True
        fetch.assert_not_called()
        result = tasks.sync_jita_book(trigger=TriggerKind.MANUAL)
        assert result.get("ok") is True
        fetch.assert_called_once()


@pytest.mark.django_db
def test_sync_jita_book_error_recorded(settings_row, jita_enabled):  # noqa: F811
    """Сбой ESI фиксируется в SyncRun со статусом failed."""
    with mock.patch.object(esi_client, "fetch_jita_orders", side_effect=RuntimeError("boom")):
        result = tasks.sync_jita_book(trigger=TriggerKind.MANUAL)
    assert result["ok"] is False
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_jita_book").latest("started_at")
    assert run.status == SyncRunStatus.FAILED
    assert "boom" in run.errors


@pytest.mark.parametrize("trigger", [TriggerKind.MANUAL, TriggerKind.BEAT])
@pytest.mark.django_db
def test_sync_jita_book_disabled_skips_esi_and_clears_cache(settings_row, trigger):
    """Выключенная Jita: нет ESI, нет SyncRun/уведомлений, старый стакан и region-кэш удалены."""
    _set_jita_location(settings_row, enabled=False)
    now = timezone.now()
    JitaOrderLevel.objects.create(type_id=34, rank=1, price=Decimal("100.00"), volume=10, fetched_at=now)
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=settings_row.jita_region_id,
        location_id=60003760,
        solar_system_id=30000142,
        region_id=settings_row.jita_region_id,
        type_id=34,
        price=Decimal("100.00"),
        volume=10,
        fetched_at=now,
    )
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=10000043,
        location_id=60008494,
        solar_system_id=30002187,
        region_id=10000043,
        type_id=34,
        price=Decimal("100.00"),
        volume=10,
        fetched_at=now,
    )
    with (
        mock.patch.object(
            esi_client, "fetch_jita_orders", side_effect=AssertionError("ESI must not be called")
        ) as fetch,
        mock.patch.object(tasks, "_chain_recalc_and_notify") as chain,
        mock.patch.object(tasks.recalc_stock_levels, "delay") as recalc_delay,
        mock.patch.object(tasks.notify_deficit, "delay") as notify_delay,
    ):
        result = tasks.sync_jita_book(trigger=trigger)

    assert result == {"skipped": True, "reason": "jita_disabled"}
    fetch.assert_not_called()
    chain.assert_not_called()
    recalc_delay.assert_not_called()
    notify_delay.assert_not_called()
    assert not JitaOrderLevel.objects.exists()
    assert not MarketOrderCache.objects.filter(
        source_kind=SourceKind.REGION, source_ref=settings_row.jita_region_id
    ).exists()
    # Чужой регион не задеваем: строгий scope (§5.1).
    assert MarketOrderCache.objects.filter(source_kind=SourceKind.REGION, source_ref=10000043).exists()
    assert SyncRun.objects.count() == 0


@pytest.mark.django_db
def test_recalc_stock_levels_replaces_by_location(settings_row):
    """recalc_stock_levels: агрегация полной заменой (total_sell_volume, best_sell)."""
    now = timezone.now()
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=10000002,
        location_id=60003760,
        solar_system_id=30000142,
        region_id=10000002,
        type_id=34,
        price=10,
        volume=30,
        fetched_at=now,
    )
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=10000002,
        location_id=60003760,
        solar_system_id=30000142,
        region_id=10000002,
        type_id=34,
        price=9,
        volume=20,
        fetched_at=now,
    )
    result = tasks.recalc_stock_levels(trigger=TriggerKind.MANUAL, location_ids=[60003760])
    assert result["ok"] is True
    level = StockLevel.objects.get(location_id=60003760, type_id=34)
    assert level.total_sell_volume == 50
    assert str(level.best_sell) == "9.00"
    # Повторный прогон не дублирует строки.
    tasks.recalc_stock_levels(trigger=TriggerKind.MANUAL, location_ids=[60003760])
    assert StockLevel.objects.filter(location_id=60003760).count() == 1


@pytest.mark.django_db
def test_sync_chain_manual_does_not_notify(settings_row, jita_enabled):  # noqa: F811
    """Цепочка §12.3: при trigger=manual notify_deficit не ставится в очередь."""
    with (
        mock.patch.object(esi_client, "fetch_jita_orders") as fetch,
        mock.patch.object(tasks.recalc_stock_levels, "delay") as recalc_delay,
        mock.patch.object(tasks.notify_deficit, "delay") as notify_delay,
    ):
        fetch.return_value = ([_order()], False)
        tasks.sync_jita_book(trigger=TriggerKind.MANUAL)
        recalc_delay.assert_called_once()
        notify_delay.assert_not_called()


@pytest.mark.django_db
def test_sync_chain_beat_queues_notify(settings_row, jita_enabled):  # noqa: F811
    """Цепочка §12.3: при trigger=beat и включённых уведомлениях notify_deficit ставится в очередь."""
    with (
        mock.patch.object(esi_client, "fetch_jita_orders") as fetch,
        mock.patch.object(tasks.recalc_stock_levels, "delay"),
        mock.patch.object(tasks.notify_deficit, "delay") as notify_delay,
    ):
        fetch.return_value = ([_order()], False)
        tasks.sync_jita_book(trigger=TriggerKind.BEAT)
        notify_delay.assert_called_once()


@pytest.mark.django_db
def test_notify_deficit_manual_skipped():
    """notify_deficit при manual не отправляет уведомления (§12.4) — каркас."""
    result = tasks.notify_deficit(trigger=TriggerKind.MANUAL)
    assert result.get("skipped") is True
    assert SyncRun.objects.filter(task_name="marketmonitoring.notify_deficit").count() == 0


@pytest.mark.django_db
def test_sync_region_orders_per_region_isolation(settings_row):
    """Сбой одного региона не прерывает остальные (§9.5); статус partial/failed фиксируется."""
    MarketLocation.objects.create(
        name="Jita",
        station_id=settings_row.jita_station_id,
        solar_system_id=30000142,
        region_id=settings_row.jita_region_id,
        enabled=True,
    )
    MarketLocation.objects.create(
        name="Amarr",
        station_id=60008494,
        solar_system_id=30002187,
        region_id=10000043,
        enabled=True,
    )
    with mock.patch.object(esi_client, "fetch_region_orders") as fetch:
        fetch.side_effect = [([_order()], False), RuntimeError("net down")]
        result = tasks.sync_region_orders(trigger=TriggerKind.MANUAL, region_id=None)
    assert result["ok"] is True
    assert result["regions_ok"] >= 1 and result["regions_failed"] >= 1
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_region_orders").latest("started_at")
    assert run.status == SyncRunStatus.PARTIAL
    assert "net down" in run.errors


@pytest.mark.django_db
def test_build_order_cache_rows_filters_buy_orders():
    """build_order_cache_rows: только sell-заказы, volume из volume_remain."""
    orders = [_order(volume=7), {**_order(), "is_buy_order": True}]
    rows = esi_client.build_order_cache_rows(orders, source_kind="region", source_ref=10000002)
    assert len(rows) == 1
    assert rows[0]["volume"] == 7
    assert rows[0]["source_kind"] == SourceKind.REGION


@pytest.mark.django_db
def test_build_order_cache_rows_reads_real_solar_system_id_and_params():
    """build_order_cache_rows: реальное поле solar_system_id, явные параметры и старый мок."""
    real_esi = {**_structure_order(), "solar_system_id": 30000142, "region_id": 10000002}
    rows = esi_client.build_order_cache_rows([real_esi], source_kind=SourceKind.STRUCTURE, source_ref=STRUCTURE_ID)
    assert len(rows) == 1
    assert rows[0]["solar_system_id"] == 30000142
    assert rows[0]["region_id"] == 10000002

    # У структур нет координат в ESI-ответе — они передаются из MarketLocation.
    rows = esi_client.build_order_cache_rows(
        [_structure_order()],
        source_kind=SourceKind.STRUCTURE,
        source_ref=STRUCTURE_ID,
        solar_system_id=STRUCTURE_SYSTEM_ID,
        region_id=STRUCTURE_REGION_ID,
    )
    assert rows[0]["solar_system_id"] == STRUCTURE_SYSTEM_ID
    assert rows[0]["region_id"] == STRUCTURE_REGION_ID

    # Обратная совместимость: старый мок с system_id продолжает работать.
    legacy = esi_client.build_order_cache_rows([_order()], source_kind=SourceKind.REGION, source_ref=10000002)
    assert legacy[0]["solar_system_id"] == 30000142
    assert legacy[0]["region_id"] == 10000002


@pytest.mark.django_db
def test_build_order_cache_rows_skips_order_without_system_id():
    """Ордер без system-полей и без параметра пропускается; остальные строки пишутся."""
    no_system = _structure_order(type_id=34)
    with_system = {**_structure_order(type_id=35), "solar_system_id": 30000142}
    rows = esi_client.build_order_cache_rows(
        [no_system, with_system],
        source_kind=SourceKind.STRUCTURE,
        source_ref=STRUCTURE_ID,
        region_id=STRUCTURE_REGION_ID,
    )
    assert len(rows) == 1
    assert rows[0]["type_id"] == 35
    assert rows[0]["solar_system_id"] == 30000142
    assert rows[0]["region_id"] == STRUCTURE_REGION_ID


@pytest.mark.django_db
def test_sync_structure_orders_uses_market_location_coordinates(settings_row):
    """sync_structure_orders: координаты строк кэша берутся из MarketLocation, не из structure_id."""
    location = _structure_location()
    _structure_provider()
    with (
        mock.patch.object(esi_client, "fetch_structure_orders", return_value=([_structure_order()], False)),
        mock.patch.object(esi_client, "fetch_structure_info", return_value={"name": "Fortizar Alpha"}),
        mock.patch.object(esi_client, "build_order_cache_rows", wraps=esi_client.build_order_cache_rows) as build_rows,
        mock.patch.object(tasks.recalc_stock_levels, "delay") as recalc_delay,
    ):
        result = tasks.sync_structure_orders(trigger=TriggerKind.MANUAL)

    assert result["ok"] is True
    assert result["locations_ok"] == 1
    recalc_delay.assert_called_once()
    build_rows.assert_called_once()
    kwargs = build_rows.call_args.kwargs
    assert kwargs["source_kind"] == SourceKind.STRUCTURE
    assert kwargs["source_ref"] == location.structure_id
    assert kwargs["solar_system_id"] == location.solar_system_id
    assert kwargs["region_id"] == location.region_id
    row = MarketOrderCache.objects.get(source_kind=SourceKind.STRUCTURE, source_ref=location.structure_id)
    assert row.location_id == location.structure_id
    assert row.solar_system_id == location.solar_system_id
    assert row.region_id == location.region_id
    assert row.region_id != location.structure_id
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_structure_orders").latest("started_at")
    assert run.errors == ""


@pytest.mark.django_db
def test_recalc_stock_levels_after_structure_sync_uses_location_coordinates(settings_row):
    """StockLevel после structure-синка: корректные solar_system_id/region_id, без structure_id вместо региона."""
    location = _structure_location()
    _structure_provider()
    with (
        mock.patch.object(esi_client, "fetch_structure_orders", return_value=([_structure_order(volume=25)], False)),
        mock.patch.object(esi_client, "fetch_structure_info", return_value={"name": "Fortizar Alpha"}),
        mock.patch.object(tasks.recalc_stock_levels, "delay"),
    ):
        result = tasks.sync_structure_orders(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True

    recalc = tasks.recalc_stock_levels(trigger=TriggerKind.MANUAL, location_ids=[location.structure_id])
    assert recalc["ok"] is True
    level = StockLevel.objects.get(location_id=location.structure_id, type_id=34)
    assert level.solar_system_id == location.solar_system_id
    assert level.region_id == location.region_id
    assert level.region_id != location.structure_id
    assert level.total_sell_volume == 25


@pytest.mark.django_db
def test_fetch_jita_orders_per_type_pagination(settings_row):
    """Обход стакана Jita по одиночным type_id с пагинацией по X-Pages/пустой странице (§9.1, §9.2)."""
    calls: list[dict] = []
    pages_seen: list[int] = []

    class FakeOp:
        """Мок операции GetMarketsRegionIdOrders со спекой вызова и result()."""

        headers = {"X-Pages": "2"}

        def __call__(self, **kwargs):
            calls.append(kwargs)
            pages_seen.append(kwargs.get("page", 1))
            return self

        def result(self, **kwargs):
            # Ровно одна непустая страница на вызов (page=1), вторая страница — пустая.
            if pages_seen[-1] >= 2:
                return ([], self)
            return ([_order(type_id=calls[-1]["type_id"])], self)

    fake_client = mock.Mock()
    fake_client.client.Market.GetMarketsRegionIdOrders = FakeOp()
    with (
        mock.patch.object(esi_client, "esi", fake_client),
        mock.patch.object(esi_client, "_check_error_limit"),
    ):
        orders, partial = esi_client.fetch_jita_orders([1, 2])
    assert partial is False
    assert len(orders) == 2
    assert [call["type_id"] for call in calls if call["page"] == 1] == [1, 2]
    assert pages_seen.count(2) == 2


@pytest.mark.django_db
def test_fetch_jita_orders_rate_limit_stop_marks_partial(settings_row, jita_enabled):  # noqa: F811
    """RateLimitStop при обходе страниц Jita → partial=True; sync_jita_book пишет SyncRun partial."""
    from esi.exceptions import ESIErrorLimitException

    with mock.patch.object(esi_client, "_page_results") as pager:
        pager.side_effect = esi_client.RateLimitStop(120)
        orders, partial = esi_client.fetch_jita_orders([34])
    assert partial is True and orders == []

    # Цепочка: sync_jita_book с partial → SyncRun со статусом PARTIAL.
    with (
        mock.patch.object(esi_client, "fetch_jita_orders") as fetch,
        mock.patch.object(esi_client, "build_order_cache_rows", return_value=[]),
        mock.patch.object(tasks, "_replace_jita_levels", return_value=0),
    ):
        fetch.return_value = ([], True)
        result = tasks.sync_jita_book(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True and result["partial"] is True
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_jita_book").latest("started_at")
    assert run.status == SyncRunStatus.PARTIAL


@pytest.mark.django_db
def test_call_with_retry_on_timeout_and_server_error(monkeypatch):
    """Ретраи на таймаутах/сетевых ошибках и 5xx: 3 попытки, backoff 2/4/8 (§9.2)."""
    import requests
    from esi.exceptions import HTTPServerError

    sleeps: list[float] = []
    monkeypatch.setattr(esi_client.time, "sleep", lambda s: sleeps.append(s))

    # Таймаут: первая попытка падает, вторая успешна.
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise requests.Timeout("timed out")
        return "ok"

    assert esi_client._call_with_retry(flaky) == "ok"
    assert sleeps == [2]

    # Исчерпание попыток на 5xx — исключение пробрасывается.
    sleeps.clear()

    def always_5xx():
        raise HTTPServerError(status_code=503, headers={}, data=None)

    with pytest.raises(HTTPServerError):
        esi_client._call_with_retry(always_5xx)
    # 3 попытки = 2 паузы (2, 4); третья попытка без сна пробрасывает исключение.
    assert sleeps == [2, 4]


@pytest.mark.django_db
def test_cleanup_orphan_cache(settings_row, jita_enabled):  # noqa: F811
    """cleanup_orphan_cache: удаляет только устаревший кэш отключённых локаций (§12.1)."""
    now = timezone.now()
    old = now - timedelta(hours=25)
    MarketLocation.objects.create(
        name="Disabled Station",
        station_id=60000001,
        solar_system_id=30000142,
        region_id=10000002,
        enabled=False,
    )
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=10000002,
        location_id=60000001,
        solar_system_id=30000142,
        region_id=10000002,
        type_id=34,
        price=1,
        volume=1,
        fetched_at=old,
    )
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=10000002,
        location_id=60003760,
        solar_system_id=30000142,
        region_id=10000002,
        type_id=34,
        price=1,
        volume=1,
        fetched_at=now,
    )
    result = tasks.cleanup_orphan_cache(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert result["deleted"] == 1
    assert MarketOrderCache.objects.filter(location_id=60003760).exists()
    assert not MarketOrderCache.objects.filter(location_id=60000001).exists()


@pytest.mark.parametrize("jita_present", [True, False])
@pytest.mark.django_db
def test_cleanup_orphan_cache_removes_jita_when_disabled_or_deleted(settings_row, jita_present):
    """Jita отключена/удалена: стакан и region-кэш удаляются сразу, без stale-цен (§12.1)."""
    if jita_present:
        _set_jita_location(settings_row, enabled=False)
    now = timezone.now()
    JitaOrderLevel.objects.create(type_id=34, rank=1, price=Decimal("100.00"), volume=10, fetched_at=now)
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=settings_row.jita_region_id,
        location_id=60003760,
        solar_system_id=30000142,
        region_id=settings_row.jita_region_id,
        type_id=34,
        price=Decimal("100.00"),
        volume=10,
        fetched_at=now,  # свежая строка — cleanup всё равно обязан убрать рынок Jita
    )
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=10000043,
        location_id=60008494,
        solar_system_id=30002187,
        region_id=10000043,
        type_id=34,
        price=Decimal("100.00"),
        volume=10,
        fetched_at=now,
    )

    result = tasks.cleanup_orphan_cache(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert not JitaOrderLevel.objects.exists()
    assert not MarketOrderCache.objects.filter(
        source_kind=SourceKind.REGION, source_ref=settings_row.jita_region_id
    ).exists()
    assert MarketOrderCache.objects.filter(source_kind=SourceKind.REGION, source_ref=10000043).exists()


@pytest.mark.django_db
def test_sync_region_orders_triggers_recalc_and_stock_level(settings_row, jita_enabled):  # noqa: F811
    """Цепочка §12.3: sync_region_orders явно передаёт список локаций в recalc_stock_levels
    (включая пустые), и StockLevel реально создаётся по локациям из свежего кэша (решение №46)."""
    with mock.patch.object(esi_client, "fetch_region_orders") as fetch:
        fetch.return_value = ([_order(type_id=34, price=7.0, volume=42)], False)
        result = tasks.sync_region_orders(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert MarketOrderCache.objects.filter(source_kind=SourceKind.REGION, source_ref=10000002).exists()
    level = StockLevel.objects.filter(location_id=60003760, type_id=34).first()
    assert level is not None
    assert level.total_sell_volume == 42
    run = SyncRun.objects.filter(task_name="marketmonitoring.recalc_stock_levels").latest("started_at")
    assert run.status == SyncRunStatus.OK


@pytest.mark.django_db
def test_sync_region_orders_marks_station_and_system_sync_status(settings_row):
    """Региональный синк обновляет статус NPC-станций и локаций-систем (§8), но не структур."""
    station = MarketLocation.objects.create(
        name="Jita IV-4",
        kind=LocationKind.STATION,
        station_id=60003760,
        solar_system_id=30000142,
        region_id=settings_row.jita_region_id,
        enabled=True,
    )
    system = MarketLocation.objects.create(
        name="Jita",
        kind=LocationKind.SYSTEM,
        solar_system_id=30000142,
        region_id=settings_row.jita_region_id,
        enabled=True,
        sync_status=SyncStatus.STALE,
    )
    structure = MarketLocation.objects.create(
        name="Fort",
        kind=LocationKind.STRUCTURE,
        structure_id=1022735589923,
        solar_system_id=30000142,
        region_id=settings_row.jita_region_id,
        enabled=True,
    )
    with mock.patch.object(esi_client, "fetch_region_orders") as fetch:
        fetch.return_value = ([_order(type_id=34, price=7.0, volume=1)], False)
        tasks.sync_region_orders(trigger=TriggerKind.MANUAL)
    station.refresh_from_db()
    system.refresh_from_db()
    assert station.sync_status == SyncStatus.OK
    assert station.last_synced_at is not None
    assert system.sync_status == SyncStatus.OK
    assert system.last_synced_at is not None
    # Структуры не затрагиваются региональным синком (структурный синк — отдельная задача).
    structure.refresh_from_db()
    assert structure.sync_status == SyncStatus.STALE

    with mock.patch.object(esi_client, "fetch_region_orders") as fetch:
        fetch.side_effect = RuntimeError("boom")
        tasks.sync_region_orders(trigger=TriggerKind.MANUAL)
    station.refresh_from_db()
    system.refresh_from_db()
    assert station.sync_status == SyncStatus.ERROR
    assert system.sync_status == SyncStatus.ERROR


@pytest.mark.django_db
def test_cleanup_orphan_cache_keeps_needed_regions(settings_row):
    """cleanup_orphan_cache не удаляет кэш регионов, которые ещё нужны по selectors (§9.4)."""
    now = timezone.now()
    old = now - timedelta(hours=25)
    # Включённая станция Jita: её location_id не должен попадать под очистку станций.
    MarketLocation.objects.create(
        name="Jita",
        station_id=60003760,
        solar_system_id=30000142,
        region_id=settings_row.jita_region_id,
        enabled=True,
    )
    # Регион, не входящий в get_sync_region_ids → удаляется.
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=10000001,
        location_id=60000010,
        solar_system_id=30000001,
        region_id=10000001,
        type_id=34,
        price=1,
        volume=1,
        fetched_at=old,
    )
    # Регион Jita — в get_sync_region_ids, пока локация Jita включена → не удаляется.
    MarketOrderCache.objects.create(
        source_kind=SourceKind.REGION,
        source_ref=settings_row.jita_region_id,
        location_id=60003760,
        solar_system_id=30000142,
        region_id=settings_row.jita_region_id,
        type_id=34,
        price=1,
        volume=1,
        fetched_at=old,
    )
    result = tasks.cleanup_orphan_cache(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert MarketOrderCache.objects.filter(source_ref=10000001).exists() is False
    assert MarketOrderCache.objects.filter(source_ref=settings_row.jita_region_id).exists() is True


@pytest.mark.django_db
def test_cleanup_orphan_cache_deletes_stale_stock_level(settings_row):
    """cleanup_orphan_cache удаляет StockLevel отключённой/удалённой локации (§12.1)."""
    now = timezone.now()
    old = now - timedelta(hours=25)
    # Отключённая локация: её устаревший StockLevel удаляется.
    MarketLocation.objects.create(
        name="Disabled Station",
        station_id=60000001,
        solar_system_id=30000142,
        region_id=10000002,
        enabled=False,
    )
    StockLevel.objects.create(
        location_id=60000001,
        solar_system_id=30000142,
        region_id=10000002,
        type_id=34,
        total_sell_volume=5,
        best_sell=Decimal("10"),
        updated_at=old,
    )
    # Активная станция: свежий StockLevel не затрагивается.
    MarketLocation.objects.create(
        name="Jita",
        station_id=60003760,
        solar_system_id=30000142,
        region_id=10000002,
        enabled=True,
    )
    StockLevel.objects.create(
        location_id=60003760,
        solar_system_id=30000142,
        region_id=10000002,
        type_id=34,
        total_sell_volume=5,
        best_sell=Decimal("10"),
        updated_at=now,
    )
    result = tasks.cleanup_orphan_cache(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert not StockLevel.objects.filter(location_id=60000001).exists()
    assert StockLevel.objects.filter(location_id=60003760).exists()


@pytest.mark.django_db
def test_verify_providers_bulk_update(settings_row):
    """verify_providers: обновляет last_verified_at у включённых провайдеров (§11)."""
    from allianceauth.eveonline.models import EveCharacter

    char = EveCharacter.objects.create(
        character_id=99000001,
        character_name="Provider",
        corporation_id=98000001,
        corporation_name="Corp",
        corporation_ticker="CRP",
    )
    from ..models import StructureProvider

    StructureProvider.objects.create(character=char, priority=10)
    with mock.patch.object(esi_client, "fetch_structure_info") as info:
        info.return_value = {"name": "Structure"}
        result = tasks.verify_providers(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True and result["verified"] == 1
    provider = StructureProvider.objects.get(character=char)
    assert provider.last_verified_at is not None
    run = SyncRun.objects.filter(task_name="marketmonitoring.verify_providers").latest("started_at")
    assert run.status == SyncRunStatus.OK
