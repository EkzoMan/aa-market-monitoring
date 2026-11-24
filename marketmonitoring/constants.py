"""Константы и перечисления плагина: статусы синков, виды локаций/областей, события уведомлений."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class SyncStatus(models.TextChoices):
    """Статус синка рыночных данных локации (§8 MarketLocation.sync_status)."""

    OK = "ok", _("ok")
    NO_ACL = "no_acl", _("no_acl")
    ERROR = "error", _("error")
    NO_PROVIDER = "no_provider", _("no_provider")
    STALE = "stale", _("stale")


class LocationKind(models.TextChoices):
    """Вид локации мониторинга (§8 MarketLocation.kind).

    SYSTEM — локация-система: расширяет область поиска контрактов (§6.12)
    всеми локациями выдачи системы, если система входит в область фита.
    """

    STATION = "station", _("station")
    STRUCTURE = "structure", _("structure")
    SYSTEM = "system", _("system")


class ScopeKind(models.TextChoices):
    """Вид области доступности (§8 Fit.scope_kind, ComponentRequest.scope_kind)."""

    STATION = "station", _("station")
    SYSTEM = "system", _("system")
    REGION = "region", _("region")


class FitStatus(models.TextChoices):
    """Статус фита в кэше расчёта (§8 Fit.status_cache, §6.4)."""

    OK = "ok", _("OK")
    DEFICIT = "deficit", _("DEFICIT")
    HULL_MISSING = "hull_missing", _("HULL_MISSING")
    NO_DATA = "no_data", _("NO_DATA")


class ContractFitStatus(models.TextChoices):
    """Статус обеспеченности фита кораблями в контрактах (§6.12, §8 Fit.contracts_status_cache)."""

    OK = "ok", _("OK")
    DEFICIT = "deficit", _("DEFICIT")
    NO_DATA = "no_data", _("NO_DATA")


class ContractSourceKind(models.TextChoices):
    """Вид источника контрактов ESI (§8 ContractProvider.source_kind, ContractCache.source_kind)."""

    CORPORATION = "corporation", _("corporation")
    CHARACTER = "character", _("character")


class ProviderStatus(models.TextChoices):
    """Статус провайдера структур (§8 StructureProvider.status, §11)."""

    OK = "ok", _("ok")
    COOLING_DOWN = "cooling_down", _("cooling_down")
    DISABLED = "disabled", _("disabled")


class SourceKind(models.TextChoices):
    """Вид источника рыночных данных (§8 MarketOrderCache.source_kind)."""

    REGION = "region", _("region")
    STRUCTURE = "structure", _("structure")


class TriggerKind(models.TextChoices):
    """Триггер запуска задачи Celery (§12.3, SyncRun.trigger)."""

    BEAT = "beat", _("beat")
    MANUAL = "manual", _("manual")


class SyncRunStatus(models.TextChoices):
    """Статус прогона задачи (§8 SyncRun.status)."""

    RUNNING = "running", _("running")
    OK = "ok", _("ok")
    PARTIAL = "partial", _("partial")
    FAILED = "failed", _("failed")


class NotificationEvent(models.TextChoices):
    """События уведомлений Discord (§13)."""

    DEFICIT = "deficit", _("deficit")
    CONTRACTS_DEFICIT = "contracts_deficit", _("contracts_deficit")
    HULL_MISSING = "hull_missing", _("hull_missing")
    PROVIDER_ACL_LOST = "provider_acl_lost", _("provider_acl_lost")
    SYNC_FAILED = "sync_failed", _("sync_failed")
    SYNC_RECOVERED = "sync_recovered", _("sync_recovered")


class DisplayLanguageMode(models.TextChoices):
    """Режим отображения имён типов (§10 display_language_mode)."""

    CLIENT_DEFAULT = "client_default", _("client_default")
    FIXED_EN = "fixed_en", _("fixed_en")
