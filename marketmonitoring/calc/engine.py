"""Расчётное ядро (§6 ТЗ): чистые функции без обращений к БД и сети.

Вход всех функций — примитивы и ``Decimal``. Все денежные величины — Decimal,
округление ISK — ROUND_HALF_UP до целого (§5.7). ``float`` в денежных расчётах
запрещён (§22.4). Только эти функции содержат бизнес-логику расчётов;
view и tasks их вызывают (§22.3).
"""

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal

# Статусы фита (§6.4); значения совпадают с marketmonitoring.constants.FitStatus.
STATUS_NO_DATA = "no_data"
STATUS_HULL_MISSING = "hull_missing"
STATUS_DEFICIT = "deficit"
STATUS_OK = "ok"

# Один ISK — целое число; квантование до целого с ROUND_HALF_UP (§5.7).
ONE_ISK = Decimal("1")
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

# В ESI raw_quantity == -2 означает Blueprint Copy (BPC), который не является готовым предметом (§6.12).
RAW_QUANTITY_BPC = -2


@dataclass(frozen=True)
class OrderBookRow:
    """Лот sell-стакана Jita (§6.6): цена и объём, ascending по цене."""

    price: Decimal
    volume: int


@dataclass(frozen=True)
class ComponentInput:
    """Позиция расчёта с уже собранной сервисом доступностью A_i (§6.2).

    Сервис (calc/service.py) резолвит область, свежесть и агрегат StockLevel;
    движок получает только примитивы.
    """

    type_id: int
    qty_per_fit: int
    availability: int  # A_i — суммарный sell-объём в области
    packaged_volume: Decimal = ZERO  # pvol типа (§6.9); 0 → доставка по позиции 0
    is_hull: bool = False


@dataclass(frozen=True)
class CoveringPriceResult:
    """Результат расчёта цены покрытия по стакану (§6.6).

    ``unit`` — цена покрытия (None при D<=0); при NOT_COVERED — средневзвешенная
    по всему хранимому стакану, ``shortfall`` = D - covered.
    """

    unit: Decimal | None
    covered: int
    shortfall: int
    is_not_covered: bool


@dataclass(frozen=True)
class ComponentCalcResult:
    """Полный расчёт одной позиции фита/запроса: дефицит, unit, стоимость, бейдж.

    Приоритет бейджей: NOT_COVERED > OVERPRICE (§6.8); оверпрайс-позиция
    входит в итоговую сумму.
    """

    type_id: int
    qty_per_fit: int
    availability: int
    deficit: int
    unit: Decimal | None
    ref: Decimal | None
    # Цена докупки отсутствует, когда рынок цены недоступен (выключенная Jita).
    cost: Decimal | None
    is_not_covered: bool
    shortfall: int | None
    is_overprice: bool
    # Процент превышения unit над ref (§6.8) для tooltip бейджа; None при отсутствии данных.
    overprice_pct: Decimal | None = None


def covering_price(book: list[OrderBookRow], deficit: int) -> CoveringPriceResult:
    """Цена покрытия unit по стакану Jita (§6.6).

    book — sell-лоты ascending по цене, уже отфильтрованные вызывающим по
    jita_book_depth. Один лот с v>=D → его цена; иначе средневзвешенная до
    минимального k, где накопленный объём >= D; иначе NOT_COVERED:
    covered = Σv, unit — средневзвешенная по всему book, shortfall = D - covered.
    """
    if deficit <= 0:
        return CoveringPriceResult(unit=None, covered=0, shortfall=0, is_not_covered=False)
    covered_total = 0
    weighted = ZERO
    for row in book:
        covered_total += row.volume
        weighted += row.price * row.volume
        if covered_total >= deficit:
            # Минимальный k, где накопленный объём покрывает D (§6.6).
            if row.volume >= deficit:
                # Единственный лот покрывает D целиком → его цена (случай 1).
                return CoveringPriceResult(unit=row.price, covered=covered_total, shortfall=0, is_not_covered=False)
            # Средневзвешенная до покрытия (случай 2); деление Decimal — точное.
            return CoveringPriceResult(
                unit=weighted / covered_total, covered=covered_total, shortfall=0, is_not_covered=False
            )
    # NOT_COVERED: unit — средневзвешенная по всему хранимому стакану (§6.6).
    unit = weighted / covered_total if covered_total > 0 else ZERO
    return CoveringPriceResult(unit=unit, covered=covered_total, shortfall=deficit - covered_total, is_not_covered=True)


