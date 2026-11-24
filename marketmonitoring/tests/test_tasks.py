"""Тесты этапа 7: уведомления и дедупликация (§13, §13.1, §18.8).

Discord полностью замокан на уровне HTTP (requests.post) — сеть не используется.
Проверяются: дедупликация deficit (state_hash + cooldown, manual никогда),
переходы sync_failed / sync_recovered, provider_acl_lost без cooldown, но
только при переходе состояния.
"""

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from allianceauth.eveonline.models import EveCharacter
from django.utils import timezone

from .. import tasks
from ..calc.service import recalc_fit
from ..constants import (
    ContractSourceKind,
    LocationKind,
    SyncRunStatus,
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
    PluginSetting,
    StockLevel,
    SyncRun,
    WebhookConfig,
)
from ..notify import discord as discord_notify
from .test_eft_parser import eve_types  # noqa: F401 — фикстура EveType записей

pytestmark = pytest.mark.django_db

STATION_ID = 60003760
SYSTEM_ID = 30000142
REGION_ID = 10000002


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
def webhook_row(db):
    """Активный Discord webhook (§8)."""
    return WebhookConfig.objects.create(url="https://discord.example/webhook/id", enabled=True)


def _http_ok(*args, **kwargs):
    """Мок requests.post: 204 No Content (Discord-успех)."""
    return SimpleNamespace(status_code=204, text="")


def _seed_deficit(db):
    """Фит с дефицитом и свежий станционный кэш (§6): DC II — 1 из 2."""
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
    StockLevel.objects.create(
        location_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=2046,
        total_sell_volume=1,
        best_sell=Decimal("100"),
        updated_at=now,
    )
    StockLevel.objects.create(
        location_id=STATION_ID,
        solar_system_id=SYSTEM_ID,
        region_id=REGION_ID,
        type_id=643,
        total_sell_volume=2,
        best_sell=Decimal("10000000"),
        updated_at=now,
    )
    JitaOrderLevel.objects.create(type_id=2046, rank=1, price=Decimal("100"), volume=1000, fetched_at=now)
    fit = Fit.objects.create(
        name="Notify fit",
        hull_type_id=643,
        eft_raw="[Ferox, Notify fit]\nDamage Control II",
        qty_target=2,
        scope_kind="station",
        scope_ref=STATION_ID,
    )
    FitComponent.objects.create(fit=fit, type_id=643, qty_per_fit=1)
    FitComponent.objects.create(fit=fit, type_id=2046, qty_per_fit=1)
    return fit


def test_notify_deficit_manual_never_sends(eve_types, webhook_row, db):  # noqa: F811
    """manual-триггер не порождает уведомлений о дефиците (§12.4)."""
    _seed_deficit(db)
    with mock.patch("marketmonitoring.notify.discord.requests.post") as post:
        result = tasks.notify_deficit(trigger=TriggerKind.MANUAL)
    assert result.get("skipped") is True
    post.assert_not_called()
    assert NotificationLog.objects.count() == 0


def test_notify_deficit_skips_zero_qty_per_fit(eve_types, webhook_row, db):  # noqa: F811
    """q_i = 0: фит пропускается с ошибкой в лог, деления на ноль нет (§6.3)."""
    fit = _seed_deficit(db)
    FitComponent.objects.create(fit=fit, type_id=34, qty_per_fit=0)
    with mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok) as post:
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
    assert result["ok"] is True
    assert result["invalid"] == 1
    # Фит пропущен — уведомление по нему не отправлено.
    assert NotificationLog.objects.count() == 0
    post.assert_not_called()


def test_notify_deficit_sends_and_dedupes_same_state(eve_types, webhook_row, db):  # noqa: F811
    """beat: дефицит отправлен; повторный прогон с тем же state_hash и
    не истёкшим cooldown — не отправлен (§13.1)."""
    _seed_deficit(db)
    with mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok) as post:
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
        assert result["sent"] == 1
        first_post_count = post.call_count
        assert first_post_count >= 1
        log = NotificationLog.objects.filter(event="deficit", ok=True).latest("sent_at")
        assert len(log.state_hash) == 64
        # Второй прогон: состояние не изменилось, cooldown не истёк.
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
        assert result["sent"] == 0
        assert post.call_count == first_post_count


