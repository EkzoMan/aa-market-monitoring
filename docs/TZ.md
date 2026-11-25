# ТЗ: `aa-market-monitoring` v1.2

**Статус:** финальная версия, готова к реализации
**Платформа:** Alliance Auth 5.x, Django 4.2, Python 3.11, PostgreSQL 15+, Redis (Celery)
**Открытых вопросов:** 0
**Документ предназначен для передачи ИИ-агенту-разработчику.** Все решения, которые могли бы стать вопросами, зафиксированы в §20 «Журнал решений». Повторно их не обсуждать и не менять без явного указания заказчика.

---

## 1. Назначение и границы

Плагин в первую очередь контролирует наличие готовых кораблей в фите в открытых контрактах (корпоративных, альянсовых и персональных), а также мониторит доступность отдельных компонентов на заданных рынках (станции/структуры/системы/регионы), считает, сколько фитов реально можно собрать «прямо сейчас», и рассчитывает стоимость докупки недостающего в Jita с наценкой и доставкой.

**Решаемая проблема:** обеспечение контрактов и рынка алли готовыми кораблями в доктринных фитах и компонентами ведётся вручную; нет сводной картины «хватает ли выставленных в контракты кораблей, чего не хватает на рынке, где и сколько стоит довезти».

### 1.1 Входит в объём

- Мониторинг открытых `item_exchange` контрактов (корпоративных, альянсовых и персональных) через ESI и проверка достаточности полных комплектов кораблей по каждому фиту ($C$ из $N$).
- Справочник локаций мониторинга (из SDE), вкл/выкл.
- Фиты: вставка EFT-текста из игры, предпросмотр разбора, ручная правка состава, целевое количество $N$, область доступности, per-component override.
- Отдельные компоненты вне фитов: тип, целевое количество, область.
- Кэширование данных контрактов и рыночных данных ESI в таблицах плагина; UI читает только БД.
- Расчёт комплектности контрактов $C$, рыночной собираемости $K$, дефицита $D_i$, цены покрытия по стакану Jita, стоимости докупки, флага `OVERPRICE`.
- Сводный подсчёт доступных кораблей $C+K$: раздельные счётчики $C$ и $K$ в строке фита и в сводке, агрегатный дефицит $\max(0, N-(C+K))$ и общий статус на его основе (§6.13).
- Фоновые задачи Celery + ручной запуск обновления из UI.
- Discord webhook (один канал): дефицит кораблей в контрактах, рыночный дефицит компонентов, агрегатный дефицит $C+K$, отсутствие корпуса, ACL/провайдеры, сбой и восстановление синка.
- Экспорт списка закупки: буфер обмена (multibuy), `.txt`, `.csv`.
- Роли `market_monitoring_view` / `market_monitoring_edit`.
- i18n-каркас (этап 1 — английские названия типов).

### 1.2 **Не входит в объём** (не реализовывать)

- Производство/промышленность, ME/TE, BPC, планы сборки.
- Размещение заказов, создание/принятие контрактов, торговые операции.
- История цен и графики (хранится только текущий срез).
- Приватные фиты; всё общее для роли.
- Аудит-журнал изменений (кто что правил).
- UI-страница логов задач; лог синков доступен только в Django-admin суперпользователю.
- Публичный JSON API, интеграции с внешними ботами, кроме исходящего webhook.
- Мультитенантность, несколько Discord-каналов, e-mail-уведомления.

---

## 2. Глоссарий

| Термин | Определение |
|---|---|
| Локация мониторинга | станция/структура из SDE, включённая в наблюдение (`MarketLocation`, `enabled=true`) |
| Область (scope) | `station` \| `system` \| `region` — где ищем товар |
| Фит | корпус + компоненты с количествами, введённые EFT-текстом |
| Собираемость $K$ | максимальное число фитов, собираемое из доступного в области $L(S)$ |
| Корабли в контрактах $C$ | полные комплекты фита в открытых `item_exchange` контрактах области (§6.12) |
| Корабли с рынка | рыночная собираемость $K$; Jita используется только для цен докупки |
| Итог доступных кораблей | $C+K$; отдельный агрегатный дефицит $\max(0, N-(C+K))$ и общий статус (§6.13) |
| Дефицит $D_i$ | сколько единиц компонента нужно докупить, чтобы выйти на $N$ |
| Цена покрытия `unit` | цена по стакану Jita, покрывающая дефицит (один лот либо средневзвешенная) |
| Оверпрайс | `unit` выше минимальной sell-цены Jita более чем на порог из настроек |
| NO_DATA | область расчёта не содержит ни одной локации ($L(S)=\varnothing$); локация без данных даёт вклад $A=0$ (решение №45) |

---

## 3. Роли и права

Группы Django, создаются миграцией данных: `market_monitoring_view`, `market_monitoring_edit`.

`_edit` подразумевает `_view`. Суперпользователь имеет все права.

| Действие | `_view` | `_edit` | Superuser |
|---|---|---|---|
| Дашборд, карточка фита, компоненты, цены Jita | ✅ | ✅ | ✅ |
| Копирование и скачивание списка закупки | ✅ | ✅ | ✅ |
| Создание/правка/удаление фитов и компонентов | — | ✅ | ✅ |
| Управление локациями | — | ✅ | ✅ |
| Ручной запуск обновления данных | — | ✅ | ✅ |
| Настройки плагина (наценка, пороги, интервалы, webhook) | — | ✅ | ✅ |
| Привязка/отзыв/включение `StructureProvider` и `ContractProvider` | — | ✅ | ✅ |
| Просмотр лога синков (`SyncRun`) | — | — | ✅ (только `/admin/`) |

Проверка прав — хелпер `has_view(user)` / `has_edit(user)` (суперпользователь или членство в группе). Пункт меню виден при `has_view`.

---

## 4. Функциональные требования

| ID | Требование |
|---|---|
| FR-1 | Локации мониторинга выбираются из SDE (регион → система → станция/структура), включаются/выключаются |
| FR-2 | Добавление фита вставкой EFT-текста из игры с предпросмотром разбора |
| FR-3 | Ручная правка состава после разбора: добавить, удалить, изменить количество, сменить область компонента |
| FR-4 | Для фита задаются: имя, целевое количество $N$, область, overrides компонентов |
| FR-5 | Карточка фита: «можно собрать $K$ из $N$», статус, узкие места, дата/время последней проверки |
| FR-6 | Таблица компонентов: требуется / доступно / дефицит / `unit` / `ref` / стоимость, бейджи `OVERPRICE`, `NOT_COVERED` |
| FR-7 | Страница компонентов вне фитов: тип, целевое количество, область, заметка |
| FR-8 | Настройки: наценка %, порог оверпрайса %, ставка доставки, интервалы, Jita, глубина стакана, webhook, язык |
| FR-9 | Celery: синк Jita, регионов, структур, пересчёт, очистка кэша; ручной запуск из UI |
| FR-10 | Флаг `OVERPRICE` и счётчик оверпрайс-позиций |
| FR-11 | Discord: дефицит, `HULL_MISSING`, ACL/провайдеры, `sync_failed`, `sync_recovered` |
| FR-12 | Экспорт списка закупки: буфер, `.txt`, `.csv` |
| FR-13 | Дедупликация уведомлений о дефиците: при изменении состояния либо не чаще 1 раза в 2 часа |
| FR-14 | Кэш ESI в БД, перезапись по срезу, автоочистка данных отключённых локаций |
| FR-15 | Провайдеры структур 2–4: приоритет, cooldown 60 мин при ACL-сбое, отключение после 3 сбоев подряд |
| FR-16 | i18n-каркас: UI-строки через gettext; имена типов по `LANGUAGE_CODE`, этап 1 — `en` |
| FR-17 | Провайдеры контрактов (`ContractProvider`: корпоративные/альянсовые и персональные контракты ESI) и фоновый синк открытых `item_exchange` контрактов с позициями |
| FR-18 | Приоритетная проверка достаточности полных комплектов кораблей в фите по открытым контрактам ($C$ из $N$), отображение в UI и Discord-событие `contracts_deficit` |
| FR-19 | Сводный подсчёт доступных кораблей $C+K$ с раздельным показом $C$ и $K$ в строке фита и в сводке |
| FR-20 | Агрегатный дефицит $\max(0, N-(C+K))$ и общий статус `ships_status_cache` (`ok|deficit|no_data`) без изменения $D_i$, цен и стоимости докупки |
| FR-21 | Discord-событие `ships_deficit` при beat-проверке с дедупликацией по изменению состояния либо кулдауну; ручной запуск событие не порождает |
| FR-22 | Сводные счётчики исключают фиты, у которых $C$ или $K$ имеет статус `no_data` |