def calc_deficit(qty_target: int, qty_per_fit: int, available: int) -> int:
    """Дефицит позиции D_i = max(0, N*q_i - A) (§6.5). Количество — целые."""
    return max(0, qty_target * qty_per_fit - available)


def calc_fits(availability: list[int], components: list[ComponentInput]) -> tuple[int, list[int]]:
    """Собираемость K и узкие места (§6.3).

    fits_i = floor(A_i / q_i); K = min_i fits_i; узкие места — все индексы i
    с fits_i == K. Возвращает (K, список индексов узких мест).
    """
    if not components:
        return 0, []
    fits = [availability[i] // components[i].qty_per_fit for i in range(len(components))]
    k = min(fits)
    bottlenecks = [i for i, value in enumerate(fits) if value == k]
    return k, bottlenecks


def calc_cost(
    unit: Decimal | None,
    shortfall_mode: bool,
    deficit: int,
    covered: int,
    markup_pct: Decimal,
    delivery_rate: Decimal,
    packaged_volume: Decimal,
) -> Decimal:
    """Стоимость докупки позиции (§6.9).

    cost = round((unit*(1+m) + c_del*pvol) * D), округление ROUND_HALF_UP до
    целого ISK; m = markup_pct/100; доставка без наценки. При shortfall_mode
    (NOT_COVERED, §6.6) множитель D заменяется на covered.
    """
    quantity = covered if shortfall_mode else deficit
    if quantity <= 0 or unit is None:
        return ZERO
    m = markup_pct / HUNDRED
    inner = unit * (ONE + m) + delivery_rate * packaged_volume
    return (inner * quantity).quantize(ONE_ISK, rounding=ROUND_HALF_UP)


def is_overprice(unit: Decimal | None, ref_price: Decimal | None, threshold_pct: Decimal) -> bool:
    """Бейдж OVERPRICE (§6.8): unit > ref*(1 + τ/100), ref = минимальная sell Jita.

    При отсутствии unit или ref оверпрайса нет.
    """
    if unit is None or ref_price is None:
        return False
    threshold = ref_price * (ONE + threshold_pct / HUNDRED)
    return unit > threshold


def calc_overprice_pct(unit: Decimal | None, ref_price: Decimal | None) -> Decimal | None:
    """Процент превышения цены покрытия над ref (§6.8): unit / ref * 100 - 100.

    Чистая функция для tooltip бейджа OVERPRICE; Decimal без float (§22.4),
    квантование до 0.01 с ROUND_HALF_UP. None при отсутствии unit/ref или ref == 0.
    """
    if unit is None or ref_price is None or ref_price == ZERO:
        return None
    pct = unit / ref_price * HUNDRED - HUNDRED
    return pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calc_fit_status(has_no_data: bool, hull_available: int, k: int, n: int, allow_hull_missing: bool = False) -> str:
    """Статус с приоритетом NO_DATA > HULL_MISSING > DEFICIT > OK (§6.4).

    ``allow_hull_missing=False`` исключает HULL_MISSING из ветвления (§6.10):
    компоненты вне фитов не имеют корпуса, и статус HULL_MISSING к ним
    не применяется (только OK / DEFICIT / NO_DATA).
    """
    if has_no_data:
        return STATUS_NO_DATA
    if allow_hull_missing and hull_available == 0:
        return STATUS_HULL_MISSING
    if k < n:
        return STATUS_DEFICIT
    return STATUS_OK


def calc_component(
    component: ComponentInput,
    qty_target: int,
    book: list[OrderBookRow],
    ref_price: Decimal | None,
    markup_pct: Decimal,
    delivery_rate: Decimal,
    overprice_threshold_pct: Decimal,
) -> ComponentCalcResult:
    """Полный расчёт одной позиции: дефицит, unit, стоимость, бейдж (§6.5–§6.9).

    Чистая функция: собирает результаты covering_price / calc_deficit /
    calc_cost / is_overprice. Бейдж NOT_COVERED имеет приоритет над OVERPRICE;
    при NOT_COVERED стоимость считается только за covered (§6.6).
    """
    deficit = calc_deficit(qty_target, component.qty_per_fit, component.availability)
    price = covering_price(book, deficit)
    cost = calc_cost(
        unit=price.unit,
        shortfall_mode=price.is_not_covered,
        deficit=deficit,
        covered=price.covered,
        markup_pct=markup_pct,
        delivery_rate=delivery_rate,
        packaged_volume=component.packaged_volume,
    )
    # При NOT_COVERED бейдж NOT_COVERED (приоритет выше OVERPRICE, §6.8).
    overpriced = (not price.is_not_covered) and is_overprice(price.unit, ref_price, overprice_threshold_pct)
    return ComponentCalcResult(
        type_id=component.type_id,
        qty_per_fit=component.qty_per_fit,
        availability=component.availability,
        deficit=deficit,
        unit=price.unit,
        ref=ref_price,
        cost=cost,
        is_not_covered=price.is_not_covered,
        shortfall=price.shortfall,
        is_overprice=overpriced,
        overprice_pct=calc_overprice_pct(price.unit, ref_price),
    )


def price_unavailable(result: ComponentCalcResult) -> ComponentCalcResult:
    """Обнуляет только ценовые поля позиции, когда рынок цены недоступен.

    Чистая функция: дефицит D_i, доступность A_i, qty_per_fit и статус фита
    остаются прежними (C/K и уведомления по решению №43 не теряются).
    ``unit/ref/cost/shortfall/overprice_pct`` становятся None, бейджи
    NOT_COVERED и OVERPRICE сбрасываются — ни stale-цен, ни нулевой стоимости.
    """
    return replace(
        result,
        unit=None,
        ref=None,
        cost=None,
        shortfall=None,
        overprice_pct=None,
        is_not_covered=False,
        is_overprice=False,
    )


@dataclass(frozen=True)
class ContractItemInput:
    """Позиция внутри контракта ESI для чистой проверки комплектности фита (§6.12)."""

    type_id: int
    quantity: int
    is_included: bool = True
    raw_quantity: int | None = None


@dataclass(frozen=True)
class ContractInput:
    """Примитив открытого item_exchange контракта для расчётного ядра (§6.12)."""

    contract_id: int
    items: tuple[ContractItemInput, ...] | list[ContractItemInput]


@dataclass(frozen=True)
class ContractMatchResult:
    """Подходящий контракт с числом полных комплектов кораблей в фите (§6.12)."""

    contract_id: int
    ships_count: int


@dataclass(frozen=True)
class ContractsFitCalcResult:
    """Итог проверки достаточности кораблей в фите по контрактам (§6.12)."""

    status: str
    ships_available: int | None
    deficit: int | None
    matched_contracts: list[ContractMatchResult]


def calc_contract_fit_sets(
    items: list[ContractItemInput] | tuple[ContractItemInput, ...],
    required_components: list[tuple[int, int]],
) -> int:
    """Число полных комплектов фита внутри одного контракта (§6.12).

    Учитываются только вложенные продавцом позиции (``is_included=True``) с
    положительным количеством, не являющиеся BPC (``raw_quantity != -2``).
    Если хотя бы одной позиции фита (корпуса, модуля, рига, дрона или заряда)
    не хватает, возвращает 0.
    """
    if not required_components:
        return 0
    totals_by_type: dict[int, int] = {}
    for item in items:
        if not item.is_included or item.raw_quantity == RAW_QUANTITY_BPC or item.quantity <= 0:
            continue
        totals_by_type[item.type_id] = totals_by_type.get(item.type_id, 0) + item.quantity
    sets_per_component: list[int] = []
    for type_id, qty_per_fit in required_components:
        if qty_per_fit <= 0:
            return 0
        sets_per_component.append(totals_by_type.get(type_id, 0) // qty_per_fit)
    return min(sets_per_component)


def calc_contracts_fit(
    contracts: list[ContractInput],
    required_components: list[tuple[int, int]],
    qty_target: int,
    has_no_data: bool,
) -> ContractsFitCalcResult:
    """Проверка достаточности кораблей в фите по открытым контрактам (§6.12).

    Чистая функция: дедуплицирует контракты по ``contract_id``, считает полные
    комплекты ``ships_c`` в каждом контракте, суммирует ``C``, вычисляет дефицит
    ``max(0, N - C)`` и статус ``no_data | deficit | ok``.
    """
    if has_no_data or not required_components:
        return ContractsFitCalcResult(
            status=STATUS_NO_DATA,
            ships_available=None,
            deficit=None,
            matched_contracts=[],
        )
    seen_ids: set[int] = set()
    matched: list[ContractMatchResult] = []
    for contract in contracts:
        if contract.contract_id in seen_ids:
            continue
        seen_ids.add(contract.contract_id)
        ships = calc_contract_fit_sets(contract.items, required_components)
        if ships > 0:
            matched.append(ContractMatchResult(contract_id=contract.contract_id, ships_count=ships))
    total_ships = sum(m.ships_count for m in matched)
    deficit = calc_deficit(qty_target, 1, total_ships)
    status = STATUS_DEFICIT if total_ships < qty_target else STATUS_OK
    return ContractsFitCalcResult(
        status=status,
        ships_available=total_ships,
        deficit=deficit,
        matched_contracts=matched,
    )


@dataclass(frozen=True)
class ShipsAvailabilityResult:
    """Итоговая доступность кораблей C+K по фиту (§6.13, решение №44).

    ``is_partial`` истинно, если хотя бы один источник (контракты или рынок)
    имеет статус ``no_data``/неизвестное значение: в этом случае итог и
    агрегатный дефицит не рассчитываются (решение №43).
    """

    ships_in_contracts: int | None
    ships_from_market: int | None
    ships_total: int | None
    ships_deficit: int | None
    ships_status: str
    is_partial: bool


def calc_ships_total(contracts_available: int | None, market_available: int | None) -> int | None:
    """Итог доступных кораблей C+K (§6.13): C из контрактов, K с рынка.

    Возвращает None, если любая из величин неизвестна; Jita в доступность
    не входит (решение №39).
    """
    if contracts_available is None or market_available is None:
        return None
    return contracts_available + market_available


def calc_ships_deficit(qty_target: int, contracts_available: int | None, market_available: int | None) -> int | None:
    """Агрегатный дефицит max(0, N-(C+K)) (§6.13); None при неизвестном C или K."""
    total = calc_ships_total(contracts_available, market_available)
    if total is None:
        return None
    return max(0, qty_target - total)


def calc_ships_availability(
    qty_target: int,
    contracts_available: int | None,
    market_available: int | None,
    contracts_status: str | None,
    market_status: str | None,
) -> ShipsAvailabilityResult:
    """Сводная доступность кораблей: итог, агрегатный дефицит и общий статус (§6.13).

    Статус ``no_data`` при любом неизвестном значении/статусе ``no_data``;
    иначе ``deficit`` при агрегатном дефиците > 0, иначе ``ok``. Рыночный
    статус ``hull_missing`` не является ``no_data``: K уже посчитан, общий
    статус определяется только дефицитом C+K (решение №43).
    """
    has_no_data = (
        contracts_available is None
        or market_available is None
        or contracts_status in (None, "", STATUS_NO_DATA)
        or market_status in (None, "", STATUS_NO_DATA)
    )
    total = None if has_no_data else calc_ships_total(contracts_available, market_available)
    deficit = None if has_no_data else calc_ships_deficit(qty_target, contracts_available, market_available)
    if has_no_data:
        status = STATUS_NO_DATA
    elif deficit is not None and deficit > 0:
        status = STATUS_DEFICIT
    else:
        status = STATUS_OK
    return ShipsAvailabilityResult(
        ships_in_contracts=contracts_available,
        ships_from_market=market_available,
        ships_total=total,
        ships_deficit=deficit,
        ships_status=status,
        is_partial=has_no_data,
    )
