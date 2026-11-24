"""Регрессионные тесты ESI-скоупа синка структур корпорации (§9.1, решение №37 §20).

ESI и Discord не используются: менеджер токенов AA и ESI-операции подменяются моками.
Проверяется, что CharLink запрашивает и клиент использует scope из официальной спеки
``GET /corporations/{corporation_id}/structures``, а не несуществующий идентификатор.
"""

import importlib
import sys
from pathlib import Path
from types import ModuleType
from unittest import mock

from ..esi import client as esi_client

NEW_SCOPE = "esi-corporations.read_structures.v1"
# Идентификатор собирается из частей намеренно: тест сканирует исходники и не должен
# находить собственную строку-литерал.
OLD_SCOPE = "esi-corporations.read_" + "corporation_structures.v1"

EXPECTED_STRUCTURE_SCOPES = [
    "esi-markets.structure_markets.v1",
    "esi-universe.read_structures.v1",
    "esi-search.search_structures.v1",
]
EXPECTED_CONTRACT_SCOPES = [
    "esi-contracts.read_corporation_contracts.v1",
    "esi-contracts.read_character_contracts.v1",
    NEW_SCOPE,
]


def _load_charlink_module():
    """Импорт плагина: внешний пакет charlink в тестовом окружении может быть не установлен.

    Заглушка нужна только для чтения констант и объектов CharLink, сетевых вызовов нет.
    Модуль с подставным CharLink не остаётся в кэше импорта после чтения констант.
    """
    module_name = "marketmonitoring.charlink"
    try:
        importlib.import_module("charlink.app_imports.utils")
    except ModuleNotFoundError:
        package = ModuleType("charlink")
        package.__path__ = []
        app_imports = ModuleType("charlink.app_imports")
        app_imports.__path__ = []
        utils = ModuleType("charlink.app_imports.utils")

        class AppImport:
            """Минимальная замена внешнего AppImport для инспекции scopes."""

            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class LoginImport:
            """Минимальная замена внешнего LoginImport для инспекции scopes."""

            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        utils.AppImport = AppImport
        utils.LoginImport = LoginImport
        app_imports.utils = utils
        package.app_imports = app_imports
        sys.modules.pop(module_name, None)
        with mock.patch.dict(
            sys.modules,
            {
                "charlink": package,
                "charlink.app_imports": app_imports,
                "charlink.app_imports.utils": utils,
            },
        ):
            charlink = importlib.import_module(module_name)
        sys.modules.pop(module_name, None)
        return charlink
    return importlib.import_module(module_name)


def _fake_token_manager(token=mock.sentinel.token):
    """Мок цепочки Token.objects.filter(...).require_scopes(...).require_valid() без БД и сети."""
    queryset = mock.Mock(name="token_queryset")
    queryset.require_scopes.return_value = queryset
    queryset.require_valid.return_value = queryset
    queryset.order_by.return_value = queryset
    queryset.first.return_value = token

    manager = mock.Mock(name="Token.objects")
    manager.filter.return_value = queryset
    return manager, queryset


def test_corp_structures_scope_matches_official_esi_spec():
    """Клиент использует scope из официальной спеки ESI для списка структур корпорации."""
    assert esi_client.SCOPE_CORP_STRUCTURES == NEW_SCOPE


def test_charlink_contract_scopes_use_valid_corp_structures_scope():
    """CharLink обновил contractprovider: новый scope на месте, старого нет, structureprovider не тронут."""
    charlink = _load_charlink_module()
    assert NEW_SCOPE in charlink.CONTRACT_SCOPES
    assert OLD_SCOPE not in charlink.CONTRACT_SCOPES
    assert set(charlink.CONTRACT_SCOPES) == set(EXPECTED_CONTRACT_SCOPES)
    assert charlink.SCOPES == EXPECTED_STRUCTURE_SCOPES

    imports = {login.unique_id: login for login in charlink.app_import.imports}
    assert set(imports["contractprovider"].scopes) == set(EXPECTED_CONTRACT_SCOPES)
    assert imports["structureprovider"].scopes == EXPECTED_STRUCTURE_SCOPES


def test_token_for_corp_structures_requires_official_scope():
    """_token_for_corp_structures запрашивает require_scopes ровно с валидным scope."""
    manager, queryset = _fake_token_manager()
    fake_token_model = mock.Mock(name="Token")
    fake_token_model.objects = manager

    with mock.patch.object(esi_client, "Token", fake_token_model):
        token = esi_client._token_for_corp_structures(9001)

    assert token is mock.sentinel.token
    manager.filter.assert_called_once_with(character_id=9001)
    queryset.require_scopes.assert_called_once_with(NEW_SCOPE)
    queryset.require_valid.assert_called_once_with()
    queryset.order_by.assert_called_once_with("pk")


def test_token_for_corp_structures_error_mentions_official_scope():
    """При отсутствии токена сообщение об ошибке содержит валидный scope."""
    manager, queryset = _fake_token_manager(token=None)
    fake_token_model = mock.Mock(name="Token")
    fake_token_model.objects = manager

    with mock.patch.object(esi_client, "Token", fake_token_model):
        try:
            esi_client._token_for_corp_structures(9001)
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("ожидался ValueError при отсутствии валидного токена")

    assert NEW_SCOPE in message
    assert OLD_SCOPE not in message
    queryset.require_scopes.assert_called_once_with(NEW_SCOPE)


def test_fetch_corporation_structures_passes_scoped_token_without_network():
    """Публичный синк структур корпорации берёт токен с валидным scope и не делает реальных запросов."""
    manager, queryset = _fake_token_manager()
    fake_token_model = mock.Mock(name="Token")
    fake_token_model.objects = manager

    op_calls = []

    class FakeOperation:
        """Подмена ESI-операции: одна страница с X-Pages, без HTTP."""

        def __call__(self, **kwargs):
            op_calls.append(kwargs)
            return self

        def result(self, **kwargs):
            response = mock.Mock(headers={"X-Pages": "1"})
            return [{"structure_id": 1048576000001, "name": "Fort Knocks", "solar_system_id": 30000142}], response

    fake_esi = mock.Mock(name="esi")
    fake_esi.client.Corporations.GetCorporationsCorporationIdStructures = FakeOperation()

    with (
        mock.patch.object(esi_client, "Token", fake_token_model),
        mock.patch.object(esi_client, "esi", fake_esi),
        mock.patch.object(esi_client, "_check_error_limit"),
    ):
        structures, partial = esi_client.fetch_corporation_structures(9002, 9001)

    assert partial is False
    assert len(structures) == 1
    assert structures[0]["structure_id"] == 1048576000001
    assert op_calls[0]["corporation_id"] == 9002
    assert op_calls[0]["token"] is mock.sentinel.token
    queryset.require_scopes.assert_called_once_with(NEW_SCOPE)


def test_old_scope_identifier_absent_from_plugin_python_sources():
    """Несуществующего идентификатора нет ни в одном Python-файле плагина (документы не сканируем)."""
    plugin_root = Path(__file__).resolve().parents[1]
    offenders = [
        str(path.relative_to(plugin_root).as_posix())
        for path in plugin_root.glob("**/*.py")
        if OLD_SCOPE in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
