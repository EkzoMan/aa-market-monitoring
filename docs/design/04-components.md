# 04. Раздел «Components» + форма компонента

Роуты: `components` (view), `component_create` / `component_update` / `component_delete`
(edit). Шаблоны: `components.html`, `component_form.html`, `component_delete.html`.

## Решение по компоновке (фиксируем, §14.4)

**Components — отдельная страница меню, на одном уровне с Dashboard** (пункт
«Components» идёт в меню после «Dashboard», перед «Locations»), **не вкладка**:
- таблица компонентов — до тысяч типов, вкладка в дашборде нарушает сценарий
  «10–15 секунд на дашборд»;
- перекрёстная связь через сводку: на дашборде счётчик «N components with deficit →»
  ведёт сюда; здесь — только собственная таблица.

## Вайрфрейм

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ← Dashboard │ Components                                  (edit) [+ New comp]│
├──────────────────────────────────────────────────────────────────────────────┤
│ SUMMARY STRIP: 12 comps with deficit ⋅ total 3,456,000 ISK                    │
├──────────────────────────────────────────────────────────────────────────────┤
│ ┌──── sticky thead ─────────────────────────────────────────────────────────┐│
│ │ Type ▲        Target qty  Scope     Deficit ▼   Cost ▼   Note        [✎]  ││
│ ├───────────────────────────────────────────────────────────────────────────┤│
│ │ Nano-Int II   9           Region    5           445,000   restock    (edit││
│ │ Cap Booster   18          Station   6           14,400    —        row)    ││
│ │ …                                                                          ││
│ └───────────────────────────────────────────────────────────────────────────┘│
│ Empty: friendly state + CTA [+ New component]                                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

- Строки с дефицитом 0 → приглушены (`text-body-secondary`), сортировка по умолчанию
  Cost desc. Действия edit/delete — иконки в конце строки, **только при `can_edit`**
  (для view колонки действий нет вовсе).
- Note — `text-truncate` с `title` (только edit-роль вводит, видеть могут все).

## Форма компонента (`component_form`)

```
┌────────────────────────────────────────────┐
│ ← Components │ New component               │
│ Type:    [SDE search input]  (поиск имени) │
│ Target qty: [9]                            │
│ Scope: [Region ▾]  (Station / Region)      │
│ Note:   [__________________] (textarea, 2) │
│              [Cancel]  [Save]              │
└────────────────────────────────────────────┘
```

- SDE-селектор: input с поиском по имени (язык — по §15), обязательное поле;
  ошибка «Type not found» — `invalid-feedback`.
- Оба edit-роута — одна форма, различие только заголовок и предзаполнение.

## Удаление компонента

Экран `component_delete.html` — тот же паттерн confirm, что у фита (03): warning-иконка,
`btn-danger` «Delete — cannot be undone», текст «Component will be removed from this
scope; fits are not affected».
