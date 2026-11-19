# Дизайн-спецификация UI — aa-market-monitoring

Аuteur: UI/UX-дизайнер. Назначение: прямая реализация в Django-шаблонах плагина
(`marketmonitoring/templates/marketmonitoring/`), Bootstrap 5 / тема Alliance Auth.

## Структура

| Файл | Содержимое |
|---|---|
| `README.md` | Индекс + визуальный язык (палитра статусов, бейджи, типографика, toast, баннеры, иконки) |
| `01-dashboard.md` | Dashboard (`index`) |
| `02-fit-detail.md` | Карточка фита (`fit_detail`) + таблица компонентов |
| `03-fit-wizard.md` | Мастер создания/правки фита + подтверждение удаления |
| `04-components.md` | Раздел «Components» + форма компонента |
| `05-locations.md` | Локации |
| `06-settings.md` | Настройки |
| `07-states.md` | Полный набор состояний: статусы, пустые, поллинг, confirm, мобильная деградация |

Визуальные мокапы: генерация изображений недоступна в текущей среде (отказ политики
генератора). ASCII-вайрфреймы в файлах страниц — канонические макеты.

## Фиксированные решения по компоновке (§14.4)

1. **Components — самостоятельная страница меню**, а не вкладка внутри Dashboard:
   отдельный пункт «Components» в меню плагина на уровне «Dashboard» (перед «Locations»).
   Причины: у компонентов своя таблица (тип, N, область, дефицит, стоимость) с сортировкой
   по стоимости — вкладка в дашборде сломала бы сценарий «10–15 секунд на дашборд»;
   масштаб до 5000 типов не влезает в секцию.
2. **Dashboard ↔ Components — перекрёстная сводка, а не дублирование:**
   в сводной панели дашборда — только агрегат «итого докупить» и счётчик компонентов
   с дефицитом со ссылкой на страницу Components; полный список живёт на своей странице.
3. **Единый каркас страницы:** `<div class="card">` с `.card-header` (заголовок + действия)
   → фильтры → `.table-responsive`. Все страницы плагина строятся по одной схеме.

## Визуальный язык (Bootstrap 5 / тема AA)

### 1. Палитра статусов — семантика неизменна

| Статус | Класс Bootstrap | Цвет | Применение |
|---|---|---|---|
| `OK` | `.bg-success` / badge `text-bg-success` | зелёный | K ≥ N |
| `DEFICIT` | `.bg-warning` (тёмный текст!) / `text-bg-warning` | оранжевый | 0 ≤ K < N |
| `HULL_MISSING` | `.bg-danger` / `text-bg-danger` | красный | корпуса нет в области |
| `NO_DATA` | `.bg-secondary` / `text-bg-secondary` | серый | нет свежих данных |

- Используем **только** семантические токены Bootstrap (`--bs-success`, `--bs-warning`,
  `--bs-danger`, `--bs-secondary`) — никаких собственных hex-констант, тогда палитра
  автоматически следует светлой/тёмной теме AA. В текущем шаблоне заданы жёсткие hex —
  заменить на `text-bg-*` (ремарка разработчику, код не меняет поведение).
- Warning-бейдж: Bootstrap рисует `text-bg-warning` с тёмным текстом — это допустимо,
  семантика «оранжевый» сохраняется, контраст лучше.

### 2. Чип статуса + прогресс «K из N»

```html
<span class="badge text-bg-success">OK</span>
<div class="progress mm-progress"><div class="progress-bar bg-success" style="width:67%"></div></div>
<span class="fw-semibold">2 <span class="text-body-secondary">of 3</span></span>
```

- `.mm-progress { height: .5rem; min-width: 90px; }` — тонкий прогресс-бар под чипом
  (компактная колонка таблицы).
- Цвет прогресс-бара = цвет статуса. Для `HULL_MISSING`/`NO_DATA` прогресс не рисуется
  (нет смысла), только чип.

### 3. Бейджи позиций

| Бейдж | Класс | Где |
|---|---|---|
| `OVERPRICE` | `badge text-bg-warning` | строка компонента, колонка «бейдж» |
| `NOT_COVERED` | `badge text-bg-danger` | строка компонента; на дашборде — счётчик |
| HULL row | выделение строки, см. `02-fit-detail.md` | таблица компонентов |

Бейджи — статичный текст без иконок внутри (шум не нужен), рядом с числом стоимости.

### 4. Числовая типографика

- Класс `.mm-num` (один utility в CSS плагина):
  `font-variant-numeric: tabular-nums; text-align: right; font-feature-settings:"tnum";`
- Денежные суммы: `123,456 ISK` — целые, разделители тысяч через `|intcomma`-подобный
  фильтр (`|isk` уже есть в `mm_tags`). Никаких дробных ISK.
- Количества: целые, right.
- Текстовые колонки — left; заголовки числовых колонок — right.
- Итоговая строка таблицы компонентов: `tfoot` с `fw-bold` + верхняя граница
  `border-top: 2px solid var(--bs-border-color)`.

### 5. Toast «Скопировано»

- Bootstrap 5 Toast, `data-bs-delay="2000"`, позиция `.toast-container.position-fixed.bottom-0.end-0`.
- Текст всегда **«Copied»** на английском (независимо от языка UI) — требование §14.3.
- При отказе Clipboard API — fallback `document.execCommand('copy')` с textarea;
  при полном отказе — инлайн-подсказка «Copy failed — use .txt export».

### 6. Баннер NO_DATA

- `alert alert-secondary` (не danger!) + иконка `fa-solid fa-circle-info`.
- Текст: «Location {X} is not synced (status: {status}). Deficit and cost are unavailable.»
- Размещение: над таблицей компонентов в карточке фита; в дашборде — отдельная строка
  сводки «N fits have no data» со ссылкой на фильтр.

### 7. Иконки

- Font Awesome (уже в теме AA), минимум: `refresh`, `copy`, `file-arrow-down`,
  `pen`, `trash` (danger), `circle-info` (баннеры), `arrow-up-down` (сортировка).
- Никаких декоративных иконок.

### 8. Компактность и темы

- Таблицы: `table table-sm table-hover align-middle` + sticky header:
  `thead th { position: sticky; top: 0; background: var(--bs-body-bg); z-index:1; }`
- Zebra не используем (hover достаточно; зебра конфликтует с выделением строки корпуса).
- Все цвета — через `var(--bs-*)`; тёмная тема AA работает без правок.
- i18n-запас: строки кнопок/заголовков не фиксированной ширины (auto), контейнеры
  с `flex-wrap`; колонки фильтров — `col-auto`; выдерживает +30% длины русских строк.

### 9. Производительность-заметки для дизайн-решений

- Дашборд ≤ 1 с при 100 фитах: никаких per-row JS-инициализаций (нет tooltip/dropdown
  в строках), один `<script>` на страницу, поллинг — vanilla `fetch` (уже реализован).
- Минимум JS: toast для копирования, поллинг `refresh_status`, confirm — нативный
  Bootstrap modal (не JS-подтверждение браузера, чтобы стилизовать danger).