def test_notify_deficit_without_jita_has_no_zero_isk(eve_types, webhook_row, db):  # noqa: F811
    """Выключенная Jita: уведомление о дефиците отправляется, но без «0 ISK» (§13)."""
    fit = _seed_deficit(db)
    MarketLocation.objects.filter(station_id=STATION_ID).update(enabled=False)
    with mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok) as post:
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)

    assert result["sent"] == 1
    assert NotificationLog.objects.filter(event="deficit", ok=True).exists()
    payload_text = "\n".join(json.dumps(call.kwargs, ensure_ascii=False) for call in post.call_args_list)
    assert "0 ISK" not in payload_text
    fit.refresh_from_db()
    assert fit.total_cost is None


def test_notify_deficit_sends_on_state_change(eve_types, webhook_row, db):  # noqa: F811
    """Изменение состояния (сумма докупки) → повторная отправка (§13.1)."""
    _seed_deficit(db)
    with mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok):
        tasks.notify_deficit(trigger=TriggerKind.BEAT)
        # Дефицит изменился: доступность выросла → сумма докупки другая.
        StockLevel.objects.filter(type_id=2046).update(total_sell_volume=0)
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
    assert result["sent"] == 1
    assert NotificationLog.objects.filter(event="deficit", ok=True).count() == 2


def test_notify_deficit_sends_after_cooldown_expired(eve_types, webhook_row, db):  # noqa: F811
    """Тот же state_hash, но cooldown истёк → отправка (§13.1 п.2)."""
    _seed_deficit(db)
    with mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok):
        tasks.notify_deficit(trigger=TriggerKind.BEAT)
        settings_obj = PluginSetting.load()
        log = NotificationLog.objects.filter(event="deficit", ok=True).latest("sent_at")
        log.sent_at = timezone.now() - timedelta(minutes=settings_obj.notification_deficit_cooldown_min + 1)
        log.save(update_fields=["sent_at"])
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
    assert result["sent"] == 1


def test_notify_deficit_disabled_setting(eve_types, webhook_row, db):  # noqa: F811
    """notification_deficit_enabled=False → отправки нет (§10)."""
    _seed_deficit(db)
    settings_obj = PluginSetting.load()
    settings_obj.notification_deficit_enabled = False
    settings_obj.save(update_fields=["notification_deficit_enabled"])
    with mock.patch("marketmonitoring.notify.discord.requests.post") as post:
        result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
    assert result["sent"] == 0
    post.assert_not_called()


def test_notify_deficit_no_webhook_no_log(eve_types, db):  # noqa: F811
    """Webhook не настроен: не отправлять, лог не создавать (§13)."""
    _seed_deficit(db)
    result = tasks.notify_deficit(trigger=TriggerKind.BEAT)
    assert result["ok"] is True
    assert result["sent"] == 0
    assert NotificationLog.objects.count() == 0


def test_sync_failed_alert_only_on_transition(db, webhook_row):
    """sync_failed: алерт при переходе в failed; повторные failed не алертятся (§13.1)."""
    with (
        mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok) as post,
        mock.patch("marketmonitoring.notify.discord.send_sync_failed", wraps=None) as send_failed,
    ):
        send_failed.side_effect = None
        # Прогон 1: ok.
        run = tasks._start_run("t1", TriggerKind.BEAT)
        tasks._finish_run(run, status=SyncRunStatus.OK)
        assert send_failed.call_count == 0
        # Прогон 2: failed → алерт.
        run = tasks._start_run("t1", TriggerKind.BEAT)
        tasks._finish_run(run, status=SyncRunStatus.FAILED, errors="boom\nboom2")
        assert send_failed.call_count == 1
        assert send_failed.call_args.kwargs["errors"] == 2
        assert "boom" in send_failed.call_args.kwargs["brief"]
        # Прогон 3: снова failed → перехода нет, алерта нет.
        run = tasks._start_run("t1", TriggerKind.BEAT)
        tasks._finish_run(run, status=SyncRunStatus.FAILED, errors="boom")
        assert send_failed.call_count == 1


