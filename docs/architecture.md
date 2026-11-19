# Архитектура aa-market-monitoring

Плагин `marketmonitoring` для Alliance Auth 5.x (Django 4.2, Python 3.11, PostgreSQL 15+). Все формулы — в `calc/engine.py` как чистые функции (вход — примитивы и `Decimal`, без БД и сети); UI никогда не обращается к ESI.

## Поток данных

```
ESI ──(Celery-задачи: esi/client.py)──► кэш БД ──(calc/service.py → calc/engine.py)──► UI (только БД)
```

1. **ESI → кэш БД.** Задачи Celery (`tasks.py` через `esi/client.py`) тянут sell-заказы: стакан Jita по типам расчёта, заказы нужных регионов (§9.4), заказы включённых структур (с токеном провайдера). Результат записывается в `MarketOrderCache` (перезапись по источнику в одной транзакции: delete + `bulk_create(batch_size=2000)`) и `JitaOrderLevel` (топ-`jita_book_depth` лотов по типам, rank 1 = минимальная цена).
2. **Агрегация.** Задача `recalc_stock_levels` агрегирует `MarketOrderCache` → `StockLevel` (`total_sell_volume`, `best_sell`) полной заменой по затронутым локациям, затем пересчитывает фиты и компоненты (`calc/service.recalc_all`) и обновляет `Fit.status_cache`, `fits_available` (K), `total_cost`, `overprice_count`, `last_checked_at`.
3. **UI.** Страницы (Dashboard, карточка фита, Components, Locations, Settings) читают только БД плагина. Ручное обновление из UI ставит ту же цепочку задач асинхронно (`trigger=manual`, блокировка `cache.add("mm:sync_lock")`, поллинг `refresh_status`).

Ключевое правило (§5.10): **UI никогда не обращается к ESI** — все запросы ESI выполняются только Celery-задачами через `esi/client.py`.

## Задачи Celery (§12)

| Задача | Назначение | Guard-интервал |
|---|---|---|
| `sync_jita_book` | снимок стакана Jita по типам расчёта + кэш региона Jita | `jita_sync_interval_min` |
| `sync_region_orders` | sell-заказы нужных регионов (объединение §9.4) | `stock_sync_interval_min` |
| `sync_structure_orders` | заказы включённых структур через пул провайдеров | `stock_sync_interval_min` |
| `recalc_stock_levels` | агрегация `StockLevel` + пересчёт фитов/компонентов | после синков |
| `cleanup_orphan_cache` | удаление кэша отключённых/удалённых локаций | 24 ч |
| `verify_providers` | валидация провайдеров через `fetch_structure_info` | 60 мин |
| `notify_deficit` | Discord-уведомления о дефиците | — |

Цепочка: `sync_*` → `recalc_stock_levels` → (`trigger=beat` и `notification_deficit_enabled`) → `notify_deficit`. Триггер (`beat`/`manual`) передаётся аргументами и фиксируется в `SyncRun`. Все задачи идемпотентны: перезапись кэша по источнику, полная замена агрегатов, дедупликация уведомлений через `NotificationLog`. Guard-интервалы проверяются самой задачей (beat дёргает часто — интервалы меняются в настройках без правки beat).

## Расчётное ядро (`calc/engine.py`)

Чистые функции над `Decimal` (float в денежных расчётах запрещён, округление ISK — `ROUND_HALF_UP` до целого):

- **Область L(S)** (§6.1): `station` → одна локация; `system`/`region` → NPC-станции (региональный кэш) + включённые структуры (кэш структур). `order_range` при агрегации не используется — заказ лежит на своей станции.
- **Доступность A** (§6.2): сумма sell-объёмов типа по локациям области (`StockLevel.total_sell_volume`).
- **Собираемость K** (§6.3): `K = min_i floor(A_i / q_i)` по всем компонентам, включая корпус (`q=1`); узкие места — компоненты с `fits_i = K`.
- **Дефицит D_i** (§6.5): `max(0, N·q_i − A_i)`; считается независимо по каждому фиту.
- **Цена покрытия unit** (§6.6): покрывающий лот по стакану Jita либо средневзвешенная до покрытия; при недостатке — `NOT_COVERED` (стоится только `covered`, остаток — `shortfall`).
- **Оверпрайс** (§6.8): `unit > ref·(1+τ/100)`, где `ref` — минимальная sell-цена Jita, τ = `overprice_threshold_pct`.
- **Стоимость** (§6.9): `cost_i = round((unit·(1+m) + delivery_rate·pvol_i) · D_i)`; доставка без наценки, объём — `packaged_volume`; Total — сумма уже округлённых строк.
- **Статусы** (§6.4): `NO_DATA` → `HULL_MISSING` → `DEFICIT` → `OK` (первый подходящий). При `NO_DATA` дефицит и стоимость не считаются (причины — §6.7: структура без свежего синка / `no_provider`, отсутствие `StockLevel`, протухшие данные).

