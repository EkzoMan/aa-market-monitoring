"""Django-admin плагина: SyncRun, NotificationLog, MarketLocation, StructureProvider, WebhookConfig, PluginSetting — только superuser (§3, §17)."""

from django.contrib import admin

from .models import (
    MarketLocation,
    NotificationLog,
    PluginSetting,
    StructureProvider,
    SyncRun,
    WebhookConfig,
)


class SuperuserOnlyAdmin(admin.ModelAdmin):
    """Базовый класс: admin-страницы плагина — только для суперпользователя (§3, §17)."""

    def has_module_permission(self, request) -> bool:
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None) -> bool:
        return request.user.is_superuser

    def has_add_permission(self, request) -> bool:
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None) -> bool:
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None) -> bool:
        return request.user.is_superuser


class ReadOnlyAdmin(SuperuserOnlyAdmin):
    """Read-only admin (§3): просмотр без добавления/правки/удаления."""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyAdmin):
    """Журнал синков — только для суперпользователя (§3)."""

    list_display = ("task_name", "trigger", "started_at", "finished_at", "status", "locations_ok", "locations_failed")
    list_filter = ("status", "trigger", "task_name")
    search_fields = ("task_name", "errors")
    readonly_fields = (
        "task_name",
        "trigger",
        "started_at",
        "finished_at",
        "status",
        "locations_ok",
        "locations_failed",
        "items_processed",
        "errors",
    )


@admin.register(NotificationLog)
class NotificationLogAdmin(ReadOnlyAdmin):
    """Журнал уведомлений — read-only диагностика для суперпользователя (§13).

    UI-страницу логов §1.2 запрещает; admin для суперпользователя —
    диагностика в духе §3 (просмотр SyncRun), объём не расширяет.
    """

    list_display = ("event", "fit_id", "component_id", "ok", "sent_at", "state_hash")
    list_filter = ("event", "ok")
    search_fields = ("state_hash",)
    readonly_fields = ("event", "fit", "component", "state_hash", "sent_at", "ok", "error")


@admin.register(MarketLocation)
class MarketLocationAdmin(SuperuserOnlyAdmin):
    """Локации мониторинга — только для суперпользователя."""

    list_display = ("name", "kind", "station_id", "structure_id", "enabled", "sync_status", "last_synced_at")
    list_filter = ("kind", "enabled", "sync_status")
    search_fields = ("name",)


@admin.register(StructureProvider)
class StructureProviderAdmin(SuperuserOnlyAdmin):
    """Провайдеры структур — только для суперпользователя."""

    list_display = (
        "character",
        "priority",
        "enabled",
        "status",
        "acl_fail_count",
        "cooldown_until",
        "last_verified_at",
    )
    list_filter = ("enabled", "status")


@admin.register(WebhookConfig)
class WebhookConfigAdmin(SuperuserOnlyAdmin):
    """Конфигурация webhook — только для суперпользователя (§3, §16)."""

    list_display = ("pk", "enabled")


@admin.register(PluginSetting)
class PluginSettingAdmin(SuperuserOnlyAdmin):
    """Singleton-настройки — стандартное редактирование в admin (§17); рабочая правка — через UI settings (§10)."""

    list_display = ("pk", "display_language_mode", "notification_deficit_enabled")

    def has_add_permission(self, request) -> bool:
        # Singleton: создание новой строки запрещено.
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