---

## 5. Критические правила предметной области

1. **Строгий scope.** Если указана станция — учитываются заказы **только** на этой станции. Заказы в системе/регионе не учитываются никогда.
2. **Полный состав фита.** В расчёт входят **все** позиции EFT: корпус, модули, риги, дроны, cargo (заряды, топливо, боеприпасы). Множители берутся «как в EFT»; отсутствие множителя = 1.
3. **Корпус обязателен.** Если корпуса нет в области — фит `HULL_MISSING` («неполный») независимо от остальных позиций.
4. **Порог полноты.** Все компоненты фита должны быть доступны; иначе фит не собирается целиком.
5. **Дефицит считается независимо по каждому фиту.** Никакого схлопывания потребностей между фитами с общим компонентом.
6. **Доставка** считается по `packaged_volume` для корпусов, модулей и ригов (упакованный объём) и по `volume`/`packaged_volume` типа — см. §7.6.
7. **Округление.** Все количества — целые. Все ISK — до целого (`ROUND_HALF_UP`). Внутренние расчёты — `Decimal`, без float.
8. **`order_range` заказа при агрегации не используется.** Заказ физически лежит на своей станции; покупатель забирает товар там же.
9. **Уведомления о дефиците — только при автоматической (beat) проверке.** Ручной запуск уведомлений о дефиците не порождает.
10. **UI никогда не обращается к ESI.** Источник данных для страниц — только БД плагина.
11. **Итог не подменяет позиционный дефицит.** $C+K$ и агрегатный дефицит не меняют $D_i$, цену покрытия, стоимость докупки и экспорт закупки (§6.13, решение №41).
12. **Отсутствие данных — нулевая доступность.** Если по локации области нет свежих данных (failed/partial/`no_provider`/stale/нет агрегата `StockLevel`), её вклад в $A_t^S$ равен 0; `NO_DATA` остаётся только для пустой области (§6.7, решение №45).

---

## 6. Алгоритм расчёта

### 6.1 Множество локаций области

Для области $S$ с видом $k$ и ссылкой $r$:

$$
L(S)=
\begin{cases}
\{r\}, & k=\text{station} \\
\big(\text{NPC-станции системы } r\big)\ \cup\ \big(\text{включённые структуры системы } r\big), & k=\text{system} \\
\big(\text{NPC-станции региона } r\big)\ \cup\ \big(\text{включённые структуры региона } r\big), & k=\text{region}
\end{cases}
$$

- NPC-станции системы/региона берутся из SDE (`eveuniverse`), данные по ним — из регионального кэша заказов.
- Структуры учитываются только если они есть в `MarketLocation` с `enabled=true`; данные — из кэша структур.

### 6.2 Доступность

$$
A_t^{S}=\sum_{o\in O(t,S)} v_o,\qquad
O(t,S)=\{\text{sell-заказы типа } t \text{ в локациях } L(S)\}
$$

sell-объём локации — из `StockLevel.total_sell_volume`. Локация без свежих данных даёт вклад 0; суммирование по локациям области выполняется **до** деления на $q_i$ (§5.12, §6.7, решение №45).

### 6.3 Собираемость

Для фита с составом $\{(t_i,q_i)\}$ (включая корпус с $q=1$) и эффективной областью компонента $S_i$:

$$
\text{fits}_i=\left\lfloor \frac{A_{t_i}^{S_i}}{q_i} \right\rfloor,
\qquad K=\min_i \text{fits}_i
$$

**Узкие места** — все $i$, для которых $\text{fits}_i=K$. В UI показывать до 5, с пометкой «и ещё N».

### 6.4 Статусы

Приоритет статусов (первый подходящий побеждает):

| Приоритет | Статус | Условие |
|---|---|---|
| 1 | `NO_DATA` | область $L(S)$ пуста (§6.7, решение №45) |
| 2 | `HULL_MISSING` | $A_{hull}^{S}=0$ |
| 3 | `DEFICIT` | $0 \le K < N$ |
| 4 | `OK` | $K \ge N$ |

При `NO_DATA` дефицит и стоимость **не рассчитываются**, уведомление о дефиците не отправляется; в UI показывается причина и баннер.

Если часть локаций области не имеет данных, их вклад в доступность равен 0; статус определяется по оставшимся локациям по правилам этой таблицы, а отсутствие данных показывается предупреждением с id локации и причиной (§6.7, решение №45).

### 6.5 Дефицит

$$
D_i=\max\!\big(0,\; N\cdot q_i - A_{t_i}^{S_i}\big)
$$

При $K\ge N$ все $D_i=0$.

### 6.6 Цена покрытия по стакану Jita

Стакан — sell-заказы Jita, ascending по цене, глубина `jita_book_depth` (default 50). Пусть $D=D_i$.

$$
\text{unit}(D)=
\begin{cases}
p_k, & v_k \ge D \\
\dfrac{\sum_{j\le k}p_j v_j}{\sum_{j\le k}v_j}, & \sum_{j\le k}v_j \ge D > v_k \\
\text{NOT\_COVERED}, & \sum_j v_j < D
\end{cases}
$$

Где $k$ — минимальный индекс, при котором накопленный объём покрывает $D$ (для второго случая).

**Если `NOT_COVERED`:**

- $\text{covered}=\sum_j v_j$ — объём, который можно закрыть;
- $\text{unit}$ = средневзвешенная цена по всему хранимому стакану;
- стоимость считается **только за `covered`**;
- $\text{shortfall}_i = D-\text{covered}$ показывается отдельной колонкой и входит в отдельную сумму «не покрыто Jita».

### 6.7 Свежесть данных и `NO_DATA`

`NO_DATA` для фита/компонента назначается только если область $S$ не резолвится ни в одну локацию ($L(S)=\varnothing$: система/регион без NPC-станций и структур либо неизвестный `scope_ref`).

Для каждой локации $l \in L(S)$, по которой нет свежих данных — структура с `sync_status != ok` или без доступного провайдера, агрегат `StockLevel` отсутствует, `last_synced_at` старше `data_stale_after_min` (default 180), региональный синк не завершался успешно за тот же период, — вклад в доступность принимается $A_t^{l}=0$ (решение №45). Такая локация **не** делает фит `NO_DATA`: дефицит, стоимость и $C+K$ считаются по всем локациям области с нулевым вкладом проблемных; в причинах пересчёта и в UI показывается предупреждение с id локации и причиной отсутствия данных.

При успешном полном синке локации, по которой заказов нет, в `StockLevel` пишется нулевой агрегат для каждого нужного типа; при исчезновении заказов старые строки удаляются полной заменой (решение №46).

### 6.8 Оверпрайс

$$
\text{ref}_t=p_{\min}^{\text{sell}}(t),\qquad
\text{OVERPRICE}_i \iff \text{unit}(D_i) > \text{ref}_t\cdot\left(1+\frac{\tau}{100}\right)
$$

$\tau$ = `overprice_threshold_pct` (default 25). Приоритет бейджей: `NOT_COVERED` > `OVERPRICE` > обычная строка. Оверпрайс-позиция **входит в итоговую сумму**. В tooltip: `unit`, `ref`, процент превышения. На дашборде — счётчик оверпрайс-позиций по фиту.

### 6.9 Стоимость

$$
\text{cost}_i=\text{round}\!\Big(\big(\text{unit}(D_i)\cdot(1+m)+c_{del}\cdot \text{pvol}_{t_i}\big)\cdot D_i\Big)
$$

- $m$ = `jita_markup_pct / 100`;
- $c_{del}$ = `delivery_rate_isk_per_m3` (default 1250), **без наценки**;
- $\text{pvol}$ = `packaged_volume` типа;
- при `NOT_COVERED` вместо $D_i$ в множителе используется `covered`;
- $\text{Total}=\sum_i \text{cost}_i$ — сумма **уже округлённых** строк; должна совпадать с суммой экспортируемого списка закупки.

### 6.10 Компоненты вне фитов

Тот же расчёт при составе из одной позиции: $q=1$, $N$ = целевое количество, область — собственная область компонента. Статусы: `OK`, `DEFICIT`, `NO_DATA` (`HULL_MISSING` к ним не применяется).

### 6.11 Эталонный пример (использовать в тестах)

