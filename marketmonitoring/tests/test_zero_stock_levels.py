"""Тесты нулевых агрегатов и уведомлений при отсутствии данных (§19.21, §19.22).

ESI и Discord замоканы; Celery работает в eager-режиме через фикстуру из
``test_tasks``. Проверяется буквальное прочтение решений №45/№46: вклад
несинканной локации равен нулю, ``NO_DATA`` остаётся только для пустой области,
а нулевые ``StockLevel`` пишутся только при успешном полном синке.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from allianceauth.eveonline.models import EveCharacter
from django.utils import timezone

from .. import tasks
from ..calc.service import _freshness_details, get_availability, recalc_fit
from ..constants import (
    ContractSourceKind,
    FitStatus,
    LocationKind,
    ScopeKind,
    SyncStatus,
    TriggerKind,
)
from ..models import (
    ContractProvider,
    Fit,
    FitComponent,
    JitaOrderLevel,
    MarketLocation,
    NotificationLog,
    StockLevel,
)
from .test_calc_engine import sde_ids  # noqa: F401 — SDE: система, станции, типы 1/2
from .test_tasks import _eager_celery, _http_ok, webhook_row  # noqa: F401 — eager Celery, webhook, HTTP-мок

pytestmark = pytest.mark.django_db

STATION_ID = 60003760
OTHER_STATION_ID = 60003761
STRUCTURE_ID = 10221127
SYSTEM_ID = 30000142
REGION_ID = 10000002


def _seed_fit(hull_type_id: int = 1) -> Fit:
    """Фит с типами 1/2: задаёт набор типов для нулевых StockLevel."""
    fit = Fit.objects.create(
        name="Zero aggregates fit",
        hull_type_id=hull_type_id,
        qty_target=2,
        scope_kind=ScopeKind.STATION,
        scope_ref=STATION_ID,
    )
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    FitComponent.objects.create(fit=fit, type_id=2, qty_per_fit=1)
    return fit


def _seed_stations() -> None:
    """Две включённые NPC-станции одного региона."""
    now = timezone.now()
    MarketLocation.objects.create(
        name="Jita IV-4",
        kind=LocationKind.STATION,
        station_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        enabled=True,
        sync_status=SyncStatus.OK,
        last_synced_at=now,
    )
    MarketLocation.objects.create(
        name="Other station",
        kind=LocationKind.STATION,
        station_id=OTHER_STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        enabled=True,
        sync_status=SyncStatus.OK,
        last_synced_at=now,
    )


def _stock(
    location_id: int, type_id: int, volume: int, updated_at=None, best_sell: Decimal | None = None
) -> StockLevel:
    """Строка StockLevel с координатами тестового региона."""
    return StockLevel.objects.create(
        location_id=location_id,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=type_id,
        total_sell_volume=volume,
        best_sell=best_sell,
        updated_at=updated_at or timezone.now(),
    )


def test_successful_empty_region_sync_writes_zero_stock_levels(sde_ids):  # noqa: F811
    """Полный региональный синк без заказов пишет нулевые агрегаты (§19.22)."""
    _seed_fit()
    _seed_stations()
    _stock(STATION_ID, 2, 7, best_sell=Decimal("500"))
    _stock(STATION_ID, 3, 3)  # тип вне расчёта — удаляется полной заменой
    before = timezone.now()

    with mock.patch.object(tasks.esi_client, "fetch_region_orders", return_value=([], False)) as fetch:
        result = tasks.sync_region_orders(trigger=TriggerKind.MANUAL, region_id=REGION_ID)
    fetch.assert_called_once_with(REGION_ID)
    assert result == {"ok": True, "regions_ok": 1, "regions_failed": 0}

    for location_id in (STATION_ID, OTHER_STATION_ID):
        for type_id in (1, 2):
            level = StockLevel.objects.get(location_id=location_id, type_id=type_id)
            assert level.total_sell_volume == 0
            assert level.best_sell is None
            assert level.updated_at >= before
    assert not StockLevel.objects.filter(type_id=3).exists()
    assert StockLevel.objects.count() == 4

    # Нулевой агрегат — это данные: не вызывает появление предупреждений и NO_DATA (§19.22).
    fit = Fit.objects.get(name="Zero aggregates fit")
    result = recalc_fit(fit)
    assert result.status == FitStatus.HULL_MISSING  # A_hull=0, но область не пуста
    assert result.k == 0
    assert result.reasons == []

    # Повторный синк не создаёт дубли (§12.5).
    with mock.patch.object(tasks.esi_client, "fetch_region_orders", return_value=([], False)):
        tasks.sync_region_orders(trigger=TriggerKind.MANUAL, region_id=REGION_ID)
    assert StockLevel.objects.count() == 4


def test_partial_region_sync_does_not_write_zero_stock_levels(sde_ids):  # noqa: F811
    """Partial-синк пересчитывает локации, но не пишет нули (§19.22, решение №46)."""
    _seed_fit()
    _seed_stations()
    _stock(STATION_ID, 2, 7, best_sell=Decimal("500"))

    with mock.patch.object(tasks.esi_client, "fetch_region_orders", return_value=([], True)):
        result = tasks.sync_region_orders(trigger=TriggerKind.MANUAL, region_id=REGION_ID)
    assert result["ok"] is True
    assert not StockLevel.objects.filter(location_id__in=(STATION_ID, OTHER_STATION_ID)).exists()
    assert MarketLocation.objects.get(station_id=STATION_ID).sync_status == SyncStatus.ERROR

    fit = Fit.objects.get(name="Zero aggregates fit")
    result = recalc_fit(fit)
    assert result.status == FitStatus.HULL_MISSING  # данные пропали, но область не пуста
    assert result.k == 0
    assert any(str(STATION_ID) in reason for reason in result.reasons)


def test_successful_empty_structure_sync_writes_zero_stock_levels(sde_ids):  # noqa: F811
    """Успешный синк структуры без заказов пишет нули; partial их удаляет (§19.22)."""
    _seed_fit()
    MarketLocation.objects.create(
        name=f"Structure {STRUCTURE_ID}",
        kind=LocationKind.STRUCTURE,
        structure_id=STRUCTURE_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        enabled=True,
    )
    _stock(STRUCTURE_ID, 2, 7, best_sell=Decimal("500"))
    provider = SimpleNamespace(character=SimpleNamespace(character_id=99000001))

    with (
        mock.patch("marketmonitoring.providers.service.switch_to_next_provider", return_value=provider) as select,
        mock.patch.object(tasks.esi_client, "fetch_structure_orders", return_value=([], False)) as fetch,
        mock.patch("marketmonitoring.providers.service.handle_provider_success") as success,
        mock.patch.object(tasks, "_update_structure_name"),
    ):
        result = tasks.sync_structure_orders(trigger=TriggerKind.MANUAL)
    assert result == {"ok": True, "locations_ok": 1, "locations_failed": 0}
    select.assert_called_once_with(STRUCTURE_ID, set())
    fetch.assert_called_once_with(STRUCTURE_ID, 99000001)
    success.assert_called_once_with(provider)
    assert StockLevel.objects.filter(location_id=STRUCTURE_ID).count() == 2
    for type_id in (1, 2):
        level = StockLevel.objects.get(location_id=STRUCTURE_ID, type_id=type_id)
        assert level.total_sell_volume == 0
        assert level.best_sell is None

    with (
        mock.patch("marketmonitoring.providers.service.switch_to_next_provider", return_value=provider),
        mock.patch.object(tasks.esi_client, "fetch_structure_orders", return_value=([], True)),
        mock.patch("marketmonitoring.providers.service.handle_provider_success"),
        mock.patch.object(tasks, "_update_structure_name"),
    ):
        tasks.sync_structure_orders(trigger=TriggerKind.MANUAL)
    assert not StockLevel.objects.filter(location_id=STRUCTURE_ID).exists()
    assert MarketLocation.objects.get(structure_id=STRUCTURE_ID).sync_status == SyncStatus.ERROR

    fit = Fit.objects.create(
        name="Partial structure fit",
        hull_type_id=1,
        qty_target=2,
        scope_kind=ScopeKind.STATION,
        scope_ref=STRUCTURE_ID,
    )
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    result = recalc_fit(fit)
    assert result.status == FitStatus.HULL_MISSING
    assert result.k == 0
    assert any(str(STRUCTURE_ID) in reason for reason in result.reasons)


def test_jita_partial_sync_does_not_write_zero_stock_levels(sde_ids):  # noqa: F811
    """Jita: partial не должен неявно получить zero_location_ids=None (§19.22)."""
    _seed_fit()
    _seed_stations()
    _stock(STATION_ID, 2, 7, best_sell=Decimal("500"))

    with mock.patch.object(tasks.esi_client, "fetch_jita_orders", return_value=([], True)) as fetch:
        result = tasks.sync_jita_book(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    assert result["partial"] is True
    fetch.assert_called_once()
    assert not StockLevel.objects.filter(location_id=STATION_ID).exists()


def test_chain_recalc_passes_zero_ids_explicitly(db):
    """Цепочка явно передаёт zero_location_ids; manual не отправляет уведомления (§12.3)."""
    with (
        mock.patch.object(tasks.recalc_stock_levels, "delay") as recalc,
        mock.patch.object(tasks.notify_deficit, "delay") as notify,
    ):
        tasks._chain_recalc_and_notify(TriggerKind.MANUAL, [1, 2], lock_token="tok-1", zero_location_ids=[2])
        recalc.assert_called_once_with(
            trigger=TriggerKind.MANUAL, location_ids=[1, 2], lock_token="tok-1", zero_location_ids=[2]
        )
        notify.assert_not_called()

        tasks._chain_recalc_and_notify(TriggerKind.BEAT, [1], zero_location_ids=[1])
        recalc.assert_called_with(trigger=TriggerKind.BEAT, location_ids=[1], lock_token=None, zero_location_ids=[1])
        notify.assert_called_once_with(trigger=TriggerKind.BEAT)


def test_deficit_notifications_not_suppressed_by_unsynced_location(sde_ids, webhook_row):  # noqa: F811
    """Несинканная локация даёт A=0, но дефицит и ships_deficit всё равно уходят (§19.21)."""
    fit = Fit.objects.create(
        name="Mixed data fit",
        hull_type_id=1,
        qty_target=2,
        scope_kind=ScopeKind.REGION,
        scope_ref=REGION_ID,
    )
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    FitComponent.objects.create(fit=fit, type_id=2, qty_per_fit=1)
    now = timezone.now()
    _seed_stations()
    MarketLocation.objects.filter(station_id=OTHER_STATION_ID).update(sync_status=SyncStatus.ERROR)
    _stock(STATION_ID, 1, 2, best_sell=Decimal("1000"))
    _stock(OTHER_STATION_ID, 2, 10, updated_at=now - timedelta(days=2), best_sell=Decimal("10"))
    JitaOrderLevel.objects.create(type_id=1, rank=1, price=Decimal("1000"), volume=100, fetched_at=now)
    JitaOrderLevel.objects.create(type_id=2, rank=1, price=Decimal("100"), volume=100, fetched_at=now)
    character = EveCharacter.objects.create(
        character_id=9101,
        character_name="Mixed Pilot",
        corporation_id=9102,
        corporation_name="Mixed Corp",
        corporation_ticker="MIX",
    )
    ContractProvider.objects.create(
        character=character,
        source_kind=ContractSourceKind.CORPORATION,
        enabled=True,
        priority=10,
        sync_status=SyncStatus.OK,
        last_synced_at=now,
    )

    result = recalc_fit(fit)
    assert result.status == FitStatus.DEFICIT
    assert result.k == 0
    assert any(str(OTHER_STATION_ID) in reason for reason in result.reasons)

    with mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok) as post:
        notify_result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
    assert notify_result["ok"] is True
    assert post.call_count >= 2
    assert NotificationLog.objects.filter(event="deficit", ok=True).exists()
    assert NotificationLog.objects.filter(event="ships_deficit", ok=True).exists()


def test_get_availability_uses_fresh_locations_and_scope_flag(sde_ids):  # noqa: F811
    """Прямая фиксация решения №45: несинканная локация не даёт объём и не NO_DATA.

    Возврат к старой семантике ``get_availability`` (игнорировать
    ``fresh_locations`` или считать ``has_data`` по всем локациям) ломает этот
    тест: A стало бы 60 или второй флаг стал бы False (§6.2, §6.7).
    """
    now = timezone.now()
    _seed_stations()
    _stock(STATION_ID, 1, 10)
    _stock(OTHER_STATION_ID, 1, 50, updated_at=now - timedelta(days=2))

    fresh_ids, reasons = _freshness_details({STATION_ID, OTHER_STATION_ID}, 180)
    assert fresh_ids == {STATION_ID}
    assert any(str(OTHER_STATION_ID) in reason for reason in reasons)

    availability, scope_resolved = get_availability(
        1,
        ScopeKind.SYSTEM,
        SYSTEM_ID,
        location_ids={STATION_ID, OTHER_STATION_ID},
        fresh_locations=fresh_ids,
    )
    assert scope_resolved is True
    assert availability == 10

    # Отсутствие агрегата по локации тоже не меняет «область резолвится»: A=10,
    # а не NO_DATA — старый has_data вернул бы False (§19.21).
    StockLevel.objects.filter(location_id=OTHER_STATION_ID).delete()
    availability, scope_resolved = get_availability(
        1,
        ScopeKind.SYSTEM,
        SYSTEM_ID,
        location_ids={STATION_ID, OTHER_STATION_ID},
        fresh_locations={STATION_ID, OTHER_STATION_ID},
    )
    assert scope_resolved is True
    assert availability == 10


def test_stale_structure_contributes_zero_without_no_data(sde_ids):  # noqa: F811
    """Протухший синк структуры даёт вклад 0; фит остаётся OK по двум станциям (§19.21)."""
    fit = Fit.objects.create(
        name="Stale structure fit",
        hull_type_id=1,
        qty_target=5,
        scope_kind=ScopeKind.SYSTEM,
        scope_ref=SYSTEM_ID,
    )
    FitComponent.objects.create(fit=fit, type_id=1, qty_per_fit=1)
    _seed_stations()
    _stock(STATION_ID, 1, 10)
    _stock(OTHER_STATION_ID, 1, 10)
    MarketLocation.objects.create(
        name=f"Structure {STRUCTURE_ID}",
        kind=LocationKind.STRUCTURE,
        structure_id=STRUCTURE_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        enabled=True,
        sync_status=SyncStatus.OK,
        last_synced_at=timezone.now() - timedelta(days=2),
    )
    _stock(STRUCTURE_ID, 1, 50)

    result = recalc_fit(fit)
    fit.refresh_from_db()
    assert result.status == FitStatus.OK
    assert result.k == 20  # 50-unit объём протухшей структуры исключён
    assert fit.status_cache == FitStatus.OK
    assert any(f"structure {STRUCTURE_ID}" in reason and "stale" in reason for reason in result.reasons)
