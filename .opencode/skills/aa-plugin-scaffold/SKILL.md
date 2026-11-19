---
name: AA Plugin Scaffold
description: Каркас плагина Alliance Auth 5 (пакет, pyproject, AppConfig, модели, миграции, группы прав, меню) для проекта aa-market-monitoring. Загружать при создании файлов каркаса, моделей или auth_hooks.
---

## Контекст проекта

Плагин `marketmonitoring` для Alliance Auth 5.x, Django 4.2, Python 3.11, PostgreSQL 15+.
Полное ТЗ: `docs/TZ.md` (модели — §8, структура репозитория — §17, журнал решений — §20 — не пересматривать).

## Правила каркаса

1. Пакет Python — `marketmonitoring`; в AA `INSTALLED_APPS` добавляется после allianceauth-приложений и `eveuniverse`.
2. `apps.py` — AppConfig с `verbose_name`, в `ready()` подключение меню (navhookspec) и создание групп прав.
3. Группы прав миграцией данных: `market_monitoring_view`, `market_monitoring_edit` (`_edit` подразумевает `_view`, суперпользователь — всё).
4. `auth_hooks.py` — только хелперы `has_view(user)` / `has_edit(user)`: суперпользователь или членство в группе. Никакой другой логики прав в views.
5. Модели — строго §8 ТЗ: денежные поля `DecimalField(max_digits=20, decimal_places=2)`, количества целые, все указанные UNIQUE-констрейнты и индексы (`Fit(status_cache)`, `StockLevel(type_id, location_id)`, `JitaOrderLevel(type_id, rank)`, `MarketOrderCache(source_kind, source_ref)`).
6. Справочники типов/станций/систем/регионов/объёмов — только из `eveuniverse` (SDE). Свои копии SDE запрещены.
7. `pyproject.toml` по §17: dependencies = `allianceauth>=5.0,<6.0`, `django-eveuniverse>=1.5`, `requests>=2.31`; black line-length 120, isort profile black.

## Запреты

- `float` в денежных расчётах (только `Decimal`), `print` вместо логгера (`logging.getLogger("marketmonitoring")`).
- Ничего из §1.2 ТЗ (не входит в объём) не реализовывать.
- Имена в коде — английские; комментарии к публичным функциям — русские.

## Проверка перед сдачей

`python manage.py makemigrations --check`; миграции должны применяться и на чистой, и на существующей БД.
