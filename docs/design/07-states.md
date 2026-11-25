# 07. Состояния

## 1. Статусы фита (§6.4, решение №45)

| Статус | Чип | Прогресс | Сумма | Поведение |
|---|---|---|---|---|
| `OK` | `text-bg-success` «OK» | зелёный, K/N | показывается | — |
| `DEFICIT` | `text-bg-warning` «DEFICIT» | оранжевый | показывается | счётчик оверпрайса может быть > 0 |
| `HULL_MISSING` | `text-bg-danger` «HULL_MISSING» | не рисуется | «—» (дефицит не определён) | строка корпуса в таблице — отсутствует/пометка; подсказка «hull not found in scope X» |
| `NO_DATA` | `text-bg-secondary` «NO_DATA» | не рисуется | **«—», цифры запрещены** | баннер с причиной; только пустая область `L(S)=∅` |

Отдельная локация без данных не меняет статус фита: её вклад в доступность 0, фит получает `OK`/`DEFICIT`/`HULL_MISSING` по оставшимся локациям, а в карточке и на дашборде показывается предупреждение с id локации и причиной (`failed`/`partial`/`no_provider`/`stale`/нет `StockLevel`).

Чипы — одна колонка «Status» на дашборде; в карточке фита чип стоит в шапке.

### 1.1. Общий статус доступных кораблей (`ships_status_cache`)

Отдельный бейдж рядом с рыночным и контрактным; не заменяет их.

| Значение | Условие | Чип | Итог C+K |
|---|---|---|---|
| `ok` | обе величины известны и `ships_deficit = 0` | `text-bg-success` «ok» | показывается |
| `deficit` | обе величины известны и `ships_deficit > 0` | `text-bg-warning` «deficit» | показывается + «Missing N ships» |
| `no_data` | пустая область рыночного скоупа (`L(S)=∅`) или нет данных контрактов | `text-bg-secondary` «no_data» | «—», фит исключён из `∑C`, `∑K`, `∑(C+K)` |

**Приоритет общего статуса:** `no_data` > `deficit` > `ok`. Он не подменяет приоритет
рыночного статуса §6.4 (`NO_DATA` → `HULL_MISSING` → `DEFICIT` → `OK`) и не меняет
контрактный статус §6.12 (`no_data` / `deficit` / `ok`): все три бейджа видны
одновременно. Если один источник `no_data`, общий статус — `no_data`, даже когда
другой источник `ok`. При `no_data` агрегатный дефицит не считается и уведомление
`ships_deficit` не отправляется (только beat и только при `deficit`).

Локация рынка без данных не приравнивается к `no_data` у `K`: она даёт нулевой вклад в доступность, фит остаётся в сводных суммах, если обе величины известны (решения №43, №45; §19.18).

## 2. `NO_DATA`-баннер и предупреждение по локации

**`NO_DATA` — только пустая область `L(S)=∅`:**

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ⓘ Scope resolves to no locations (area: ...).                            │  alert-secondary
│   Deficit, cost and C+K are unavailable. Check [Locations].              │
└──────────────────────────────────────────────────────────────────────────┘
```

- Серый (secondary), не красный; иконка `circle-info`; ссылка на «Locations» — только
  для ролей с доступом к разделу (edit), для view — без ссылки.
- Причины: область системы/региона без NPC-станций и включённых структур либо
  неизвестный `scope_ref`. Свежесть локальных локаций и `no_provider` причинами
  `NO_DATA` не являются.

**Предупреждение по локации (статус фита не меняется):**

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ⚠ Location 70000001 (structure) has no fresh data (status: failed).      │  alert-warning
│   Its contribution to availability is 0; deficit and cost are            │
│   calculated from the other locations. Check [Locations].                │
└──────────────────────────────────────────────────────────────────────────┘
```

- Жёлтый (`alert-warning`), иконка `triangle-exclamation`, id локации обязателен;
  показывается в карточке фита, строке дашборда и в причинах пересчёта.
