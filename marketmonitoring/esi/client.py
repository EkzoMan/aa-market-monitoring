"""ESI-клиент плагина: обёртки django-esi с батчингом, ретраями и учётом лимитов (§9).

UI никогда не обращается к ESI — только Celery-задачи через этот модуль (§5).
Плагин токены не хранит: токены берутся из AA (`esi.models.Token`) по character_id и скоупам.
"""

import logging
import time
from typing import Any, Iterable, Iterator

import requests
from django.utils import timezone
from esi.exceptions import ESIErrorLimitException, HTTPClientError, HTTPServerError
from esi.models import Token
from esi.openapi_clients import ESIClientProvider

from ..models import PluginSetting

logger = logging.getLogger("marketmonitoring")

# Скоупы провайдера структур (§9.1, §11).
SCOPE_STRUCTURE_MARKETS = "esi-markets.structure_markets.v1"
SCOPE_READ_STRUCTURES = "esi-universe.read_structures.v1"
PROVIDER_SCOPES = [SCOPE_STRUCTURE_MARKETS, SCOPE_READ_STRUCTURES]

# Порог заголовка X-ESI-Error-Limit-Remain (§9.2).
ERROR_LIMIT_REMAIN_THRESHOLD = 10

# Ретраи: 3 попытки, backoff 2/4/8 с (§9.2).
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2, 4, 8)

# Публичные операции ESI, используемые плагином (фильтр спеки для экономии памяти).
ESI_OPERATIONS = [
    "GetMarketsRegionIdOrders",
    "GetMarketsStructuresStructureId",
    "GetUniverseStructuresStructureId",
]

# Собственный провайдер django-esi: `esi.client.Market.GetMarketsRegionIdOrders` и т.д.
esi = ESIClientProvider(
    ua_appname="aa-market-monitoring",
    ua_version="1.0.0",
    compatibility_date="2026-08-18",
    operations=ESI_OPERATIONS,
)


class RateLimitStop(Exception):
    """Исключение «осталось < 10 ошибок ESI» — задачу нужно прервать и пометить partial (§9.2)."""

    def __init__(self, reset_seconds: int | None = None) -> None:
        super().__init__(f"ESI error limit remain < {ERROR_LIMIT_REMAIN_THRESHOLD}")
        self.reset_seconds = reset_seconds


def _get_header(headers: Any, name: str) -> str | None:
    """Чтение заголовка независимо от типа объекта headers (dict, aiopenapi3-обёртка и т.п.)."""
    if headers is None:
        return None
    # 1) словарь-подобный интерфейс
    getter = getattr(headers, "get", None)
    if callable(getter):
        try:
            value = getter(name)
            if value is not None:
                return value
        except Exception:
            pass
    # 2) доступ по индексу
    try:
        return headers[name]
    except (KeyError, IndexError, TypeError):
        pass
    # 3) доступ как к атрибуту (в т.ч. case-insensitive для common-имён)
    attr = name.replace("-", "_").lower()
    value = getattr(headers, attr, None)
    if value is not None:
        return value
    return getattr(headers, name, None)


def _get_error_limit_remain(response: Any) -> int | None:
    """Извлечение X-ESI-Error-Limit-Remain из заголовков ответа (если есть)."""
    headers = getattr(response, "headers", None)
    raw = _get_header(headers, "X-ESI-Error-Limit-Remain")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _check_error_limit(response: Any) -> None:
    """Проверка глобального лимита ошибок ESI: остаток < 10 → останов (§9.2)."""
    remain = _get_error_limit_remain(response)
    if remain is not None and remain < ERROR_LIMIT_REMAIN_THRESHOLD:
        reset_raw = getattr(response.headers, "get", lambda *_: None)("X-ESI-Error-Limit-Reset")
        try:
            reset_seconds = int(reset_raw) if reset_raw else None
        except (TypeError, ValueError):
            reset_seconds = None
        logger.warning("ESI error limit remain=%s < %s — stopping", remain, ERROR_LIMIT_REMAIN_THRESHOLD)
        raise RateLimitStop(reset_seconds)


def _call_with_retry(op_result_fn):
    """Вызов операции ESI с ретраями: 3 попытки, backoff 2/4/8 с на 5xx, таймаутах и сетевых сбоях (§9.2).

    На 420 (error limit) — пауза до Retry-After/reset, затем RateLimitStop.
    """

    # Сетевые сбои и таймауты ретраятся так же, как 5xx (§9.2).
    RETRYABLE_NETWORK_ERRORS = (requests.Timeout, requests.ConnectionError)

    def _sleep_until(reset_seconds: int | None) -> None:
        if reset_seconds and reset_seconds > 0:
            # Буквальное прочтение §9.2: пауза до Retry-After, без капа
            # (в Celery-задаче долгая пауза допустима).
            logger.info("ESI 420: pausing %s s until Retry-After", reset_seconds)
            time.sleep(reset_seconds)

    attempt = 0
    while True:
        try:
            return op_result_fn()
        except ESIErrorLimitException as exc:
            reset = getattr(exc, "reset", None)
            _sleep_until(int(reset) if reset else None)
            raise RateLimitStop(int(reset) if reset else None) from exc
        except (HTTPServerError, *RETRYABLE_NETWORK_ERRORS) as exc:
            if attempt >= RETRY_ATTEMPTS - 1:
                raise
            delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning("ESI retry %s/%s in %s s after %s", attempt + 1, RETRY_ATTEMPTS, delay, type(exc).__name__)
            time.sleep(delay)
            attempt += 1


