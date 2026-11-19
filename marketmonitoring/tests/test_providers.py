"""Тесты этапа 6: пул провайдеров структур (§11, §18.5).

ESI полностью замокан на уровне marketmonitoring.esi.client — сеть не используется.
Проверяются: выбор по приоритету, cooldown 60 мин при 403 и переход к следующему
провайдеру в том же прогоне, отключение после 3 сбоев с алертом, сброс счётчика
при успехе, связка no_provider → sync_status → NO_DATA у фита, verify_providers.
"""

import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from allianceauth.eveonline.models import EveCharacter
from django.utils import timezone
from esi.exceptions import HTTPClientError

from .. import tasks
from ..calc import service as calc_service
from ..constants import (
    FitStatus,
    LocationKind,
    ProviderStatus,
    SourceKind,
    SyncRunStatus,
    SyncStatus,
    TriggerKind,
)
from ..esi import client as esi_client
from ..models import (
    Fit,
    FitComponent,
    MarketLocation,
    MarketOrderCache,
    PluginSetting,
    StructureProvider,
    SyncRun,
)
from ..providers import service as provider_service


def _order(type_id=34, price=5.5, volume=100, location_id=10221127, system_id=30000142, region_id=10000002):
    """Сырой sell-заказ структуры в формате ESI."""
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


def _http_403() -> HTTPClientError:
    """HTTPClientError с кодом 403 (ACL-сбой провайдера, §11)."""
    return HTTPClientError(status_code=403, headers={}, data=None)


def _raise_403(*args, **kwargs):
    """Сайд-эффект: выбрасывает HTTPClientError 403."""
    raise _http_403()


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


@pytest.fixture
def structure_location(db):
    """Включённая структура-локация с временным именем-плейсхолдером."""
    return MarketLocation.objects.create(
        name="Structure 10221127",
        kind=LocationKind.STRUCTURE,
        structure_id=10221127,
        solar_system_id=30000142,
        region_id=10000002,
        enabled=True,
    )


def _provider(character_id: int, priority: int = 100, **kwargs) -> StructureProvider:
    """Создание провайдера с персонажем AA (токены — из AA, плагин их не хранит)."""
    char, _ = EveCharacter.objects.get_or_create(
        character_id=character_id,
        defaults={
            "character_name": f"Provider {character_id}",
            "corporation_id": 98000001,
            "corporation_name": "Corp",
            "corporation_ticker": "CRP",
        },
    )
    return StructureProvider.objects.create(character=char, priority=priority, **kwargs)


@pytest.mark.django_db
def test_select_provider_by_priority(settings_row):
    """Выбор по приоритету (§11): меньше priority = выше; при равенстве — по character_id."""
    provider_high = _provider(99000001, priority=10)
    _provider(99000002, priority=50)
    assert provider_service.select_provider(10221127) == provider_high
    # Одинаковый приоритет → меньший character_id первым.
    provider_high.priority = 50
    provider_high.save(update_fields=["priority"])
    ordered = list(provider_service._available_providers())
    # ordering по FK character_id (CCP character id); меньший CCP id первым.
    assert [p.character.character_id for p in ordered] == [99000001, 99000002]


@pytest.mark.django_db
def test_select_provider_excludes_disabled_and_cooldown(settings_row):
    """Исключаются disabled и провайдеры в активном cooldown (§11); истёкший cooldown доступен."""
    cooling = _provider(
        99000001,
        priority=1,
        status=ProviderStatus.COOLING_DOWN,
        cooldown_until=timezone.now() + timedelta(minutes=30),
    )
    assert provider_service.select_provider(10221127) is None
    # Истёкший cooldown снова доступен (cooldown_until <= now, §11).
    cooling.cooldown_until = timezone.now() - timedelta(minutes=1)
    cooling.save(update_fields=["cooldown_until"])
    assert provider_service.select_provider(10221127) == cooling
    # disabled не выбирается никогда.
    cooling.status = ProviderStatus.DISABLED
    cooling.cooldown_until = None
    cooling.save(update_fields=["status", "cooldown_until"])
    assert provider_service.select_provider(10221127) is None


