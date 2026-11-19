"""Вспомогательные фикстуры тестов: EveCharacter без сети."""

from allianceauth.eveonline.models import EveCharacter


def make_character(character_id: int = 95465499, name: str = "Test Pilot") -> EveCharacter:
    """Создание EveCharacter AA (справочник, не токен — плагин токены не хранит, §8)."""
    return EveCharacter.objects.create(
        character_id=character_id,
        character_name=name,
        corporation_id=1,
        corporation_name="Corp",
        corporation_ticker="CRP",
    )
