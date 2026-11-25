# Архитектура aa-market-monitoring

Плагин `marketmonitoring` для Alliance Auth 5.x (Django 4.2, Python 3.11, PostgreSQL 15+). Все формулы — в `calc/engine.py` как чистые функции (вход — примитивы и `Decimal`, без БД и сети); UI никогда не обращается к ESI.

## Поток данных

```
ESI (рынок + контракты) ──(Celery-задачи: esi/client.py)──► кэш БД ──(calc/service.py → calc/engine.py)──► UI (только БД)
```

1. **ESI → кэш БД.** Задачи Celery (`tasks.py` через `esi/client.py`) тянут sell-заказы: стакан Jita по типам расчёта (только при включённой локации Jita IV-4 — см. эррату ниже), заказы нужных регионов (§9.4; The Forge входит в набор регионов только вместе с включённой Jita), заказы включённых структур (с токеном провайдера). Результат записывается в `MarketOrderCache` (перезапись по источнику в одной транзакции: delete + `bulk_create(batch_size=2000)`) и `JitaOrderLevel` (топ-`jita_book_depth` лотов по типам, rank 1 = минимальная цена). Задача `sync_contracts` через `ContractProvider` забирает открытые `item_exchange` контракты и их позиции в `ContractCache` / `ContractItemCache` с дедупликацией по `contract_id`.
2. **Агрегация и пересчёт.** Задача `recalc_stock_levels` агрегирует `MarketOrderCache` → `StockLevel` (`total_sell_volume`, `best_sell`) полной заменой по затронутым локациям, затем пересчитывает фиты и компоненты (`calc/service.recalc_all`) и обновляет `Fit.status_cache`, `fits_available` (K), `contracts_status_cache`, `contracts_available` (C), `ships_status_cache`, `total_cost`, `overprice_count`, `last_checked_at`. Агрегатный дефицит `ships_deficit` не хранится — он вычисляется из N, C и K. При успешном полном синке локации, по которой заказов нет, в `StockLevel` пишутся нулевые агрегаты для нужных типов: список локаций, включая пустые, передаётся в пересчёт явно, запись идёт полной заменой в одной транзакции; `partial`-синк нули не пишет (решение №46).
3. **UI.** Страницы (Dashboard, карточка фита, Components, Locations, Settings) читают только БД плагина: рыночные счётчики из `StockLevel`, контрактные — из `ContractCache`, итог C+K — из полей `Fit`. Ручное обновление из UI ставит ту же цепочку задач асинхронно (`trigger=manual`, блокировка `cache.add("mm:sync_lock")`, поллинг `refresh_status`).

Ключевое правило (§5.10): **UI никогда не обращается к ESI** — все запросы ESI выполняются только Celery-задачами через `esi/client.py`.

## Задачи Celery (§12)

| Задача | Назначение | Guard-интервал |
|---|---|---|
| `sync_jita_book` | снимок стакана Jita по типам расчёта + кэш региона Jita — только при добавленной и включённой локации Jita IV-4 | `jita_sync_interval_min` |
| `sync_region_orders` | sell-заказы нужных регионов (объединение §9.4) | `stock_sync_interval_min` |
| `sync_structure_orders` | заказы включённых структур через пул провайдеров | `stock_sync_interval_min` |
| `sync_contracts` | открытые `item_exchange` контракты и позиции провайдеров контрактов | `contracts_sync_interval_min` |
| `sync_corp_structures` | структуры корпораций `ContractProvider` → `MarketLocation(kind=structure, enabled=false)` | 24 ч (решение №37) |
| `recalc_stock_levels` | агрегация `StockLevel` + пересчёт фитов/компонентов, в т.ч. C, K и `ships_status_cache` | после синков |
| `cleanup_orphan_cache` | удаление кэша отключённых/удалённых локаций | 24 ч |
| `verify_providers` | валидация провайдеров через `fetch_structure_info` | 60 мин |
| `notify_deficit` | Discord-уведомления `deficit`, `contracts_deficit`, `ships_deficit` | — |

