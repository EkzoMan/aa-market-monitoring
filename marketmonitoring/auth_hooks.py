"""Хелперы проверки прав плагина (§3): суперпользователь или членство в группе."""

from django.contrib.auth.models import Group

GROUP_VIEW = "market_monitoring_view"
GROUP_EDIT = "market_monitoring_edit"

#: Права роли _view: просмотр дашборда, карточек фитов, компонентов и экспорт.
VIEW_PERMISSION_CODENAMES = [
    "view_fit",
    "view_fitcomponent",
    "view_componentrequest",
    "view_marketlocation",
    "view_structureprovider",
    "view_contractprovider",
    "view_pluginsetting",
    "view_webhookconfig",
]

#: Права роли _edit: создание/правка/удаление фитов, компонентов, локаций, провайдеров, настроек.
EDIT_PERMISSION_CODENAMES = [
    "add_fit",
    "change_fit",
    "delete_fit",
    "add_fitcomponent",
    "change_fitcomponent",
    "delete_fitcomponent",
    "add_componentrequest",
    "change_componentrequest",
    "delete_componentrequest",
    "add_marketlocation",
    "change_marketlocation",
    "delete_marketlocation",
    "add_structureprovider",
    "change_structureprovider",
    "delete_structureprovider",
    "add_contractprovider",
    "change_contractprovider",
    "delete_contractprovider",
    "add_pluginsetting",
    "change_pluginsetting",
    "delete_pluginsetting",
    "add_webhookconfig",
    "change_webhookconfig",
    "delete_webhookconfig",
]


def has_view(user) -> bool:
    """Проверка права просмотра: суперпользователь или членство в группе `market_monitoring_view`/`_edit`."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=[GROUP_VIEW, GROUP_EDIT]).exists()


def has_edit(user) -> bool:
    """Проверка права управления: суперпользователь или членство в группе `market_monitoring_edit`."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=GROUP_EDIT).exists()


def create_groups() -> None:
    """Создание групп прав (без назначения пермишенов — их назначает post_migrate)."""
    Group.objects.get_or_create(name=GROUP_VIEW)
    Group.objects.get_or_create(name=GROUP_EDIT)


def assign_permissions() -> None:
    """Назначение пермишенов группам (вызывается post_migrate, т.к. Permissions создаются тогда же)."""
    from django.contrib.auth import get_permission_codename
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType

    from .models import (
        ComponentRequest,
        ContractProvider,
        Fit,
        FitComponent,
        MarketLocation,
        PluginSetting,
        StructureProvider,
        WebhookConfig,
    )

    model_classes = [
        Fit,
        FitComponent,
        ComponentRequest,
        MarketLocation,
        StructureProvider,
        ContractProvider,
        PluginSetting,
        WebhookConfig,
    ]
    permission_pks = []
    for model_class in model_classes:
        content_type = ContentType.objects.get_for_model(model_class)
        for action in ("view", "add", "change", "delete"):
            permission = Permission.objects.filter(
                content_type=content_type,
                codename=get_permission_codename(action, model_class._meta),
            ).first()
            if permission:
                permission_pks.append(permission.pk)

    view_group, _ = Group.objects.get_or_create(name=GROUP_VIEW)
    edit_group, _ = Group.objects.get_or_create(name=GROUP_EDIT)

    view_permissions = Permission.objects.filter(pk__in=permission_pks, codename__startswith="view_")
    view_group.permissions.set(view_permissions)
    # _edit подразумевает _view (§3).
    edit_group.permissions.set(Permission.objects.filter(pk__in=permission_pks))