Фит, $N=30$, область — станция (структура). Состав: корпус `Ferox` $q=1$; модуль $q=3$; заряды $q=1000$.

Доступно: корпус $A=10$, модуль $A=100$, заряды $A=10000$.

$$
\text{fits}=\big(10,\ \lfloor 100/3\rfloor=33,\ \lfloor 10000/1000\rfloor=10\big),\quad K=10
$$

**Узкие места:** корпус (10) и заряды (10). Статус `DEFICIT` («10 из 30»).

Дефициты: корпус $D=30-10=20$; модуль $D=90-100<0\Rightarrow 0$; заряды $D=30000-10000=20000$.

Стакан Jita по корпусу: лоты $(40\,000\,000;\ 5)$ и $(42\,000\,000;\ 15)$.

$$
\text{unit}=\frac{40\,000\,000\cdot 5+42\,000\,000\cdot 15}{20}=41\,500\,000
$$

Наценка $m=0.05$, доставка $c_{del}=1250$, `pvol` корпуса $=15\,000$:

$$
\text{cost}_{hull}=\text{round}\big((41\,500\,000\cdot1.05+1250\cdot 15\,000)\cdot 20\big)=1\,246\,500\,000
$$

Заряды: лоты $(1000;\ 10\,000)$ и $(1200;\ 10\,000)$, $D=20\,000$ → $\text{unit}=\frac{1000\cdot10\,000+1200\cdot10\,000}{20\,000}=1100$.

$$
\text{cost}_{ammo}=\text{round}\big((1100\cdot1.05+1250\cdot0.0125)\cdot 20\,000\big)=23\,412\,500
$$

$$
\text{Total}=1\,269\,912\,500
$$

`ref` корпуса $=40\,000\,000$; порог $\tau=25\%$ → $50\,000\,000$; `unit` $41\,500\,000$ → оверпрайса нет.

### 6.12 Комплектность кораблей в контрактах

Для фита с составом $\{(t_i, q_i)\}_{i=1..m}$ (корпус + все модули, риги, дроны и заряды) и областью фита $S_{\text{fit}}$ проверяются кэшированные контракты со статусом `outstanding`, типом `item_exchange`, не истёкшим сроком (`date_expired > now`) и локацией выдачи `start_location_id` $\in L(S_{\text{fit}})$. Дедупликация выполняется по уникальному `contract_id` (контракт, полученный сразу через корпоративный и персональный эндпоинты, учитывается один раз).

Для каждого подходящего контракта $c$ по позициям с `is_included = true` и `raw_quantity != -2` (исключая BPC) суммируется количество каждого типа $Q_c(t)$. Число полных комплектов корабля в фите внутри контракта $c$:

$$
\text{ships}_c = \min_{i=1..m} \left\lfloor \frac{Q_c(t_i)}{q_i} \right\rfloor
$$

Если хотя бы одной позиции фита в контракте не хватает ($Q_c(t_i) < q_i$), $\text{ships}_c = 0$. Итоговое число кораблей в фите на контрактах и дефицит контрактов:

$$
C = \sum_{c} \text{ships}_c, \qquad D_{\text{contracts}} = \max(0,\; N - C)
$$

Статус контрактов фита (`contracts_status_cache`):
- `no_data` — нет включённых `ContractProvider`, либо ни один включённый провайдер не имеет свежего успешного синка (`sync_status = ok`, `last_synced_at >= now - data_stale_after_min`);
- `deficit` — данные свежие и $C < N$;
- `ok` — данные свежие и $C \ge N$.

### 6.13 Итоговое число доступных кораблей

Для фита с целевым количеством $N$ вводится сводная величина:

$$
\text{ships\_total}=C+K,
$$

где $C$ — число полных комплектов в контрактах (§6.12), $K$ — рыночная собираемость в области $L(S)$ (§6.3). Jita участвует только в расчёте цены докупки (§6.6) и не добавляется к доступности, если фит явно не скоупится на The Forge.

Агрегатный дефицит и общий статус:

$$
\text{ships\_deficit}=\max(0,\; N-\text{ships\_total}),\qquad
\text{ships\_status}=
\begin{cases}
\texttt{no\_data}, & C \text{ или } K \text{ имеет статус } \texttt{no\_data}\\
\texttt{deficit}, & \text{ships\_deficit}>0\\
\texttt{ok}, & \text{ships\_deficit}=0
\end{cases}
$$

Агрегатный дефицит и общий статус **не заменяют** $D_i$ (§6.5), рыночный статус (§6.4) и контрактный статус (§6.12). Цена покрытия, стоимость докупки, экспорт закупки и узкие места считаются по §6.5–§6.9 без изменений.

В сводке дашборда отдельно показываются $\sum C$, $\sum K$ и $\sum(C+K)$ только по фитам, где обе величины известны; фиты с `no_data` в любом источнике из сумм исключаются. Сумма $K$ по фитам — сумма независимых пер-фитных оценок (§5.5), а не количество уникальных модулей на рынке (подпись «per fit»).

---

## 7. EFT-парсер

### 7.1 Вход

Текст кнопки **Copy to clipboard** из игрового окна фита. Обязан обрабатывать экспорт **русского и английского** клиента (резолв имён — по SDE на любом доступном языке).

### 7.2 Грамматика (практическая)

```
fit            := header NEWLINE section*
header         := "[" hull_name "," fit_name "]"
section        := (item | EMPTY_LINE)*          # группы разделяются пустыми строками
item           := empty_slot | line
empty_slot     := "[" "Empty" ... "]"
line           := name [ "," charge_name ] [ " x" digits ]
```

Пример входа:

```
[Ferox, Fleet Ferox]
Damage Control II
Magnetic Field Stabilizer II
Magnetic Field Stabilizer II

10MN Afterburner II
Warp Scrambler II

Heavy Neutron Blaster II, Antimatter Charge M
Heavy Neutron Blaster II, Antimatter Charge M

Medium Core Defense Field Extender I

Hobgoblin II x5

Antimatter Charge M x1000
```

### 7.3 Правила разбора

1. Первая строка вида `[Ship, Name]` → корпус, `qty_per_fit=1`. Не распознан корпус → ошибка сохранения.
2. `[Empty ...]` игнорируются.
3. Строки разбиваются на группы по пустым строкам; сами группы для расчёта не важны.
4. Если в строке есть `,` — левая часть модуль, правая — заряд; заряд добавляется отдельной позицией с `qty_per_fit=1`, `is_charge=true` (если после заряда указан `xN` — количество применяется к заряду).
5. Суффикс ` xN` в конце строки → количество, применяется к позиции этой строки.
6. Отсутствие множителя → 1.
7. **Дубликаты типов внутри фита суммируются** по `qty_per_fit` (в т.ч. между секциями).
8. Резолв имени → `type_id`: точное совпадение без учёта регистра по локализованным именам SDE (все доступные языки) с приоритетом языка интерфейса → `en`; затем попытка нормализации пробелов.
9. `slot` определяется best-effort (эвристика по порядку групп и категории SDE); **в расчётах не используется**, только для отображения и правки.
10. Любая нераспознанная строка → ошибка с указанием строки и номера; форма не сохраняется, но разобранные строки сохраняются в форме для ручной правки.
11. Парсер не делает запросов к ESI, только SDE/БД.

### 7.4 Выход

Список позиций: `type_id`, `qty_per_fit`, `is_charge`, `slot` (nullable), `raw_line`. Плюс `hull_type_id`, `fit_name`.

### 7.5 Предпросмотр

После «Разобрать» пользователь видит таблицу позиций, может править количества, удалять строки, добавлять типы через SDE-селектор и задавать per-component override области. Сохранение — только при валидном составе (корпус + хотя бы одна позиция, все количества ≥ 1).

### 7.6 Объём для доставки

Берётся `packaged_volume` типа из SDE. Если `packaged_volume` отсутствует/равен 0, использовать `volume`; если и он 0 — доставка по этой позиции 0.

---

## 8. Модель данных

Django-приложение `marketmonitoring`. Все денежные поля — `DecimalField(max_digits=20, decimal_places=2)`, количества — целые.

