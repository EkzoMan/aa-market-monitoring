"""Интеграция с CharLink: привязка персонажей-провайдеров структур и контрактов с нужными ESI-скоупами (§9.1).

CharLink вызывает callback'и с полученным токеном; модуль регистрирует персонажа
как StructureProvider или ContractProvider (корпоративные + персональные контракты).
"""

from allianceauth.eveonline.models import EveCharacter
from allianceauth.services.hooks import get_extension_logger
from charlink.app_imports.utils import AppImport, LoginImport
from django.db.models import Exists, OuterRef, Q
from esi.models import Token

from .constants import ContractSourceKind
from .models import ContractProvider, StructureProvider

logger = get_extension_logger(__name__)

SCOPES = [
    "esi-markets.structure_markets.v1",
    "esi-universe.read_structures.v1",
    # Поиск структур персонажа (§9 шаг 1) — структуры корпорации, доступные для дока.
    "esi-search.search_structures.v1",
]

CONTRACT_SCOPES = [
    "esi-contracts.read_corporation_contracts.v1",
    "esi-contracts.read_character_contracts.v1",
    # Структуры корпорации провайдера (решение №37 §20) — для sync_corp_structures.
    "esi-corporations.read_corporation_structures.v1",
]


def _ensure_eve_character(character_id: int) -> EveCharacter:
    """Получение или создание EveCharacter в AA по character_id токена."""
    eve_char = EveCharacter.objects.filter(character_id=character_id).first()
    if eve_char is None:
        # CharLink может выдать токен до создания EveCharacter в модели.
        eve_char = EveCharacter.objects.create_character(character_id)
    return eve_char


def _add_character(request, token: Token) -> None:
    """Привязка владельца токена как StructureProvider (idempotent)."""
    eve_char = _ensure_eve_character(token.character_id)
    StructureProvider.objects.get_or_create(
        character=eve_char,
        defaults={"enabled": True, "priority": 100},
    )
    logger.info("CharLink: added structure provider %s", eve_char)


def _add_contract_character(request, token: Token) -> None:
    """Привязка владельца токена как ContractProvider для корп. и персональных контрактов (idempotent)."""
    eve_char = _ensure_eve_character(token.character_id)
    for kind in (ContractSourceKind.CORPORATION, ContractSourceKind.CHARACTER):
        ContractProvider.objects.get_or_create(
            character=eve_char,
            source_kind=kind,
            defaults={"enabled": True, "priority": 100},
        )
    logger.info("CharLink: added contract provider %s", eve_char)


def _check_permissions(user) -> bool:
    """Добавлять провайдеров разрешено роли _edit (или суперпользователю)."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    from .auth_hooks import GROUP_EDIT

    return user.groups.filter(name=GROUP_EDIT).exists()


def _is_character_added(eve_char: EveCharacter) -> bool:
    return StructureProvider.objects.filter(character=eve_char).exists()


def _is_contract_character_added(eve_char: EveCharacter) -> bool:
    return ContractProvider.objects.filter(character=eve_char).exists()


def _get_users_with_perms():
    from django.contrib.auth.models import User

    from .auth_hooks import GROUP_EDIT

    return User.objects.filter(Q(is_superuser=True) | Q(groups__name=GROUP_EDIT)).distinct()


# Exists-аннотации должны быть созданы на уровне модуля (validate_import проверяет isinstance).
is_character_added_annotation = Exists(
    StructureProvider.objects.filter(character=OuterRef("pk")),
)
is_contract_character_added_annotation = Exists(
    ContractProvider.objects.filter(character=OuterRef("pk")),
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
        ),
        LoginImport(
            app_label="marketmonitoring",
            unique_id="contractprovider",
            field_label="Market Monitoring: contracts provider (corp/alliance/personal)",
            add_character=_add_contract_character,
            scopes=CONTRACT_SCOPES,
            check_permissions=_check_permissions,
            is_character_added=_is_contract_character_added,
            is_character_added_annotation=is_contract_character_added_annotation,
            get_users_with_perms=_get_users_with_perms,
            default_initial_selection=False,
        ),
    ],
)
