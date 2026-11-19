"""Тесты этапа 3: ESI-клиент, синк задач, перезапись кэша, guard-интервалы, SyncRun (§18).

ESI полностью замокан на уровне marketmonitoring.esi.client — сеть не используется.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from .. import tasks
from ..constants import SourceKind, SyncRunStatus, TriggerKind
from ..esi import client as esi_client
from ..models import (
    JitaOrderLevel,
    MarketLocation,
    MarketOrderCache,
    PluginSetting,
    StockLevel,
    SyncRun,
)


def _order(type_id=34, price=5.5, volume=100, location_id=60003760, system_id=30000142, region_id=10000002):
    """Сырой sell-заказ в формате ESI."""
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
def test_sync_jita_book_writes_cache_and_levels(settings_row):
    """sync_jita_book: запись кэша по источнику + JitaOrderLevel + SyncRun ok."""
    with mock.patch.object(esi_client, "fetch_jita_orders") as fetch:
        fetch.return_value = (
            [_order(type_id=34, price=10.0, volume=50), _order(type_id=34, price=9.0, volume=20)],
            False,
        )
        result = tasks.sync_jita_book(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert MarketOrderCache.objects.filter(source_kind=SourceKind.REGION, source_ref=10000002).count() == 2
    levels = JitaOrderLevel.objects.filter(type_id=34).order_by("rank")
    assert levels.count() == 2
    assert levels.first().rank == 1 and levels.first().price == 9.0
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_jita_book").latest("started_at")
    assert run.status == SyncRunStatus.OK
    assert run.trigger == TriggerKind.MANUAL
    assert run.finished_at is not None


@pytest.mark.django_db
def test_sync_jita_book_guard_interval(settings_row):
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
def test_sync_jita_book_error_recorded(settings_row):
    """Сбой ESI фиксируется в SyncRun со статусом failed."""
    with mock.patch.object(esi_client, "fetch_jita_orders", side_effect=RuntimeError("boom")):
        result = tasks.sync_jita_book(trigger=TriggerKind.MANUAL)
    assert result["ok"] is False
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_jita_book").latest("started_at")
    assert run.status == SyncRunStatus.FAILED
    assert "boom" in run.errors


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
def test_sync_chain_manual_does_not_notify(settings_row):
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
def test_sync_chain_beat_queues_notify(settings_row):
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
def test_fetch_jita_orders_batch_halving(settings_row):
    """При 400 из-за размера батча — уменьшение вдвое и повтор (§9.2).

    Мок со спекой: проверяется page_size=1000, обход страниц до пустой и порядок батчей.
    """
    from esi.exceptions import HTTPClientError

    calls: list[dict] = []
    pages_seen: list[int] = []

    class FakeOp:
        """Мок операции GetMarketsRegionIdOrders со спекой вызова и result()."""

        def __call__(self, **kwargs):
            calls.append(kwargs)
            pages_seen.append(kwargs.get("page", 1))
            if len(kwargs["type_id"]) > 1:
                raise HTTPClientError(status_code=400, headers={}, data=None)
            return self

        def result(self, **kwargs):
            # Ровно одна непустая страница на вызов (page=1), вторая страница — пустая.
            if pages_seen[-1] >= 2:
                return ([], self)
            return ([_order(type_id=calls[-1]["type_id"][0])], self)

    fake_client = mock.Mock()
    fake_client.client.Market.GetMarketsRegionIdOrders = FakeOp()
    with (
        mock.patch.object(esi_client, "esi", fake_client),
        mock.patch.object(esi_client, "_check_error_limit"),
    ):
        orders, partial = esi_client.fetch_jita_orders([1, 2])
    assert partial is False
    assert len(orders) == 2  # по одному заказу на каждый тип после уменьшения батча
    # Все запросы — с page_size=1000 (§9.1).
    assert all(call.get("page_size") == 1000 for call in calls)
    # Первый вызов — исходный батч, после 400 идут одиночные батчи.
    assert len(calls[0]["type_id"]) == 2
    assert all(len(call["type_id"]) == 1 for call in calls[1:])
    # Страницы обходятся до пустой: для каждого одиночного батча есть page=2.
    assert pages_seen.count(2) >= 2


@pytest.mark.django_db
def test_fetch_jita_orders_rate_limit_stop_marks_partial(settings_row):
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
def test_cleanup_orphan_cache(settings_row):
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


@pytest.mark.django_db
def test_sync_region_orders_triggers_recalc_and_stock_level(settings_row):
    """Цепочка §12.3: после sync_region_orders recalc_stock_levels получает location_ids=None
    и StockLevel реально создаётся по локациям из свежего кэша."""
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
    # Регион, не входящий в get_sync_region_ids (Jita region 10000002 всегда нужен) → удаляется.
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
    # Регион Jita — в get_sync_region_ids всегда → не удаляется.
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
