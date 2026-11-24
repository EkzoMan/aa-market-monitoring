"""Модель данных плагина — строго по §8 ТЗ. Денежные поля — Decimal(max_digits=20, decimal_places=2)."""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from .constants import (
    ContractFitStatus,
    ContractSourceKind,
    DisplayLanguageMode,
    FitStatus,
    LocationKind,
    ProviderStatus,
    ScopeKind,
    ShipsStatus,
    SourceKind,
    SyncRunStatus,
    SyncStatus,
    TriggerKind,
)

# Денежные поля и поля цен — Decimal по §8.
DECIMAL_MAX_DIGITS = 20
DECIMAL_PLACES = 2


class MarketLocation(models.Model):
    """Локация мониторинга: NPC-станция или структура из SDE (§8)."""

    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=16, choices=LocationKind.choices, default=LocationKind.STATION)
    station_id = models.BigIntegerField(null=True, blank=True)
    structure_id = models.BigIntegerField(null=True, blank=True)
    solar_system_id = models.IntegerField()
    region_id = models.IntegerField()
    enabled = models.BooleanField(default=True)
    sync_status = models.CharField(max_length=16, choices=SyncStatus.choices, default=SyncStatus.STALE)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Market location")
        verbose_name_plural = _("Market locations")
        constraints = [
            models.UniqueConstraint(fields=["kind", "station_id"], name="unique_marketlocation_station"),
            models.UniqueConstraint(fields=["kind", "structure_id"], name="unique_marketlocation_structure"),
        ]

    def __str__(self) -> str:
        return self.name


class StructureProvider(models.Model):
    """Провайдер структур: персонаж AA с нужными скоупами; токены не хранятся (§8, §11)."""

    # Модель Character входит в состав allianceauth.eveonline; app_label — "eveonline".
    # Персонаж AA: модель EveCharacter из allianceauth.eveonline (app_label "eveonline"); токены не хранятся.
    character = models.ForeignKey(
        "eveonline.EveCharacter",
        on_delete=models.PROTECT,
        related_name="marketmonitoring_provider",
    )
    priority = models.PositiveSmallIntegerField(default=100)  # меньше = выше приоритет
    enabled = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=ProviderStatus.choices, default=ProviderStatus.OK)
    acl_fail_count = models.PositiveSmallIntegerField(default=0)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = _("Structure provider")
        verbose_name_plural = _("Structure providers")

    def __str__(self) -> str:
        return f"{self.character} (prio {self.priority})"


class Fit(models.Model):
    """Фит: корпус + состав из EFT, целевое количество N, область (§8)."""

    name = models.CharField(max_length=255)
    hull_type_id = models.IntegerField()
    eft_raw = models.TextField(blank=True, default="")
    qty_target = models.PositiveIntegerField(default=1)
    scope_kind = models.CharField(max_length=16, choices=ScopeKind.choices)
    scope_ref = models.BigIntegerField()
    status_cache = models.CharField(max_length=16, choices=FitStatus.choices, default=FitStatus.NO_DATA, db_index=True)
    fits_available = models.IntegerField(null=True, blank=True)  # K
    total_cost = models.DecimalField(
        max_digits=DECIMAL_MAX_DIGITS, decimal_places=DECIMAL_PLACES, null=True, blank=True
    )
    overprice_count = models.IntegerField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    contracts_status_cache = models.CharField(
        max_length=16, choices=ContractFitStatus.choices, default=ContractFitStatus.NO_DATA, db_index=True
    )
    contracts_available = models.IntegerField(null=True, blank=True)  # C — комплектов кораблей в контрактах (§6.12)
    ships_status_cache = models.CharField(
        max_length=16, choices=ShipsStatus.choices, default=ShipsStatus.NO_DATA  # общий статус C+K (§6.13)
    )

    class Meta:
        verbose_name = _("Fit")
        verbose_name_plural = _("Fits")
        indexes = [
            models.Index(fields=["status_cache"], name="fit_status_cache_idx"),
        ]

    def __str__(self) -> str:
        return self.name