def test_sync_recovered_after_failed_series(db, webhook_row):
    """sync_recovered: ok после серии failed, downtime от последнего ok-прогона (§13)."""
    now = timezone.now()
    SyncRun.objects.create(
        task_name="t2", trigger=TriggerKind.BEAT, started_at=now, finished_at=now, status=SyncRunStatus.OK
    )
    SyncRun.objects.create(
        task_name="t2",
        trigger=TriggerKind.BEAT,
        started_at=now,
        finished_at=now,
        status=SyncRunStatus.FAILED,
    )
    with mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok):
        with mock.patch("marketmonitoring.notify.discord.send_sync_recovered") as send_recovered:
            # _finish_run проставляет finished_at = timezone.now(); время фиксируем.
            later = now + timedelta(minutes=30)
            run = tasks._start_run("t2", TriggerKind.BEAT)
            with mock.patch("marketmonitoring.tasks.timezone.now", return_value=later):
                tasks._finish_run(run, status=SyncRunStatus.OK)
    assert send_recovered.call_count == 1
    # Downtime = 30 мин от последнего ok-прогона.
    assert send_recovered.call_args.kwargs["downtime_min"] == 30


def test_provider_acl_lost_not_repeated_same_state(db, webhook_row):
    """provider_acl_lost: не ограничен cooldown, но не повторяется при том же
    состоянии (§13.1); смена состояния отправляется снова."""
    with mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok) as post:
        ok1 = discord_notify.send_provider_acl_lost(
            character_id=99000001, acl_fail_count=3, status="disabled", disabled=True
        )
        count1 = post.call_count
        assert ok1 is True and count1 == 1
        # То же состояние → повторной отправки нет.
        ok2 = discord_notify.send_provider_acl_lost(
            character_id=99000001, acl_fail_count=3, status="disabled", disabled=True
        )
        assert ok2 is False
        assert post.call_count == count1
        # Смена состояния → отправка.
        ok3 = discord_notify.send_provider_acl_lost(
            character_id=99000002, acl_fail_count=1, status="cooling_down", disabled=False
        )
        assert ok3 is True
        assert post.call_count == count1 + 1


def test_notify_deficit_fit_status_deficit(eve_types, webhook_row, db):  # noqa: F811
    """Согласованность расчёта: recalc_fit даёт статус deficit и целую сумму,
    которая используется в хэше и в embed (§13.1)."""
    fit = _seed_deficit(db)
    recalc_fit(fit)
    fit.refresh_from_db()
    assert fit.status_cache == "deficit"
    assert fit.total_cost is not None
    assert NotificationLog.objects.count() == 0


# --- sync_corp_structures (решение №37 §20) --------------------------------

STRUCTURE_ID = 1048576000001


@pytest.fixture
def corp_contract_provider(db):
    """Персонаж AA + включённый corporation ContractProvider для синка структур.

    Локальный PK намеренно равен 9001, а CCP character_id — 99001: регрессия
    подмены CCP ID локальным PK (боевой лог провайдеров структур).
    """
    eve_char = EveCharacter.objects.create(
        pk=9001,
        character_id=99001,
        character_name="Structure Pilot",
        corporation_id=99002,
        corporation_name="Corp",
        corporation_ticker="CRP",
    )
    return ContractProvider.objects.create(
        character=eve_char,
        source_kind=ContractSourceKind.CORPORATION,
        enabled=True,
        priority=10,
        sync_status=SyncStatus.OK,
    )