def _page_results(op, **params) -> Iterator[tuple[list[dict], Any]]:
    """Обход страниц операции постранично; конец — по заголовку X-Pages или пустой странице.

    Примечание: параметр page_size в текущей версии ESI не поддерживается
    (эндпоинты всегда отдают до 1000 записей на страницу).
    Возвращает пары (строка-данных, response) — заказчик проверяет заголовки лимита.
    """
    page = 1
    total_pages: int | None = None
    while True:
        params["page"] = page

        def _fetch(p=params.copy()):
            # use_etag=False: стакан заказов меняется постоянно, ETag тут бесполезен.
            return op(**p).result(use_etag=False, return_response=True, use_cache=False)

        data, response = _call_with_retry(_fetch)
        _check_error_limit(response)
        orders = data if isinstance(data, list) else ([] if data is None else [data])
        yield orders, response
        # Конец пагинации: по X-Pages (ESI 404 "Requested page does not exist!"
        # вместо пустой страницы, если запросить страницу за границей).
        headers = getattr(response, "headers", None)
        raw_pages = _get_header(headers, "X-Pages")
        if raw_pages is not None:
            try:
                total_pages = int(raw_pages)
            except (TypeError, ValueError):
                pass
        if not orders or (total_pages and page >= total_pages):
            return
        page += 1


def fetch_jita_orders(type_ids: list[int]) -> tuple[list[dict], bool]:
    """Снимок стакана Jita (§9.1, §9.2): sell-заказы региона Jita по типам расчёта.

    В текущей версии ESI type_id — одиночный параметр, поэтому запрос идёт
    по одному типу за вызов (page_size не поддерживается; конец пагинации — X-Pages).
    Возвращает (заказы, partial): partial=True при нехватке лимита ошибок ESI.
    """
    settings_obj = PluginSetting.load()
    region_id = settings_obj.jita_region_id
    orders: list[dict] = []
    partial = False
    op = esi.client.Market.GetMarketsRegionIdOrders
    for type_id in type_ids:
        try:
            for batch_orders, _resp in _page_results(op, region_id=region_id, order_type="sell", type_id=type_id):
                orders.extend(batch_orders)
        except RateLimitStop:
            # Нехватка лимита ошибок: помечаем partial и останавливаем обход (§9.2).
            partial = True
            break
    return orders, partial


def fetch_region_orders(region_id: int) -> tuple[list[dict], bool]:
    """Полный обход sell-заказов региона (§9.1, §9.2) без токена, страницы до пустой.

    Возвращает (заказы, partial): partial=True при нехватке лимита ошибок ESI.
    """
    orders: list[dict] = []
    op = esi.client.Market.GetMarketsRegionIdOrders
    try:
        for page_orders, _resp in _page_results(op, region_id=region_id, order_type="sell"):
            orders.extend(page_orders)
        return orders, False
    except RateLimitStop:
        return orders, True


def get_provider_token(character_id: int | None = None) -> Token | None:
    """Токен из AA по character_id и скоупам структуры (§9.1, §11); плагин токены не хранит."""
    qs = (
        Token.objects.filter(character_id=character_id)
        .require_scopes(SCOPE_STRUCTURE_MARKETS)
        .require_scopes(SCOPE_READ_STRUCTURES)
        .require_valid()
        .order_by("pk")
    )
    return qs.first()


def _token_for_provider(character_id: int) -> Token:
    """Токен конкретного провайдера; при отсутствии валидного — ValueError (§11)."""
    token = get_provider_token(character_id)
    if token is None:
        raise ValueError(f"No valid AA token with scopes {PROVIDER_SCOPES} for character {character_id}")
    return token


def fetch_structure_orders(structure_id: int, character_id: int) -> tuple[list[dict], bool]:
    """Sell-заказы структуры с токеном провайдера (§9.1): скоуп esi-markets.structure_markets.v1.

    Возвращает (заказы, partial): partial=True при нехватке лимита ошибок ESI.
    """
    token = _token_for_provider(character_id)
    orders: list[dict] = []
    op = esi.client.Market.GetMarketsStructuresStructureId
    try:
        for page_orders, _resp in _page_results(op, structure_id=structure_id, order_type="sell", token=token):
            orders.extend(page_orders)
        return orders, False
    except RateLimitStop:
        return orders, True


def fetch_structure_info(structure_id: int, character_id: int) -> dict:
    """Информация о структуре (§9.1): имя и система; скоуп esi-universe.read_structures.v1."""
    token = _token_for_provider(character_id)

    def _fetch():
        return esi.client.Universe.GetUniverseStructuresStructureId(structure_id=structure_id, token=token).result(
            use_etag=False
        )

    return _call_with_retry(_fetch)


def _order_field(order: Any, name: str, default: Any = None) -> Any:
    """Чтение поля ордера ESI: django-esi 9.9 отдаёт pydantic-модели (атрибуты), старые версии — dict."""
    if isinstance(order, dict):
        return order.get(name, default)
    return getattr(order, name, default)


def build_order_cache_rows(
    orders: Iterable[Any],
    *,
    source_kind: str,
    source_ref: int,
) -> list[dict]:
    """Преобразование сырых заказов ESI в строки MarketOrderCache (без записи в БД).

    Фильтруются только sell-заказы (is_buy_order=False); fetched_at — момент синка.
    """

    now = timezone.now()
    rows: list[dict] = []
    for order in orders:
        if _order_field(order, "is_buy_order", False):
            continue
        volume = _order_field(order, "volume_remain", None)
        if volume is None:
            volume = _order_field(order, "volume_total", 0)
        rows.append(
            {
                "source_kind": source_kind,
                "source_ref": source_ref,
                "location_id": _order_field(order, "location_id"),
                "solar_system_id": _order_field(order, "system_id"),
                "region_id": _order_field(order, "region_id") or source_ref,
                "type_id": _order_field(order, "type_id"),
                "price": _order_field(order, "price"),
                "volume": int(volume or 0),
                "fetched_at": now,
            }
        )
    return rows
