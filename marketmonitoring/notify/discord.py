"""Discord-уведомления (§13): webhook, embed-форматирование, журналирование.

Все вызовы сети — только отсюда (UI к сети не обращается, §5.10). POST через
``requests`` с таймаутом 10 с и 3 ретрями backoff 2/4/8 с; результат каждой
попытки фиксируется в NotificationLog (§8, §13). Дедупликация событий
provider_acl_lost / sync_failed / sync_recovered — по переходу состояния:
отправка, только если последняя ok-запись этого события имеет другой
state_hash (SHA-256 от канонической JSON-строки, §13.1). Если webhook не
настроен/отключён — отправки нет, лог не создаётся (нет url — нет события).
"""

import hashlib
import json
import logging
import time
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from ..constants import NotificationEvent
from ..models import Fit, NotificationLog, WebhookConfig

logger = logging.getLogger("marketmonitoring")

# Параметры HTTP (§13): таймаут 10 с, 3 ретрая, backoff 2/4/8 с.
REQUEST_TIMEOUT = 10
RETRY_DELAYS = (2, 4, 8)

# Цвета embed (§13.2).
COLOR_DEFICIT = 0xE67E22  # оранжевый
COLOR_ERROR = 0xE74C3C  # красный
COLOR_OK = 0x2ECC71  # зелёный


def canonical_state_hash(payload: dict) -> str:
    """SHA-256 от канонической JSON-строки (§13.1): json.dumps sort_keys=True."""
    canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_webhook_url() -> str | None:
    """Активный webhook (§13): WebhookConfig с enabled=True; иначе None."""
    config = WebhookConfig.objects.filter(enabled=True).first()
    if config and config.url:
        return config.url
    return None


def _card_url(fit: Fit) -> str:
    """Полная ссылка на карточку фита: SITE_URL + reverse (§13.2)."""
    base = getattr(settings, "SITE_URL", "") or ""
    path = reverse("marketmonitoring:fit_detail", args=[fit.pk])
    return base.rstrip("/") + path


def _last_ok_log(event: str, fit=None) -> NotificationLog | None:
    """Последняя успешная отправка события (для дедупликации по переходу, §13.1)."""
    return NotificationLog.objects.filter(event=event, ok=True, fit=fit).order_by("-sent_at", "-id").first()


def send_webhook(
    embed: dict,
    event: str,
    state_hash: str,
    fit: Fit | None = None,
    component=None,
    dedup_transition: bool = False,
) -> tuple[bool, str]:
    """Отправка embed в Discord webhook с ретрями и журналированием (§13).

    Возвращает (ok, error). При dedup_transition отправка выполняется только
    если state_hash отличается от последней ok-записи этого события (переход
    состояния). Нет webhook — (False, "no webhook configured"), лог не пишется.
    """
    if dedup_transition:
        last = _last_ok_log(event, fit)
        if last is not None and last.state_hash == state_hash:
            logger.debug("Notification %s skipped: state unchanged (hash %s)", event, state_hash[:12])
            return False, "state unchanged"
    url = get_webhook_url()
    if not url:
        # Нет url — нет события: не отправлять и лог не создавать (§13).
        return False, "no webhook configured"
    payload = {"embeds": [embed]}
    ok = False
    error = ""
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if 200 <= response.status_code < 300:
                ok = True
                error = ""
                break
            error = f"HTTP {response.status_code}: {response.text[:500]}"
        except requests.RequestException as exc:
            error = str(exc)
        if attempt < len(RETRY_DELAYS):
            time.sleep(RETRY_DELAYS[attempt])
    NotificationLog.objects.create(
        event=event,
        fit=fit,
        component=component,
        state_hash=state_hash,
        sent_at=timezone.now(),
        ok=ok,
        error=error,
    )
    if not ok:
        logger.warning("Discord webhook for %s failed after retries: %s", event, error)
    return ok, error


