# Настройка aa-market-monitoring

Все настройки редактируются на странице **Settings** (`market/`, URL name `settings`, доступ — роль `market_monitoring_edit`) и хранятся в singleton-модели `PluginSetting` (строка с `pk=1`). Права `_view` не видят форму настроек; webhook URL показывается только пользователям с `_edit`.

## Настройки PluginSetting

| Ключ | Тип | Default | Ограничения | Что влияет |
|---|---|---|---|---|
| `jita_markup_pct` | Decimal | 5 | 0…500 | Наценка m (процент), применяется к цене покрытия `unit` (не к доставке) |
| `overprice_threshold_pct` | Decimal | 25 | 0…1000 | Порог τ: бейдж `OVERPRICE`, если `unit` > `ref`·(1+τ/100), где `ref` — минимальная sell-цена Jita |
| `delivery_rate_isk_per_m3` | Decimal | 1250 | ≥ 0 | Ставка доставки ISK/м³; объём — `packaged_volume` типа; доставка без наценки |
| `jita_region_id` | Integer | 10000002 | — | Регион The Forge для стакана и цен Jita |
| `jita_station_id` | Integer | 60003760 | — | Станция Jita IV-4; заказы этой станции попадают в кэш через синк региона |
| `jita_book_depth` | Integer | 50 | 1…500 | Глубина стакана Jita (`JitaOrderLevel`), используемая при расчёте цены покрытия |
| `jita_type_batch` | Integer | 50 | 1…200 | Размер батча `type_id` при запросе заказов региона по типам; при 400 из-за размера батч уменьшается вдвое |
| `jita_sync_interval_min` | Integer | 60 | 5…1440 | Guard-интервал задачи `sync_jita_book` |
| `stock_sync_interval_min` | Integer | 60 | 5…1440 | Guard-интервал задач `sync_region_orders` и `sync_structure_orders` |
| `data_stale_after_min` | Integer | 180 | ≥ 5 | Порог свежести данных: устаревание даёт статус `NO_DATA` |
| `provider_acl_cooldown_min` | Integer | 60 | 1…1440 | Cooldown провайдера при ACL-сбое (403/401) |
| `provider_acl_max_failures` | Integer | 3 | 1…10 | Число ACL-сбоев до автоматического отключения провайдера |
| `notification_deficit_enabled` | Boolean | True | — | Отправка уведомлений `deficit` **и `ships_deficit`** (только при trigger=beat); `contracts_deficit` управляется отдельным `notification_contracts_deficit_enabled` |
| `notification_deficit_cooldown_min` | Integer | 120 | 1…1440 | Общий cooldown повторной отправки неизменного `deficit`, `contracts_deficit` и `ships_deficit` |
| `webhook_url` | URL | — (пусто) | один канал | Discord webhook для уведомлений |
| `display_language_mode` | Enum | `client_default` | `client_default` \| `fixed_en` | Язык имён типов: `client_default` — `LANGUAGE_CODE` пользователя (fallback `en`); `fixed_en` — всегда английский |
| `contracts_sync_interval_min` | Integer | 30 | 5…1440 | Guard-интервал задачи `sync_contracts` (контракты — источник C) |
| `notification_contracts_deficit_enabled` | Boolean | True | — | Отправка уведомлений `contracts_deficit` (только при trigger=beat) |

Отдельной настройки включения или кулдауна для `ships_deficit` **нет**: новое событие делит `notification_deficit_enabled` и `notification_deficit_cooldown_min` с `deficit` (и тот же cooldown с `contracts_deficit`).

### Изменение интервалов без перезапуска beat

Beat дёргает задачи синка каждые 5 минут; каждая задача сама проверяет по `SyncRun`, истёк ли её guard-интервал (`jita_sync_interval_min`, `stock_sync_interval_min`, `contracts_sync_interval_min`, 60 мин для `verify_providers`, 24 ч для `cleanup_orphan_cache` и `sync_corp_structures`). Поэтому изменение интервалов в настройках применяется к следующему прогону **без перезапуска Celery beat**. То же верно для остальных настроек (наценка, пороги, глубина стакана, webhook). Рыночные синки и `sync_contracts` включены в beat.

## Роли

