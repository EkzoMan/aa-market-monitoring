# aa-market-monitoring

Плагин **Market Monitoring** для [Alliance Auth](https://github.com/allianceauth/allianceauth) 5.x. Мониторит доступность компонентов и фитов на заданных рынках (станции/структуры/системы/регионы), считает, сколько фитов можно собрать «прямо сейчас», и рассчитывает стоимость докупки недостающего в Jita с наценкой и доставкой.

Требования зафиксированы в [docs/TZ.md](docs/TZ.md) (версия 1.2).

## Возможности

- Справочник локаций мониторинга (из SDE через `django-eveuniverse`), включение/выключение.
- Фиты: вставка EFT-текста из игры (русский и английский клиент), предпросмотр разбора, ручная правка состава, целевое количество N, область доступности, per-component override области.
- Отдельные компоненты вне фитов: тип, целевое количество, область.
- Кэширование рыночных и контрактных данных ESI в таблицах плагина; UI читает **только** БД, никогда не обращается к ESI.
- Расчёт собираемости K, дефицита D по каждому компоненту, цены покрытия по стакану Jita, стоимости докупки, флагов `OVERPRICE` / `NOT_COVERED`, статусов `OK` / `DEFICIT` / `HULL_MISSING` / `NO_DATA`.
- Проверка полных комплектов кораблей фита в открытых `item_exchange` контрактах (корпоративных, альянсовых и персональных): C из N.
- Сводка доступных кораблей C + K: отдельные счётчики C (контракты) и K (рынок в области фита), агрегатный дефицит `max(0, N-(C+K))` и общий статус `ships_status_cache`.
- Фоновые задачи Celery (синк Jita, регионов, структур, контрактов, пересчёт, очистка кэша, проверка провайдеров) + ручной запуск обновления из UI.
- Пул провайдеров структур и контрактов (персонажи AA): приоритет, cooldown при ACL-сбое, автоматическое отключение после серии сбоев.
- Discord webhook (один канал): дефицит (`deficit`, `contracts_deficit`, `ships_deficit`, только при beat), `HULL_MISSING`, сбои провайдеров, сбой и восстановление синков.
- Экспорт списка закупки: буфер обмена (multibuy), `.txt`, `.csv`.
- Роли `market_monitoring_view` / `market_monitoring_edit`.
- i18n-каркас (этап 1 — английские названия типов; экспорт всегда на английском).

## Сводка доступных кораблей (C + K)

- **C** — число полных комплектов фита в открытых, не истёкших `item_exchange` контрактах области фита (свежие данные провайдеров контрактов).
- **K** — рыночная собираемость в области `L(S)` (станция / система / регион со включёнными структурами).
- Итог — `C + K`; агрегатный дефицит — `max(0, N-(C+K))`; общий статус хранится в `ships_status_cache` (`ok` / `deficit` / `no_data`). Jita остаётся только источником цен докупки и не добавляет доступность, если фит явно не скоупится на The Forge.

В строке фита и в сводной панели Dashboard показываются отдельные `C`, `K` и `C+K`. Счётчик `K` подписан как **per fit**: это сумма независимых пер-фитных оценок, а не количество уникальных модулей на рынке. Фиты, у которых `C` или `K` имеет статус `no_data`, исключаются из сводных сумм; в UI вместо итога показывается «—». Позиционные дефициты `D_i`, цена покрытия, стоимость докупки и экспорт закупки при этом не меняются и считаются по тем же правилам, что и раньше.

Новое Discord-событие `ships_deficit` отправляется только при автоматической (beat) проверке и `ships_status_cache='deficit'`; ручной запуск («Обновить сейчас») его не порождает. Событие делит существующие настройки `notification_deficit_enabled` и `notification_deficit_cooldown_min` с `deficit` (кулдаун — также с `contracts_deficit`); отдельных настроек для него нет. См. [docs/configuration.md](docs/configuration.md).

## Требования

- Alliance Auth 5.x
- Django 4.2+ (в тестовом окружении допустим Django 5.x)
- Python 3.11+
- PostgreSQL 15+
- Redis / Celery (стандартная инфраструктура AA)

## Установка

```sh
pip install aa-market-monitoring
```

В `INSTALLED_APPS` локального `settings.py` добавьте `"marketmonitoring"` — **после** приложений `allianceauth` (в т.ч. `esi`, `eveonline`) и `django-eveuniverse`:

```python
INSTALLED_APPS += [
    "marketmonitoring",
]
```

Примените миграции:

```sh
python manage.py migrate
```

Группы прав `market_monitoring_view` и `market_monitoring_edit` создаются автоматически миграцией данных (см. [docs/configuration.md](docs/configuration.md)).

Настройка наценки/порогов, привязка провайдеров структур и контрактов, а также диагностика `NO_DATA` описаны в [docs/configuration.md](docs/configuration.md).

## Beat-расписание (§12.2)

Эталонное расписание Celery beat — фабрика `marketmonitoring.tasks.build_beat_schedule()`:

- каждые 5 минут: `sync_jita_book`, `sync_region_orders`, `sync_structure_orders`, `sync_contracts`, `verify_providers`, `notify_deficit`;
- ежедневно: `cleanup_orphan_cache`, `sync_corp_structures`.

Фактические интервалы выполнения проверяются guard'ами задач (§10, §12.2): guard-интервалы задач и настройки `jita_sync_interval_min` / `stock_sync_interval_min` / `contracts_sync_interval_min` не позволят задаче выполниться чаще разрешённого, даже при более частом beat-тике.

## Документация

- [docs/installation.md](docs/installation.md) — детальная установка, Celery/beat, ESI-приложение
- [docs/configuration.md](docs/configuration.md) — все настройки, роли, провайдеры, Discord webhook, диагностика `NO_DATA`
- [docs/architecture.md](docs/architecture.md) — архитектура, поток данных, расчётное ядро
- [docs/TZ.md](docs/TZ.md) — техническое задание (источник требований)