Цепочка: `sync_*` → `recalc_stock_levels` → (`trigger=beat`) → `notify_deficit`. Триггер (`beat`/`manual`) передаётся аргументами и фиксируется в `SyncRun`. Уведомления о дефиците при `manual` не отправляются; системные события (`provider_acl_lost`, `sync_failed`, `sync_recovered`) отправляются независимо от триггера. Все задачи идемпотентны: перезапись кэша по источнику, полная замена агрегатов, дедупликация уведомлений через `NotificationLog`. Guard-интервалы проверяются самой задачей (beat дёргает часто — интервалы меняются в настройках без правки beat). Рыночные синки `sync_jita_book`, `sync_region_orders`, `sync_structure_orders` и синк контрактов `sync_contracts` включены в beat, при этом `sync_jita_book` — условный (см. эррату ниже).

**Ручная цепочка и cleanup Jita.** «Обновить сейчас» ставит `sync_region_orders`, `sync_structure_orders`, `sync_contracts` и затем `recalc_stock_levels` (уведомления о дефиците при `trigger=manual` не отправляются). `sync_jita_book` добавляется в цепочку только при включённой локации Jita IV-4: при выключенной/удалённой Jita ручная цепочка состоит из **3 задач вместо 4**, без `sync_jita_book`. В beat задача остаётся в расписании, но при отсутствии включённой локации Jita IV-4 не делает ни одного ESI-запроса. Если локация удалена или отключена, The Forge **жёстко исключается** из набора регионов §9.4 (`get_sync_region_ids()` применяет discard после всех источников — в том числе после явных `scope_kind='region'/'system'` у фита/компонента и регионов, полученных из SDE), стакан и кэш чистятся: удаляются `JitaOrderLevel` и `MarketOrderCache(region, jita_region_id)` (и в `sync_jita_book`, и в ежедневной очистке). При выключенной Jita недоступна **цена докупки**: `unit`, `ref`, `cost`, `shortfall`, `overprice_pct` не показываются (`&mdash;` / `None`), `Fit.total_cost=None`, бейджи `OVERPRICE` и экспорт закупки пусты. Статус фита, `K`, `D_i` и контракты продолжают считаться по добавленным/включённым локальным рынкам; полный `NO_DATA` остаётся только при пустой области `L(S)` (§6.7, решение №45), а локация без данных даёт нулевой вклад и предупреждение с id.

Очистка при отключённой Jita не дожидается 24-часового интервала `cleanup_orphan_cache`: `sync_jita_book` удаляет стакан и кэш региона до guard-интервала и до любого ESI-запроса, без запуска цепочки пересчёта; ежедневная задача делает ту же чистку как страховку. Условие проверяется селектором `is_jita_enabled()`: одним запросом `.exists()` по `MarketLocation(enabled=true, kind=station, station_id=jita_station_id)`.

**Эррата к решению №37 §20 ТЗ.** Решение сохраняется без изменений: `sync_corp_structures` работает по тому же эндпоинту `GET /corporations/{corporation_id}/structures/` раз в сутки (beat), создаёт новые `MarketLocation(kind=structure)` с `enabled=false` (включение вручную в Locations UI) и обрабатывает ACL-сбои по §11. В тексте ТЗ указан опечатанный идентификатор `esi-corporations.read_corporation_structures.v1` — такого scope не существует, SSO/CharLink отклоняет его как `invalid_scope`. В коде и CharLink используется корректный scope из официальной OpenAPI-спеки ESI: `esi-corporations.read_structures.v1`. Решения №1–44 не пересматриваются; по явному указанию заказчика в §20 добавлены только решения №45/№46, меняющие обработку отсутствия данных.

**Эррата к §9.4 и решениям №3/8/25/39 §20 ТЗ (условный синк Jita).** Решения №1–44 не пересматриваются: Jita — The Forge, станция 60003760 (`jita_station_id`, настраивается, №3); обновление Jita и доступности — Celery с настраиваемыми интервалами (№8); оверпрайс — по минимальной sell-цене Jita и порогу-проценту из настроек (№25); Jita — только источник цен докупки, а `K` считается по `L(S)` (№39). Буква §9.4 требует безусловно включать регион Jita в объединение регионов синка; по явному указанию заказчика The Forge (`jita_region_id`) входит в регионы синка и `sync_jita_book` запускается только если локация Jita IV-4 (`MarketLocation(kind=station, station_id=jita_station_id)`) добавлена и включена в Locations UI. При удалённой/отключённой Jita ESI-запросов к рынку Jita нет ни в beat, ни при «Обновить сейчас»: `get_sync_region_ids()` жёстко выбрасывает The Forge даже при явном `scope_kind='region'/'system'`, `JitaOrderLevel` и `MarketOrderCache(region, jita_region_id)` удаляются, цена докупки недоступна (`unit/ref/cost/shortfall/overprice_pct` — `None`/`&mdash;`, `Fit.total_cost=None`, OVERPRICE и экспорт закупки пусты), но статус фита, `K`, `D_i` и контракты считаются по локальным рынкам; полный `NO_DATA` — только при пустой области `L(S)` (§6.7, решение №45), локация без данных даёт нулевой вклад и предупреждение с id. При выключенной Jita ручная цепочка состоит из 3 задач вместо 4 (без `sync_jita_book`; при включённой — 4). Решения №1–44 не пересматриваются; по явному указанию заказчика в §20 добавлены только решения №45/№46, меняющие обработку отсутствия данных.

