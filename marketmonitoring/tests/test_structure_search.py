"""Тесты поиска структур персонажа (§9 шаги 1–3): клиент, задача, view.

ESI полностью замокан — сеть не используется (§18). UI к ESI не обращается:
view только ставит задачу в очередь и читает кэш (§5.10).
"""

import json
from unittest import mock

import pytest
from django.core.cache import cache
from esi.models import Token

from .. import tasks
from ..esi import client as esi_client

# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def editor(db):
    """Пользователь с правом edit (группа market_monitoring_edit)."""
    from django.contrib.auth.models import Group, User

    user = User.objects.create_user(username="editor", password="x")
    user.groups.add(Group.objects.get(name="market_monitoring_edit"))
    return user


@pytest.fixture
def editor_client(client, editor):
    client.force_login(editor)
    return client


@pytest.fixture
def viewer_client(client, db):
    """Клиент под пользователем с правом только просмотра."""
    from django.contrib.auth.models import Group, User

    user = User.objects.create_user(username="viewer", password="x")
    user.groups.add(Group.objects.get(name="market_monitoring_view"))
    client.force_login(user)
    return client


def _make_search_token(user, character_id):
    """Токен персонажа со скоупом поиска структур (модель AA, без сети).

    Token post-save сигнал django-esi создаёт EveCharacter через ESI —
    подменяем get_or_create_esi на офлайн-создание (§18: тесты без сети).
    """
    from allianceauth.eveonline.managers import EveCharacterManager
    from allianceauth.eveonline.models import EveCharacter
    from esi.models import Scope

    from .test_views_permissions_helpers import make_character

    scope, _created = Scope.objects.get_or_create(name=esi_client.SCOPE_SEARCH_STRUCTURES)

    def _offline_character(character_id):
        eve_char = EveCharacter.objects.filter(character_id=character_id).first()
        return eve_char or make_character(character_id=character_id)

    token = None
    with mock.patch.object(EveCharacterManager, "get_or_create_esi", side_effect=_offline_character):
        token = Token.objects.create(
            user=user,
            character_id=character_id,
            character_name=f"Char {character_id}",
            character_owner_hash=f"hash-{character_id}",
        )
    token.scopes.add(scope)
    return token


# ---------------------------------------------------------------------------
# esi/client: fetch-слой
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_search_structures_returns_deduped_rows_with_names(db):
    """search_structures_for_character: шаг 1 (ids) + шаг 3 (имя/система); дубликаты схлопнуты."""
    from django.contrib.auth.models import User

    u = User.objects.create_user("searcher", password="x")
    _make_search_token(u, 2112000001)

    search_result = {"structure": [10221127, 10221127, 10221128]}
    infos = {
        10221127: {"name": "Corp Fort", "solar_system_id": 30000142},
        10221128: {"name": "Corp HS", "solar_system_id": 30000144},
    }

    class FakeOp:
        def __call__(self, **kwargs):
            assert kwargs["categories"] == ["structure"]
            assert kwargs["search"] == "fort"
            assert kwargs["character_id"] == 2112000001
            return self

        def result(self, **kwargs):
            return search_result

    class FakeInfo:
        def __init__(self, structure_id):
            self.structure_id = structure_id

        def result(self, **kwargs):
            return infos[self.structure_id]

    fake_client = mock.Mock()
    fake_client.client.Search.GetCharactersCharacterIdSearch = FakeOp()

    def _info_op(structure_id, token=None):
        return FakeInfo(structure_id)

    fake_client.client.Universe.GetUniverseStructuresStructureId = mock.Mock(side_effect=_info_op)
    with (
        mock.patch.object(esi_client, "esi", fake_client),
        mock.patch.object(esi_client, "get_search_token", return_value=mock.Mock()),
    ):
        rows = esi_client.search_structures_for_character(2112000001, "fort")
    assert rows == [
        {"id": 10221127, "name": "Corp Fort", "solar_system_id": 30000142},
        {"id": 10221128, "name": "Corp HS", "solar_system_id": 30000144},
    ]


@pytest.mark.django_db
def test_search_structures_without_token_raises():
    """Нет валидного токена со скоупом поиска — ValueError (§9 шаг 1)."""
    with mock.patch.object(esi_client, "get_search_token", return_value=None):
        with pytest.raises(ValueError):
            esi_client.search_structures_for_character(2112000001, "fort")


@pytest.mark.django_db
def test_search_structures_info_failure_keeps_row(db):
    """Сбой шага 3 (имя/система) не прерывает поиск: строка остаётся с name=None."""
    from django.contrib.auth.models import User

    u = User.objects.create_user("searcher2", password="x")
    _make_search_token(u, 2112000002)

    class FakeOp:
        def __call__(self, **kwargs):
            return self

        def result(self, **kwargs):
            return {"structure": [10221127]}

    fake_client = mock.Mock()
    fake_client.client.Search.GetCharactersCharacterIdSearch = FakeOp()
    fake_client.client.Universe.GetUniverseStructuresStructureId.side_effect = RuntimeError("esi down")
    with (
        mock.patch.object(esi_client, "esi", fake_client),
        mock.patch.object(esi_client, "get_search_token", return_value=mock.Mock()),
    ):
        rows = esi_client.search_structures_for_character(2112000002, "fort")
    assert rows == [{"id": 10221127, "name": None, "solar_system_id": None}]