```
MarketLocation
    name                CharField(255)
    kind                CharField(choices: station|structure)
    station_id          BigInteger null
    structure_id        BigInteger null
    solar_system_id     Integer (SDE)
    region_id           Integer (SDE)
    enabled             Boolean default True
    sync_status         CharField(choices: ok|no_acl|error|no_provider|stale) default stale
    last_synced_at      DateTime null
    UNIQUE (kind, station_id), UNIQUE (kind, structure_id)

StructureProvider
    character_id        FK -> eveonline.Character (on_delete=PROTECT)
    priority            PositiveSmallInteger default 100   # меньше = выше приоритет
    enabled             Boolean default True
    status              CharField(choices: ok|cooling_down|disabled) default ok
    acl_fail_count      PositiveSmallInteger default 0
    cooldown_until      DateTime null
    last_verified_at    DateTime null
    note                CharField(255, blank)
    # токены НЕ хранятся в плагине — используются токены AA по character_id

Fit
    name                CharField(255)
    hull_type_id        Integer (SDE)
    eft_raw             TextField (исходный текст для повторного разбора)
    qty_target          PositiveInteger  # N, >= 1
    scope_kind          CharField(choices: station|system|region)
    scope_ref           BigInteger
    status_cache        CharField(choices: ok|deficit|hull_missing|no_data) default no_data
    fits_available      Integer null      # K
    total_cost          Decimal null
    overprice_count     Integer null
    created_by          FK -> AUTH_USER_MODEL (SET_NULL, null)
    created_at / updated_at
    last_checked_at     DateTime null     # для UI «последняя проверка»
    contracts_status_cache CharField(choices: ok|deficit|no_data) default no_data
    contracts_available Integer null      # C — полных комплектов кораблей в контрактах
    ships_status_cache  CharField(choices: ok|deficit|no_data) default no_data  # общий статус по C+K (§6.13); ships_deficit вычисляется

ContractProvider
    character           FK -> eveonline.Character (on_delete=PROTECT)
    source_kind         CharField(choices: corporation|character) default corporation
    priority            PositiveSmallInteger default 100
    enabled             Boolean default True
    status              CharField(choices: ok|cooling_down|disabled) default ok
    sync_status         CharField(choices: ok|no_acl|error|no_provider|stale) default stale
    acl_fail_count      PositiveSmallInteger default 0
    cooldown_until      DateTime null
    last_synced_at      DateTime null
    note                CharField(255, blank)
    UNIQUE (character, source_kind)

ContractCache
    contract_id         BigInteger unique
    source_kind         CharField(corporation|character)
    source_ref          BigInteger      # corporation_id или character_id
    issuer_id           BigInteger
    issuer_corporation_id BigInteger
    assignee_id         BigInteger
    availability        CharField(32)   # corporation|alliance|personal|public
    contract_type       CharField(32)   # item_exchange
    status              CharField(32)   # outstanding
    title               CharField(255, blank)
    start_location_id   BigInteger
    price               Decimal
    date_issued         DateTime
    date_expired        DateTime
    fetched_at          DateTime

ContractItemCache
    contract            FK -> ContractCache (CASCADE, related_name='items')
    record_id           BigInteger
    type_id             Integer
    quantity            BigInteger
    is_included         Boolean default True
    is_singleton        Boolean default False
    raw_quantity        Integer null

FitComponent
    fit                 FK -> Fit (CASCADE, related_name='components')
    type_id             Integer (SDE)
    qty_per_fit         PositiveInteger >= 1
    slot                CharField(32, blank)
    is_charge           Boolean default False
    scope_override_kind CharField(choices: station|system|region, blank/null)
    scope_override_ref  BigInteger null
    UNIQUE (fit, type_id)                 # дубликаты слиты парсером

ComponentRequest
    type_id             Integer (SDE)
    qty_target          PositiveInteger >= 1
    scope_kind          CharField(choices)
    scope_ref           BigInteger
    note                CharField(255, blank)
    created_by / created_at / updated_at

MarketOrderCache                        # сырые sell-заказы
    source_kind         CharField(region|structure)
    source_ref          BigInteger      # region_id или structure_id
    location_id         BigInteger      # station_id / structure_id заказа
    solar_system_id     Integer
    region_id           Integer
    type_id             Integer
    price               Decimal
    volume              BigInteger
    fetched_at          DateTime
    INDEX (type_id, location_id), INDEX (source_kind, source_ref)

StockLevel                              # агрегат sell по локации и типу
    location_id         BigInteger
    solar_system_id     Integer
    region_id           Integer
    type_id             Integer
    total_sell_volume   BigInteger
    best_sell           Decimal null
    updated_at          DateTime
    UNIQUE (location_id, type_id)

JitaOrderLevel                          # снимок стакана Jita
    type_id             Integer
    rank                SmallInteger    # 1 = минимальная цена
    price               Decimal
    volume              BigInteger
    fetched_at          DateTime
    UNIQUE (type_id, rank)

SyncRun                                 # только для admin
    task_name           CharField
    trigger             CharField(beat|manual)
    started_at / finished_at
    status              CharField(running|ok|partial|failed)
    locations_ok / locations_failed  Integer
    items_processed     Integer
    errors              TextField

WebhookConfig
    url                 URLField
    enabled             Boolean default True

NotificationLog
    event               CharField
    fit                 FK -> Fit (SET_NULL, null)
    component           FK -> ComponentRequest (SET_NULL, null)
    state_hash          CharField(64)
    sent_at             DateTime
    ok                  Boolean
    error               TextField blank
    INDEX (event, fit, sent_at)

PluginSetting                           # singleton
    см. §10
```

Индексы обязательны: `Fit(status_cache)`, `StockLevel(type_id, location_id)`, `JitaOrderLevel(type_id, rank)`, `MarketOrderCache(source_kind, source_ref)`.

Справочники типов, станций, систем, регионов, объёмов — только из `eveuniverse` (SDE). Свои копии SDE не создавать.

---

## 9. Интеграция с ESI

### 9.1 Клиент

Использовать `django-esi` (поставляется с AA 5) — `esi.client`. Публичные эндпоинты — без токена; структуры — с токеном провайдера.

| Данные | Эндпоинт | Токен |
|---|---|---|
| Стакан Jita | `Market.get_region_orders(region_id, type_id=[...], order_type='sell', page=N)` | нет |
| Заказы региона | `Market.get_region_orders(region_id, order_type='sell', page=N)` | нет |
| Заказы структуры | `Market.get_structure_orders(structure_id, order_type='sell', page=N)` | провайдер, скоуп `esi-markets.structure_markets.v1` |
| Инфо о структуре | `Universe.get_universe_structures_structure_id` | провайдер, `esi-universe.read_structures.v1` |
| Контракты корпорации (+ альянса) | `Contracts.GetCorporationsCorporationIdContracts` + `...Items` | провайдер контрактов, `esi-contracts.read_corporation_contracts.v1` |
| Контракты персонажа | `Contracts.GetCharactersCharacterIdContracts` + `...Items` | провайдер контрактов, `esi-contracts.read_character_contracts.v1` |

Скоупы провайдера проверяются при привязке: `esi-markets.structure_markets.v1`, `esi-universe.read_structures.v1`. Если у персонажа скоупов нет — отказ в привязке с текстом «добавьте персонажа с нужными скоупами».

### 9.2 Батчинг и лимиты

- Jita: параметр `type_id` повторяемый; батч `jita_type_batch` (default 50); `page_size=1000`; идти по страницам до пустой.
- При 400 из-за размера батча — уменьшать батч вдвое и повторять.
- Регион: только регионы, нужные расчёту (§9.4). Полный обход страниц.
- Уважать заголовки `X-ESI-Error-Limit-Remain` / `X-ESI-Error-Limit-Reset`: при остатке < 10 останавливать задачу, помечать `partial` и переносить на следующий запуск.
- Ретраи: 3 попытки с экспоненциальным backoff (2/4/8 с) на 5xx и таймаутах; на 420 — пауза до `Retry-After`.
- Кэш ESI (регион ≈ 5 мин, структуры ≈ 30 мин) — допустимое поведение, задокументировать в README.

### 9.3 Перезапись кэша

Синк источника выполняется как: собрать полный набор заказов → в одной транзакции удалить `MarketOrderCache` по `(source_kind, source_ref)` → `bulk_create(batch_size=2000)`. Частично записанных срезов не бывает.

`JitaOrderLevel` перезаписывается по типам, участвующим в расчёте: удалить строки по этим `type_id`, вставить топ-`jita_book_depth` лотов.

### 9.4 Какие регионы синкать

Объединение: регионы всех включённых `MarketLocation`; регионы, встречающиеся в `Fit.scope_ref`, `FitComponent.scope_override_ref`, `ComponentRequest.scope_ref` при `scope_kind='region'`; регионы систем из scope `system`; регион Jita.