class ContractProvider(models.Model):
    """Провайдер контрактов ESI: персонаж AA со скоупом чтения корп./перс. контрактов (§8, §9.1)."""

    character = models.ForeignKey(
        "eveonline.EveCharacter",
        on_delete=models.PROTECT,
        related_name="marketmonitoring_contract_providers",
    )
    source_kind = models.CharField(
        max_length=16, choices=ContractSourceKind.choices, default=ContractSourceKind.CORPORATION
    )
    priority = models.PositiveSmallIntegerField(default=100)  # меньше = выше приоритет
    enabled = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=ProviderStatus.choices, default=ProviderStatus.OK)
    sync_status = models.CharField(max_length=16, choices=SyncStatus.choices, default=SyncStatus.STALE)
    acl_fail_count = models.PositiveSmallIntegerField(default=0)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = _("Contract provider")
        verbose_name_plural = _("Contract providers")
        constraints = [
            models.UniqueConstraint(fields=["character", "source_kind"], name="unique_contractprovider_char_kind"),
        ]

    def __str__(self) -> str:
        return f"{self.character} ({self.source_kind}, prio {self.priority})"


class ContractCache(models.Model):
    """Кэш открытых item_exchange контрактов ESI (§6.12, §8); дедупликация по уникальному contract_id."""

    contract_id = models.BigIntegerField(unique=True, db_index=True)
    source_kind = models.CharField(max_length=16, choices=ContractSourceKind.choices)
    source_ref = models.BigIntegerField()  # corporation_id или character_id
    issuer_id = models.BigIntegerField()
    issuer_corporation_id = models.BigIntegerField()
    assignee_id = models.BigIntegerField()
    availability = models.CharField(max_length=32)
    contract_type = models.CharField(max_length=32)
    status = models.CharField(max_length=32)
    title = models.CharField(max_length=255, blank=True, default="")
    start_location_id = models.BigIntegerField(db_index=True)
    price = models.DecimalField(max_digits=DECIMAL_MAX_DIGITS, decimal_places=DECIMAL_PLACES, default=0)
    date_issued = models.DateTimeField()
    date_expired = models.DateTimeField()
    fetched_at = models.DateTimeField()

    class Meta:
        verbose_name = _("Contract cache")
        verbose_name_plural = _("Contract cache")
        indexes = [
            models.Index(fields=["source_kind", "source_ref"], name="contractcache_source_idx"),
            models.Index(fields=["start_location_id", "status"], name="contractcache_loc_status_idx"),
        ]

    def __str__(self) -> str:
        return f"contract {self.contract_id} ({self.availability})"


class ContractItemCache(models.Model):
    """Позиции кэшированного контракта ESI (§6.12, §8)."""

    contract = models.ForeignKey(ContractCache, on_delete=models.CASCADE, related_name="items")
    record_id = models.BigIntegerField()
    type_id = models.IntegerField(db_index=True)
    quantity = models.BigIntegerField()
    is_included = models.BooleanField(default=True)
    is_singleton = models.BooleanField(default=False)
    raw_quantity = models.IntegerField(null=True, blank=True)

    class Meta:
        verbose_name = _("Contract item cache")
        verbose_name_plural = _("Contract item cache")
        indexes = [
            models.Index(fields=["contract", "type_id"], name="contractitem_contract_type_idx"),
        ]

    def __str__(self) -> str:
        return f"contract {self.contract_id} type {self.type_id} x{self.quantity}"


class FitComponent(models.Model):
    """Компонент фита: тип, количество на фит, опциональный override области (§8)."""

    fit = models.ForeignKey(Fit, on_delete=models.CASCADE, related_name="components")
    type_id = models.IntegerField()
    qty_per_fit = models.PositiveIntegerField(default=1)
    slot = models.CharField(max_length=32, blank=True, default="")
    is_charge = models.BooleanField(default=False)
    scope_override_kind = models.CharField(max_length=16, choices=ScopeKind.choices, null=True, blank=True)
    scope_override_ref = models.BigIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = _("Fit component")
        verbose_name_plural = _("Fit components")
        constraints = [
            models.UniqueConstraint(fields=["fit", "type_id"], name="unique_fitcomponent_type"),
        ]

    def __str__(self) -> str:
        return f"{self.fit.name} / type {self.type_id}"


