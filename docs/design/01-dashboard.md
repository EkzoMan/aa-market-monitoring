# 01. Dashboard (`index`)

Роут: `marketmonitoring:index`, доступ `view`. Шаблон: `dashboard.html` (существует —
спецификация уточняет компоновку, структура шаблона сохраняется).

## Вайрфрейм

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Market Monitoring                                            [Refresh now]   │  ← page header row
├──────────────────────┬───────────────────────────────────────────────────────┤
│ SUMMARY (card)       │ FILTERS (card, col-8)                                 │
│  Total to buy:       │  [Status ▾] [Scope ▾] [Bottleneck ▾] [Sort ▾] [Apply] │
│  12,345,678 ISK      │                                                       │
│  Ships: C 14 + K 9   │  Polling indicator: ⟨spinner⟩ Updating… / ✓ Updated   │
│       (per fit) = 23 │                                                       │
│  ⚠ 2 HULL_MISSING    │                                                       │
│  ⓘ 3 fits NO_DATA →  │  (из сумм C/K/C+K исключены)                          │
│  5 deficit comps →   │                                                       │
│  Last checked: …     │                                                       │
├──────────────────────┴───────────────────────────────────────────────────────┤
│ FITS (card)                                                            (37)  │
│ ┌──── sticky thead ─────────────────────────────────────────────────────────┐│
│ │ Fit ▲ │ C/N │ Contr. │ Collect. K/N │ C+K │ Ships │ Status │ Buy ▼ │ Overp. │ Bottlen. │ Last ││
│ ├───────────────────────────────────────────────────────────────────────────┤│
│ │ My Logi│ 1/3 │ ●OK │ 2 of 3 ▓▓▓░ │ 3 │ ●ok      │ ●OK │ 145,200 │ — │ Nano   │ … ││
│ │ My DPS │ 2/2 │ ●DEFICIT │ 0 of 2 ░░░░ │ 2 │ ●deficit │ ●DEFICIT │ 892,000 │ (2) │ → +4 │ … ││
│ │ My Cas │ — │ ●NO_DATA │ 0 of 1 │ — │ ●no_data │ ●HULL_MISSING │ — │ — │ — │ … ││
│ │ My Kite│ — │ ●NO_DATA │ — │ — │ ●no_data │ ●NO_DATA │ — │ — │ — │ … ││
│ └───────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────────┘
```

## Компоновка

1. **Шапка-строка**: заголовок + (edit) кнопка «Refresh now» + индикатор поллинга.
2. **Сводная панель** (`card card-primary`, col-md-4):
   - «Total to buy» — крупно (`.h3.mm-num`), сумма только по фицам без NO_DATA;
   - подпись «NO_DATA fits are not included in the sum. (3)»;
   - отдельные счётчики доступных кораблей: `∑C` (контракты), `∑K` **per fit**
     и `∑(C+K)`; в суммы входят только фиты с известными C и K;
   - счётчики: HULL_MISSING (danger-текст), оверпрайс-позиций суммарно,
     компонентов с дефицитом → ссылка на Components;
   - «Last checked», «Last sync» — мелкий `.text-body-secondary`.
3. **Фильтры** (col-md-8): один `<form method=get>`, selects: status / scope /
   bottleneck / sort. Кнопка Apply (`btn-outline-secondary`). Сортировка — select
   с вариантами по возрастанию/убыванию (текущая реализация сохраняется).
4. **Таблица фитов**: `table-sm table-hover`, sticky thead. Имя фита — ссылка.
   Колонка «Collectability» объединяет «K из N» + прогресс (см. README, раздел «Возможности»).
   Отдельные колонки «Ships on contracts» (C/N), «Contracts status» и «Ships total (C+K)»;
   рядом с итогом — бейдж общего статуса `ships_status_cache` (`ok` / `deficit` /
   `no_data`). NO_DATA/HULL: прогресс не рисуем, «—» в сумме. При `no_data`
   любого источника C или K итог и сводные счётчики для фита — «—».

## Состояния

- Пусто (нет фитов): empty state, см. `07-states.md` §Empty.
- Поллинг: кнопка disabled, spinner в статусе, авто-reload при успехе, alert-danger
  при ошибке (см. `07-states.md` §Polling).
- Общий статус кораблей: в строке показываются только `ships_total` и бейдж
  `ships_status_cache`; при `no_data` — «—» вместо C/K/C+K. Число агрегатного
  дефицита `N−(C+K)` в строке дашборда не выводится — оно показывается в карточке
  фита (подробнее — `07-states.md` §1.1).
- «Обновить сейчас» — **скрыт** для `view` (уже так: `{% if can_edit %}`).

## Мобильная деградация (приоритет 2)

- Сводка и фильтры — в столбец (col-12).
- Таблица — горизонтальный скролл `.table-responsive` (не карточная трансформация:
  строк много, скролл предсказуем). Первые 3 колонки остаются читаемыми при повороте.