### 9.5 Ошибки доступа к структуре

403/401 по структуре → обработка провайдера (§11), локация `no_acl` или переводится на другого провайдера. Задача продолжается по остальным локациям.

### 9.6 Локации, доступные для дока персонажа

Единого эндпоинта «все станции, куда может докнуться персонаж», в ESI нет. Список собирается через персональный поиск с токеном персонажа, затем ID разыменовываются через universe-эндпоинты.

**Конвейер:**

| Шаг | Эндпоинт | Токен / scope |
|---|---|---|
| 1. Структуры с правами дока | `GET /characters/{char_id}/search/?categories=structure&search=<term>` | токен персонажа, скоуп `esi-search.search_structures.v1` |
| 2. NPC-станции по имени | `GET /characters/{char_id}/search/?categories=station&search=<term>` | токен персонажа, тот же скоуп |
| 3. Инфо о цитадели | `GET /universe/structures/{id}/` | любой валидный токен (без скоупа); 403, если дока нет |
| 4. Инфо о NPC-станции | `GET /universe/stations/{id}/` | нет (публичный) |
| 5. Система | `GET /universe/systems/{id}/` | нет (публичный) |
| 6. Регион | `system.constellation_id` → `GET /universe/constellations/{id}/` → `region_id` → `GET /universe/regions/{id}/` | нет (публичные) |

Результат разыменования: **регион / система / название станции** — фиксируется в модели локации или через `django-eveuniverse` (`EveSolarSystem`, `EveRegion`), без своих копий SDE.

**Ограничения и правила:**

- Параметр `search` в шагах 1–2 обязателен (мин. ~3 символа), полный перебор структур невозможен. Перебирать типовые подстроки (например, `"the"`, названия систем региона) и объединять результаты; дубликаты ID схлопывать.
- `categories=structure` возвращает только структуры, на которые у персонажа есть права дока — это и есть определение «доступна для дока».
- Шаги 4–6 публичны и кешируются ESI на дни; их результаты разрешено разрешать через `django-eveuniverse` вместо повторных запросов.
- Поисковые запросы выполняются только в Celery-задачах (§9.1, UI в ESI не ходит); соблюдать лимиты ошибок §9.2.

---

## 10. Настройки

`PluginSetting` (singleton, редактируются формой; доступ — `market_monitoring_edit`).

| Ключ | Тип | Default | Ограничения |
|---|---|---|---|
| `jita_markup_pct` | Decimal | 5 | 0…500 |
| `overprice_threshold_pct` | Decimal | 25 | 0…1000 |
| `delivery_rate_isk_per_m3` | Decimal | 1250 | ≥ 0 |
| `jita_region_id` | Integer | 10000002 | The Forge |
| `jita_station_id` | Integer | 60003760 | Jita IV-4 |
| `jita_book_depth` | Integer | 50 | 1…500 |
| `jita_type_batch` | Integer | 50 | 1…200 |
| `jita_sync_interval_min` | Integer | 60 | 5…1440 |
| `stock_sync_interval_min` | Integer | 60 | 5…1440 |
| `data_stale_after_min` | Integer | 180 | ≥ 5 |
| `provider_acl_cooldown_min` | Integer | 60 | 1…1440 |
| `provider_acl_max_failures` | Integer | 3 | 1…10 |
| `notification_deficit_enabled` | Boolean | True | — |
| `notification_deficit_cooldown_min` | Integer | 120 | 1…1440 |
| `webhook_url` | URL | — | один канал |
| `display_language_mode` | Enum | `client_default` | `client_default` \| `fixed_en` |
| `contracts_sync_interval_min` | Integer | 30 | 5…1440 |
| `notification_contracts_deficit_enabled` | Boolean | True | — |

Изменение интервалов не требует перезапуска beat: beat дёргает задачи часто, задача сама проверяет, истёк ли интервал (§12).

---

## 11. Пул провайдеров структур

Ожидается 2–4 персонажа с ACL на структуры. Токены — из AA (модель `eveonline.Character`), плагин токенов не хранит.

**Выбор провайдера для структуры:** среди `enabled=true`, `status != disabled`, и (`status != cooling_down` или `cooldown_until <= now`) — по возрастанию `priority`, затем по `character_id`.

**Обработка 403/401:**

1. `acl_fail_count += 1`; `status = cooling_down`; `cooldown_until = now + provider_acl_cooldown_min`.
2. Структура повторно обрабатывается следующим доступным провайдером в том же прогоне.
3. Если `acl_fail_count >= provider_acl_max_failures` → `status = disabled`, алерт `provider_acl_lost` с пометкой «отключён».
4. Успешный запрос (200) сбрасывает `acl_fail_count = 0` и `status = ok`.
5. `disabled` включается только вручную через форму провайдеров; при включении счётчик сбрасывается.
6. Если провайдеров с доступом нет — локация `no_provider`, её вклад в доступность равен 0; участвующие фиты считаются по оставшимся локациям (§6.7, решение №45), алерт `provider_acl_lost` отправляется по общим правилам.

Задача `verify_providers` раз в час проверяет `last_verified_at` и наличие структуры в ответе ESI, не меняя `acl_fail_count` без фактического 403.

---

## 12. Celery

### 12.1 Задачи

| Задача | Назначение | Guard-интервал |
|---|---|---|
| `sync_jita_book` | снимок стакана Jita по типам расчёта | `jita_sync_interval_min` |
| `sync_region_orders` | заказы нужных регионов | `stock_sync_interval_min` |
| `sync_structure_orders` | заказы включённых структур | `stock_sync_interval_min` |
| `sync_contracts` | открытые контракты корпораций/персонажей и их позиции | `contracts_sync_interval_min` |
| `recalc_stock_levels` | агрегация кэша → `StockLevel` + пересчёт фитов/компонентов | после синков |
| `cleanup_orphan_cache` | удаление кэша отключённых/удалённых локаций | 24 ч |
| `verify_providers` | валидация провайдеров | 60 мин |
| `notify_deficit` | уведомления по результатам пересчёта | — |

### 12.2 Расписание

Beat дёргает `sync_jita_book`, `sync_region_orders`, `sync_structure_orders`, `verify_providers` каждые 5 минут; каждая задача проверяет, истёк ли её интервал по `last_synced_at` / `last_verified_at`, и при необходимости выполняется. `cleanup_orphan_cache` — ежедневно. Это позволяет менять интервалы в настройках без правки beat.

### 12.3 Цепочка

`sync_*` → `recalc_stock_levels` → (если `trigger=beat` и `notification_deficit_enabled`) `notify_deficit`.

`trigger` (`beat`|`manual`) передаётся через аргументы задач и фиксируется в `SyncRun`.

### 12.4 Ручной запуск

- Доступен `_edit`; кнопка «Обновить сейчас» на Locations и Dashboard.
- Ставит цепочку с `trigger='manual'`, запускается асинхронно.
- Блокировка: `cache.add("mm:sync_lock", 1, timeout=3600)`; при наличии блокировки — ответ 409 и текст «обновление уже выполняется».
- UI опрашивает `marketmonitoring:refresh_status` (JSON: `in_progress`, `last_checked_at`, `last_result`) каждые 3 с, максимум 5 минут; страница логов при этом не создаётся.
- Уведомления о дефиците при `manual` **не отправляются**. Системные события (`provider_acl_lost`, `sync_failed`, `sync_recovered`) отправляются независимо от триггера.

### 12.5 Идемпотентность

Задачи должны быть безопасны при повторном запуске: перезапись кэша по источнику, пересчёт агрегатов полной заменой по затронутым локациям, дедупликация уведомлений через `NotificationLog`.

---

## 13. Уведомления (Discord)

Один webhook (`webhook_url`), один канал. HTTP POST JSON (`requests`), таймаут 10 с, 3 ретрая, backoff 2/4/8 с. Результат пишется в `NotificationLog` (`ok`, `error`).

| Событие | Когда | Содержимое |
|---|---|---|
| `deficit` | beat-проверка, $K<N$ | фит, $K/N$, до 5 узких мест, сумма докупки, число оверпрайс-позиций, ссылка |
| `contracts_deficit` | beat-проверка, $C<N$ | фит, $C/N$ кораблей в контрактах, нехватка $N-C$, область, ссылка |
| `ships_deficit` | beat-проверка, $C+K<N$ | фит, $C+K$ и $N$, нехватка $N-(C+K)$, раздельно $C$ и $K$, область, ссылка |
| `hull_missing` | корпус отсутствует в области | фит, область, ссылка |
| `provider_acl_lost` | ACL-сбой / отключение провайдера | персонаж, локация, `acl_fail_count`, статус |
| `sync_failed` | задача упала / превышен порог ошибок | задача, число ошибок, краткий текст |
| `sync_recovered` | успешный прогон после серии ошибок | задача, длительность простоя |

