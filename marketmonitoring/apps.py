"""AppConfig плагина: подключение пунктов меню через MenuItemHookCustom (группы прав создаются миграцией данных)."""

import logging

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger("marketmonitoring")


class MarketmonitoringConfig(AppConfig):
    name = "marketmonitoring"
    verbose_name = _("Market Monitoring")

    def ready(self) -> None:
        """Подключение пунктов меню и post_migrate-обработчика назначения прав (§14.4)."""
        from django.db.models.signals import post_migrate

        post_migrate.connect(_on_post_migrate, sender=self)
        _register_menu_hooks()


def _register_menu_hooks() -> None:
    """Регистрация пунктов меню и URL-ов. Выполняется в ready(), когда реестр приложений уже загружен."""
    from allianceauth import hooks
    from allianceauth.menu.core.menu_item_hooks import MenuItemHookCustom

    @hooks.register("url_hook")
    def register_urls():
        """Подключение URL-ов плагина (§14): /marketmonitoring/..."""
        from allianceauth.services.hooks import UrlHook

        return UrlHook("marketmonitoring.urls", "marketmonitoring", r"^marketmonitoring/")

    @hooks.register("charlink")
    def register_charlink():
        """Интеграция с CharLink: добавление персонажа-провайдера структур (§9.1)."""
        return "marketmonitoring.charlink"

    class _BaseMenuItem(MenuItemHookCustom):
        """Пункт меню плагина: проверка прав в render() (§14.4)."""

        def __init__(self, title, url_name: str, fa_icon_class: str, order: int, perm_func) -> None:
            super().__init__(text=title, classes=fa_icon_class, url_name=url_name, order=order)
            self._perm_func = perm_func

        def render(self, request) -> str:
            """Не показывать пункт, если у пользователя нет доступа."""
            if self._perm_func(request.user):
                return super().render(request)
            return ""

    # Важно: у каждого пункта свой класс — hash хука считается из имени класса
    # (см. menu.core.menu_item_hooks.generate_hash), и он уникален в menu_menuitem.
    class MarketMonitoringMenuItem(_BaseMenuItem):
        pass

    class ComponentsMenuItem(_BaseMenuItem):
        pass

    class LocationsMenuItem(_BaseMenuItem):
        pass

    class SettingsMenuItem(_BaseMenuItem):
        pass

    @hooks.register("menu_item_hook")
    def register_menu():
        from .auth_hooks import has_view

        return MarketMonitoringMenuItem(
            _("Market Monitoring"), "marketmonitoring:index", "fas fa-chart-line", 1200, has_view
        )

    @hooks.register("menu_item_hook")
    def register_components_menu():
        """Пункт меню «Components» — отдельный раздел рядом с Dashboard (§14.4)."""
        from .auth_hooks import has_view

        return ComponentsMenuItem(_("Components"), "marketmonitoring:components", "fas fa-cubes", 1210, has_view)

    @hooks.register("menu_item_hook")
    def register_locations_menu():
        """Пункт меню «Locations» — только для роли _edit (§14)."""
        from .auth_hooks import has_edit

        return LocationsMenuItem(_("Locations"), "marketmonitoring:locations", "fas fa-map-marker-alt", 1220, has_edit)

    @hooks.register("menu_item_hook")
    def register_settings_menu():
        """Пункт меню «Settings» — только для роли _edit (§14, §16)."""
        from .auth_hooks import has_edit

        return SettingsMenuItem(_("Settings"), "marketmonitoring:settings", "fas fa-cogs", 1230, has_edit)


def _on_post_migrate(sender, **kwargs) -> None:
    """После миграций: создание групп прав и назначение пермишенов (§3)."""
    from .auth_hooks import assign_permissions, create_groups

    create_groups()
    assign_permissions()
    _ensure_menu_folder()


def _ensure_menu_folder() -> None:
    """Создаёт папку «Market Monitoring» в меню и помещает в неё пункты плагина.

    Папка — это MenuItem без hook_hash и url (см. menu.models.MenuItem.item_type).
    Апп-пункты создаются синком без parent, поэтому привязываем их к папке здесь.
    """
    from allianceauth.menu.models import MenuItem

    folder, _created = MenuItem.objects.get_or_create(
        text="Market Monitoring (folder)",
        defaults={"order": 1199, "classes": "fas fa-chart-line"},
    )
    # Пункты плагина имеют order 1200..1230 и создаются синком с hook_hash и без parent.
    MenuItem.objects.filter(hook_hash__isnull=False, order__gte=1200, order__lte=1230).update(parent=folder)
    logger.info("Menu folder ensured: %s", folder.text)