Группы Django `market_monitoring_view` и `market_monitoring_edit` создаются миграцией данных (`0002_create_groups`) и `post_migrate`-обработчиком. `_edit` подразумевает `_view`; суперпользователь имеет все права. Пункты меню видны при `has_view`; Locations/Settings — при `has_edit`.

| Действие | `_view` | `_edit` | Superuser |
|---|---|---|---|
| Дашборд, карточка фита, компоненты, цены Jita | ✅ | ✅ | ✅ |
| Копирование и скачивание списка закупки | ✅ | ✅ | ✅ |
| Создание/правка/удаление фитов и компонентов | — | ✅ | ✅ |
| Управление локациями | — | ✅ | ✅ |
| Ручной запуск обновления («Обновить сейчас») | — | ✅ | ✅ |
| Настройки плагина (наценка, пороги, интервалы, webhook) | — | ✅ | ✅ |
| Привязка/отзыв/включение `StructureProvider` | — | ✅ | ✅ |
| Привязка/отзыв/включение `ContractProvider` | — | ✅ | ✅ |
| Просмотр лога синков (`SyncRun`) | — | — | ✅ (только `/admin/`) |

## Провайдеры структур

Структуры синкаются через персонажей AA с ACL на структуру. Токены плагин **не хранит** — используются токены AA (`esi.models.Token`) по `character_id` модели `eveonline.EveCharacter`.

### Привязка персонажа

1. Персонаж должен иметь скоупы `esi-markets.structure_markets.v1` и `esi-universe.read_structures.v1`. Если у персонажа скоупов нет — привязка отклоняется с текстом «добавьте персонажа с нужными скоупами».
2. Провайдер привязывается на странице Settings: приоритет (меньше = выше), включение/выключение.
3. Рекомендуется 2–4 провайдера.

### Поведение при сбоях

| Ситуация | Реакция |
|---|---|
| Успешный запрос (200) | `acl_fail_count = 0`, `status = ok` |
| 403/401 | `acl_fail_count += 1`, `status = cooling_down`, `cooldown_until = now + provider_acl_cooldown_min` (default 60 мин); структура в том же прогоне переобрабатывается **следующим** провайдером |
| `acl_fail_count >= provider_acl_max_failures` (default 3) | `status = disabled` + алерт `provider_acl_lost` («отключён») |
| Провайдеров с доступом нет | локация получает `sync_status = no_provider`, фиты по ней — `NO_DATA` |

Провайдер в статусе `disabled` включается **только вручную** через форму провайдеров; при включении счётчик сбоев сбрасывается. Задача `verify_providers` (раз в час) проверяет доступ провайдера к структурам через ESI и обновляет `last_verified_at`; счётчик сбоев меняется только при фактическом 403/401.

### Провайдеры контрактов

`ContractProvider` — персонаж AA с источником `corporation` (корпоративные и альянсовые контракты) или `character` (персональные); токены плагин не хранит, используются токены AA. Задача `sync_contracts` (guard — `contracts_sync_interval_min`) кэширует открытые `item_exchange` контракты и их позиции, дедуплицируя по `contract_id`. Нет включённого провайдера с свежим успешным синком (`sync_status = ok`, `last_synced_at >= now - data_stale_after_min`) → `contracts_status_cache = no_data` → общий `ships_status_cache = no_data`: фит исключается из сводных сумм C+K.

## Discord webhook

Настраивается один webhook (`webhook_url`) — один канал. HTTP POST JSON (`requests`), таймаут 10 с, 3 ретрая с backoff 2/4/8 с; результат пишется в `NotificationLog` (`ok`, `error`). Пример значения (маскировано):

```
https://discord.com/api/webhooks/1234567890/XXXXXXXXXXXXXXXXXXXXXXXX
```

События:

| Событие | Когда | Цвет embed |
|---|---|---|
| `deficit` | beat-проверка, K < N: фит, K/N, узкие места (до 5), сумма докупки, число оверпрайс-позиций, ссылка | оранжевый |
| `contracts_deficit` | beat-проверка, C < N: фит, C/N кораблей в контрактах, нехватка N−C, область, ссылка | оранжевый |
| `ships_deficit` | beat-проверка, `ships_status_cache='deficit'` (C+K < N): фит, C+K и N, нехватка N−(C+K), раздельно C и K, область, ссылка | оранжевый |
| `hull_missing` | корпус отсутствует в области | красный |
| `provider_acl_lost` | ACL-сбой / отключение провайдера | красный |
| `sync_failed` | задача упала / превышен порог ошибок (только при переходе состояния) | красный |
| `sync_recovered` | успешный прогон после серии failed (с длительностью простоя) | зелёный |

Дедупликация `deficit`: уведомление отправляется, если изменился хэш состояния (fit_id ‖ K ‖ набор дефицитных типов ‖ округлённый Total) **или** прошло ≥ `notification_deficit_cooldown_min` с последней ok-отправки при сохраняющемся дефиците. У `contracts_deficit` свой хэш (fit_id ‖ C ‖ N) и свой флаг включения `notification_contracts_deficit_enabled`, но тот же cooldown. У `ships_deficit` свой хэш (fit_id ‖ `ships_status` ‖ `ships_deficit`); отправка при изменении хэша либо по `notification_deficit_cooldown_min`, управление — через `notification_deficit_enabled`. Хэши и записи `NotificationLog` у трёх событий независимы: изменение одного не перезаписывает состояние другого. Системные события (`provider_acl_lost`, `sync_failed`, `sync_recovered`) отправляются только при переходе состояния. Уведомления о дефиците — **только при автоматическом (beat) запуске**; ручной запуск их не порождает, включая `ships_deficit`.

## Диагностика NO_DATA

`NO_DATA` означает: хотя бы одна локация, нужная для расчёта, не имеет свежих данных. При этом дефицит и стоимость **не считаются**, уведомление о дефиците не отправляется; в UI показывается причина и баннер.

Для сводки C+K действует то же правило на уровне источников: `no_data` у C (нет включённого `ContractProvider` либо нет свежей успешной синхронизации) или у K (причины ниже) даёт `ships_status_cache='no_data'`. Агрегатный дефицит в этом случае не считается, `ships_deficit` не отправляется, фит исключается из `∑C`, `∑K` и `∑(C+K)`.

### Причины (§6.7 ТЗ)

1. **Структура без свежего синка:** локация — структура и её `sync_status != ok`, либо у неё нет доступного провайдера (`no_provider`).
2. **Отсутствие агрегата:** для локации нет строки `StockLevel` (данные ещё не синкались/не агрегировались).
3. **Протухшие данные:** `last_synced_at` локации старше `data_stale_after_min` (default 180 мин), либо региональный синк соответствующего региона не завершался успешно за тот же период.

### Как смотреть статус локаций

- Страница **Locations** (`_edit`): для каждой локации — статус синка (`ok` / `no_acl` / `error` / `no_provider` / `stale`) и время последней синхронизации.
- Django-admin (`/admin/`, суперпользователь): модели `MarketLocation`, `StructureProvider`, `SyncRun` — история прогонов задач с триггером (`beat`/`manual`), статусом (`ok`/`partial`/`failed`) и текстом ошибок.

### Что делать

| Симптом | Действие |
|---|---|
| `no_provider` | Привязать/включить провайдера с нужными скоупами (см. выше) |
| `no_acl` | У провайдера нет ACL на структуру — выдать доступ персонажу-провайдеру в игре |
| `stale` / давно нет `last_synced_at` | Проверить, что Celery worker и beat работают; нажать «Обновить сейчас»; проверить `SyncRun` в admin |
| Задачи `failed` | Текст ошибки в `SyncRun.errors`; часто — лимиты ESI (задача переносится на следующий прогон) или неверные токены AA |
| Регион синк OK, но `NO_DATA` | Проверить, что `data_stale_after_min` больше интервала синка и что регион локации входит в синк (объединение регионов включённых локаций и областей расчёта) |
| `ships_status_cache='no_data'` при исправном рынке | Проверить `contracts_status_cache`: включить `ContractProvider` с нужными скоупами и дождаться успешного `sync_contracts` в интервале `data_stale_after_min` |

Примечание: ESI кэширует ответы (регион ≈ 5 мин, структуры ≈ 30 мин) — это ожидаемое поведение.