Если отдельная локация не имеет данных, её вклад в доступность равен 0 и дефицит считается по общему правилу (§6.7, решение №45); уведомления отправляются по этой же таблице. `NO_DATA` возникает только при пустой области и дефицит-события не порождает.

### 13.1 Дедупликация `deficit`

Уведомление отправляется, если:

1. состояние изменилось с прошлой отправки (переход в дефицит, смена набора узких мест, изменение суммы докупки), **или**
2. прошло ≥ `notification_deficit_cooldown_min` (default 120 мин) с прошлой отправки, а дефицит сохраняется.

Хэш состояния:

$$
\text{hash}=H\big(\text{fit\_id}\ \|\ K\ \|\ \{t_i: D_i>0\}\ \|\ \text{rounded Total}\big)
$$

`H` = SHA-256 от канонической JSON-строки. При совпадении хэша и не истёкшем cooldown — не отправлять.

Для `ships_deficit` используется отдельный хэш состояния:

$$
\text{hash}=H\big(\text{fit\_id}\ \|\ \text{ships\_status}\ \|\ \text{ships\_deficit}\big)
$$

Дедупликация — по изменению этого хэша либо по `notification_deficit_cooldown_min`; хэши `deficit` и `contracts_deficit` не изменяются. При `trigger=manual` событие `ships_deficit` не отправляется.

`provider_acl_lost`, `sync_failed`, `sync_recovered` кулдауном не ограничены, но отправляются только при **переходе** состояния (не повторять на каждом прогоне).

### 13.2 Формат

Discord embed: `title` = событие и имя фита, `color` (`deficit` — оранжевый, `hull_missing` — красный, `sync_failed` — красный, `sync_recovered` — зелёный), поля — параметры события, ссылка на карточку — `url`.

---

## 14. Интерфейс

URL-префикс `market/`, namespace `marketmonitoring`.

| Страница | URL name | Доступ | Содержимое |
|---|---|---|---|
| Dashboard | `index` | view | таблица фитов: имя, $K/N$, корабли в контрактах $C/N$, корабли с рынка $K/N$, итог $C+K$, общий статус `ships_status_cache`, сумма докупки, счётчик оверпрайса, узкое место, последняя проверка; фильтры (статус, область, узкое место); сортировки; сводная панель с отдельными счётчиками $C$, $K$, $C+K$ и «итого докупить»; кнопка «Обновить сейчас» |
| Карточка фита | `fit_detail` | view | шапка ($K/N$, $C/N$, итог $C+K$, общий статус, сумма докупки), таблица компонентов, бейджи `OVERPRICE`/`NOT_COVERED`, кнопки экспорта |
| Создание/правка фита | `fit_create` / `fit_update` | edit | EFT-textarea → «Разобрать» → предпросмотр, $N$, область, overrides |
| Удаление фита | `fit_delete` | edit | подтверждение |
| Компоненты | `components` | view | список компонентов вне фитов: тип, целевое количество, область, дефицит, стоимость |
| Создание/правка компонента | `component_create` / `component_update` | edit | SDE-селектор типа, количество, область, заметка |
| Локации | `locations` | edit | список локаций из SDE, статус синка, последняя синхронизация, вкл/выкл, добавление |
| Настройки | `settings` | edit | §10 + webhook + провайдеры (список, приоритет, вкл/выкл, статус) |
| Статус обновления | `refresh_status` | view | JSON для поллинга |
| Экспорт | `export_txt` / `export_csv` | view | файлы по фиту |

### 14.1 Таблица компонентов (карточка фита)

Колонки: компонент (имя + кол-во на фит), область компонента, требуется ($N q_i$), доступно ($A$), дефицит ($D_i$), `unit`, `ref`, стоимость, бейдж. Строка корпуса визуально выделена.

### 14.2 Представление собираемости

Числом «$K$ из $N$» + прогресс-бар + цвет (зелёный `OK`, оранжевый `DEFICIT`, красный `HULL_MISSING`, серый `NO_DATA`).

Общий статус `ships_status_cache` показывается отдельным бейджем рядом с рыночным и контрактным. При `no_data` в любом источнике итог $C+K$ и сводные счётчики выводятся как «—», а сам фит исключается из сумм (§6.13). Счётчик кораблей с рынка подписывается как per-fit, так как суммирует независимые оценки по фитам (§5.5).

### 14.3 Экспорт

- **Буфер обмена (multibuy):** строки `TypeName<TAB>Qty` только для позиций с $D_i>0$; только **английские** имена типов; сортировка по имени типа (case-insensitive); копирование через Clipboard API с подтверждением «Скопировано».
- **`.txt`:** тот же multibuy-формат, `Content-Type: text/plain; charset=utf-8`, имя `fit_<id>_buyback.txt`.
- **`.csv`:** заголовок `type_name,type_id,qty,unit_price,cost`; `unit_price` и `cost` — целые ISK; `qty` — $D_i$ (при `NOT_COVERED` — `covered`, отдельная колонка `shortfall` допускается); разделитель `,`, кодировка UTF-8 без BOM, имя `fit_<id>_buyback.csv`.
- Сумма `cost` в CSV равна `Fit.total_cost`.

### 14.4 Размещение страницы компонентов

Страница — самостоятельный раздел меню «Components» рядом с Dashboard. Точная компоновка (вкладка/секция) может быть уточнена дизайнером без изменения функциональности; маршрут и модель остаются.

---

## 15. i18n

- UI-строки — через `gettext`/`gettext_lazy`.
- Имена типов рендерятся из SDE по языку: при `display_language_mode='client_default'` — `LANGUAGE_CODE` пользователя (fallback `en`); при `fixed_en` — всегда `en`.
- **Этап 1 к реализации: английские имена.** Русские строки добавляются каталогом переводов без изменения кода.
- Парсер EFT принимает имена на **любом** языке SDE независимо от языка интерфейса.
- Экспорт закупки всегда на английском.

---

## 16. Нефункциональные требования

| Категория | Требование |
|---|---|
| Масштаб | 5–10 локаций, 20–100 фитов, 2–4 провайдера, до ~5000 уникальных типов в расчёте |
| Производительность | дашборд при 100 фитах ≤ 1 с; карточка фита ≤ 500 мс; пересчёт всех фитов ≤ 60 с |
| Данные | UI не обращается к ESI; все запросы страниц — только БД |
| Надёжность | падение одного провайдера не блокирует остальные; локация без данных даёт нулевую доступность, `NO_DATA` — только при пустой области (решение №45) |
| Точность | `Decimal` во всех расчётах; сравнение цен — без float |
| Логирование | `logging.getLogger("marketmonitoring")`; `SyncRun` для истории синков |
| Безопасность | все view — `login_required` + проверка группы; CSRF на формах; webhook URL не показывается пользователям без `_edit` |
| Совместимость | миграции с пустой и с существующей БД; AA 5.x; отсутствие жёстких зависимостей от версии SDE |
| Тесты | см. §18, покрытие расчётного ядра и парсера — обязательно |

---

## 17. Структура репозитория

```
aa-market-monitoring/
├── pyproject.toml
├── README.md
├── docs/
│   ├── installation.md
│   ├── configuration.md
│   └── architecture.md
├── marketmonitoring/
│   ├── __init__.py
│   ├── apps.py                 # AppConfig: verbose_name, ready() для меню/групп
│   ├── constants.py            # enum'ы статусов, событий, kinds
│   ├── auth_hooks.py           # has_view / has_edit
│   ├── models.py
│   ├── forms.py
│   ├── views/
│   │   ├── dashboard.py, fits.py, components.py, locations.py,
│   │   ├── settings.py, export.py, refresh.py
│   ├── urls.py
│   ├── eft/parser.py
│   ├── esi/client.py           # обёртки endpoints, батчинг, ретраи
│   ├── providers/service.py    # выбор провайдера, cooldown, fail-count
│   ├── calc/engine.py          # L(S), A, K, D, unit, overprice, cost — чистые функции
│   ├── tasks.py
│   ├── notify/discord.py
│   ├── admin.py                # SyncRun, MarketLocation, providers (superuser)
│   ├── templatetags/mm_tags.py # рендер имён типов, форматирование ISK
│   ├── templates/marketmonitoring/
│   ├── migrations/
│   └── tests/
│       ├── test_eft_parser.py
│       ├── test_calc_engine.py
│       ├── test_orderbook.py
│       ├── test_providers.py
│       ├── test_tasks.py
│       ├── test_views_permissions.py
│       └── test_export.py
```

