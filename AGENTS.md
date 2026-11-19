# AGENTS.md

## Что это

Плагин `marketmonitoring` для **Alliance Auth 5.x** (Django 4.2, Python 3.11, PostgreSQL 15+, Redis/Celery). Плагин ещё не реализован — единственный источник требований: **`docs/TZ.md`**. Читай его перед любой задачей.

## Жёсткие правила ТЗ (нарушения = переделка)

- Решения в §20 «Журнал решений» **не пересматривать**; объём — строго §1, ничего из §1.2 (исключено) не добавлять.
- Все формулы — только в `calc/engine.py` как чистые функции (вход — примитивы и `Decimal`, без БД/сети).
- **Запрещены:** `float` в денежных расчётах (только `Decimal`, ROUND_HALF_UP до целого ISK), N+1 в списках, запросы ESI в цикле по компонентам, `print` вместо логгера.
- **UI никогда не обращается к ESI** — только Celery-задачи через `esi/client.py`. Страницы читают только БД плагина.
- Токены ESI — из AA (`eveonline.Character`); плагин токены не хранит. Справочники SDE — только через `django-eveuniverse`, своих копий нет.
- Язык: имена в коде — английские, комментарии к публичным функциям и документация — русские.

## Ключевые инварианты предметной области (§5)

- Строгий scope: станция — только заказы этой станции; `order_range` при агрегации не используется.
- Корпус обязателен: нет корпуса в области → `HULL_MISSING`; при `NO_DATA` дефицит/стоимость не считаются.
- Дефицит считается независимо по каждому фиту (без схлопывания потребностей).
- Уведомления о дефиците — только при `trigger=beat`; ручной запуск их не порождает.

## Команды

```sh
python manage.py makemigrations --check   # перед сдачей — обязательно
python manage.py test marketmonitoring    # тесты
```

- Тесты выполняются **без сети**: ESI/webhook — через `responses`/`unittest.mock`.
- Линт: black + isort, line-length 120.
- Миграции должны применяться и на чистой, и на существующей БД.

## Локальная конфигурация OpenCode

- `.opencode/skills/` — скиллы `aa-plugin-scaffold`, `aa-calc-engine`, `aa-esi-client`, `aa-tests`, `aa-acceptance-check` (загружать при работе с соответствующей областью).
- `.opencode/agents/` — субагенты `aa-backend-developer`, `aa-tester`, `aa-reviewer`, `aa-docs-writer`, `aa-orchestrator` (reviewer/docs-writer — read-only).

## Рабочий процесс

- Этапы реализации — §21 ТЗ; issues в Gitea (git.misyagin.com) помечены `stage-1`…`stage-8`.
- Definition of Done этапа: тесты §18 зелёные, `makemigrations --check`, линт, критерии приёмки §19. Финальная проверка — скилл `aa-acceptance-check`.