## Расчётное ядро (`calc/engine.py`)

Чистые функции над `Decimal` (float в денежных расчётах запрещён, округление ISK — `ROUND_HALF_UP` до целого):

- **Область L(S)** (§6.1, §5.1): `station` → строго одна локация `r` (добавление других станций в Locations её не расширяет); `system`/`region` → NPC-станции (региональный кэш) + включённые структуры (кэш структур). `order_range` при агрегации не используется — заказ лежит на своей станции.
- **Доступность A** (§6.2): сумма sell-объёмов типа по локациям области (`StockLevel.total_sell_volume`).
- **Собираемость K** (§6.3): `K = min_i floor(A_i / q_i)` по всем компонентам, включая корпус (`q=1`); узкие места — компоненты с `fits_i = K`.
- **Комплектность контрактов C** (§6.12): число полных комплектов фита в кэшированных открытых `item_exchange` контрактах области; отдельный контрактный статус `ok|deficit|no_data`. C не смешивается с рыночным K и не меняет формулы §6.1–§6.11.
- **Дефицит D_i** (§6.5): `max(0, N·q_i − A_i)`; считается независимо по каждому фиту.
- **Цена покрытия unit** (§6.6): покрывающий лот по стакану Jita либо средневзвешенная до покрытия; при недостатке — `NOT_COVERED` (считается только `covered`, остаток — `shortfall`). Если локация Jita IV-4 отключена/удалена, стакан не читается: `unit`, `ref`, `cost`, `shortfall`, `overprice_pct` — `None`/`&mdash;`, а не устаревшая цена.
- **Оверпрайс** (§6.8): `unit > ref·(1+τ/100)`, где `ref` — минимальная sell-цена Jita, τ = `overprice_threshold_pct`; при недоступной цене Jita бейдж `OVERPRICE` не выставляется.
- **Стоимость** (§6.9): `cost_i = round((unit·(1+m) + delivery_rate·pvol_i) · D_i)`; доставка без наценки, объём — `packaged_volume`; Total — сумма уже округлённых строк. При выключенной Jita `Fit.total_cost=None`, экспорт закупки пуст.
- **Статусы** (§6.4, решение №45): `NO_DATA` → `HULL_MISSING` → `DEFICIT` → `OK` (первый подходящий). `NO_DATA` назначается только при пустой области `L(S)`: при нём дефицит, стоимость и `C+K` не считаются, уведомление не отправляется, в UI — причина и баннер. Отсутствие данных по отдельной локации статус фита не меняет: её вклад в доступность 0, остальные локации области считаются по правилам этой таблицы, в причинах пересчёта и UI — предупреждение с id локации и причиной. Отсутствие Jita само по себе `NO_DATA` не даёт: статус фита, `K`, `D_i` и контракты считаются по локальным рынкам.

**Свежесть: два независимых канала (§6.7, решения №45/46).** NPC-станции получают данные только региональным синком (`sync_region_orders` → `recalc_stock_levels`): свежесть подтверждается свежей строкой `StockLevel` по каждой нужной станции (проверка `updated_at` за `data_stale_after_min`), а не статусом записи `MarketLocation`. Поэтому только что добавленная станция с `sync_status=stale` и пустым `last_synced_at` не даёт предупреждения, если по ней есть свежий `StockLevel`; если строки нет — её вклад в доступность 0 и в причинах пересчёта/UI показывается предупреждение с id станции. Структуры получают данные только структурным синком (`sync_structure_orders` через `StructureProvider`): обязательны `sync_status=ok`, доступный провайдер и непротухший `last_synced_at`; региональная проверка к структурам не применяется. При `failed`/`partial`/`no_provider`/`stale`/отсутствии `StockLevel` вклад структуры в доступность 0 и показывается предупреждение с id структуры.

