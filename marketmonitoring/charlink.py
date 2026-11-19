"""Интеграция с CharLink: добавление персонажа-провайдера структур с нужными ESI-скоупами (§9.1).

CharLink вызывает `add_character` с полученным токеном; плагин регистрирует
персонажа как StructureProvider, если он ещё не добавлен.
"""

from allianceauth.eveonline.models import EveCharacter
from allianceauth.services.hooks import get_extension_logger
from charlink.app_imports.utils import AppImport, LoginImport
from django.db.models import Exists, OuterRef, Q
from esi.models import Token

from .models import StructureProvider

logger = get_extension_logger(__name__)

SCOPES = [
    "esi-markets.structure_markets.v1",
    "esi-universe.read_structures.v1",
]


def _add_character(request, token: Token) -> None:
    """Добавить персонажа токена как StructureProvider (idempotent)."""
    character_id = token.character_id
    eve_char = EveCharacter.objects.filter(character_id=character_id).first()
    if eve_char is None:
        # CharLink может отдать токен до создания EveCharacter — создаём.
        eve_char = EveCharacter.objects.create_character(character_id)
    StructureProvider.objects.get_or_create(
        character=eve_char,
        defaults={"enabled": True, "priority": 100},
    )
    logger.info("CharLink: added structure provider %s", eve_char)


def _check_permissions(user) -> bool:
    """Разрешаем добавление персонажа-провайдера роли _edit (она управляет провайдерами)."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    from .auth_hooks import GROUP_EDIT

    return user.groups.filter(name=GROUP_EDIT).exists()


def _is_character_added(eve_char: EveCharacter) -> bool:
    return StructureProvider.objects.filter(character=eve_char).exists()


def _get_users_with_perms():
    from django.contrib.auth.models import User

    from .auth_hooks import GROUP_EDIT

    return User.objects.filter(Q(is_superuser=True) | Q(groups__name=GROUP_EDIT)).distinct()


# Exists-аннотация должна быть объектом на уровне модуля (validate_import проверяет isinstance).
is_character_added_annotation = Exists(
    StructureProvider.objects.filter(character=OuterRef("pk")),
)


app_import = AppImport(
    app_label="marketmonitoring",
    imports=[
        LoginImport(
            app_label="marketmonitoring",
            unique_id="structureprovider",
            field_label="Market Monitoring: structure provider",
            add_character=_add_character,
            scopes=SCOPES,
            check_permissions=_check_permissions,
            is_character_added=_is_character_added,
            is_character_added_annotation=is_character_added_annotation,
            get_users_with_perms=_get_users_with_perms,
            default_initial_selection=False,
        )
    ],
)