# ---------------------------------------------------------------------------
# Задача search_structures
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_search_structures_task_caches_done_with_region(db):
    """Задача кладёт результат в кэш: status=done, region_id из SDE (eveuniverse, без ESI)."""
    rows = [
        {"id": 10221127, "name": "Corp Fort", "solar_system_id": 30000142},
        {"id": 10221128, "name": "No Info", "solar_system_id": None},
    ]
    with (
        mock.patch.object(esi_client, "search_structures_for_character", return_value=rows),
        mock.patch("eveuniverse.models.EveSolarSystem") as eve_system,
    ):
        eve_system.objects.filter.return_value.values_list.return_value = [
            (30000142, 10000002),
        ]
        result = tasks.search_structures(2112000001, "fort", "a" * 32)
    assert result["ok"] is True
    data = cache.get(f"{tasks.SEARCH_CACHE_PREFIX}:{'a' * 32}")
    assert data["status"] == "done"
    assert data["results"][0]["region_id"] == 10000002
    assert data["results"][1]["region_id"] is None


@pytest.mark.django_db
def test_search_structures_task_error_cached(db):
    """Сбой задачи фиксируется в кэше status=error — UI показывает отсутствие результатов."""
    with mock.patch.object(esi_client, "search_structures_for_character", side_effect=ValueError("no token")):
        result = tasks.search_structures(2112000001, "fort", "b" * 32)
    assert result["ok"] is False
    data = cache.get(f"{tasks.SEARCH_CACHE_PREFIX}:{'b' * 32}")
    assert data["status"] == "error"
    assert data["results"] == []


# ---------------------------------------------------------------------------
# View-эндпоинты
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_structure_search_start_without_token(editor_client):
    """Нет токена со скоупом поиска — no_token (§9 шаг 1 требует токен персонажа)."""
    resp = editor_client.get("/marketmonitoring/locations/structures.json?q=fort")
    assert resp.status_code == 400
    assert resp.json()["error"] == "no_token"


@pytest.mark.django_db
def test_structure_search_start_queues_task(editor_client, editor):
    """Запуск: job_id + задача в очереди; character_id — из валидного токена пользователя."""
    _make_search_token(editor, 2112000001)
    with mock.patch.object(tasks.search_structures, "delay") as delay:
        resp = editor_client.get("/marketmonitoring/locations/structures.json?q=fort")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["job_id"]) == 32
    assert data["character_id"] == 2112000001
    delay.assert_called_once()
    assert delay.call_args.args == (2112000001, "fort", data["job_id"])


@pytest.mark.django_db
def test_structure_search_start_short_query(editor_client, editor):
    """Меньше 3 символов — задача не ставится (§9: параметр search обязателен, мин ~3)."""
    _make_search_token(editor, 2112000001)
    with mock.patch.object(tasks.search_structures, "delay") as delay:
        resp = editor_client.get("/marketmonitoring/locations/structures.json?q=fo")
    assert resp.status_code == 200
    assert resp.json()["job_id"] is None
    delay.assert_not_called()


@pytest.mark.django_db
def test_structure_search_start_denied_for_viewer(viewer_client):
    """Право edit обязательно (§14): view-роли — 403."""
    resp = viewer_client.get("/marketmonitoring/locations/structures.json?q=fort")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_structure_search_result_done(editor_client):
    """Готовый результат читается из кэша (status=done + results)."""
    cache.set(
        f"{tasks.SEARCH_CACHE_PREFIX}:{'c' * 32}",
        {"status": "done", "results": [{"id": 1, "name": "Corp Fort"}]},
        60,
    )
    resp = editor_client.get("/marketmonitoring/locations/structures/result.json?job_id=" + "c" * 32)
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    assert resp.json()["results"][0]["name"] == "Corp Fort"


@pytest.mark.django_db
def test_structure_search_result_pending_and_invalid(editor_client):
    """Нет записи в кэше — pending; неверный формат job_id — 400."""
    resp = editor_client.get("/marketmonitoring/locations/structures/result.json?job_id=" + "d" * 32)
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    resp = editor_client.get("/marketmonitoring/locations/structures/result.json?job_id=zzz")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_structure_search_result_denied_for_viewer(viewer_client):
    """Право edit обязательно и на чтении результата (§14)."""
    resp = viewer_client.get("/marketmonitoring/locations/structures/result.json?job_id=" + "e" * 32)
    assert resp.status_code == 403