def test_sync_corp_structures_registers_new_structures_disabled(corp_contract_provider):
    """Новые структуры корпорации регистрируются как MarketLocation(enabled=false) — решение №37."""
    structures = [
        {"structure_id": STRUCTURE_ID, "name": "Fort Knocks", "solar_system_id": SYSTEM_ID},
    ]
    with mock.patch.object(tasks.esi_client, "fetch_corporation_structures", return_value=(structures, False)) as fetch:
        result = tasks.sync_corp_structures(trigger="manual")

    fetch.assert_called_once_with(99002, 99001)
    # Локальный PK EveCharacter отличается от CCP ID — в ESI уходит именно CCP ID.
    assert corp_contract_provider.character.pk == 9001
    assert result["ok"] is True
    assert result["structures"] == 1
    location = MarketLocation.objects.get(structure_id=STRUCTURE_ID)
    assert location.kind == LocationKind.STRUCTURE
    # Решение №37 §20: новые структуры создаются выключенными.
    assert location.enabled is False
    assert location.name == "Fort Knocks"
    assert location.solar_system_id == SYSTEM_ID


def test_sync_corp_structures_idempotent_and_fills_placeholder(corp_contract_provider):
    """Повторный синк не дублирует записи; плейсхолдерное имя и пустые координаты заполняются."""
    MarketLocation.objects.create(
        name=f"Structure {STRUCTURE_ID}",
        kind=LocationKind.STRUCTURE,
        structure_id=STRUCTURE_ID,
        solar_system_id=0,
        region_id=0,
        enabled=True,
    )
    structures = [
        {"structure_id": STRUCTURE_ID, "name": "Fort Knocks", "solar_system_id": SYSTEM_ID},
    ]
    with mock.patch.object(tasks.esi_client, "fetch_corporation_structures", return_value=(structures, False)):
        tasks.sync_corp_structures(trigger="manual")
        tasks.sync_corp_structures(trigger="manual")

    assert MarketLocation.objects.filter(kind=LocationKind.STRUCTURE).count() == 1
    location = MarketLocation.objects.get(structure_id=STRUCTURE_ID)
    assert location.name == "Fort Knocks"
    assert location.solar_system_id == SYSTEM_ID
    # Включённая вручную запись не отключается синком (существующие не трогаем).
    assert location.enabled is True


def test_sync_corp_structures_guard_interval(corp_contract_provider):
    """Beat-запуск чаще guard-интервала 1440 мин пропускается (§12.2, решение №37)."""
    now = timezone.now()
    SyncRun.objects.create(
        task_name="marketmonitoring.sync_corp_structures",
        trigger=TriggerKind.BEAT,
        started_at=now,
        finished_at=now,
        status=SyncRunStatus.OK,
    )
    with mock.patch.object(tasks.esi_client, "fetch_corporation_structures") as fetch:
        result = tasks.sync_corp_structures(trigger="beat")
    fetch.assert_not_called()
    assert result == {"skipped": True}


def test_sync_corp_structures_acl_failure_marks_provider(corp_contract_provider, webhook_row):
    """403/401 при списке структур — cooldown провайдера и запись об ошибке (§11)."""
    from esi.exceptions import HTTPClientError

    exc = HTTPClientError(status_code=403, headers={}, data=None)
    with (
        mock.patch.object(tasks.esi_client, "fetch_corporation_structures", side_effect=exc),
        mock.patch("marketmonitoring.notify.discord.requests.post", side_effect=_http_ok),
    ):
        result = tasks.sync_corp_structures(trigger="manual")

    assert result["ok"] is True
    assert result["providers_failed"] == 1
    corp_contract_provider.refresh_from_db()
    assert corp_contract_provider.sync_status == SyncStatus.NO_ACL
    run = SyncRun.objects.filter(task_name="marketmonitoring.sync_corp_structures").order_by("-pk").first()
    assert "ACL 403" in run.errors
    # В логах/ошибках — CCP character_id, а не локальный PK EveCharacter.
    assert "corp structures provider 99001: ACL 403" in run.errors
    assert "corp structures provider 9001: ACL 403" not in run.errors