def send_deficit(
    fit: Fit,
    k: int | None,
    n: int,
    bottlenecks: list[str],
    total_cost: Decimal | None,
    overprice_count: int,
    state_hash: str,
) -> tuple[bool, str]:
    """Уведомление deficit (§13, §13.2): фит, K/N, до 5 узких мест, сумма
    докупки, число оверпрайс-позиций, ссылка на карточку; color оранжевый.
    Дедупликация по cooldown выполняется вызывающим (tasks.notify_deficit).
    """
    fields = [
        {"name": "Fits available", "value": f"{k if k is not None else '?'}/{n}", "inline": True},
    ]
    if bottlenecks:
        fields.append({"name": "Bottlenecks", "value": "\n".join(bottlenecks[:5]), "inline": False})
    if total_cost is not None:
        rounded = total_cost.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        fields.append({"name": "Total buy cost", "value": f"{rounded} ISK", "inline": True})
    fields.append({"name": "Overpriced positions", "value": str(overprice_count), "inline": True})
    embed = {
        "title": f"DEFICIT: {fit.name}",
        "color": COLOR_DEFICIT,
        "url": _card_url(fit),
        "fields": fields,
    }
    return send_webhook(
        embed=embed,
        event=NotificationEvent.DEFICIT,
        state_hash=state_hash,
        fit=fit,
    )


def send_hull_missing(fit: Fit, scope_label: str, state_hash: str) -> tuple[bool, str]:
    """Уведомление hull_missing (§13): фит, область, ссылка; color красный."""
    embed = {
        "title": f"HULL MISSING: {fit.name}",
        "color": COLOR_ERROR,
        "url": _card_url(fit),
        "fields": [{"name": "Scope", "value": scope_label, "inline": False}],
    }
    return send_webhook(
        embed=embed,
        event=NotificationEvent.HULL_MISSING,
        state_hash=state_hash,
        fit=fit,
        dedup_transition=True,
    )


def send_provider_acl_lost(
    provider=None,
    character_id=None,
    acl_fail_count: int = 0,
    status: str = "",
    disabled: bool = False,
    event: str = NotificationEvent.PROVIDER_ACL_LOST,
    location: str | None = None,
) -> bool:
    """Уведомление provider_acl_lost (§13): персонаж, локация, acl_fail_count,
    статус. Сигнатура согласована с guarded-вызовом providers/service.py
    (importlib + hasattr, §11.3). Кулдауном не ограничено, но отправляется
    только при переходе состояния (§13.1).
    """
    char_label = str(character_id)
    if provider is not None and getattr(provider, "character", None) is not None:
        char_label = f"{provider.character.character_name} ({character_id})"
    state_hash = canonical_state_hash(
        {
            "character_id": character_id,
            "acl_fail_count": acl_fail_count,
            "status": str(status),
            "disabled": bool(disabled),
        }
    )
    fields = [
        {"name": "Character", "value": char_label, "inline": True},
        {"name": "ACL failures", "value": str(acl_fail_count), "inline": True},
        {"name": "Status", "value": f"{status}{' (disabled)' if disabled else ''}", "inline": True},
    ]
    if location:
        fields.append({"name": "Location", "value": location, "inline": False})
    embed = {
        "title": "PROVIDER ACL LOST" if not disabled else "PROVIDER DISABLED (ACL)",
        "color": COLOR_ERROR,
        "fields": fields,
    }
    ok, _error = send_webhook(
        embed=embed,
        event=event,
        state_hash=state_hash,
        dedup_transition=True,
    )
    return ok


def send_sync_failed(task_name: str, errors: int, brief: str, state_hash: str) -> tuple[bool, str]:
    """Уведомление sync_failed (§13): задача, число ошибок, краткий текст; красный."""
    embed = {
        "title": f"SYNC FAILED: {task_name}",
        "color": COLOR_ERROR,
        "fields": [
            {"name": "Errors", "value": str(errors), "inline": True},
            {"name": "Brief", "value": brief[:1000] or "—", "inline": False},
        ],
    }
    return send_webhook(embed=embed, event=NotificationEvent.SYNC_FAILED, state_hash=state_hash)


def send_sync_recovered(task_name: str, downtime_min: int, state_hash: str) -> tuple[bool, str]:
    """Уведомление sync_recovered (§13): задача, длительность простоя; зелёный."""
    embed = {
        "title": f"SYNC RECOVERED: {task_name}",
        "color": COLOR_OK,
        "fields": [{"name": "Downtime", "value": f"{downtime_min} min", "inline": True}],
    }
    return send_webhook(embed=embed, event=NotificationEvent.SYNC_RECOVERED, state_hash=state_hash)