`pyproject.toml`:

```toml
[project]
name = "aa-market-monitoring"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = [
    "allianceauth>=5.0,<6.0",
    "django-eveuniverse>=1.5",
    "requests>=2.31",
]

[tool.black]
line-length = 120

[tool.isort]
profile = "black"
```

Пакет Python — `marketmonitoring`; в AA `INSTALLED_APPS` добавляется `"marketmonitoring"` (после `allianceauth`-приложений и `eveuniverse`).

---

## 18. Тестирование

Обязательные группы тестов:

1. **EFT-парсер:** английский экспорт; русский экспорт; пустые слоты; `xN` у дронов и зарядов; заряды через запятую; дубликаты модулей суммируются; нераспознанная строка → список ошибок с номерами; нераспознанный корпус → отказ.
2. **Расчётное ядро (чистые функции):** эталонный пример §6.11 целиком (K, узкие места, $D_i$, `unit`, `cost`, `Total`); покрытие одним лотом; покрытие несколькими лотами; `NOT_COVERED` и `shortfall`; оверпрайс на пороге и чуть выше; `τ=0`.
3. **Scope:** `station` не учитывает заказы системы/региона; `system` учитывает NPC-станции системы и только включённые структуры; `region` — аналогично; переопределение области компонента.
4. **Статусы:** `NO_DATA` только при пустой области; структура/станция без данных, с протухшими данными или `no_provider` даёт вклад 0 и `HULL_MISSING`/`DEFICIT`/`OK` по оставшимся локациям; `HULL_MISSING` при нулевом корпусе; `OK` при $K=N$.
5. **Провайдеры:** выбор по приоритету; 403 → cooldown на 60 мин и переход к следующему; 3 сбоя → `disabled` и алерт; успех сбрасывает счётчик; `no_provider` → вклад локации 0 (решение №45).
6. **Задачи:** `trigger=manual` не вызывает `notify_deficit`; `beat` вызывает; дедупликация по хэшу и по 2-часовому кулдауну; перезапись кэша атомарна; блокировка ручного запуска даёт 409.
7. **Права:** `_view` не может создавать/править/удалять, не видит форму настроек, не видит `/admin/`-лог; `_edit` может всё, кроме просмотра `SyncRun`.
8. **Экспорт:** multibuy содержит только $D_i>0$, английские имена, табы; CSV-заголовки и сумма `cost` = `Fit.total_cost`.
9. **Сводка кораблей:** чистые функции $C+K$, $\max(0, N-(C+K))$ и общего статуса; эталон §6.11 с добавленным $C$; неизменность $D_i$, `unit`, `cost`, `Total` и CSV; исключение фитов с `no_data` (пустая область) из сводных сумм; дедупликация `ships_deficit`; отсутствие ESI-вызовов из view. Локация без данных даёт $K$ с нулевым вкладом и участвует в сводке.

Тесты ESI — с `responses`/`unittest.mock`, без реальной сети.

---

## 19. Критерии приёмки

1. EFT-текст из игры (включая русский клиент) разбирается полностью; состав совпадает с окном фита; корпус, дроны и cargo присутствуют.
2. Фит с $N=30$, где по одному компоненту хватает на 12, показывает «12 из 30», статус `DEFICIT`, узкое место ровно этот компонент.
3. Отсутствие корпуса в области даёт `HULL_MISSING` независимо от остальных позиций.
4. При области `station` заказы на других станциях системы и региона не учитываются.
5. Дефицит по двум фитам с общим компонентом считается независимо (две строки, без схлопывания).
6. Цена покрытия: один лот ≥ $D$ → его цена; иначе средневзвешенная до покрытия; при недостатке — `NOT_COVERED` с показом `shortfall`.
7. `unit` выше $\text{ref}\cdot(1+\tau/100)$ даёт `OVERPRICE`, он виден в UI и не исключает позицию из итога.
8. Формула `cost` соответствует §6.9; сумма строк равна `Fit.total_cost` и сумме `cost` в CSV.
9. Ручной запуск обновления не порождает уведомлений о дефиците; автоматический — порождает.
10. Повторное уведомление о неизменном дефиците приходит не чаще 1 раза в 2 часа; при изменении состояния — сразу.
11. 403 у провайдера → cooldown 60 мин, локация уходит на другого провайдера; после 3 сбоев подряд провайдер `disabled` и приходит алерт.
12. Отключение локации удаляет её записи кэша задачей `cleanup_orphan_cache`.
13. Multibuy-экспорт импортируется в игру (формат `Name<TAB>Qty`), содержит только английские имена.
14. `_view` не может изменить данные; `_edit` не видит лог синков в `/admin/`.
15. `SyncRun` фиксирует каждый прогон с триггером, статусом и числом обработанных локаций.
16. Настройки меняются без перезапуска beat и применяются к следующему прогону.
17. Для фита эталона §6.11 с $C=7$ сводка показывает 7 контрактных и 10 рыночных кораблей, итог 17, агрегатный дефицит 13; $D_i$, `unit`, `cost`, `Total` и CSV совпадают с эталоном.
18. Сводная панель показывает отдельные $\sum C$, $\sum K$, $\sum(C+K)$; фиты с `no_data` в любом источнике (пустая область рыночного скоупа или отсутствие данных контрактов) исключаются; счётчик $K$ подписан как per-fit. Локация рынка без данных даёт нулевой вклад в $K$ и не исключает фит из сводки.
19. Общий статус `ships_status_cache` и событие `ships_deficit` появляются только при beat и $C+K<N$; ручной запуск их не порождает; дедупликация — по изменению состояния либо кулдауну 2 ч.
20. Новый агрегатный дефицит не меняет статусы §6.4/§6.12, позиционные дефициты и экспорт закупки; миграция добавляет только `ships_status_cache`.
21. Структура или NPC-станция без заказов, без данных, с `no_provider` либо с протухшим сином даёт вклад 0 в доступность; фит получает `DEFICIT`/`HULL_MISSING`/`OK` по остальным рынкам системы, а не `NO_DATA`; дефицит, стоимость и уведомления считаются по общим правилам (решение №45).
22. При успешном полном синке локации без заказов создаются нулевые агрегаты `StockLevel`; при исчезновении заказов старые положительные строки удаляются, исключая устаревший объём (решение №46). `NO_DATA` сохраняется только для пустой области.

---

## 20. Журнал решений (не пересматривать)

