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
│  ⚠ 2 HULL_MISSING    │  Polling indicator: ⟨spinner⟩ Updating… / ✓ Updated   │
│  ⓘ 3 fits NO_DATA →  │                                                       │
│  5 deficit comps →   │                                                       │
│  Last checked: …     │                                                       │
├──────────────────────┴───────────────────────────────────────────────────────┤
│ FITS (card)                                                            (37)  │
│ ┌──── sticky thead ─────────────────────────────────────────────────────────┐│
│ │ Fit ▲   Collectability   Status   Total to buy ▼  Overprice  Bottleneck   ││
│ │                                          Last check                        ││
│ ├───────────────────────────────────────────────────────────────────────────┤│
│ │ My Logi│  2 of 3 ▓▓▓░ │ ●OK     │ 145,200 ISK │   —     │ Nano-Int II  ││
│ │ My DPS │  0 of 2 ░░░░ │ ●DEFICIT│ 892,000 ISK │  (2)    │ → and 4 more ││
│ │ My Cas │  0 of 1      │ ●HULL   │      —      │   —     │       —      ││
│ │ My Kite│  —           │ ●NO_DATA│      —      │   —     │       —      ││
│ └───────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────────┘
```

## Компоновка

1. **Шапка-строка**: заголовок + (edit) кнопка «Refresh now» + индикатор поллинга.
2. **Сводная панель** (`card card-primary`, col-md-4):
   - «Total to buy» — крупно (`.h3.mm-num`), сумма только по фицам без NO_DATA;
   - подпись «NO_DATA fits are not included in the sum. (3)»;
   - счётчики: HULL_MISSING (danger-текст), оверпрайс-позиций суммарно,
     компонентов с дефицитом → ссылка на Components;
   - «Last checked», «Last sync» — мелкий `.text-body-secondary`.
3. **Фильтры** (col-md-8): один `<form method=get>`, selects: status / scope /
   bottleneck / sort. Кнопка Apply (`btn-outline-secondary`). Сортировка — select
   с вариантами по возрастанию/убыванию (текущая реализация сохраняется).
4. **Таблица фитов**: `table-sm table-hover`, sticky thead. Имя фита — ссылка.
   Колонка «Collectability» объединяет «K из N» + прогресс (см. README §2).
   NO_DATA/HULL: прогресс не рисуем, «—» в сумме.

## Состояния

- Пусто (нет фитов): empty state, см. `07-states.md` §Empty.
- Поллинг: кнопка disabled, spinner в статусе, авто-reload при успехе, alert-danger
  при ошибке (см. `07-states.md` §Polling).
- «Обновить сейчас» — **скрыт** для `view` (уже так: `{% if can_edit %}`).

## Мобильная деградация (приоритет 2)

- Сводка и фильтры — в столбец (col-12).
- Таблица — горизонтальный скролл `.table-responsive` (не карточная трансформация:
  строк много, скролл предсказуем). Первые 3 колонки остаются читаемыми при повороте.