- Категории причин: `no_provider` / `failed` / `partial` / `stale` / `no_stock`
  (нет строки `StockLevel`). Для NPC-станции со свежим региональным `StockLevel`
  предупреждения нет, даже если чип `stale` и `last_synced_at` пуст.
- Предупреждение **не** подменяет чип статуса и не обнуляет показываемые $D_i$,
  стоимость и `C+K`; фит остаётся в сводке, если обе величины известны
  (§6.4, §6.7, решения №45/46).

## 3. Поллинг обновления (`refresh_status`)

Индикатор в шапке дашборда (и рядом с кнопкой на карточке фита, если refresh доступен):

| Состояние | UI |
|---|---|
| idle | кнопка «Refresh now» (edit), индикатора нет |
| обновляется | кнопка disabled + `spinner-border spinner-border-sm`, текст «Updating data…»; повторный заход на страницу при `in_progress` — сразу это состояние (уже реализовано) |
| успех | toast/alert-success «Update finished», авто-reload списка |
| ошибка | alert-danger «Update failed — see details in sync log», кнопка снова активна; retry — тот же «Refresh now» |

- Vanilla `fetch` + `setTimeout(3000)` (текущий скрипт), тайм-аут поллинга 5 мин —
  показать нейтральное «Still running in background; reload later», не ошибку.
- Индикатор доступен роли view тоже (поллинг read-only JSON, но кнопка скрыта;
  во время чужого запуска view видит «Updating data…» без кнопки).

## 4. Пустые состояния

| Место | Текст (gettext, en) | CTA |
|---|---|---|
| Dashboard, нет фитов | «No fits yet. Paste an EFT to start tracking.» | (edit) `[+ New fit]`; view — «Ask your logistics manager to add fits.» |
| Dashboard, фильтр отсеял всё | «Nothing matches the current filters.» | `[Clear filters]` |
| Components, пусто | «No standalone components yet.» | (edit) `[+ New component]` |
| Components, всё в норме | «All components are stocked. Nothing to buy.» | — (не ошибка, зелёная галка-иконка) |
| Locations, пусто | «No locations yet. Add a location so fits can be priced.» | `[+ Add location]` |

Паттерн: центрированный блок, иконка `fa-box-open`/`fa-location-dot` в
`text-body-secondary` 2×, одна строка текста, одна кнопка. Без иллюстраций.

## 5. Confirm-диалоги (destructive)

- Фиты и компоненты — **отдельный confirm-экран** (`fit_delete`, `component_delete`),
  стиль: `card` с `border-danger`, иконка `fa-triangle-exclamation`, заголовок с именем
  объекта, объяснение последствий, `[Cancel]` (outline-secondary) +
  `[Delete — cannot be undone]` (`btn-danger`).
- Локации — тот же паттерн (`location_delete.html`).
- Переключение вкл/выкл локации и настроек — **не** destructive, без confirm.
- Confirm-экраны рендерятся серверно (работают при медленной сети и переживают
  поллинг-перезагрузки), JS-модалы не требуются.

## 6. Мобильная деградация таблиц

- Дашборд и карточка фита: `.table-responsive` → горизонтальный скролл; sticky thead
  работает и в скролле (`position: sticky` внутри контейнера).
- Минимально читаемый набор при скролле: Fit + Collectability (C+K) + Status остаются в
  первых видимых колонках; Collectability показывает итог C+K, а не рыночный K;
  денежные колонки right-aligned — обрезаются последними.
- Сводная панель и фильтры: `col-12`, фильтры стеком (`d-grid` по 2 кнопки в ряд).
- Таблица компонентов (20–60 строк): тот же скролл; имя компонента + Qty — не сжимать
  (`white-space: nowrap` на имени и числах).
- Кнопки шапки: сворачиваются в ряд с переносом (`flex-wrap`), не в бургер
  (список короткий).

## 7. Toast «Скопировано» — все состояния

1. Успех: toast `text-bg-success` (тёмный) «Copied», 2 с, bottom-right.
2. Clipboard API недоступен → fallback `execCommand` — тот же toast.
3. Полный отказ: инлайн `alert-warning` под кнопкой «Copy failed — use .txt export».
   Тексты копирования — всегда en.