| № | Решение |
|---|---|
| 1 | Источник рыночных данных — ESI; данные кэшируются в БД плагина; UI читает только БД |
| 2 | Токены хранятся в AA; плагин ссылается на персонажа, своих токенов не хранит |
| 3 | Jita = The Forge, станция 60003760 (настраивается) |
| 4 | Парсер обязан обрабатывать игровой экспорт, включая русский |
| 5 | Цена покрытия — покрывающий лот либо средневзвешенная по стакану; глубина стакана настраивается |
| 6 | Наценка — процент из настроек, применяется к `unit`, не к доставке |
| 7 | Доставка — фиксированная ставка ISK/м³ из настроек, объём — `packaged_volume` |
| 8 | Обновление Jita и доступности — Celery, по умолчанию раз в час, интервалы настраиваются |
| 9 | Ручной запуск обновления из UI обязателен |
| 10 | Расчёт собираемости нужен; методика — §6.3 |
| 11 | Область доступности задаётся при добавлении фита/компонента; бывает per-component override |
| 12 | Область: `station` (строго одна локация), `system`, `region` (ESI-регион + включённые структуры) |
| 13 | В состав фита входят все позиции EFT, включая корпус и cargo; множители — из EFT |
| 14 | Дефицит считается независимо по каждому фиту |
| 15 | Все компоненты фита должны быть доступны, иначе фит неполный; отсутствие корпуса — `HULL_MISSING` |
| 16 | При указанной станции заказы системы/региона не учитываются |
| 17 | SDE берётся из AA (`eveuniverse`); локации выбираются из SDE |
| 18 | Этап 1 — английские названия типов; i18n-каркас заложен |
| 19 | Роли: `market_monitoring_view` (просмотр + экспорт), `market_monitoring_edit` (управление + настройки) |
| 20 | Приватных фитов нет; аудит-журнала нет |
| 21 | Логи задач — только в Django-admin, суперпользователю; UI-страницы логов нет |
| 22 | Celery-уведомления о дефиците отправляются, ручные — нет |
| 23 | Один Discord-канал; события — §13 |
| 24 | Дедупликация дефицита: изменение состояния либо не чаще 1 раза в 2 часа |
| 25 | Оверпрайс определяется по минимальной sell-цене Jita и порогу-проценту из настроек |
| 26 | Провайдеров 2–4; ACL-сбой → cooldown 60 мин; 3 сбоя подряд → отключение |
| 27 | Кэш перезаписывается; данные отключённых локаций удаляются автоматически |
| 28 | Округление: количества целые, ISK до целого; расчёты в `Decimal` |
| 29 | История цен не хранится; `order_range` при агрегации не используется |
| 30 | Платформа: AA 5.x, Django 4.2, Python 3.11, PostgreSQL 15+ |
| 31 | Первичная проверка достаточности фита ведётся по открытым (`outstanding`, не истёкшим) `item_exchange` контрактам в области фита |
| 32 | Источники контрактов ESI: корпоративный (`/corporations/{id}/contracts/`, включая корпоративные и альянсовые контракты) и персональный (`/characters/{id}/contracts/`); дедупликация по `contract_id` |
| 33 | «Корабль в фите» в контракте — полный комплект всех позиций `FitComponent` (`is_included=true`, кроме BPC `raw_quantity=-2`); в одном контракте может быть $\ge 1$ комплектов ($\min_i \lfloor Q_c(t_i)/q_i \rfloor$) |
| 34 | Статус и количество кораблей в контрактах (`contracts_status_cache`, `contracts_available`) хранятся в `Fit` отдельно от рыночной собираемости $K$, не изменяя формулы §6.1–§6.11 |
| 35 | Позиции `items` открытого `item_exchange` контракта неизменны и запрашиваются из ESI один раз при появлении `contract_id` в кэше |
| 36 | При `trigger=beat` и $C < N$ отправляется Discord-уведомление `contracts_deficit` с дедупликацией по изменению состояния либо кулдауну `notification_deficit_cooldown_min` |
| 37 | Задача `sync_corp_structures` (beat, раз в сутки): структуры корпораций включённых `ContractProvider` (`GET /corporations/{id}/structures/`, скоуп `esi-corporations.read_corporation_structures.v1`) регистрируются в `MarketLocation(kind=structure)` с координатами; новые записи создаются `enabled=false` и включаются вручную в Locations UI. Существующие записи не изменяются, кроме заполнения пустых `solar_system_id`/`region_id` и плейсхолдерного имени. Провайдеры без скоупа помечаются ошибкой синка, ACL-сбои обрабатываются по §11 |
| 38 | Удаление исчезнувших контрактов в `sync_contracts` выполняется один раз в конце прогона по объединённому active-множеству `contract_id` всех источников (а не по каждому источнику отдельно в `_upsert_contracts_source`): кросс-эндпоинтовый контракт, видимый и в корпоративном, и в персонажном срезах, не удаляется при синке одного источника. Частичные (partial) источники не чистятся (§12.5) |
| 39 | Рыночная часть доступных кораблей — $K$ (§6.3), посчитанная по локациям $L(S)$; Jita остаётся только источником цен для докупки (§6.6) |
| 40 | Итог $C+K$ показывается в строке фита и в сводке; сводка содержит отдельные $\sum C$, $\sum K$ и $\sum(C+K)$ только по фитам с известными обеими величинами; фиты с `no_data` в одном источнике из сумм исключаются |
| 41 | Вводится агрегатный дефицит $\max(0, N-(C+K))$ и общий статус `ships_status_cache` (`ok|deficit|no_data`) по §6.13; $D_i$, `unit`, `cost`, `Total` и экспорт закупки сохраняются по §6.5–§6.9 |
| 42 | Событие `ships_deficit` отправляется только при `trigger=beat` и `ships_status_cache='deficit'`; дедупликация — по изменению `ships_status_cache`/`ships_deficit` либо по `notification_deficit_cooldown_min`; события `deficit` и `contracts_deficit` не изменяются |
| 43 | При `no_data` любого источника `ships_status_cache='no_data'`, `ships_deficit` не считается, уведомление не отправляется, фит исключается из сводных сумм |
| 44 | Расчёт $C+K$, агрегатного дефицита и общего статуса — чистые функции `calc/engine.py`; view читает только БД; в `Fit` добавляется поле `ships_status_cache`, `ships_deficit` не хранится |
| 45 | По явному указанию заказчика семантика отсутствия данных изменена: каждая локация без свежих данных (failed/partial/`no_provider`/stale/отсутствует `StockLevel`) даёт вклад $A_t^l=0$; `NO_DATA` остаётся только если $L(S)=\varnothing$. Дефицит, стоимость, уведомления и $C+K$ считаются по общему правилу; UI показывает предупреждение с id локации, но не меняет статус фита. Решение переопределяет прежнюю трактовку §2/§6.4/§6.7/§11.6/§16/§18/§19 и часть решений №27/№39/№43 в части обработки локаций без данных; остальные формулы и решения не пересматриваются |
| 46 | При успешном полном синке локации (NPC-станция или структура), по которой заказов нет, в `StockLevel` пишутся нулевые агрегаты для нужных типов; список локаций передаётся в пересчёт явно, включая пустые; запись идёт полной заменой в одной транзакции, старые строки при исчезновении заказов удаляются. `partial`-синк нулевые агрегаты не пишет |

---

## 21. Порядок реализации

1. **Каркас:** пакет, `pyproject`, `AppConfig`, модели, миграции, группы прав, пункт меню, `eveuniverse`-интеграция, хелперы прав.
2. **EFT-парсер + форма фита:** разбор, предпросмотр, ручная правка, сохранение состава (без расчётов). Тесты парсера.
3. **ESI-клиент и кэш:** синк Jita, регионов, структур; `MarketOrderCache`, `JitaOrderLevel`, агрегат `StockLevel`; задачи с guard-интервалами.
4. **Расчётное ядро:** чистые функции §6 + пересчёт фитов/компонентов + `status_cache`, `last_checked_at`.
5. **UI:** дашборд, карточка фита, компоненты, локации, настройки, ручное обновление с поллингом.
6. **Провайдеры:** приоритет, cooldown, отключение, `no_provider`, `NO_DATA`.
7. **Экспорт и уведомления:** multibuy/txt/csv, Discord, дедупликация, `NotificationLog`.
8. **Полировка:** i18n-каркас, admin `SyncRun`, `cleanup_orphan_cache`, документация на русском, полный прогон тестов и критериев приёмки.
9. **Сводка доступных кораблей:** вернуть рыночные синки в beat; добавить чистые функции $C+K$, агрегатного дефицита и общего статуса, поле `ships_status_cache`, событие `ships_deficit`, колонки и счётчики в UI, тесты группы 9 §18. Расхождения ESI-спеки с §9.2/§9.6 и решение №37 фиксируются отдельно без пересмотра принятых решений.

**Definition of Done:** все 20 критериев приёмки §19 выполняются; тесты §18 зелёные; миграции применяются на чистой БД AA 5; README описывает установку, привязку провайдеров, настройку наценки/порогов и диагностику `NO_DATA`; в коде нет обращений к ESI из веб-запросов.

---

## 22. Инструкции агенту-разработчику

1. Реализовать строго объём §1; ничего из §1.2 не добавлять.
2. Не менять решения §20. Если решение выглядит противоречивым — выбрать буквальное прочтение §20 и отметить это в комментарии к коду, но не расширять объём.
3. Все формулы — в `calc/engine.py` как чистые функции без обращений к БД и сети; вход — примитивы и `Decimal`. Только они содержат бизнес-логику; view и task их вызывают.
4. Запрещены `float` в денежных расчётах, N+1 в списках, запросы ESI в цикле по компонентам, `print` вместо логгера.
5. Все внешние вызовы (ESI, webhook) — с таймаутами, ретраями и записью результата.
6. Перед сдачей: `python manage.py makemigrations --check`, тесты, линт; тесты должны выполняться без сети.
7. Язык документации и комментариев к публичным функциям — русский; имена в коде — английские.
