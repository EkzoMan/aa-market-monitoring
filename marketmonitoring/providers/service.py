"""Пул провайдеров структур (§11): выбор, cooldown, ACL-сбои, отключение.

Все операции — только с БД плагина и токенами AA; сети здесь нет.
Esi-вызовы выполняют Celery-задачи (tasks.py) через esi/client.py (§5.10).
"""

import importlib
import logging
from datetime import timedelta

from django.utils import timezone

from ..constants import NotificationEvent, ProviderStatus
from ..models import ContractProvider, PluginSetting, StructureProvider

logger = logging.getLogger("marketmonitoring")

# Имя точки вызова алерта в notify-модуле (этап 7); вызов защищён hasattr (§11.3).
NOTIFY_MODULE = "marketmonitoring.notify.discord"
NOTIFY_FUNC = "send_provider_acl_lost"


def _available_providers(exclude_character_ids: set[int] | None = None):
    """QuerySet провайдеров, доступных для выбора (§11): enabled, не disabled,
    не в активном cooldown; сортировка по priority, затем character_id."""
    now = timezone.now()
    # Буквальное прочтение §11: cooling_down доступен, если cooldown_until <= now.
    qs = (
        StructureProvider.objects.filter(enabled=True)
        .exclude(status=ProviderStatus.DISABLED)
        .filter(_cooldown_ok_q(now))
    )
    if exclude_character_ids:
        qs = qs.exclude(character_id__in=exclude_character_ids)
    # §11 сортировка: по CCP character_id (character__character_id), а не по pk FK.
    return qs.order_by("priority", "character__character_id")


def _cooldown_ok_q(now):
    """Q-объект: провайдер не в активном cooldown (§11)."""
    from django.db.models import Q

    return ~Q(status=ProviderStatus.COOLING_DOWN, cooldown_until__gt=now)


def select_provider(structure_id: int, exclude_character_ids: set[int] | None = None) -> StructureProvider | None:
    """Выбор провайдера для структуры (§11): среди enabled=true, status != disabled,
    (status != cooling_down ИЛИ cooldown_until <= now) — по возрастанию priority,
    затем character_id. Возвращает StructureProvider или None (локация no_provider).

    structure_id в выборке не участвует (пул общий для всех структур), параметр
    сохранён для сигнатуры задач и будущего расширения.
    """
    return _available_providers(exclude_character_ids).first()


def cooldown_expired(provider: StructureProvider) -> bool:
    """Хелпер: истёк ли cooldown провайдера (§11).

    True, если провайдер не в cooldown либо cooldown_until <= now.
    """
    if provider.status != ProviderStatus.COOLING_DOWN:
        return True
    return provider.cooldown_until is None or provider.cooldown_until <= timezone.now()


def _notify_acl_lost(provider: StructureProvider, *, disabled: bool) -> None:
    """Точка вызова алерта provider_acl_lost (§11.3).

    notify-модуль появляется на этапе 7; пока функция вызывается через
    guarded import (importlib + hasattr) и молча пропускается, если её нет.
    """
    try:
        module = importlib.import_module(NOTIFY_MODULE)
    except ImportError:
        return
    func = getattr(module, NOTIFY_FUNC, None)
    if not callable(func):
        return
    try:
        func(
            provider=provider,
            character_id=provider.character_id,
            acl_fail_count=provider.acl_fail_count,
            status=provider.status,
            disabled=disabled,
            event=NotificationEvent.PROVIDER_ACL_LOST,
        )
    except Exception:  # noqa: BLE001 — сбой уведомления не должен ломать обработку провайдера
        logger.exception("provider_acl_lost notification hook failed")