## Провайдеры структур (§11)

`StructureProvider` — персонаж AA (`eveonline.EveCharacter`, PROTECT); токены не хранятся, используются токены AA по `character_id`. Выбор: `enabled=true`, не `disabled`, не в активном cooldown — по возрастанию `priority`, затем `character_id`. 403/401 → счётчик + cooldown (`provider_acl_cooldown_min`), структура переобрабатывается следующим провайдером в том же прогоне; после `provider_acl_max_failures` сбоев — `disabled` + алерт `provider_acl_lost`; успех сбрасывает счётчик. Провайдеров нет → локация `no_provider` → фиты `NO_DATA`. Скоупы (`esi-markets.structure_markets.v1`, `esi-universe.read_structures.v1`) проверяются при привязке.

## Модель данных (§8)

| Модель | Назначение |
|---|---|
| `MarketLocation` | локация мониторинга (station/structure), `enabled`, `sync_status`, `last_synced_at` |
| `StructureProvider` | FK → `eveonline.EveCharacter`; `priority`, `status` (ok/cooling_down/disabled), `acl_fail_count`, `cooldown_until` |
| `Fit` | имя, `hull_type_id`, `eft_raw`, `qty_target` (N), `scope_kind`/`scope_ref`, `status_cache`, `fits_available` (K), `total_cost`, `overprice_count` |
| `FitComponent` | FK → Fit; `type_id`, `qty_per_fit`, `is_charge`, опциональный override области; UNIQUE (fit, type_id) |
| `ComponentRequest` | компонент вне фита: тип, N, область, заметка |
| `MarketOrderCache` | сырые sell-заказы по источнику (region/structure) с индексами (type_id, location_id) и (source_kind, source_ref) |
| `StockLevel` | агрегат sell по (location_id, type_id), UNIQUE |
| `JitaOrderLevel` | снимок стакана Jita, UNIQUE (type_id, rank) |
| `SyncRun` | журнал прогонов задач (admin-only) |
| `WebhookConfig` | Discord webhook (один канал) |
| `NotificationLog` | журнал уведомлений + `state_hash` для дедупликации |
| `PluginSetting` | singleton настроек (§10) |

Денежные поля — `DecimalField(max_digits=20, decimal_places=2)`. Справочники типов/станций/систем/регионов — только через `django-eveuniverse` (SDE), своих копий нет.

## Права (§3)

Группы `market_monitoring_view` / `market_monitoring_edit` (миграция данных + `post_migrate`). Проверка — хелперы `has_view(user)` / `has_edit(user)` (`auth_hooks.py`): суперпользователь или членство в группе. `_view`: просмотр + экспорт; `_edit`: управление фитами/компонентами/локациями, настройки, провайдеры, ручное обновление. Лог `SyncRun` — только суперпользователю в `/admin/`.

## Структура пакетов (§17)

```
marketmonitoring/
├── apps.py            # AppConfig: меню (Market Monitoring / Components / Locations / Settings), post_migrate для групп
├── constants.py       # enum'ы статусов, событий, kinds
├── auth_hooks.py      # has_view / has_edit, create_groups / assign_permissions
├── models.py          # модели §8
├── forms.py           # формы UI
├── views/             # dashboard, fits, components, locations, settings, exports, refresh (+ _calc_view)
├── urls.py            # префикс market/, namespace marketmonitoring
├── eft/parser.py      # EFT-парсер (рус./англ. экспорт; только SDE/БД, без ESI)
├── esi/client.py      # django-esi: батчинг, ретраи, лимиты ESI; esi/selectors.py
├── providers/service.py # выбор провайдера, cooldown, fail-count
├── calc/engine.py     # чистые функции расчёта; calc/service.py — связка с БД
├── tasks.py           # задачи Celery §12
├── notify/discord.py  # webhook, embed, дедупликация
├── admin.py           # SyncRun, локации, провайдеры (superuser)
├── templatetags/mm_tags.py
├── templates/marketmonitoring/
├── migrations/
└── tests/             # парсер, calc-ядро, orderbook, провайдеры, задачи, права, экспорт
```

## Известные ограничения

- **История цен не хранится** — только текущий срез (`MarketOrderCache` перезаписывается по источнику); графики не предусмотрены (§1.2).
- **Один Discord-канал** — один `webhook_url`, несколько каналов не поддерживаются.
- Логи задач доступны только через Django-admin (суперпользователю), UI-страницы логов нет.
- Локализация: этап 1 — английские имена типов (§15); UI-строки через gettext, экспорт закупки всегда на английском; парсер EFT принимает имена на любом языке SDE.
- ESI кэширует ответы (регион ≈ 5 мин, структуры ≈ 30 мин) — данные могут отставать на это время.
