# Установка aa-market-monitoring

Плагин устанавливается в существующий инстанс Alliance Auth 5.x.

## Зависимости

Из `pyproject.toml` (устанавливаются автоматически через `pip`):

- `allianceauth>=5.0,<6.0`
- `django-eveuniverse>=1.5` — справочники SDE (типы, станции, системы, регионы, `packaged_volume`)
- `requests>=2.31` — Discord webhook

Также требуются стандартные компоненты AA: `django-esi` (поставляется с AA 5), Celery, Redis, PostgreSQL 15+.

## Шаги

### 1. Установка пакета

```sh
pip install aa-market-monitoring
```

### 2. INSTALLED_APPS

Добавьте `"marketmonitoring"` в `INSTALLED_APPS` — **после** приложений `allianceauth` (включая `esi`, `eveonline`) и `eveuniverse`:

```python
INSTALLED_APPS = [
    # ... стандартные приложения AA ...
    "allianceauth.eveonline",
    "esi",
    "django_eveuniverse",
    # ... остальные приложения ...
    "marketmonitoring",
]
```

Порядок важен: плагин ссылается на модели `eveonline.EveCharacter` (персонажи-провайдеры) и `django_eveuniverse` (SDE-справочники).

### 3. Миграции

```sh
python manage.py migrate
```

Миграции применяются и на чистой БД, и на существующей БД AA. Миграция данных `0002_create_groups` создаёт группы прав `market_monitoring_view` и `market_monitoring_edit`; повторное создание групп также выполняется `post_migrate`-обработчиком `AppConfig`.

Проверка, что незапланированных миграций нет:

```sh
python manage.py makemigrations --check
```

### 4. Настройка ESI (django-esi)

Плагин использует стандартный механизм `django-esi`. Для работы с публичными эндпоинтами (Jita, регионы) токен не нужен. Для синка заказов структур зарегистрируйте ESI-приложение как обычно для AA:

1. https://developers.eveonline.com → Create New Application.
2. Type: Authentication + API Access.
3. Scopes: плагин сам запрашивает нужные скоупы через токены персонажей (`esi-markets.structure_markets.v1`, `esi-universe.read_structures.v1` — проверяются при привязке провайдера); типовые скоупы AA приложения оставьте стандартными.
4. Callback URL — стандартный для вашей AA (`https://<ваш-auth>/sso/callback`).
5. Пропишите в `settings.py`:

```python
ESI_SSO_CLIENT_ID = "..."
ESI_SSO_CLIENT_SECRET = "..."
ESI_SSO_CALLBACK_URL = "https://<ваш-auth>/sso/callback"
```

### 5. Celery / beat

Beat дёргает `sync_jita_book`, `sync_region_orders`, `sync_structure_orders`, `sync_contracts`, `verify_providers` и `notify_deficit` **каждые 5 минут**; `cleanup_orphan_cache` и `sync_corp_structures` — ежедневно. Каждая задача сама проверяет, истёк ли её интервал (`jita_sync_interval_min`, `stock_sync_interval_min`, `contracts_sync_interval_min` — из настроек плагина; 60 мин для `verify_providers`, 24 ч для ежедневных задач), поэтому интервалы меняются в UI без правки beat-расписания. Частый beat — намеренное решение ТЗ (§12.2): расписание остаётся фиксированным, а логика частоты живёт в настройках. Готовое расписание даёт фабрика `marketmonitoring.tasks.build_beat_schedule()` (см. [README.md](../README.md)).

Пример beat-расписания (обычно задаётся в `beat_schedule` проекта AA):

```python
from celery.schedules import crontab

CELERYBEAT_SCHEDULE["marketmonitoring_sync"] = {
    "task": "marketmonitoring.sync_jita_book",  # см. ниже — все задачи синка
    "schedule": 300.0,
}
```

На практике удобно завести одну запись на каждую задачу с именами:

- `marketmonitoring.sync_jita_book`
- `marketmonitoring.sync_region_orders`
- `marketmonitoring.sync_structure_orders`
- `marketmonitoring.sync_contracts`
- `marketmonitoring.verify_providers`
- `marketmonitoring.notify_deficit` — вызывается beat-тиком каждые 5 минут, но отправляет уведомления только при `trigger=beat`
- `marketmonitoring.cleanup_orphan_cache` — ежедневно (интервал 24 ч внутри задачи)
- `marketmonitoring.sync_corp_structures` — ежедневно (решение №37 §20, guard 24 ч внутри задачи)

Цепочка: `sync_*` → `recalc_stock_levels` → `notify_deficit`. Уведомления о дефиците (`deficit`, `contracts_deficit`, `ships_deficit`) отправляются только при `trigger=beat`; ручной запуск их не порождает (§12.4, этап 9 §21). Фабрика `build_beat_schedule()` уже включает `notify_deficit` наравне с задачами синка: при её использовании отдельная запись не требуется, а при собственном `CELERYBEAT_SCHEDULE` задачу нужно прописать явно (см. [README.md](../README.md)).

### 6. Проверка установки

```sh
python manage.py check
```

Признаки успешной установки:

- в меню AA появились пункты **Market Monitoring** и **Components** (видны при наличии права view) и **Locations**/**Settings** (видны при `_edit`);
- `python manage.py migrate` проходит без ошибок на чистой и существующей БД;
- в `/admin/` доступны модели плагина (Market locations, Structure providers, Sync runs и др. — суперпользователю).

## Обновление

```sh
pip install -U aa-market-monitoring
python manage.py migrate
python manage.py collectstatic --noinput
```

Перезапуск Celery worker/beat — как обычно для AA. Обратных миграций с потерей данных нет; миграции применяются на существующей БД.
