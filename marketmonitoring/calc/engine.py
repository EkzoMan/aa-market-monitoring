"""Расчётное ядро (§6 ТЗ): чистые функции без обращений к БД и сети.

Вход всех функций — примитивы и ``Decimal``. Все денежные величины — Decimal,
округление ISK — ROUND_HALF_UP до целого (§5.7). ``float`` в денежных расчётах
запрещён (§22.4). Только эти функции содержат бизнес-логику расчётов;
view и tasks их вызывают (§22.3).
"""

from dataclasses import dataclass
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
    cost: Decimal
    is_not_covered: bool
    shortfall: int
    is_overprice: bool


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
    )
