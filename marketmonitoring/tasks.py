"""Задачи Celery плагина (§12): синки ESI, агрегация StockLevel, уведомления, обслуживание.

Все задачи идемпотентны (§12.5): перезапись кэша по источнику в одной транзакции,
пересчёт агрегатов полной заменой по затронутым локациям, запись SyncRun на каждый прогон.
"""

import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .constants import (
    LocationKind,
    NotificationEvent,
    ScopeKind,
    SourceKind,
    SyncRunStatus,
    SyncStatus,
    TriggerKind,
)
from .esi import client as esi_client
from .esi.selectors import get_calc_type_ids, get_sync_region_ids
from .models import (
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

# Guard-интервалы (§12.2).
CLEANUP_INTERVAL_HOURS = 24
VERIFY_PROVIDERS_INTERVAL_MIN = 60

# Beat-интервалы (§12.2): sync_*/verify_providers каждые 5 минут, cleanup ежедневно.
BEAT_SYNC_INTERVAL_SECONDS = 300
BEAT_CLEANUP_INTERVAL_SECONDS = 86400


def build_beat_schedule() -> dict:
    """Фабрика beat-расписания (§12.2) — для документации и конфигурации Celery beat.

    Каждые 5 минут: sync_jita_book, sync_region_orders, sync_structure_orders,
    verify_providers (и notify_deficit — уведомления о дефиците только при
    trigger=beat, §12.4). Ежедневно: cleanup_orphan_cache. Фактические интервалы
    выполнения проверяются guard'ами задач (§10, §12.2): guard-интервалы
    CLEANUP_INTERVAL_HOURS / VERIFY_PROVIDERS_INTERVAL_MIN и интервалы настроек
    jita_sync_interval_min / stock_sync_interval_min не позволят задаче выполниться
    чаще, чем разрешено, даже при более частом beat-тике.
    """
    every_five_minutes = {
        "task": name,
        "schedule": BEAT_SYNC_INTERVAL_SECONDS,
        "options": {"trigger": TriggerKind.BEAT},
    }
    return {
        "marketmonitoring-sync-jita-book": {**every_five_minutes, "task": "marketmonitoring.sync_jita_book"},
        "marketmonitoring-sync-region-orders": {**every_five_minutes, "task": "marketmonitoring.sync_region_orders"},
        "marketmonitoring-sync-structure-orders": {
            **every_five_minutes,
            "task": "marketmonitoring.sync_structure_orders",
        },
        "marketmonitoring-verify-providers": every_five_minutes | {"task": "marketmonitoring.verify_providers"},
        "marketmonitoring-notify-deficit": every_five_minutes | {"task": "marketmonitoring.notify_deficit"},
        "marketmonitoring-cleanup-orphan-cache": {
            "task": "marketmonitoring.cleanup_orphan_cache",
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


def _replace_jita_levels(type_ids: list[int], orders: list[dict]) -> int:
    """Полная замена JitaOrderLevel по затронутым type_id: топ-jita_book_depth лотов (§9.3).

    rank=1 — минимальная цена. Возвращает число вставленных лотов.
    """
    depth = PluginSetting.load().jita_book_depth
    orders_by_type: dict[int, list[dict]] = {}
    for order in orders:
        if order.get("is_buy_order", False):
            continue
        orders_by_type.setdefault(order["type_id"], []).append(order)
    now = timezone.now()
    with transaction.atomic():
        JitaOrderLevel.objects.filter(type_id__in=type_ids).delete()
        levels = []
        for type_id, type_orders in orders_by_type.items():
            sorted_orders = sorted(type_orders, key=lambda o: o["price"])[:depth]
            for rank, order in enumerate(sorted_orders, start=1):
                levels.append(
                    JitaOrderLevel(
                        type_id=type_id,
                        rank=rank,
                        price=order["price"],
                        volume=int(order.get("volume_remain", 0)),
                        fetched_at=now,
                    )
                )
        if levels:
            JitaOrderLevel.objects.bulk_create(levels, batch_size=2000)
        return len(levels)


def _recalc_stock_levels_for_locations(location_ids: list[int]) -> int:
    """Пересчёт StockLevel полной заменой по затронутым локациям (§12.5).

    Агрегация MarketOrderCache → total_sell_volume / best_sell по (location_id, type_id).
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
    if trigger == TriggerKind.BEAT and PluginSetting.load().notification_deficit_enabled:
        notify_deficit.delay(trigger=trigger)


@shared_task(bind=True, name="marketmonitoring.sync_jita_book")
def sync_jita_book(self, trigger: str = TriggerKind.BEAT, lock_token: str | None = None) -> dict:
    """Снимок стакана Jita по типам расчёта (§12.1); guard-интервал jita_sync_interval_min.

    Записывает MarketOrderCache по источнику (region, jita_region_id) и JitaOrderLevel.
    """
    settings_obj = PluginSetting.load()
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
                rows = esi_client.build_order_cache_rows(orders, source_kind=SourceKind.REGION, source_ref=rid)
                items += _replace_source_cache(SourceKind.REGION, rid, rows)
                ok += 1
            except Exception as exc:  # noqa: BLE001 — сбой одного региона не прерывает задачу (§9.5)
                failed += 1
                errors.append(f"region {rid}: {exc}")
                logger.warning("Region %s sync failed: %s", rid, exc)
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
                try:
                    orders, partial = esi_client.fetch_structure_orders(structure_id, provider.character_id)
                except HTTPClientError as exc:
                    status_code = getattr(exc, "status_code", None)
                    if status_code in (401, 403):
                        # ACL-сбой: счётчик, cooldown, при пороге — disabled + алерт (§11).
                        provider_service.handle_acl_failure(provider)
                        failed_character_ids.add(provider.character_id)
                        errors.append(f"structure {structure_id}: provider {provider.character_id} ACL {status_code}")
                        continue
                    # Не-ACL HTTP-ошибка (404 при удалённой структуре, 502 и т.п.):
                    # сбой этой структуры не прерывает задачу (§9.5).
                    failed += 1
                    http_failed = True
                    errors.append(
                        f"structure {structure_id}: provider {provider.character_id} HTTP {status_code}: {exc}"
                    )
                    logger.warning(
                        "Structure %s sync via provider %s failed: %s", structure_id, provider.character_id, exc
                    )
                    break
                except ValueError as exc:
                    # Нет валидного токена AA у провайдера — не ACL, но провайдер не годится.
                    logger.warning("Provider %s has no valid token: %s", provider.character_id, exc)
                    failed_character_ids.add(provider.character_id)
                    continue
                # Успех (200): сброс acl_fail_count, status=ok (§11.4).
                provider_service.handle_provider_success(provider)
                partial_any = partial_any or partial
                rows = esi_client.build_order_cache_rows(
                    orders, source_kind=SourceKind.STRUCTURE, source_ref=structure_id
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
    на результат синка.
    """
    placeholder = f"Structure {location.structure_id}"
    if location.name != placeholder:
        return
    try:
        info = esi_client.fetch_structure_info(location.structure_id, provider.character_id)
    except Exception as exc:  # noqa: BLE001 — имя не критично для расчётов
        logger.info("Could not fetch structure info for %s: %s", location.structure_id, exc)
        return
    name = info.get("name")
    if name:
        location.name = name
        location.save(update_fields=["name"])


@shared_task(bind=True, name="marketmonitoring.recalc_stock_levels")
def recalc_stock_levels(
    self, trigger: str = TriggerKind.BEAT, location_ids: list[int] | None = None, lock_token: str | None = None
) -> dict:
    """Агрегация MarketOrderCache → StockLevel (§12.1); пересчёт полной заменой по затронутым локациям.

    Без аргумента (None) — все локации, встречающиеся в кэше; пересчитываются агрегаты
    только по локациям, у которых есть строки MarketOrderCache. В конце — пересчёт фитов/компонентов (§6, calc-ядро).
    lock_token: финальная задача цепочки ручного запуска снимает mm:sync_lock,
    если её значение совпадает с токеном (§12.4); при beat блокировка не трогается.
    """
    run = _start_run("marketmonitoring.recalc_stock_levels", trigger)
    try:
        if location_ids is None:
            location_ids = list(MarketOrderCache.objects.values_list("location_id", flat=True).distinct())
        count = _recalc_stock_levels_for_locations(list(location_ids))
        # Пересчёт фитов/компонентов (§12.1) — calc-ядро этапа 4.
        from .calc import service as calc_service

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
    """Удаление кэша отключённых/удалённых локаций (§12.1); интервал 24 ч (§12.2)."""
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
        total = 0
        with transaction.atomic():
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
        providers = list(StructureProvider.objects.filter(enabled=True).order_by("priority", "character_id"))
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
            for structure_id in structure_ids:
                try:
                    esi_client.fetch_structure_info(structure_id, provider.character_id)
                except HTTPClientError as exc:
                    status_code = getattr(exc, "status_code", None)
                    if status_code in (401, 403):
                        # Фактический ACL-сбой — обработка §11 (счётчик/cooldown).
                        provider_service.handle_acl_failure(provider)
                        errors.append(f"provider {provider.character_id} structure {structure_id}: ACL {status_code}")
                        break
                    if status_code == 404:
                        # Структура недоступна/не существует — без изменения счётчика (§11).
                        # Провайдер при этом верифицирован: сбой относится к структуре,
                        # а не к доступу провайдера.
                        logger.warning("Structure %s not found for provider %s", structure_id, provider.character_id)
                        verified = True
                        continue
                    raise
                except ValueError as exc:
                    # Нет валидного токена у провайдера — без изменения счётчика.
                    logger.info("Provider %s has no valid token: %s", provider.character_id, exc)
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

        from .calc.service import recalc_fit
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
                rounded_total = (result.total_cost or Decimal("0")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                # Хэш состояния deficit (§13.1): fit_id || K || {t: D>0} || rounded Total.
                state_hash = discord.canonical_state_hash(
                    {
                        "fit_id": fit.pk,
                        "k": result.k,
                        "deficits": deficit_map,
                        "total": str(rounded_total),
                    }
                )
                if not _deficit_notification_due(fit.pk, state_hash, settings_obj.notification_deficit_cooldown_min):
                    skipped += 1
                    continue
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
        _finish_run(run, status=SyncRunStatus.OK, items_processed=sent)
        return {"ok": True, "sent": sent, "skipped": skipped, "invalid": invalid}
    except Exception as exc:  # noqa: BLE001
        logger.exception("notify_deficit failed")
        _finish_run(run, status=SyncRunStatus.FAILED, errors=str(exc))
        return {"ok": False, "error": str(exc)}


def _deficit_notification_due(fit_id: int, state_hash: str, cooldown_min: int) -> bool:
    """Дедупликация deficit (§13.1): отправлять, если состояние изменилось с
    последней ok-отправки ИЛИ прошло >= cooldown_min с неё при сохраняющемся
    дефиците. Ни одной ok-отправки — отправлять."""
    from .models import NotificationLog

    last_ok = (
        NotificationLog.objects.filter(event=NotificationEvent.DEFICIT, fit_id=fit_id, ok=True)
        .order_by("-sent_at", "-id")
        .first()
    )
    if last_ok is None:
        return True
    if last_ok.state_hash != state_hash:
        return True
    return timezone.now() >= last_ok.sent_at + timedelta(minutes=cooldown_min)
