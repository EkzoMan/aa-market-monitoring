# 05. Локации

Роут: `locations` (edit — по ТЗ весь раздел edit). Шаблон: `locations.html`,
`location_delete.html`.

## Вайрфрейм

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Locations                                                 [+ Add location]   │
├──────────────────────────────────────────────────────────────────────────────┤
│ ┌──── sticky thead ─────────────────────────────────────────────────────────┐│
│ │ Location ▲            Sync status          Last sync        [⏻]     [🗑]  ││
│ ├───────────────────────────────────────────────────────────────────────────┤│
│ │ Jita 4-4 // Perimeter  ●●● Synced (green)   2026-11-19 08:00  [on]  (icon)││
│ │ Amarr VIII // Emperor  ●◐ Syncing (spinner) 2026-11-19 06:30  [on]        ││
│ │ Tash-Murkon Prime      ●○ Failed (red)      2026-11-18 22:00  [off]       ││
│ │ Dodixie V // Fed Navy  ○○ Never synced      —                 [off]       ││
│ └───────────────────────────────────────────────────────────────────────────┘│
│ ⓘ A disabled location keeps its data, but is excluded from aggregation.       │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Детали

- **Статус синка** — чип/иконка + текст: `Synced` (success), `Syncing` (warning +
  spinner), `Failed` (danger), `Never synced` (secondary). Это отражается в
  предупреждении по локации в карточке фита и на дашборде
  («Location X … status: Failed»); `NO_DATA`-баннер показывается только при
  пустой области `L(S)=∅` (решение №45).
- **Вкл/выкл** — форма-toggle (`form-switch`, POST на toggle с CSRF), подтверждение
  не нужно (не destructive: данные сохраняются, меняется только агрегация).
- **Delete** — иконка → `location_delete.html` (confirm-экран, danger).
- **Добавление** — форма/модал «Region → System → Station/Structure»: каскадные
  select'ы (server-rendered: выбор региона POST → список систем → станций/структур).
  Структуры — только те, что отдал провайдер структур (кэш БД, не ESI из UI!).
- Все элементы страницы — edit-роль целиком (раздел edit), поэтому без условных
  скрытий; view-пользователь пункта меню «Locations» не видит.

## Пустое состояние

Иконка `fa-location-dot`, «No locations yet. Add a location so fits can be priced.»,
CTA `[+ Add location]`.
