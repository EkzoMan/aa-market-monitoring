"""AppConfig плагина: подключение пункта меню через navhookspec (группы прав создаются миграцией данных)."""

import logging

from allianceauth import hooks
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

        @hooks.register("menu_item_hook")
        def register_menu():
            from .auth_hooks import has_view

            return {
                "perm": lambda user: has_view(user),
                "title": _("Market Monitoring"),
                "url_name": "marketmonitoring:index",
                "order": 1200,
            }

        @hooks.register("menu_item_hook")
        def register_components_menu():
            """Пункт меню «Components» — отдельный раздел рядом с Dashboard (§14.4)."""
            from .auth_hooks import has_view

            return {
                "perm": lambda user: has_view(user),
                "title": _("Components"),
                "url_name": "marketmonitoring:components",
                "order": 1210,
            }

        @hooks.register("menu_item_hook")
        def register_locations_menu():
            """Пункт меню «Locations» — только для роли _edit (§14)."""
            from .auth_hooks import has_edit

            return {
                "perm": lambda user: has_edit(user),
                "title": _("Locations"),
                "url_name": "marketmonitoring:locations",
                "order": 1220,
            }

        @hooks.register("menu_item_hook")
        def register_settings_menu():
            """Пункт меню «Settings» — только для роли _edit (§14, §16)."""
            from .auth_hooks import has_edit

            return {
                "perm": lambda user: has_edit(user),
                "title": _("Settings"),
                "url_name": "marketmonitoring:settings",
                "order": 1230,
            }


def _on_post_migrate(sender, **kwargs) -> None:
    """После миграций: создание групп прав и назначение пермишенов (§3)."""
    from .auth_hooks import assign_permissions, create_groups

    create_groups()
    assign_permissions()