Отсутствие данных по локации **не блокирует расчёт** и не переводит фит в `NO_DATA`: дефицит $D_i$, цена покрытия, стоимость, `K`, `C+K` и уведомления считаются по общему правилу по всем локациям области с нулевым вкладом проблемных (§6.4, §6.7, решение №45). `NO_DATA` сохраняется только для пустой области `L(S)`.

При успешном **полном** синке локации, по которой заказов нет, `recalc_stock_levels` пишет нулевые агрегаты `StockLevel` для нужных типов: список локаций, включая пустые, передаётся явно; при исчезновении заказов старые строки удаляются полной заменой в одной транзакции. `partial`-синк нулевые агрегаты не пишет, поэтому локация продолжает давать 0 с предупреждением до успешного полного синка (решение №46).
- **Итог доступных кораблей** (§6.13): `calc_ships_total`, `calc_ships_deficit` и `calc_ships_availability` в `calc/engine.py` дают `ships_total = C + K`, `ships_deficit = max(0, N − ships_total)` и общий статус. Чистые функции над примитивами и `Decimal`, без БД и сети (решение №44).

## Сводка доступных кораблей C + K (§6.13)

Рыночная часть итога — `K` из §6.3, посчитанная по локациям области фита `L(S)`. **Jita остаётся только источником цен для докупки** (решение №39): при включённой локации Jita IV-4 стакан участвует в `unit`, `ref`, `OVERPRICE` и `cost`, но не прибавляется к доступности; если фит явно скоупится на The Forge, заказы Jita попадают в `L(S)` как обычная локация через региональный кэш и учитываются в `K` на общих основаниях. При отключённой/удалённой Jita The Forge жёстко исключается из регионов синка, поэтому в `L(S)` нет данных Jita и цена докупки недоступна; `C`, `K` и позиционные дефициты `D_i` продолжают считаться по добавленным/включённым локальным рынкам (см. эррату к §9.4 и решениям №3/8/25/39 выше).

Чистые функции ядра оперируют уже посчитанными `C`, `K`, `N` и статусами источников:

- `ships_total = C + K`;
- `ships_deficit = max(0, N − ships_total)`;
- `ships_status`: `no_data`, если `C` или `K` имеет статус `no_data`; иначе `deficit`, если `ships_deficit > 0`; иначе `ok`.

Приоритет общего статуса: `no_data` > `deficit` > `ok`. Он не подменяет рыночный статус §6.4 и контрактный статус §6.12 — в UI показываются все три бейджа. При `no_data` любого источника `ships_deficit` не вычисляется, уведомление не отправляется, фит исключается из сводных сумм (решение №43).

В `Fit` добавляется только поле `ships_status_cache` (`ok|deficit|no_data`, default `no_data`); `ships_deficit` не хранится и вычисляется при чтении/пересчёте (решение №44). Сводная панель Dashboard показывает отдельные `∑C`, `∑K` и `∑(C+K)` только по фитам с известными обеими величинами; `∑K` подписан как **per fit** (сумма независимых оценок по фитам, а не уникальных модулей на рынке). `D_i`, цена покрытия, стоимость докупки, `Total` и экспорт закупки остаются по §6.5–§6.9 без изменений (решение №41), кроме частного случая выключенной Jita, описанного в эррате выше (цена докупки недоступна).

**Событие `ships_deficit`** (§13, решение №42): отправляется только при `trigger=beat` и `ships_status_cache='deficit'`. Состояние для дедупликации — `H(fit_id ‖ ships_status ‖ ships_deficit)`; отправка при изменении хэша либо не чаще `notification_deficit_cooldown_min`. Хэши `deficit` и `contracts_deficit` не изменяются, события пишутся в `NotificationLog` с собственным `event`. Ручной запуск событие не порождает. Управление — теми же `notification_deficit_enabled` и `notification_deficit_cooldown_min`, что и у `deficit` (см. [configuration.md](configuration.md)).

## Провайдеры структур (§11)

