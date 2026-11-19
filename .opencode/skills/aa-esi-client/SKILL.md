---
name: AA ESI Client
description: Правила работы с django-esi в aa-market-monitoring (эндпоинты, батчинг, лимиты, ретраи, перезапись кэша, провайдеры структур). Загружать при работе с esi/client.py, tasks.py и провайдерами.
---

## Ключевое правило

UI никогда не обращается к ESI (§5 п.10 ТЗ). Все запросы ESI — только из Celery-задач через `esi/client.py` (обёртки endpoints, батчинг, ретраи).

## Эндпоинты (§9 ТЗ)

- Стакан Jita / заказы региона: `Market.get_region_orders(...)`, `order_type='sell'` — без токена.
- Заказы структуры: `Market.get_structure_orders(...)` — токен провайдера, скоуп `esi-markets.structure_markets.v1`.
- Инфо структуры: `Universe.get_universe_structures_structure_id` — скоуп `esi-universe.read_structures.v1`.
- Скоупы провайдера проверяются при привязке; нет — отказ «добавьте персонажа с нужными скоупами».

## Батчинг и лимиты

- Jita: повторяемый параметр `type_id`, батч `jita_type_batch` (default 50), `page_size=1000`, идти по страницам до пустой.
- 400 из-за размера батча → уменьшить батч вдвое и повторить.
- Уважать `X-ESI-Error-Limit-Remain/Reset`: остаток < 10 → остановить задачу, пометить `partial`, перенос на следующий запуск.
- Ретраи: 3 попытки, backoff 2/4/8 с на 5xx/таймаутах; на 420 — пауза до `Retry-After`.

## Перезапись кэша (§9.3)

Синк источника: полный набор заказов → одна транзакция: удалить `MarketOrderCache` по `(source_kind, source_ref)` → `bulk_create(batch_size=2000)`. Частично записанных срезов не бывает. `JitaOrderLevel` — полная замена по затронутым `type_id`, топ-`jita_book_depth` лотов.

## Провайдеры структур (§11)

- Токены — из AA (`eveonline.Character` по character_id), плагин токены не хранит.
- Выбор: `enabled`, не `disabled`, не в cooldown → по возрастанию `priority`, затем `character_id`.
- 403/401: `acl_fail_count += 1`, `status=cooling_down` на `provider_acl_cooldown_min`; структура повторно идёт следующему провайдеру; `acl_fail_count >= provider_acl_max_failures` → `disabled` + алерт `provider_acl_lost`. Успех (200) сбрасывает счётчик.
- Нет доступных провайдеров → локация `no_provider`, фиты `NO_DATA`.
- Все внешние вызовы — с таймаутами, ретраями и записью результата. Тесты ESI — `responses`/`unittest.mock`, без сети.