def handle_acl_failure(provider: StructureProvider) -> StructureProvider:
    """Обработка 403/401 от ESI для провайдера (§11).

    acl_fail_count += 1; status=cooling_down; cooldown_until = now +
    provider_acl_cooldown_min (из PluginSetting). При достижении
    provider_acl_max_failures — status=disabled и алерт event
    provider_acl_lost с пометкой «отключён». Возвращает обновлённый объект.
    """
    settings_obj = PluginSetting.load()
    provider.acl_fail_count += 1
    if provider.acl_fail_count >= settings_obj.provider_acl_max_failures:
        provider.status = ProviderStatus.DISABLED
        provider.cooldown_until = None
        disabled = True
        logger.warning("Provider %s disabled after %s ACL failures", provider.character_id, provider.acl_fail_count)
    else:
        provider.status = ProviderStatus.COOLING_DOWN
        provider.cooldown_until = timezone.now() + timedelta(minutes=settings_obj.provider_acl_cooldown_min)
        disabled = False
        logger.info(
            "Provider %s cooling down until %s (ACL failure %s/%s)",
            provider.character_id,
            provider.cooldown_until,
            provider.acl_fail_count,
            settings_obj.provider_acl_max_failures,
        )
    provider.save(update_fields=["acl_fail_count", "status", "cooldown_until"])
    _notify_acl_lost(provider, disabled=disabled)
    return provider


def handle_provider_success(provider: StructureProvider) -> StructureProvider:
    """Успешный запрос (200) сбрасывает acl_fail_count=0 и status=ok (§11.4)."""
    if provider.acl_fail_count != 0 or provider.status != ProviderStatus.OK:
        provider.acl_fail_count = 0
        provider.status = ProviderStatus.OK
        provider.cooldown_until = None
        provider.save(update_fields=["acl_fail_count", "status", "cooldown_until"])
    return provider


def switch_to_next_provider(structure_id: int, failed_character_ids: set[int]) -> StructureProvider | None:
    """Структура после 403 переобрабатывается следующим доступным провайдером
    в том же прогоне (§11.2): выбор с исключением уже отказавших персонажей."""
    return select_provider(structure_id, exclude_character_ids=set(failed_character_ids))


def available_contract_providers():
    """Доступные провайдеры контрактов (§11, §6.12): enabled, не disabled, не в активном cooldown."""
    now = timezone.now()
    return (
        ContractProvider.objects.select_related("character")
        .filter(enabled=True)
        .exclude(status=ProviderStatus.DISABLED)
        .filter(_cooldown_ok_q(now))
        .order_by("priority", "character__character_id", "pk")
    )


def handle_contract_provider_acl_failure(provider: ContractProvider) -> ContractProvider:
    """Обработка 401/403 от ESI для провайдера контрактов (§11): cooldown и отключение при пороге."""
    settings_obj = PluginSetting.load()
    provider.acl_fail_count += 1
    if provider.acl_fail_count >= settings_obj.provider_acl_max_failures:
        provider.status = ProviderStatus.DISABLED
        provider.cooldown_until = None
        disabled = True
        logger.warning(
            "Contract provider %s (%s) disabled after %s ACL failures",
            provider.character_id,
            provider.source_kind,
            provider.acl_fail_count,
        )
    else:
        provider.status = ProviderStatus.COOLING_DOWN
        provider.cooldown_until = timezone.now() + timedelta(minutes=settings_obj.provider_acl_cooldown_min)
        disabled = False
        logger.info(
            "Contract provider %s (%s) cooling down until %s (ACL failure %s/%s)",
            provider.character_id,
            provider.source_kind,
            provider.cooldown_until,
            provider.acl_fail_count,
            settings_obj.provider_acl_max_failures,
        )
    provider.save(update_fields=["acl_fail_count", "status", "cooldown_until"])
    _notify_acl_lost(provider, disabled=disabled)
    return provider


def handle_contract_provider_success(provider: ContractProvider) -> ContractProvider:
    """Успешный запрос контрактов сбрасывает acl_fail_count=0 и status=ok (§11.4)."""
    if provider.acl_fail_count != 0 or provider.status != ProviderStatus.OK:
        provider.acl_fail_count = 0
        provider.status = ProviderStatus.OK
        provider.cooldown_until = None
        provider.save(update_fields=["acl_fail_count", "status", "cooldown_until"])
    return provider