`StructureProvider` — персонаж AA (`eveonline.EveCharacter`, PROTECT); токены не хранятся, используются токены AA по `character_id`. Выбор: `enabled=true`, не `disabled`, не в активном cooldown — по возрастанию `priority`, затем `character_id`. 403/401 → счётчик + cooldown (`provider_acl_cooldown_min`), структура переобрабатывается следующим провайдером в том же прогоне; после `provider_acl_max_failures` сбоев — `disabled` + алерт `provider_acl_lost`; успех сбрасывает счётчик. Провайдеров нет → локация `no_provider` даёт вклад 0 в доступность; фиты считаются по оставшимся локациям, в UI — предупреждение с id локации, `NO_DATA` — только при пустой области (§6.7, решение №45). CharLink запрашивает скоупы `esi-markets.structure_markets.v1`, `esi-universe.read_structures.v1` и `esi-search.search_structures.v1`; при привязке проверяются `esi-markets.structure_markets.v1` и `esi-universe.read_structures.v1`. Для `ContractProvider` CharLink запрашивает `esi-contracts.read_corporation_contracts.v1`, `esi-contracts.read_character_contracts.v1` и `esi-corporations.read_structures.v1` (последний — для `sync_corp_structures`, см. эррату к решению №37 выше).

## Модель данных (§8)

| Модель | Назначение |
|---|---|
| `MarketLocation` | локация мониторинга (station/structure), `enabled`, `sync_status`, `last_synced_at` |
| `StructureProvider` | FK → `eveonline.EveCharacter`; `priority`, `status` (ok/cooling_down/disabled), `acl_fail_count`, `cooldown_until` |
| `Fit` | имя, `hull_type_id`, `eft_raw`, `qty_target` (N), `scope_kind`/`scope_ref`, `status_cache`, `fits_available` (K), `contracts_status_cache`, `contracts_available` (C), `ships_status_cache`, `total_cost`, `overprice_count` |
| `FitComponent` | FK → Fit; `type_id`, `qty_per_fit`, `is_charge`, опциональный override области; UNIQUE (fit, type_id) |
| `ContractProvider` | FK → `eveonline.EveCharacter`; `source_kind` (corporation/character), `priority`, `status`, `sync_status`, счётчики ACL-сбоев |
| `ContractCache` / `ContractItemCache` | кэш открытых контрактов и их позиций; дедупликация по `contract_id` (решение №32) |
| `ComponentRequest` | компонент вне фита: тип, N, область, заметка |
| `MarketOrderCache` | сырые sell-заказы по источнику (region/structure) с индексами (type_id, location_id) и (source_kind, source_ref) |
| `StockLevel` | агрегат sell по (location_id, type_id), UNIQUE |
| `JitaOrderLevel` | снимок стакана Jita, UNIQUE (type_id, rank); удаляется при отключённой/удалённой локации Jita IV-4 |
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
├── calc/engine.py     # чистые функции расчёта (L(S), A, K, D, C+K, ships_deficit); calc/service.py — связка с БД
├── tasks.py           # задачи Celery §12
├── notify/discord.py  # webhook, embed, дедупликация
├── admin.py           # SyncRun, локации, провайдеры (superuser)
├── templatetags/mm_tags.py
├── templates/marketmonitoring/
├── migrations/
└── tests/             # парсер, calc-ядро, orderbook, провайдеры, задачи, права, экспорт, сводка кораблей (группа 9 §18)
```

## Известные ограничения

- **История цен не хранится** — только текущий срез (`MarketOrderCache` перезаписывается по источнику); графики не предусмотрены (§1.2).
- **Один Discord-канал** — один `webhook_url`, несколько каналов не поддерживаются.
- Логи задач доступны только через Django-admin (суперпользователю), UI-страницы логов нет.
- Локализация: этап 1 — английские имена типов (§15); UI-строки через gettext, экспорт закупки всегда на английском; парсер EFT принимает имена на любом языке SDE.
- Цена докупки требует включённой локации Jita IV-4: без неё стакан не синкается, кэш очищается; карточка фита и Dashboard показывают `&mdash;` вместо `0 ISK`, `Fit.total_cost=None`, экспорт закупки пуст, уведомления о дефиците не содержат стоимость. Статус/`K`/`D_i`/контракты считаются по локальным рынкам, полный `NO_DATA` — только при пустой области `L(S)` (§6.7, решение №45); локация без данных даёт нулевой вклад и предупреждение с id.
- ESI кэширует ответы (регион ≈ 5 мин, структуры ≈ 30 мин) — данные могут отставать на это время.