class ComponentRequest(models.Model):
    """Компонент вне фитов: тип, целевое количество, область, заметка (§8)."""

    type_id = models.IntegerField()
    qty_target = models.PositiveIntegerField(default=1)
    scope_kind = models.CharField(max_length=16, choices=ScopeKind.choices)
    scope_ref = models.BigIntegerField()
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Component request")
        verbose_name_plural = _("Component requests")

    def __str__(self) -> str:
        return f"type {self.type_id} @ {self.scope_kind} {self.scope_ref}"


class MarketOrderCache(models.Model):
    """Кэш сырых sell-заказов ESI (§8)."""

    source_kind = models.CharField(max_length=16, choices=SourceKind.choices)
    source_ref = models.BigIntegerField()  # region_id или structure_id
    location_id = models.BigIntegerField()  # station_id / structure_id заказа
    solar_system_id = models.IntegerField()
    region_id = models.IntegerField()
    type_id = models.IntegerField()
    price = models.DecimalField(max_digits=DECIMAL_MAX_DIGITS, decimal_places=DECIMAL_PLACES)
    volume = models.BigIntegerField()
    fetched_at = models.DateTimeField()

    class Meta:
        verbose_name = _("Market order cache")
        verbose_name_plural = _("Market order cache")
        indexes = [
            models.Index(fields=["type_id", "location_id"], name="marketordercache_type_loc_idx"),
            models.Index(fields=["source_kind", "source_ref"], name="marketordercache_source_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.source_kind}/{self.source_ref} type {self.type_id}"


class StockLevel(models.Model):
    """Агрегат sell-объёма по локации и типу (§8)."""

    location_id = models.BigIntegerField()
    solar_system_id = models.IntegerField()
    region_id = models.IntegerField()
    type_id = models.IntegerField()
    total_sell_volume = models.BigIntegerField()
    best_sell = models.DecimalField(max_digits=DECIMAL_MAX_DIGITS, decimal_places=DECIMAL_PLACES, null=True, blank=True)
    updated_at = models.DateTimeField()

    class Meta:
        verbose_name = _("Stock level")
        verbose_name_plural = _("Stock levels")
        constraints = [
            models.UniqueConstraint(fields=["location_id", "type_id"], name="unique_stocklevel_loc_type"),
        ]
        indexes = [
            models.Index(fields=["type_id", "location_id"], name="stocklevel_type_loc_idx"),
        ]

    def __str__(self) -> str:
        return f"loc {self.location_id} type {self.type_id} = {self.total_sell_volume}"


class JitaOrderLevel(models.Model):
    """Снимок стакана Jita: лоты по рангу, rank=1 — минимальная цена (§8)."""

    type_id = models.IntegerField()
    rank = models.SmallIntegerField()  # 1 = минимальная цена
    price = models.DecimalField(max_digits=DECIMAL_MAX_DIGITS, decimal_places=DECIMAL_PLACES)
    volume = models.BigIntegerField()
    fetched_at = models.DateTimeField()

    class Meta:
        verbose_name = _("Jita order level")
        verbose_name_plural = _("Jita order levels")
        constraints = [
            models.UniqueConstraint(fields=["type_id", "rank"], name="unique_jitaorderlevel_type_rank"),
        ]
        indexes = [
            models.Index(fields=["type_id", "rank"], name="jitaorderlevel_type_rank_idx"),
        ]

    def __str__(self) -> str:
        return f"type {self.type_id} rank {self.rank}"


class SyncRun(models.Model):
    """Журнал прогонов задач Celery — только для Django-admin (§8, §12)."""

    task_name = models.CharField(max_length=255)
    trigger = models.CharField(max_length=16, choices=TriggerKind.choices)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=SyncRunStatus.choices, default=SyncRunStatus.RUNNING)
    locations_ok = models.IntegerField(default=0)
    locations_failed = models.IntegerField(default=0)
    items_processed = models.IntegerField(default=0)
    errors = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = _("Sync run")
        verbose_name_plural = _("Sync runs")

    def __str__(self) -> str:
        return f"{self.task_name} ({self.trigger}) @ {self.started_at:%Y-%m-%d %H:%M}"


class WebhookConfig(models.Model):
    """Конфигурация Discord webhook — один канал (§8, §13)."""

    url = models.URLField()
    enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("Webhook config")
        verbose_name_plural = _("Webhook configs")

    def __str__(self) -> str:
        return "Discord webhook"


class NotificationLog(models.Model):
    """Журнал отправленных уведомлений; используется для дедупликации (§8, §13.1)."""

    event = models.CharField(max_length=32)
    fit = models.ForeignKey(Fit, on_delete=models.SET_NULL, null=True, blank=True)
    component = models.ForeignKey(ComponentRequest, on_delete=models.SET_NULL, null=True, blank=True)
    state_hash = models.CharField(max_length=64)
    sent_at = models.DateTimeField()
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = _("Notification log")
        verbose_name_plural = _("Notification logs")
        indexes = [
            models.Index(fields=["event", "fit", "sent_at"], name="notificationlog_event_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event} @ {self.sent_at:%Y-%m-%d %H:%M}"


class PluginSetting(models.Model):
    """Настройки плагина — singleton (§10)."""

    # Диапазоны значений настроек по §10 (валидаторы не меняют схему БД).
    jita_markup_pct = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        default=5,
        validators=[MinValueValidator(0), MaxValueValidator(500)],
        help_text=_("Markup percent, 0..500"),
    )
    overprice_threshold_pct = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        default=25,
        validators=[MinValueValidator(0), MaxValueValidator(1000)],
        help_text=_("Overprice threshold percent, 0..1000"),
    )
    delivery_rate_isk_per_m3 = models.DecimalField(
        max_digits=DECIMAL_MAX_DIGITS,
        decimal_places=DECIMAL_PLACES,
        default=1250,
        validators=[MinValueValidator(0)],
        help_text=_("Delivery rate ISK per m3, >= 0"),
    )
    jita_region_id = models.IntegerField(default=10000002)  # The Forge
    jita_station_id = models.IntegerField(default=60003760)  # Jita IV-4
    jita_book_depth = models.IntegerField(
        default=50, validators=[MinValueValidator(1), MaxValueValidator(500)], help_text=_("1..500")
    )
    jita_type_batch = models.IntegerField(
        default=50, validators=[MinValueValidator(1), MaxValueValidator(200)], help_text=_("1..200")
    )
    jita_sync_interval_min = models.IntegerField(
        default=60, validators=[MinValueValidator(5), MaxValueValidator(1440)], help_text=_("5..1440")
    )
    stock_sync_interval_min = models.IntegerField(
        default=60, validators=[MinValueValidator(5), MaxValueValidator(1440)], help_text=_("5..1440")
    )
    data_stale_after_min = models.IntegerField(default=180, validators=[MinValueValidator(5)], help_text=_(">= 5"))
    provider_acl_cooldown_min = models.IntegerField(
        default=60, validators=[MinValueValidator(1), MaxValueValidator(1440)], help_text=_("1..1440")
    )
    provider_acl_max_failures = models.IntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(10)], help_text=_("1..10")
    )
    notification_deficit_enabled = models.BooleanField(default=True)
    notification_deficit_cooldown_min = models.IntegerField(
        default=120, validators=[MinValueValidator(1), MaxValueValidator(1440)], help_text=_("1..1440")
    )
    webhook_url = models.URLField(blank=True, default="")
    display_language_mode = models.CharField(
        max_length=16,
        choices=DisplayLanguageMode.choices,
        default=DisplayLanguageMode.CLIENT_DEFAULT,
    )
    contracts_sync_interval_min = models.IntegerField(
        default=30, validators=[MinValueValidator(5), MaxValueValidator(1440)], help_text=_("5..1440")
    )
    notification_contracts_deficit_enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("Plugin setting")
        verbose_name_plural = _("Plugin settings")

    def save(self, *args, **kwargs):
        """Singleton: сохранение всегда в строку с pk=1."""
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "PluginSetting":
        """Загрузка singleton-настроек, при отсутствии — создание с дефолтами."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Plugin settings"