@pytest.mark.django_db
def test_acl_failure_sets_cooldown_and_next_provider_in_same_run(settings_row, structure_location):
    """403 → acl_fail_count=1, cooldown на 60 мин, структура синкается следующим провайдером (§11)."""
    first = _provider(99000001, priority=1)
    second = _provider(99000002, priority=2)
    with mock.patch.object(esi_client, "fetch_structure_orders") as fetch:
        fetch.side_effect = [_http_403(), ([_order(volume=77)], False)]
        result = tasks.sync_structure_orders(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    first.refresh_from_db()
    second.refresh_from_db()
    # Первый провайдер: счётчик 1, cooling_down, cooldown ≈ 60 мин.
    assert first.acl_fail_count == 1
    assert first.status == ProviderStatus.COOLING_DOWN
    assert first.cooldown_until is not None
    delta = first.cooldown_until - timezone.now()
    assert timedelta(minutes=55) < delta <= timedelta(minutes=60)
    # Второй провайдер успешно синканул структуру.
    assert second.status == ProviderStatus.OK
    assert second.acl_fail_count == 0
    structure_location.refresh_from_db()
    assert structure_location.sync_status == SyncStatus.OK
    assert MarketOrderCache.objects.filter(source_kind=SourceKind.STRUCTURE, source_ref=10221127, volume=77).exists()
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_structure_orders").latest("started_at")
    assert "ACL 403" in run.errors


@pytest.mark.django_db
def test_three_failures_disable_provider_and_alert_hook(settings_row):
    """3 сбоя подряд → status=disabled и алерт provider_acl_lost с пометкой «отключён» (§11.3)."""
    provider = _provider(99000001)
    calls = []

    def fake_hook(*args, **kwargs):
        calls.append(kwargs)

    with mock.patch.object(provider_service, "_notify_acl_lost", side_effect=fake_hook):
        for _ in range(settings_row.provider_acl_max_failures - 1):
            provider_service.handle_acl_failure(provider)
        provider.refresh_from_db()
        assert provider.status == ProviderStatus.COOLING_DOWN
        provider_service.handle_acl_failure(provider)
    provider.refresh_from_db()
    assert provider.status == ProviderStatus.DISABLED
    assert provider.acl_fail_count == settings_row.provider_acl_max_failures
    assert len(calls) == settings_row.provider_acl_max_failures
    # Последний вызов — с пометкой «отключён».
    assert calls[-1]["disabled"] is True


@pytest.mark.django_db
def test_acl_alert_hook_guarded_import(settings_row):
    """Точка вызова алерта защищена guarded import (этап 7): функция вызывается, если есть."""
    provider = _provider(99000001)
    sent = []

    fake_module = SimpleNamespace(send_provider_acl_lost=lambda **kwargs: sent.append(kwargs))
    with mock.patch.dict(sys.modules, {"marketmonitoring.notify.discord": fake_module}):
        provider_service.handle_acl_failure(provider)
    assert len(sent) == 1
    assert sent[0]["character_id"] == provider.character_id
    assert sent[0]["event"] == "provider_acl_lost"
    # Без функции в модуле — молча пропускается.
    empty_module = SimpleNamespace()
    with mock.patch.dict(sys.modules, {"marketmonitoring.notify.discord": empty_module}):
        provider_service.handle_acl_failure(provider)
    assert len(sent) == 1


@pytest.mark.django_db
def test_success_resets_fail_count(settings_row):
    """Успешный запрос (200) сбрасывает acl_fail_count=0 и status=ok (§11.4)."""
    provider = _provider(
        99000001,
        status=ProviderStatus.COOLING_DOWN,
        acl_fail_count=2,
        cooldown_until=timezone.now() + timedelta(minutes=10),
    )
    provider_service.handle_provider_success(provider)
    provider.refresh_from_db()
    assert provider.acl_fail_count == 0
    assert provider.status == ProviderStatus.OK
    assert provider.cooldown_until is None


@pytest.mark.django_db
def test_no_provider_fit_no_data(settings_row, structure_location):
    """Нет провайдеров → sync_status=no_provider (§11.6), фит получает NO_DATA через calc (§6.7)."""
    fit = Fit.objects.create(
        name="Test fit",
        hull_type_id=34,
        qty_target=30,
        scope_kind="station",
        scope_ref=structure_location.structure_id,
        status_cache=FitStatus.OK,  # преднамеренно «свежий» статус до пересчёта
    )
    FitComponent.objects.create(fit=fit, type_id=34, qty_per_fit=1)
    result = tasks.sync_structure_orders(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    structure_location.refresh_from_db()
    assert structure_location.sync_status == SyncStatus.NO_PROVIDER
    # Цепочка пересчёта выполняется eager: фит — NO_DATA (§6.7: структура sync_status != ok).
    fit.refresh_from_db()
    assert fit.status_cache == FitStatus.NO_DATA
    assert fit.fits_available is None
    assert fit.total_cost is None
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_structure_orders").latest("started_at")
    assert "no_provider" in run.errors
    # Прямой пересчёт тоже даёт NO_DATA с причиной no_provider.
    reasons = calc_service.recalc_fit(fit).reasons
    assert any("no_provider" in reason for reason in reasons)


@pytest.mark.django_db
def test_sync_structure_success_updates_name(settings_row, structure_location):
    """Успешный синк: временное имя «Structure <id>» заменяется именем из fetch_structure_info."""
    _provider(99000001)
    with (
        mock.patch.object(esi_client, "fetch_structure_orders") as fetch,
        mock.patch.object(esi_client, "fetch_structure_info") as info,
    ):
        fetch.return_value = ([_order(volume=10)], False)
        info.return_value = {"name": "Fortizar Alpha"}
        result = tasks.sync_structure_orders(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    structure_location.refresh_from_db()
    assert structure_location.name == "Fortizar Alpha"
    assert structure_location.last_synced_at is not None


@pytest.mark.django_db
def test_verify_providers_updates_last_verified(settings_row, structure_location):
    """verify_providers: обновляет last_verified_at, не трогает счётчик без 403 (§11)."""
    provider = _provider(99000001)
    with mock.patch.object(esi_client, "fetch_structure_info") as info:
        info.return_value = {"name": "Fortizar Alpha"}
        result = tasks.verify_providers(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    provider.refresh_from_db()
    assert provider.last_verified_at is not None
    assert provider.acl_fail_count == 0
    assert provider.status == ProviderStatus.OK


@pytest.mark.django_db
def test_verify_providers_403_counts_acl_failure(settings_row, structure_location):
    """verify_providers при фактическом 403 применяет обработку §11 (счётчик/cooldown)."""
    provider = _provider(99000001)
    with mock.patch.object(esi_client, "fetch_structure_info") as info:
        info.side_effect = _http_403()
        tasks.verify_providers(trigger=TriggerKind.MANUAL)
    provider.refresh_from_db()
    assert provider.acl_fail_count == 1
    assert provider.status == ProviderStatus.COOLING_DOWN
    run = SyncRun.objects.filter(task_name="marketmonitoring.verify_providers").latest("started_at")
    assert run.status == SyncRunStatus.OK


@pytest.mark.django_db
def test_verify_providers_404_does_not_count_acl_failure(settings_row, structure_location):
    """verify_providers: 404 (структура исчезла) не инкрементирует acl_fail_count (§11).

    Счётчик и статус не меняются, last_verified_at обновляется, задача завершается OK.
    """
    provider = _provider(99000001)
    with mock.patch.object(esi_client, "fetch_structure_info") as info:
        info.side_effect = HTTPClientError(status_code=404, headers={}, data=None)
        result = tasks.verify_providers(trigger=TriggerKind.MANUAL)
    assert result["ok"] is True
    provider.refresh_from_db()
    assert provider.acl_fail_count == 0
    assert provider.status == ProviderStatus.OK
    assert provider.cooldown_until is None
    assert provider.last_verified_at is not None
    run = SyncRun.objects.filter(task_name="marketmonitoring.verify_providers").latest("started_at")
    assert run.status == SyncRunStatus.OK


@pytest.mark.django_db
def test_verify_providers_guard_interval(settings_row):
    """Guard 60 мин: запуск раньше интервала пропускается (§12.2)."""
    tasks._start_run("marketmonitoring.verify_providers", TriggerKind.BEAT)
    with mock.patch.object(esi_client, "fetch_structure_info") as info:
        result = tasks.verify_providers(trigger=TriggerKind.BEAT)
        assert result.get("skipped") is True
        info.assert_not_called()


@pytest.mark.django_db
def test_cooldown_expired_helper(settings_row):
    """Хелпер cooldown_expired (§11): не в cooldown — True; активный cooldown — False."""
    provider = _provider(99000001)
    assert provider_service.cooldown_expired(provider) is True
    provider.status = ProviderStatus.COOLING_DOWN
    provider.cooldown_until = timezone.now() + timedelta(minutes=5)
    assert provider_service.cooldown_expired(provider) is False
    provider.cooldown_until = timezone.now() - timedelta(minutes=5)
    assert provider_service.cooldown_expired(provider) is True


@pytest.mark.django_db
def test_acl_error_recorded_in_syncrun(settings_row, structure_location):
    """Ошибки ACL фиксируются в errors SyncRun, задача не прерывается (§9.5)."""
    _provider(99000001, priority=1)
    _provider(99000002, priority=2)
    with mock.patch.object(esi_client, "fetch_structure_orders") as fetch:
        fetch.side_effect = [_http_403(), ([_order(volume=5)], False)]
        tasks.sync_structure_orders(trigger=TriggerKind.MANUAL)
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_structure_orders").latest("started_at")
    assert run.status == SyncRunStatus.OK
    assert "ACL 403" in run.errors
