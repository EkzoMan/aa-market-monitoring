"""Задачи Celery плагина (§12): синки ESI, агрегация StockLevel, уведомления, обслуживание.

Все задачи идемпотентны (§12.5): перезапись кэша по источнику в одной транзакции,
пересчёт агрегатов полной заменой по затронутым локациям, запись SyncRun на каждый прогон.
"""

import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .constants import (
    ContractFitStatus,
    ContractSourceKind,
    LocationKind,
    NotificationEvent,
    ScopeKind,
    ShipsStatus,
    SourceKind,
    SyncRunStatus,
    SyncStatus,
    TriggerKind,
)
from .esi import client as esi_client
from .esi.selectors import get_calc_type_ids, get_sync_region_ids, is_jita_enabled
from .models import (
    ContractCache,
    ContractItemCache,
    ContractProvider,
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

logger = logging.getLogger("marketmonitoring")

# Ключ блокировки ручного обновления (§12.4).
SYNC_LOCK_KEY = "mm:sync_lock"

# Кэш результатов поиска структур (§9): префикс ключа и TTL.
SEARCH_CACHE_PREFIX = "mm:structure_search"
SEARCH_RESULT_TTL_SECONDS = 300

# Guard-интервалы (§12.2).
CLEANUP_INTERVAL_HOURS = 24
VERIFY_PROVIDERS_INTERVAL_MIN = 60

# Guard-интервал синка структур корпораций (решение №37 §20): раз в сутки.
CORP_STRUCTURES_SYNC_INTERVAL_MIN = 1440

# Beat-интервалы (§12.2): sync_*/verify_providers каждые 5 минут, cleanup ежедневно.
BEAT_SYNC_INTERVAL_SECONDS = 300
BEAT_CLEANUP_INTERVAL_SECONDS = 86400


def build_beat_schedule() -> dict:
    """Фабрика beat-расписания (§12.2) — для документации и конфигурации Celery beat.

    Каждые 5 минут: sync_jita_book, sync_region_orders, sync_structure_orders,
    sync_contracts, verify_providers (и notify_deficit — уведомления о
    дефиците только при trigger=beat, §12.4). Ежедневно: cleanup_orphan_cache.
    Фактические интервалы выполнения проверяются guard'ами задач (§10, §12.2):
    guard'ы по jita_sync_interval_min / stock_sync_interval_min /
    contracts_sync_interval_min / CLEANUP_INTERVAL_HOURS /
    VERIFY_PROVIDERS_INTERVAL_MIN не позволят задаче выполниться чаще,
    чем разрешено, даже при более частом beat-тике.
    """
    every_five_minutes = {
        "schedule": BEAT_SYNC_INTERVAL_SECONDS,
        "options": {"trigger": TriggerKind.BEAT},
    }
    return {
        "marketmonitoring-sync-jita-book": every_five_minutes | {"task": "marketmonitoring.sync_jita_book"},
        "marketmonitoring-sync-region-orders": every_five_minutes | {"task": "marketmonitoring.sync_region_orders"},
        "marketmonitoring-sync-structure-orders": every_five_minutes
        | {"task": "marketmonitoring.sync_structure_orders"},
        "marketmonitoring-sync-contracts": {**every_five_minutes, "task": "marketmonitoring.sync_contracts"},
        "marketmonitoring-verify-providers": every_five_minutes | {"task": "marketmonitoring.verify_providers"},
        "marketmonitoring-notify-deficit": every_five_minutes | {"task": "marketmonitoring.notify_deficit"},
        "marketmonitoring-cleanup-orphan-cache": {
            "task": "marketmonitoring.cleanup_orphan_cache",
            "schedule": BEAT_CLEANUP_INTERVAL_SECONDS,
            "options": {"trigger": TriggerKind.BEAT},
        },
        # Структуры корпораций провайдеров (решение №37 §20): раз в сутки.
        # Guard CORP_STRUCTURES_SYNC_INTERVAL_MIN не даст выполниться чаще.
        "marketmonitoring-sync-corp-structures": {
            "task": "marketmonitoring.sync_corp_structures",
            "schedule": BEAT_CLEANUP_INTERVAL_SECONDS,
            "options": {"trigger": TriggerKind.BEAT},
        },
    }


def _interval_expired(last_at, interval_min: int) -> bool:
    """Проверка guard-интервала: истёк ли интервал с последнего запуска (§12.2)."""
    if last_at is None:
        return True
    return timezone.now() >= last_at + timedelta(minutes=interval_min)


def _start_run(task_name: str, trigger: str) -> SyncRun:
    """Создание записи SyncRun для прогона (§8)."""
    return SyncRun.objects.create(
        task_name=task_name,
        trigger=trigger,
        started_at=timezone.now(),
        status=SyncRunStatus.RUNNING,
    )


def _finish_run(
    run: SyncRun,
    *,
    status: str,
    locations_ok: int = 0,
    locations_failed: int = 0,
    items_processed: int = 0,
    errors: str = "",
) -> SyncRun:
    """Завершение SyncRun с итогами прогона.

    Этап 7 (§13): при статусе failed — алерт sync_failed только при переходе
    (предыдущий прогон этой задачи был не failed); при ok после серии failed —
    sync_recovered с длительностью простоя (от последнего ok-прогона).
    """
    run.finished_at = timezone.now()
    run.status = status
    run.locations_ok = locations_ok
    run.locations_failed = locations_failed
    run.items_processed = items_processed
    run.errors = errors
    run.save()
    _notify_sync_state(run, status, errors)
    return run


def _notify_sync_state(run: SyncRun, status: str, errors: str) -> None:
    """Discord-алерты sync_failed / sync_recovered при переходе состояния (§13).

    Сбой: предыдущий прогон задачи не failed → sync_failed (число ошибок =
    количество строк в errors, краткий текст — первая строка). Успех после
    серии failed → sync_recovered, downtime = now - последний ok-прогон.
    state_hash = SHA-256 канонического JSON {"task": name, "prev": status}.
    """
    from .notify import discord

    previous = SyncRun.objects.filter(task_name=run.task_name).exclude(pk=run.pk).order_by("-started_at", "-id").first()
    prev_status = previous.status if previous else SyncRunStatus.OK
    state_hash = discord.canonical_state_hash({"task": run.task_name, "prev": str(prev_status)})
    if status == SyncRunStatus.FAILED:
        if prev_status != SyncRunStatus.FAILED:
            error_lines = [line for line in errors.splitlines() if line.strip()]
            try:
                discord.send_sync_failed(
                    task_name=run.task_name,
                    errors=len(error_lines),
                    brief=error_lines[0] if error_lines else "",
                    state_hash=state_hash,
                )
            except Exception:  # noqa: BLE001 — уведомление не ломает задачу
                logger.exception("sync_failed notification failed")
    elif status == SyncRunStatus.OK and prev_status == SyncRunStatus.FAILED:
        last_ok = (
            SyncRun.objects.filter(task_name=run.task_name, status=SyncRunStatus.OK)
            .exclude(pk=run.pk)
            .order_by("-finished_at", "-id")
            .first()
        )
        if last_ok and last_ok.finished_at:
            downtime_min = int((run.finished_at - last_ok.finished_at).total_seconds() // 60)
        else:
            downtime_min = 0
        try:
            discord.send_sync_recovered(
                task_name=run.task_name,
                downtime_min=downtime_min,
                state_hash=state_hash,
            )
        except Exception:  # noqa: BLE001
            logger.exception("sync_recovered notification failed")


def _replace_source_cache(source_kind: str, source_ref: int, rows: list[dict]) -> int:
    """Идемпотентная перезапись кэша источника в одной транзакции (§9.3, §12.5).

    Удаление всех строк MarketOrderCache по (source_kind, source_ref) и bulk_create;
    частично записанных срезов не бывает.
    """
    from .models import MarketOrderCache

    with transaction.atomic():
        MarketOrderCache.objects.filter(source_kind=source_kind, source_ref=source_ref).delete()
        if not rows:
            return 0
        objs = [MarketOrderCache(**row) for row in rows]
        MarketOrderCache.objects.bulk_create(objs, batch_size=2000)
        return len(objs)


def _mark_region_stations(region_id: int, status: str) -> int:
    """Обновление sync_status локаций региона после регионального синка (§8).

    NPC-станции получают данные из регионального кэша заказов, поэтому их
    sync_status/last_synced_at отражают результат синка региона: успех — ok,
    сбой/частичность — error. Локации-системы используют те же региональные
    данные для NPC-станций системы (§6.12), поэтому их статус обновляется
    вместе со станциями. Один запрос .update() — без N+1. Структуры не
    затрагиваются: их синкает отдельная задача sync_structure_orders.
    """
    now = timezone.now()
    qs = MarketLocation.objects.filter(kind__in=(LocationKind.STATION, LocationKind.SYSTEM), region_id=region_id)
    if status == SyncStatus.OK:
        return qs.update(sync_status=SyncStatus.OK, last_synced_at=now)
    return qs.update(sync_status=status)


def _replace_jita_levels(type_ids: list[int], orders: list) -> int:
    """Полная замена JitaOrderLevel по затронутым type_id: топ-jita_book_depth лотов (§9.3).

    Элементы orders — dict или pydantic-модель ордера ESI (django-esi 9.9).
    rank=1 — минимальная цена. Возвращает число вставленных лотов.
    """
    depth = PluginSetting.load().jita_book_depth

    def _f(order, name, default=None):
        return order.get(name, default) if isinstance(order, dict) else getattr(order, name, default)

    orders_by_type: dict[int, list] = {}
    for order in orders:
        if _f(order, "is_buy_order", False):
            continue
        orders_by_type.setdefault(_f(order, "type_id"), []).append(order)
    now = timezone.now()
    with transaction.atomic():
        JitaOrderLevel.objects.filter(type_id__in=type_ids).delete()
        levels = []
        for type_id, type_orders in orders_by_type.items():
            sorted_orders = sorted(type_orders, key=lambda o: _f(o, "price"))[:depth]
            for rank, order in enumerate(sorted_orders, start=1):
                levels.append(
                    JitaOrderLevel(
                        type_id=type_id,
                        rank=rank,
                        price=_f(order, "price"),
                        volume=int(_f(order, "volume_remain", 0) or 0),
                        fetched_at=now,
                    )
                )
        if levels:
            JitaOrderLevel.objects.bulk_create(levels, batch_size=2000)
        return len(levels)


def _recalc_stock_levels_for_locations(location_ids: list[int]) -> int:
    """Пересчёт StockLevel полной заменой по затронутым локациям (§12.5).

    Агрегация MarketOrderCache → total_sell_volume / best_sell по (location_id, type_id).
    ``solar_system_id``/``region_id`` берутся из строк кэша: для структур они
    записаны из MarketLocation, а не из ответа ESI (§9.3).
    """
    from django.db.models import Min, Sum

    if not location_ids:
        return 0
    now = timezone.now()
    with transaction.atomic():
        StockLevel.objects.filter(location_id__in=location_ids).delete()
        agg = (
            MarketOrderCache.objects.filter(location_id__in=location_ids)
            .values("location_id", "solar_system_id", "region_id", "type_id")
            .annotate(total=Sum("volume"), best=Min("price"))
        )
        levels = [
            StockLevel(
                location_id=row["location_id"],
                solar_system_id=row["solar_system_id"],
                region_id=row["region_id"],
                type_id=row["type_id"],
                total_sell_volume=row["total"],
                best_sell=row["best"],
                updated_at=now,
            )
            for row in agg
        ]
        if levels:
            StockLevel.objects.bulk_create(levels, batch_size=2000)
        return len(levels)


def _release_sync_lock(trigger: str, lock_token: str | None) -> None:
    """Снятие блокировки ручного обновления (§12.4) — только владельцем токена.

    Финальная задача цепочки (recalc_stock_levels) при trigger=manual снимает
    mm:sync_lock, если значение ключа совпадает с переданным ей токеном:
    параллельный ручной запуск (свой токен) не может быть снят чужой цепочкой.
    При trigger=beat или без токена блокировка не трогается.
    """
    if trigger != TriggerKind.MANUAL or not lock_token:
        return
    current = cache.get(SYNC_LOCK_KEY)
    if current == lock_token:
        cache.delete(SYNC_LOCK_KEY)
        logger.info("Manual sync lock released by chain token")


def _chain_recalc_and_notify(trigger: str, location_ids: list[int] | None, lock_token: str | None = None) -> None:
    """Цепочка §12.3: sync_* → recalc_stock_levels → notify_deficit (только при trigger=beat).

    location_ids=None означает «все локации, встречающиеся в свежем кэше».
    lock_token (только при trigger=manual) передаётся финальной задаче цепочки
    для снятия блокировки ручного запуска (§12.4).
    """
    recalc_stock_levels.delay(trigger=trigger, location_ids=location_ids, lock_token=lock_token)
    if trigger == TriggerKind.BEAT:
        settings_obj = PluginSetting.load()
        if settings_obj.notification_deficit_enabled or settings_obj.notification_contracts_deficit_enabled:
            notify_deficit.delay(trigger=trigger)


@shared_task(name="marketmonitoring.search_structures")
def search_structures(character_id: int, query: str, job_id: str) -> dict:
    """Поиск структур персонажа (§9 шаги 1–3) — единственное место обращения к ESI (§5.10).

    Результат [{id, name, solar_system_id, region_id}] кладётся в кэш Django
    по ключу job_id (TTL 300 с); region_id резолвится из SDE (eveuniverse,
    без ESI). Ошибка не ретраится — результат запроса уже виден в UI.
    """
    cache_key = f"{SEARCH_CACHE_PREFIX}:{job_id}"
    try:
        rows = esi_client.search_structures_for_character(character_id, query)
        # region_id — из справочника SDE (eveuniverse), без ESI (§8).
        system_ids = {row["solar_system_id"] for row in rows if row.get("solar_system_id")}
        region_by_system: dict[int, int | None] = {}
        if system_ids:
            from eveuniverse.models import EveSolarSystem

            region_by_system = dict(
                EveSolarSystem.objects.filter(pk__in=system_ids).values_list("pk", "eve_constellation__eve_region_id")
            )
        for row in rows:
            row["region_id"] = region_by_system.get(row.get("solar_system_id"))
        cache.set(cache_key, {"status": "done", "results": rows}, SEARCH_RESULT_TTL_SECONDS)
        return {"ok": True, "count": len(rows)}
    except Exception as exc:  # noqa: BLE001 — ошибка поиска отдаётся в UI через кэш
        logger.warning("Structure search failed (character %s): %s", character_id, exc)
        cache.set(cache_key, {"status": "error", "error": str(exc), "results": []}, SEARCH_RESULT_TTL_SECONDS)
        return {"ok": False, "error": str(exc)}


@shared_task(bind=True, name="marketmonitoring.sync_jita_book")
def sync_jita_book(self, trigger: str = TriggerKind.BEAT, lock_token: str | None = None) -> dict:
    """Снимок стакана Jita по типам расчёта (§12.1); guard-интервал jita_sync_interval_min.

    Записывает MarketOrderCache по источнику (region, jita_region_id) и JitaOrderLevel.
    Если локация Jita выключена/удалена из Locations — ESI не вызывается,
    старый стакан и кэш региона удаляются, цепочка пересчёта не запускается
    (явное указание заказчика; расхождение с §9.4 и №3/8/25/39 фиксируется в docs).
    """
    settings_obj = PluginSetting.load()
    if not is_jita_enabled():
        # До _start_run и до любого ESI: отключённая Jita не должна синкаться
        # ни ручным запуском, ни beat. Чистка в одной транзакции (§12.5);
        # .exists() не делает пустые DELETE при каждом beat-прогоне.
        levels_qs = JitaOrderLevel.objects.all()
        cache_qs = MarketOrderCache.objects.filter(
            source_kind=SourceKind.REGION, source_ref=settings_obj.jita_region_id
        )
        levels_deleted = cache_deleted = 0
        with transaction.atomic():
            if levels_qs.exists():
                levels_deleted = levels_qs.delete()[0] or 0
            if cache_qs.exists():
                cache_deleted = cache_qs.delete()[0] or 0
        logger.info(
            "Jita sync skipped: location disabled; deleted %s levels and %s order rows",
            levels_deleted,
            cache_deleted,
        )
        return {"skipped": True, "reason": "jita_disabled"}
    last = (
        SyncRun.objects.filter(task_name="marketmonitoring.sync_jita_book", trigger=TriggerKind.BEAT)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    if trigger == TriggerKind.BEAT and not _interval_expired(last, settings_obj.jita_sync_interval_min):
        return {"skipped": True}
    run = _start_run("marketmonitoring.sync_jita_book", trigger)
    try:
        type_ids = sorted(get_calc_type_ids())
        orders, partial = esi_client.fetch_jita_orders(type_ids)
        rows = esi_client.build_order_cache_rows(
            orders, source_kind=SourceKind.REGION, source_ref=settings_obj.jita_region_id
        )
        inserted = _replace_source_cache(SourceKind.REGION, settings_obj.jita_region_id, rows)
        levels = _replace_jita_levels(type_ids, orders)
        _finish_run(
            run,
            status=SyncRunStatus.PARTIAL if partial else SyncRunStatus.OK,
            items_processed=inserted + levels,
        )
        _chain_recalc_and_notify(trigger, [settings_obj.jita_station_id], lock_token=lock_token)
        return {"ok": True, "items": inserted, "levels": levels, "partial": partial}
    except Exception as exc:  # noqa: BLE001 — фиксирование любого сбоя в SyncRun
        logger.exception("sync_jita_book failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        return {"ok": False, "error": str(exc)}


@shared_task(bind=True, name="marketmonitoring.sync_region_orders")
def sync_region_orders(
    self, trigger: str = TriggerKind.BEAT, region_id: int | None = None, lock_token: str | None = None
) -> dict:
    """Заказы нужных регионов (§9.4, §12.1); guard-интервал stock_sync_interval_min.

    Без аргумента region_id синкаются все нужные регионы (объединение §9.4).
    """
    settings_obj = PluginSetting.load()
    last = (
        SyncRun.objects.filter(task_name="marketmonitoring.sync_region_orders", trigger=TriggerKind.BEAT)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    region_ids = [region_id] if region_id else sorted(get_sync_region_ids())
    # Один DEBUG-лог на задачу: почему выбранных регионов 0/несколько (§9.4),
    # и какие типы включённых локаций есть в конфигурации. Один запрос, без N+1.
    enabled_location_kinds = dict(
        MarketLocation.objects.filter(enabled=True)
        .values("kind")
        .annotate(count=Count("pk"))
        .values_list("kind", "count")
    )
    logger.debug(
        "sync_region_orders: region_ids=%s, enabled MarketLocation total=%s by_kind=%s",
        region_ids,
        sum(enabled_location_kinds.values()),
        enabled_location_kinds,
    )
    if trigger == TriggerKind.BEAT and not _interval_expired(last, settings_obj.stock_sync_interval_min):
        return {"skipped": True}
    run = _start_run("marketmonitoring.sync_region_orders", trigger)
    ok = failed = items = 0
    errors: list[str] = []
    partial_any = False
    try:
        for rid in region_ids:
            try:
                orders, partial = esi_client.fetch_region_orders(rid)
                partial_any = partial_any or partial
                rows = esi_client.build_order_cache_rows(
                    orders, source_kind=SourceKind.REGION, source_ref=rid, region_id=rid
                )
                items += _replace_source_cache(SourceKind.REGION, rid, rows)
                # NPC-станции и локации-системы региона получают данные из
                # регионального кэша: успех — sync_status=ok и свежий
                # last_synced_at (иначе чип навсегда остаётся stale, §8).
                _mark_region_stations(rid, SyncStatus.OK if not partial else SyncStatus.ERROR)
                ok += 1
            except Exception as exc:  # noqa: BLE001 — сбой одного региона не прерывает задачу (§9.5)
                failed += 1
                errors.append(f"region {rid}: {exc}")
                logger.warning("Region %s sync failed: %s", rid, exc)
                _mark_region_stations(rid, SyncStatus.ERROR)
        _finish_run(
            run,
            status=(
                SyncRunStatus.FAILED
                if failed and not ok
                else SyncRunStatus.PARTIAL if (failed or partial_any) else SyncRunStatus.OK
            ),
            locations_ok=ok,
            locations_failed=failed,
            items_processed=items,
            errors="\n".join(errors),
        )
        _chain_recalc_and_notify(trigger, None, lock_token=lock_token)
        return {"ok": True, "regions_ok": ok, "regions_failed": failed}
    except Exception as exc:  # noqa: BLE001
        logger.exception("sync_region_orders failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        return {"ok": False, "error": str(exc)}


@shared_task(bind=True, name="marketmonitoring.sync_structure_orders")
def sync_structure_orders(self, trigger: str = TriggerKind.BEAT, lock_token: str | None = None) -> dict:
    """Заказы включённых структур (§12.1, §11); guard-интервал stock_sync_interval_min.

    Для каждой структуры: перебор провайдеров через providers.service.select_provider;
    403/401 → handle_acl_failure → следующий провайдер в том же прогоне (§11.2);
    успех → сброс счётчика (§11.4), перезапись кэша источником structure (§9.3),
    обновление временного имени структуры через fetch_structure_info.
    Провайдеров нет → sync_status=no_provider (§11.6). Сбой одной структуры
    не прерывает задачу (§9.5).
    """
    from esi.exceptions import HTTPClientError

    from .providers import service as provider_service

    settings_obj = PluginSetting.load()
    last = (
        SyncRun.objects.filter(task_name="marketmonitoring.sync_structure_orders", trigger=TriggerKind.BEAT)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    if trigger == TriggerKind.BEAT and not _interval_expired(last, settings_obj.stock_sync_interval_min):
        return {"skipped": True}
    run = _start_run("marketmonitoring.sync_structure_orders", trigger)
    locations = MarketLocation.objects.filter(enabled=True, kind=LocationKind.STRUCTURE).order_by("pk")
    ok = failed = items = 0
    errors: list[str] = []
    partial_any = False
    try:
        for location in locations:
            structure_id = location.structure_id
            if structure_id is None:
                failed += 1
                errors.append(f"location {location.pk}: no structure_id")
                continue
            # Перебор провайдеров в том же прогоне (§11.2): 403/401 → cooldown → следующий.
            failed_character_ids: set[int] = set()
            synced = False
            http_failed = False
            while True:
                provider = provider_service.switch_to_next_provider(structure_id, failed_character_ids)
                if provider is None:
                    break
                # CCP character_id из EveCharacter; provider.character_id — локальный FK PK (§8, §11).
                character_id = provider.character.character_id
                try:
                    orders, partial = esi_client.fetch_structure_orders(structure_id, character_id)
                except HTTPClientError as exc:
                    status_code = getattr(exc, "status_code", None)
                    if status_code in (401, 403):
                        # ACL-сбой: счётчик, cooldown, при пороге — disabled + алерт (§11).
                        provider_service.handle_acl_failure(provider)
                        failed_character_ids.add(character_id)
                        errors.append(f"structure {structure_id}: provider {character_id} ACL {status_code}")
                        continue
                    # Не-ACL HTTP-ошибка (404 при удалённой структуре, 502 и т.п.):
                    # сбой этой структуры не прерывает задачу (§9.5).
                    failed += 1
                    http_failed = True
                    errors.append(f"structure {structure_id}: provider {character_id} HTTP {status_code}: {exc}")
                    logger.warning("Structure %s sync via provider %s failed: %s", structure_id, character_id, exc)
                    break
                except ValueError as exc:
                    # Нет валидного токена AA у провайдера — не ACL, но провайдер не годится.
                    logger.warning("Provider %s has no valid token: %s", character_id, exc)
                    failed_character_ids.add(character_id)
                    continue
                # Успех (200): сброс acl_fail_count, status=ok (§11.4).
                provider_service.handle_provider_success(provider)
                partial_any = partial_any or partial
                # ESI structural orders не содержат system/region — берём их из
                # MarketLocation; source_ref — это structure_id, не region_id (§9.3).
                rows = esi_client.build_order_cache_rows(
                    orders,
                    source_kind=SourceKind.STRUCTURE,
                    source_ref=structure_id,
                    solar_system_id=location.solar_system_id,
                    region_id=location.region_id,
                )
                items += _replace_source_cache(SourceKind.STRUCTURE, structure_id, rows)
                _update_structure_name(location, provider)
                location.sync_status = SyncStatus.OK if not partial else SyncStatus.ERROR
                location.last_synced_at = timezone.now()
                location.save(update_fields=["sync_status", "last_synced_at"])
                synced = True
                ok += 1
                break
            if not synced and not http_failed:
                # Доступных провайдеров не осталось → no_provider (§11.6).
                location.sync_status = SyncStatus.NO_PROVIDER
                location.save(update_fields=["sync_status"])
                failed += 1
                if not failed_character_ids:
                    errors.append(f"structure {structure_id}: no_provider")
        _finish_run(
            run,
            status=(
                SyncRunStatus.FAILED
                if failed and not ok
                else SyncRunStatus.PARTIAL if (failed or partial_any) else SyncRunStatus.OK
            ),
            locations_ok=ok,
            locations_failed=failed,
            items_processed=items,
            errors="\n".join(errors),
        )
        _chain_recalc_and_notify(trigger, None, lock_token=lock_token)
        return {"ok": True, "locations_ok": ok, "locations_failed": failed}
    except Exception as exc:  # noqa: BLE001
        logger.exception("sync_structure_orders failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        return {"ok": False, "error": str(exc)}


def _update_structure_name(location: MarketLocation, provider) -> None:
    """Обновление временного имени структуры через fetch_structure_info (§11, §9.1).

    Временное имя — плейсхолдер «Structure <id>»; после успешного синка
    подтягивается настоящее имя структуры. Сбой обновления имени не влияет
    на результат синка. Токен ESI ищется по CCP ID персонажа
    (``provider.character.character_id``), а не по PK FK.
    """
    placeholder = f"Structure {location.structure_id}"
    if location.name != placeholder:
        return
    try:
        info = esi_client.fetch_structure_info(location.structure_id, provider.character.character_id)
    except Exception as exc:  # noqa: BLE001 — имя не критично для расчётов
        logger.info("Could not fetch structure info for %s: %s", location.structure_id, exc)
        return
    name = info.get("name")
    if name:
        location.name = name
        location.save(update_fields=["name"])


def _upsert_contracts_source(
    *,
    source_kind: str,
    source_ref: int,
    active_rows: list[dict],
    fetched_items_by_cid: dict[int, list],
) -> tuple[int, set[int]]:
    """Атомарная запись ContractCache/ContractItemCache по источнику (§6.12, §12.5).

    Удаление исчезнувших контрактов здесь НЕ выполняется: зачистка делается один
    раз в конце прогона sync_contracts по объединённому active-множеству всех
    источников (решение №38 §20 — кросс-эндпоинтовый контракт, видимый сразу в
    корпоративном и персонажном срезах, не должен удаляться при синке одного
    источника). Позиции ``items`` вставляются один раз при первой загрузке
    ``contract_id`` (решение №35). Возвращает (число обработанных строк,
    активные contract_id источника).
    """
    active_cids = {row["contract_id"] for row in active_rows}
    processed = 0
    with transaction.atomic():
        existing = {c.contract_id: c for c in ContractCache.objects.filter(contract_id__in=active_cids)}
        existing_with_items = set(
            ContractItemCache.objects.filter(contract__contract_id__in=active_cids)
            .values_list("contract__contract_id", flat=True)
            .distinct()
        )
        new_item_objs: list[ContractItemCache] = []
        for row in active_rows:
            cid = row["contract_id"]
            if cid not in existing_with_items and cid not in fetched_items_by_cid:
                # Если лимит ESI прервал догрузку items нового контракта — не пишем полупустую шапку.
                continue
            cached = existing.get(cid)
            if cached is None:
                cached = ContractCache.objects.create(**row)
                existing[cid] = cached
            else:
                for field, value in row.items():
                    if field in ("contract_id", "source_kind", "source_ref"):
                        continue
                    setattr(cached, field, value)
                cached.save()
            processed += 1
            if cid not in existing_with_items and cid in fetched_items_by_cid:
                item_dicts = esi_client.build_contract_item_rows(fetched_items_by_cid[cid], contract_pk=cached.pk)
                new_item_objs.extend(ContractItemCache(**d) for d in item_dicts)
                existing_with_items.add(cid)
        if new_item_objs:
            ContractItemCache.objects.bulk_create(new_item_objs, batch_size=2000)
    return processed, active_cids


@shared_task(bind=True, name="marketmonitoring.sync_contracts")
def sync_contracts(self, trigger: str = TriggerKind.BEAT, lock_token: str | None = None) -> dict:
    """Синхронизация открытых item_exchange контрактов корпораций/персонажей (§6.12, §12.1).

    Guard-интервал ``contracts_sync_interval_min``. Для каждого включённого
    ``ContractProvider`` тянет список контрактов (корпоративных + альянсовых или
    персональных), фильтрует ``status=outstanding``, ``type=item_exchange``,
    ``date_expired > now``, запрашивает ``items`` только для новых ``contract_id``
    (решение №35), обновляет кэш и запускает ``recalc_stock_levels`` → ``notify_deficit``.
    """
    from esi.exceptions import HTTPClientError

    from .providers import service as provider_service

    settings_obj = PluginSetting.load()
    last = (
        SyncRun.objects.filter(task_name="marketmonitoring.sync_contracts", trigger=TriggerKind.BEAT)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    if trigger == TriggerKind.BEAT and not _interval_expired(last, settings_obj.contracts_sync_interval_min):
        return {"skipped": True}
    run = _start_run("marketmonitoring.sync_contracts", trigger)
    providers = list(provider_service.available_contract_providers())
    ok = failed = items_processed = 0
    errors: list[str] = []
    partial_any = False
    try:
        # Очистка глобально истёкших контрактов до обхода провайдеров.
        ContractCache.objects.filter(date_expired__lte=timezone.now()).delete()
        seen_sources: set[tuple[str, int]] = set()
        # Решение №38 §20: активные contract_id всех источников прогона —
        # финальная зачистка выполняется один раз после обхода всех провайдеров,
        # чтобы кросс-эндпоинтовый контракт не удалился синком одного источника.
        active_cids_global: set[int] = set()
        full_run_sources: list[tuple[str, int]] = []
        for provider in providers:
            char_id = provider.character.character_id
            if provider.source_kind == ContractSourceKind.CORPORATION:
                corp_id = provider.character.corporation_id
                if not corp_id:
                    failed += 1
                    provider.sync_status = SyncStatus.ERROR
                    provider.save(update_fields=["sync_status"])
                    errors.append(f"contract provider {char_id}: missing corporation_id")
                    continue
                source_key = (ContractSourceKind.CORPORATION, int(corp_id))
            else:
                source_key = (ContractSourceKind.CHARACTER, int(char_id))
            if source_key in seen_sources:
                # Тот же источник в этом прогоне уже успешно синхронизирован более приоритетным провайдером.
                provider.sync_status = SyncStatus.OK
                provider.last_synced_at = timezone.now()
                provider.save(update_fields=["sync_status", "last_synced_at"])
                ok += 1
                continue
            try:
                if provider.source_kind == ContractSourceKind.CORPORATION:
                    raw_contracts, partial = esi_client.fetch_corporation_contracts(source_key[1], char_id)
                else:
                    raw_contracts, partial = esi_client.fetch_character_contracts(char_id)
            except HTTPClientError as exc:
                status_code = getattr(exc, "status_code", None)
                if status_code in (401, 403):
                    provider_service.handle_contract_provider_acl_failure(provider)
                    provider.sync_status = SyncStatus.NO_ACL
                    provider.save(update_fields=["sync_status"])
                    failed += 1
                    errors.append(f"contract provider {char_id} ({provider.source_kind}): ACL {status_code}")
                    continue
                provider.sync_status = SyncStatus.ERROR
                provider.save(update_fields=["sync_status"])
                failed += 1
                errors.append(f"contract provider {char_id} ({provider.source_kind}): HTTP {status_code}: {exc}")
                logger.warning("Contract sync via provider %s failed: %s", char_id, exc)
                continue
            except ValueError as exc:
                provider.sync_status = SyncStatus.NO_ACL
                provider.save(update_fields=["sync_status"])
                failed += 1
                errors.append(f"contract provider {char_id} ({provider.source_kind}): {exc}")
                logger.warning("Contract provider %s has no valid token: %s", char_id, exc)
                continue

            now = timezone.now()
            dedup_by_cid: dict[int, dict] = {}
            for raw in raw_contracts:
                row = esi_client.build_contract_cache_row(
                    raw, source_kind=source_key[0], source_ref=source_key[1], now=now
                )
                if row is not None:
                    dedup_by_cid[row["contract_id"]] = row
            active_rows = list(dedup_by_cid.values())

            already_have_items = set(
                ContractItemCache.objects.filter(contract__contract_id__in=dedup_by_cid.keys())
                .values_list("contract__contract_id", flat=True)
                .distinct()
            )
            fetched_items_by_cid: dict[int, list] = {}
            items_partial = False
            for cid in dedup_by_cid:
                if cid in already_have_items:
                    continue
                try:
                    if provider.source_kind == ContractSourceKind.CORPORATION:
                        fetched_items_by_cid[cid] = esi_client.fetch_corporation_contract_items(
                            source_key[1], cid, char_id
                        )
                    else:
                        fetched_items_by_cid[cid] = esi_client.fetch_character_contract_items(cid, char_id)
                except esi_client.RateLimitStop:
                    items_partial = True
                    break
                except HTTPClientError as exc:
                    status_code = getattr(exc, "status_code", None)
                    errors.append(f"contract {cid} items HTTP {status_code}: {exc}")
                    logger.warning("Contract %s items fetch failed: %s", cid, exc)
                    items_partial = True

            provider_service.handle_contract_provider_success(provider)
            provider_partial = partial or items_partial
            partial_any = partial_any or provider_partial
            processed_rows, source_active_cids = _upsert_contracts_source(
                source_kind=source_key[0],
                source_ref=source_key[1],
                active_rows=active_rows,
                fetched_items_by_cid=fetched_items_by_cid,
            )
            items_processed += processed_rows
            active_cids_global |= source_active_cids
            provider.sync_status = SyncStatus.OK if not provider_partial else SyncStatus.ERROR
            provider.last_synced_at = timezone.now()
            provider.save(update_fields=["sync_status", "last_synced_at"])
            if not provider_partial:
                seen_sources.add(source_key)
                # Полный срез источника — по нему можно безопасно чистить исчезнувшие контракты.
                full_run_sources.append(source_key)
            ok += 1

        # Решение №38 §20: финальная зачистка исчезнувших контрактов — один раз после
        # обхода всех провайдеров, по объединённому active-множеству прогона. Контракт,
        # активный хотя бы в одном источнике (например, виден и в корпоративном, и в
        # персонажном срезе), не удаляется. Зачистка выполняется только для источников
        # с полным срезом; частичные (partial) источники не чистятся (§12.5).
        with transaction.atomic():
            for source_kind, source_ref in full_run_sources:
                ContractCache.objects.filter(source_kind=source_kind, source_ref=source_ref).exclude(
                    contract_id__in=active_cids_global
                ).delete()

        _finish_run(
            run,
            status=(
                SyncRunStatus.FAILED
                if failed and not ok
                else SyncRunStatus.PARTIAL if (failed or partial_any) else SyncRunStatus.OK
            ),
            locations_ok=ok,
            locations_failed=failed,
            items_processed=items_processed,
            errors="\n".join(errors),
        )
        _chain_recalc_and_notify(trigger, [], lock_token=lock_token)
        return {"ok": True, "providers_ok": ok, "providers_failed": failed, "contracts": items_processed}
    except Exception as exc:  # noqa: BLE001
        logger.exception("sync_contracts failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        return {"ok": False, "error": str(exc)}


@shared_task(bind=True, name="marketmonitoring.sync_corp_structures")
def sync_corp_structures(self, trigger: str = TriggerKind.BEAT) -> dict:
    """Синк структур корпораций провайдеров контрактов (решение №37 §20).

    Для каждого включённого ``ContractProvider`` (corporation) тянет список
    структур его корпорации (``GET /corporations/{id}/structures/``, скоуп
    ``esi-corporations.read_structures.v1``) и регистрирует
    незнакомые структуры в ``MarketLocation(kind=structure, enabled=False)``
    — включаются вручную в Locations UI (решение №37). Существующие записи
    не трогаются, кроме заполнения пустого ``solar_system_id``/``region_id``
    и плейсхолдерного имени. Guard-интервал — раз в сутки. ACL-сбой
    провайдера обрабатывается как в sync_contracts (cooldown/отключение §11).
    """
    from esi.exceptions import HTTPClientError

    from .providers import service as provider_service

    last = (
        SyncRun.objects.filter(task_name="marketmonitoring.sync_corp_structures", trigger=TriggerKind.BEAT)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    if trigger == TriggerKind.BEAT and not _interval_expired(last, CORP_STRUCTURES_SYNC_INTERVAL_MIN):
        return {"skipped": True}
    run = _start_run("marketmonitoring.sync_corp_structures", trigger)
    providers = list(provider_service.available_contract_providers())
    ok = failed = items_processed = 0
    errors: list[str] = []
    partial_any = False
    try:
        seen_corps: set[int] = set()
        for provider in providers:
            char_id = provider.character.character_id
            if provider.source_kind != ContractSourceKind.CORPORATION:
                continue
            corp_id = provider.character.corporation_id
            if not corp_id:
                continue
            if int(corp_id) in seen_corps:
                continue
            seen_corps.add(int(corp_id))
            try:
                structures, partial = esi_client.fetch_corporation_structures(int(corp_id), char_id)
            except HTTPClientError as exc:
                status_code = getattr(exc, "status_code", None)
                if status_code in (401, 403):
                    provider_service.handle_contract_provider_acl_failure(provider)
                    provider.sync_status = SyncStatus.NO_ACL
                    provider.save(update_fields=["sync_status"])
                    failed += 1
                    errors.append(f"corp structures provider {char_id}: ACL {status_code}")
                    continue
                failed += 1
                errors.append(f"corp structures provider {char_id}: HTTP {status_code}: {exc}")
                logger.warning("Corp structures sync via provider %s failed: %s", char_id, exc)
                continue
            except ValueError as exc:
                failed += 1
                errors.append(f"corp structures provider {char_id}: {exc}")
                logger.warning("Corp structures provider %s has no valid token: %s", char_id, exc)
                continue
            partial_any = partial_any or partial
            # Регионы систем структур — одним пакетным запросом из SDE (§8, без ESI).
            system_ids = {
                int(sys_id)
                for sys_id in (_structure_field(row, "solar_system_id") for row in structures)
                if sys_id is not None
            }
            region_by_system: dict[int, int | None] = {}
            if system_ids:
                from eveuniverse.models import EveSolarSystem

                region_by_system = dict(
                    EveSolarSystem.objects.filter(pk__in=system_ids).values_list(
                        "pk", "eve_constellation__eve_region_id"
                    )
                )
            for row in structures:
                structure_id = _structure_field(row, "structure_id")
                if structure_id is None:
                    continue
                structure_id = int(structure_id)
                name = str(_structure_field(row, "name") or f"Structure {structure_id}")
                solar_system_id = _structure_field(row, "solar_system_id")
                solar_system_id = int(solar_system_id) if solar_system_id is not None else None
                region_id = region_by_system.get(solar_system_id) if solar_system_id is not None else None
                location = MarketLocation.objects.filter(kind=LocationKind.STRUCTURE, structure_id=structure_id).first()
                if location is None:
                    MarketLocation.objects.create(
                        name=name,
                        kind=LocationKind.STRUCTURE,
                        structure_id=structure_id,
                        solar_system_id=solar_system_id or 0,
                        region_id=region_id or 0,
                        # Решение №37 §20: новые структуры регистрируются выключенными.
                        enabled=False,
                    )
                    items_processed += 1
                else:
                    # Существующие записи не трогаем, кроме незаполненных координат
                    # и плейсхолдерного имени (реальное имя подтягивается из ESI-синка).
                    update_fields = []
                    if (not location.solar_system_id) and solar_system_id:
                        location.solar_system_id = solar_system_id
                        update_fields.append("solar_system_id")
                    if (not location.region_id) and region_id:
                        location.region_id = region_id
                        update_fields.append("region_id")
                    if location.name == f"Structure {structure_id}" and name != location.name:
                        location.name = name
                        update_fields.append("name")
                    if update_fields:
                        location.save(update_fields=update_fields)
            provider_service.handle_contract_provider_success(provider)
            ok += 1
        _finish_run(
            run,
            status=(
                SyncRunStatus.FAILED
                if failed and not ok
                else SyncRunStatus.PARTIAL if (failed or partial_any) else SyncRunStatus.OK
            ),
            locations_ok=ok,
            locations_failed=failed,
            items_processed=items_processed,
            errors="\n".join(errors),
        )
        return {"ok": True, "providers_ok": ok, "providers_failed": failed, "structures": items_processed}
    except Exception as exc:  # noqa: BLE001
        logger.exception("sync_corp_structures failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        return {"ok": False, "error": str(exc)}


def _structure_field(row: Any, name: str, default: Any = None) -> Any:
    """Чтение поля структуры ESI: dict или pydantic-модель (как _order_field)."""
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


@shared_task(bind=True, name="marketmonitoring.recalc_stock_levels")
def recalc_stock_levels(
    self, trigger: str = TriggerKind.BEAT, location_ids: list[int] | None = None, lock_token: str | None = None
) -> dict:
    """Агрегация MarketOrderCache → StockLevel (§12.1); пересчёт полной заменой по затронутым локациям.

    Без аргумента (None) — все локации, встречающиеся в кэше; пересчитываются агрегаты
    только по локациям, у которых есть строки MarketOrderCache. В конце — пересчёт фитов/компонентов (§6, calc-ядро).
    lock_token: финальная задача цепочки ручного запуска снимает mm:sync_lock,
    если её значение совпадает с токеном (§12.4); при beat блокировка не трогается.

    Ручное обновление ставит несколько sync-задач параллельно, и каждая из них
    запускает сюда пересчёт. Блокировка строки singleton-настроек сериализует
    пересчёты: два concurrent-прогона с разными снимками locations не могут
    стереть StockLevel локаций, появившихся в кэше между снимками (§12.5).
    """
    run = _start_run("marketmonitoring.recalc_stock_levels", trigger)
    try:
        # Пересчёт фитов/компонентов (§12.1) — calc-ядро этапа 4.
        from .calc import service as calc_service

        with transaction.atomic():
            PluginSetting.objects.get_or_create(pk=1)
            PluginSetting.objects.select_for_update().get(pk=1)
            if location_ids is None:
                location_ids = list(MarketOrderCache.objects.values_list("location_id", flat=True).distinct())
            count = _recalc_stock_levels_for_locations(list(location_ids))
            calc_stats = calc_service.recalc_all()
        _finish_run(run, status=SyncRunStatus.OK, items_processed=count)
        _release_sync_lock(trigger, lock_token)
        return {"ok": True, "levels": count, **calc_stats}
    except Exception as exc:  # noqa: BLE001
        logger.exception("recalc_stock_levels failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        _release_sync_lock(trigger, lock_token)
        return {"ok": False, "error": str(exc)}


@shared_task(bind=True, name="marketmonitoring.cleanup_orphan_cache")
def cleanup_orphan_cache(self, trigger: str = TriggerKind.BEAT) -> dict:
    """Удаление кэша отключённых/удалённых локаций (§12.1); интервал 24 ч (§12.2).

    Дополнительно: если Jita выключена/удалена, явно удаляются JitaOrderLevel
    и кэш заказов (region, jita_region_id) — без stale-цен в UI (указание заказчика).
    """
    run = _start_run("marketmonitoring.cleanup_orphan_cache", trigger)
    try:
        # Guard 24 ч: только строки кэша, не обновлённые за интервал, у неактивных источников.
        cutoff = timezone.now() - timedelta(hours=CLEANUP_INTERVAL_HOURS)
        enabled_stations = set(
            MarketLocation.objects.filter(enabled=True, station_id__isnull=False).values_list("station_id", flat=True)
        )
        enabled_structures = set(
            MarketLocation.objects.filter(enabled=True, structure_id__isnull=False).values_list(
                "structure_id", flat=True
            )
        )
        # Регионы, которые ещё нужны (§9.4): регионы включённых локаций и синка — их кэш не удаляем.
        needed_regions = set(get_sync_region_ids())
        settings_obj = PluginSetting.load()
        total = 0
        with transaction.atomic():
            if not is_jita_enabled():
                # Jita удалена/отключена: get_sync_region_ids уже исключает её регион,
                # но стакан не привязан к региону — удаляем явно, чтобы не осталось
                # stale-цен. Кэш (region, jita_region_id) чистим без ожидания 24 ч.
                total += JitaOrderLevel.objects.all().delete()[0] or 0
                total += (
                    MarketOrderCache.objects.filter(
                        source_kind=SourceKind.REGION, source_ref=settings_obj.jita_region_id
                    ).delete()[0]
                    or 0
                )
            for kind, valid_refs in (
                (SourceKind.REGION, needed_regions),
                (SourceKind.STRUCTURE, enabled_structures),
            ):
                qs = MarketOrderCache.objects.filter(source_kind=kind, fetched_at__lt=cutoff)
                if valid_refs is not None:
                    qs = qs.exclude(source_ref__in=valid_refs)
                total += qs.delete()[0] or 0
        # Кэш станций: location_id, не входящий в активные станции и не равный структуре.
        stale = MarketOrderCache.objects.filter(fetched_at__lt=cutoff).exclude(
            location_id__in=enabled_stations | enabled_structures
        )
        total += stale.delete()[0] or 0
        # Хвосты агрегатов StockLevel по отключённым/удалённым локациям (§12.1):
        # только строки, не обновлённые за интервал — нужные локации непрерывно
        # синкаются и имеют свежий updated_at, поэтому не затрагиваются.
        stale_stock = StockLevel.objects.filter(updated_at__lt=cutoff).exclude(
            location_id__in=enabled_stations | enabled_structures
        )
        total += stale_stock.delete()[0] or 0
        # Очистка истёкших контрактов и старого кэша отключённых провайдеров контрактов (§6.12, §12.1).
        total += ContractCache.objects.filter(date_expired__lte=timezone.now()).delete()[0] or 0
        enabled_corp_refs = {
            cid
            for cid in ContractProvider.objects.filter(
                enabled=True, source_kind=ContractSourceKind.CORPORATION
            ).values_list("character__corporation_id", flat=True)
            if cid
        }
        enabled_char_refs = set(
            ContractProvider.objects.filter(enabled=True, source_kind=ContractSourceKind.CHARACTER).values_list(
                "character__character_id", flat=True
            )
        )
        total += (
            ContractCache.objects.filter(source_kind=ContractSourceKind.CORPORATION, fetched_at__lt=cutoff)
            .exclude(source_ref__in=enabled_corp_refs)
            .delete()[0]
            or 0
        )
        total += (
            ContractCache.objects.filter(source_kind=ContractSourceKind.CHARACTER, fetched_at__lt=cutoff)
            .exclude(source_ref__in=enabled_char_refs)
            .delete()[0]
            or 0
        )
        _finish_run(run, status=SyncRunStatus.OK, items_processed=total)
        return {"ok": True, "deleted": total}
    except Exception as exc:  # noqa: BLE001
        logger.exception("cleanup_orphan_cache failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        return {"ok": False, "error": str(exc)}


@shared_task(bind=True, name="marketmonitoring.verify_providers")
def verify_providers(self, trigger: str = TriggerKind.BEAT) -> dict:
    """Валидация провайдеров структур (§12.1, §11); интервал 60 мин по last_verified_at.

    Проверяет наличие структуры в ответе ESI через fetch_structure_info.
    acl_fail_count меняется только при фактическом 403/401 (§11); 404
    (структура исчезла) — только предупреждение в лог. Обновляет
    last_verified_at у провайдеров.
    """
    from esi.exceptions import HTTPClientError

    from .providers import service as provider_service

    last = (
        SyncRun.objects.filter(task_name="marketmonitoring.verify_providers")
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    if not _interval_expired(last, VERIFY_PROVIDERS_INTERVAL_MIN):
        return {"skipped": True}
    run = _start_run("marketmonitoring.verify_providers", trigger)
    try:
        providers = list(
            StructureProvider.objects.select_related("character")
            .filter(enabled=True)
            .order_by("priority", "character__character_id", "pk")
        )
        structure_ids = list(
            MarketLocation.objects.filter(enabled=True, kind=LocationKind.STRUCTURE, structure_id__isnull=False)
            .order_by("pk")
            .values_list("structure_id", flat=True)
        )
        now = timezone.now()
        checked = 0
        errors: list[str] = []
        for provider in providers:
            verified = False
            # CCP character_id, а не локальный PK FK (provider.character_id).
            character_id = provider.character.character_id
            for structure_id in structure_ids:
                try:
                    esi_client.fetch_structure_info(structure_id, character_id)
                except HTTPClientError as exc:
                    status_code = getattr(exc, "status_code", None)
                    if status_code in (401, 403):
                        # Фактический ACL-сбой — обработка §11 (счётчик/cooldown).
                        provider_service.handle_acl_failure(provider)
                        errors.append(f"provider {character_id} structure {structure_id}: ACL {status_code}")
                        break
                    if status_code == 404:
                        # Структура недоступна/не существует — без изменения счётчика (§11).
                        # Провайдер при этом верифицирован: сбой относится к структуре,
                        # а не к доступу провайдера.
                        logger.warning("Structure %s not found for provider %s", structure_id, character_id)
                        verified = True
                        continue
                    raise
                except ValueError as exc:
                    # Нет валидного токена у провайдера — без изменения счётчика.
                    logger.info("Provider %s has no valid token: %s", character_id, exc)
                    break
                verified = True
            # Если включённых структур нет — проверять нечего, провайдер считается верифицированным.
            verified = verified or not structure_ids
            if verified:
                provider.last_verified_at = now
                provider.save(update_fields=["last_verified_at"])
                checked += 1
        _finish_run(run, status=SyncRunStatus.OK, items_processed=checked, errors="\n".join(errors))
        return {"ok": True, "verified": checked}
    except Exception as exc:  # noqa: BLE001
        logger.exception("verify_providers failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        return {"ok": False, "error": str(exc)}


@shared_task(bind=True, name="marketmonitoring.notify_deficit")
def notify_deficit(self, trigger: str = TriggerKind.BEAT) -> dict:
    """Уведомления о дефиците (§12.1, §13) — только при trigger=beat (§12.3, §12.4).

    Этап 7: пересчёт каждого фита (recalc_fit, без сети), дедупликация deficit
    через NotificationLog (§13.1) и hull_missing по переходу состояния.
    """
    if trigger != TriggerKind.BEAT:
        # Уведомления о дефиците при manual не отправляются (§12.4).
        return {"skipped": True, "reason": "manual trigger"}
    run = _start_run("marketmonitoring.notify_deficit", trigger)
    try:
        from eveuniverse.models import EveType  # noqa: E402 — SDE-справочник (§7)

        from .calc.service import get_ships_availability, recalc_fit
        from .constants import FitStatus
        from .models import NotificationLog
        from .notify import discord

        settings_obj = PluginSetting.load()

        # Имена типов — один пакетный запрос на все типы всех фитов (без N+1, §22.4).
        all_type_ids = set(Fit.objects.values_list("components__type_id", flat=True))
        all_type_ids.discard(None)
        type_names = dict(EveType.objects.filter(pk__in=all_type_ids).values_list("pk", "name"))

        # Защита от деления на ноль (§6.3): q_i > 0 по ТЗ; фиты с q_i=0 —
        # некорректные данные, пропускаются с ошибкой в лог.
        zero_qty_fit_ids = set(FitComponent.objects.filter(qty_per_fit=0).values_list("fit_id", flat=True))

        sent = 0
        skipped = 0
        invalid = 0
        for fit in Fit.objects.all().order_by("pk"):
            if fit.pk in zero_qty_fit_ids:
                logger.error("notify_deficit: fit %s has a component with qty_per_fit=0, fit skipped", fit.pk)
                invalid += 1
                continue
            result = recalc_fit(fit)
            # Общий статус уже записан recalc_fit в Fit.ships_status_cache;
            # результат ядра переиспользуем, а для моков/старых вызовов читаем кэш.
            ships = getattr(result, "ships_availability", None)
            if ships is None:
                ships = get_ships_availability(fit)
            if (
                result.contracts_status == ContractFitStatus.DEFICIT
                and result.contracts_available is not None
                and settings_obj.notification_contracts_deficit_enabled
            ):
                contracts_scope_label = f"{fit.scope_kind} {fit.scope_ref}"
                contracts_hash = discord.canonical_state_hash(
                    {
                        "event": NotificationEvent.CONTRACTS_DEFICIT,
                        "fit_id": fit.pk,
                        "contracts_available": result.contracts_available,
                        "qty_target": fit.qty_target,
                    }
                )
                if _deficit_notification_due(
                    fit.pk,
                    contracts_hash,
                    settings_obj.notification_deficit_cooldown_min,
                    event=NotificationEvent.CONTRACTS_DEFICIT,
                ):
                    ok_c, _err_c = discord.send_contracts_deficit(
                        fit=fit,
                        ships_available=result.contracts_available,
                        n=fit.qty_target,
                        scope_label=contracts_scope_label,
                        state_hash=contracts_hash,
                    )
                    if ok_c:
                        sent += 1
                else:
                    skipped += 1

            if (
                result.status == FitStatus.DEFICIT
                and result.k is not None
                and settings_obj.notification_deficit_enabled
            ):
                # Узкие места: fits_i = floor(A_i / q_i), min → имя типа (§6.3).
                fits_per_type = {
                    component.type_id: component.availability // component.qty_per_fit
                    for component in result.components
                }
                k_min = min(fits_per_type.values())
                bottleneck_names = [
                    str(type_names.get(component.type_id, f"type {component.type_id}"))
                    for component in result.components
                    if fits_per_type.get(component.type_id) == k_min
                ]
                deficit_map = {
                    str(component.type_id): component.deficit
                    for component in result.components
                    if component.deficit > 0
                }
                # Отключённая Jita: цены нет — в хэш пишем "n/a", не "0 ISK".
                total_for_hash = (
                    "n/a"
                    if result.total_cost is None
                    else str(result.total_cost.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                )
                # Хэш состояния deficit (§13.1): fit_id || K || {t: D>0} || rounded Total.
                state_hash = discord.canonical_state_hash(
                    {
                        "fit_id": fit.pk,
                        "k": result.k,
                        "deficits": deficit_map,
                        "total": total_for_hash,
                    }
                )
                deficit_due = _deficit_notification_due(
                    fit.pk, state_hash, settings_obj.notification_deficit_cooldown_min
                )
                if not deficit_due:
                    skipped += 1
                if deficit_due:
                    ok, _error = discord.send_deficit(
                        fit=fit,
                        k=result.k,
                        n=fit.qty_target,
                        bottlenecks=bottleneck_names,
                        total_cost=result.total_cost,
                        overprice_count=result.overprice_count or 0,
                        state_hash=state_hash,
                    )
                    if ok:
                        sent += 1
            elif result.status == FitStatus.HULL_MISSING:
                # hull_missing (§13): только при переходе состояния (§13.1) —
                # дедуп по последней ok-записи с тем же state_hash.
                scope_label = f"{fit.scope_kind} {fit.scope_ref}"
                state_hash = discord.canonical_state_hash(
                    {
                        "fit_id": fit.pk,
                        "hull_type_id": fit.hull_type_id,
                        "scope_kind": fit.scope_kind,
                        "scope_ref": fit.scope_ref,
                    }
                )
                ok, _error = discord.send_hull_missing(fit=fit, scope_label=scope_label, state_hash=state_hash)
                if ok:
                    sent += 1

            # ships_deficit (§13, решение №42): только beat и общий статус deficit.
            # Старые события deficit/contracts_deficit и их хэши не изменяются.
            if (
                ships.ships_status == ShipsStatus.DEFICIT
                and ships.ships_total is not None
                and ships.ships_deficit is not None
                and ships.ships_in_contracts is not None
                and ships.ships_from_market is not None
                and settings_obj.notification_deficit_enabled
            ):
                ships_scope_label = f"{fit.scope_kind} {fit.scope_ref}"
                ships_hash = discord.ships_deficit_state_hash(
                    fit_id=fit.pk,
                    ships_status=ships.ships_status,
                    ships_deficit=ships.ships_deficit,
                )
                if _deficit_notification_due(
                    fit.pk,
                    ships_hash,
                    settings_obj.notification_deficit_cooldown_min,
                    event=NotificationEvent.SHIPS_DEFICIT,
                ):
                    ok_s, _err_s = discord.send_ships_deficit(
                        fit=fit,
                        ships_in_contracts=ships.ships_in_contracts,
                        ships_from_market=ships.ships_from_market,
                        ships_total=ships.ships_total,
                        n=fit.qty_target,
                        ships_deficit=ships.ships_deficit,
                        scope_label=ships_scope_label,
                        state_hash=ships_hash,
                    )
                    if ok_s:
                        sent += 1
                else:
                    skipped += 1
        _finish_run(run, status=SyncRunStatus.OK, items_processed=sent)
        return {"ok": True, "sent": sent, "skipped": skipped, "invalid": invalid}
    except Exception as exc:  # noqa: BLE001
        logger.exception("notify_deficit failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        return {"ok": False, "error": str(exc)}


def _deficit_notification_due(
    fit_id: int,
    state_hash: str,
    cooldown_min: int,
    event: str = NotificationEvent.DEFICIT,
) -> bool:
    """Дедупликация deficit / contracts_deficit / ships_deficit (§13.1): отправлять,
    если состояние изменилось с последней ok-отправки ИЛИ прошло >= cooldown_min
    с неё при сохраняющемся дефиците. Ни одной ok-отправки — отправлять."""
    from .models import NotificationLog

    last_ok = NotificationLog.objects.filter(event=event, fit_id=fit_id, ok=True).order_by("-sent_at", "-id").first()
    if last_ok is None:
        return True
    if last_ok.state_hash != state_hash:
        return True
    return timezone.now() >= last_ok.sent_at + timedelta(minutes=cooldown_min)
