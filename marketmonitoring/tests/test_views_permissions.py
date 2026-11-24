"""Тесты прав доступа UI (§18.7, §14, §16): _view/_edit/аноним, webhook_url, refresh_status, блокировка 409.

Тесты без сети: задачи Celery патчатся (CELERY_TASK_ALWAYS_EAGER в тестовом окружении).
"""

import json

import pytest
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from ..forms import PluginSettingForm
from ..models import ComponentRequest, Fit, MarketLocation, PluginSetting, WebhookConfig
from ..tasks import SYNC_LOCK_KEY
from .test_calc_engine import sde_ids  # noqa: F401 — справочники eveuniverse
from .test_eft_parser import eve_types  # noqa: F401 — записи EveType

pytestmark = pytest.mark.django_db

VIEW_URLS = ["marketmonitoring:index", "marketmonitoring:components"]


@pytest.fixture
def viewer_client(db):
    """Клиент под пользователем с правом только просмотра."""
    user = User.objects.create_user(username="viewer", password="x")
    user.groups.add(Group.objects.get(name="market_monitoring_view"))
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def editor_client(db):
    """Клиент под пользователем с правом редактирования."""
    user = User.objects.create_user(username="editor", password="x")
    user.groups.add(Group.objects.get(name="market_monitoring_edit"))
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def fit_with_component(eve_types):  # noqa: F811
    """Фит с одним компонентом (тип 2046) в области системы 30000142."""
    fit = Fit.objects.create(name="Test fit", hull_type_id=643, qty_target=2, scope_kind="system", scope_ref=30000142)
    fit.components.create(type_id=2046, qty_per_fit=1)
    return fit


def _component_post(**overrides):
    """Валидный POST для создания компонента."""
    data = {
        "type_id": 2046,
        "type_name": "Damage Control II",
        "qty_target": 5,
        "scope_kind": "system",
        "scope_ref": 30000142,
        "note": "",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Анонимный доступ → redirect (§16)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url_name,args,method",
    [
        ("marketmonitoring:index", None, "get"),
        ("marketmonitoring:fit_detail", [1], "get"),
        ("marketmonitoring:fit_create", None, "get"),
        ("marketmonitoring:fit_update", [1], "get"),
        ("marketmonitoring:fit_delete", [1], "get"),
        ("marketmonitoring:components", None, "get"),
        ("marketmonitoring:component_create", None, "get"),
        ("marketmonitoring:component_update", [1], "get"),
        ("marketmonitoring:component_delete", [1], "get"),
        ("marketmonitoring:locations", None, "get"),
        ("marketmonitoring:location_toggle", [1], "post"),
        ("marketmonitoring:location_delete", [1], "post"),
        ("marketmonitoring:system_search", None, "get"),
        ("marketmonitoring:station_search", None, "get"),
        ("marketmonitoring:type_search", None, "get"),
        ("marketmonitoring:settings", None, "get"),
        ("marketmonitoring:provider_add", None, "post"),
        ("marketmonitoring:provider_toggle", [1], "post"),
        ("marketmonitoring:provider_enable", [1], "post"),
        ("marketmonitoring:export_txt", [1], "get"),
        ("marketmonitoring:export_csv", [1], "get"),
        ("marketmonitoring:refresh", None, "post"),
        ("marketmonitoring:refresh_status", None, "get"),
    ],
)
def test_anonymous_redirects_to_login(client, db, url_name, args, method):
    """Без логина любой view запрещён — redirect на вход (§16, стандартный login_required)."""
    url = reverse(url_name, args=args) if args else reverse(url_name)
    response = getattr(client, method)(url)
    assert response.status_code == 302
    assert "login" in response["Location"].lower()


# ---------------------------------------------------------------------------
# Роль _view: просмотр разрешён, управление запрещено
# ---------------------------------------------------------------------------


def test_view_user_sees_dashboard(viewer_client, fit_with_component):
    """_view видит dashboard и карточку фита (§14)."""
    assert viewer_client.get(reverse("marketmonitoring:index")).status_code == 200
    response = viewer_client.get(reverse("marketmonitoring:fit_detail", args=[fit_with_component.pk]))
    assert response.status_code == 200
    assert "Damage Control II" in response.content.decode()


def test_view_user_sees_components(viewer_client, eve_types):  # noqa: F811
    """_view видит страницу Components (§14.4)."""
    ComponentRequest.objects.create(type_id=2046, qty_target=3, scope_kind="system", scope_ref=30000142)
    response = viewer_client.get(reverse("marketmonitoring:components"))
    assert response.status_code == 200
    assert "Damage Control II" in response.content.decode()


@pytest.mark.parametrize(
    "url_name,args",
    [
        ("marketmonitoring:component_create", None),
        ("marketmonitoring:component_update", [1]),
        ("marketmonitoring:component_delete", [1]),
        ("marketmonitoring:locations", None),
        ("marketmonitoring:settings", None),
        ("marketmonitoring:provider_add", None),
        ("marketmonitoring:refresh", None),
        ("marketmonitoring:type_search", None),
    ],
)
def test_view_user_cannot_manage(viewer_client, fit_with_component, url_name, args):
    """_view не может создавать/править/удалять: redirect и отсутствие изменений (§16)."""
    url = reverse(url_name, args=args) if args else reverse(url_name)
    response = viewer_client.get(url)
    assert response.status_code in (302, 403, 405)
    response = viewer_client.post(url, data=_component_post())
    assert response.status_code in (302, 403, 405)
    assert ComponentRequest.objects.count() == 0


def test_view_user_cannot_save_settings(viewer_client):
    """_view не видит форму настроек и не может её сохранить (§14, §16)."""
    response = viewer_client.get(reverse("marketmonitoring:settings"))
    assert response.status_code == 302
    response = viewer_client.post(reverse("marketmonitoring:settings"), data={"jita_markup_pct": 50})
    assert response.status_code == 302
    assert PluginSetting.load().jita_markup_pct != 50


def test_webhook_url_hidden_from_view_role(viewer_client, editor_client, fit_with_component):
    """webhook_url не показывается пользователям без _edit (§16)."""
    WebhookConfig.objects.get_or_create(pk=1, defaults={"url": "https://discord.example/hook/secret", "enabled": True})
    for url_name, args in [("marketmonitoring:index", None), ("marketmonitoring:fit_detail", [fit_with_component.pk])]:
        response = viewer_client.get(reverse(url_name, args=args) if args else reverse(url_name))
        assert b"discord.example" not in response.content
    # _edit получает доступ к странице настроек с формой webhook (только здесь виден URL, §16).
    response = editor_client.get(reverse("marketmonitoring:settings"))
    assert response.status_code == 200
    assert b"webhook" in response.content.lower()


def test_view_user_denied_settings_page_with_webhook(viewer_client):
    """Страница настроек (единственная, где рендерится webhook_url) закрыта для _view (§16)."""
    WebhookConfig.objects.get_or_create(pk=1, defaults={"url": "https://discord.example/hook/secret", "enabled": True})
    response = viewer_client.get(reverse("marketmonitoring:settings"))
    assert response.status_code == 302


def test_view_user_type_search_denied(viewer_client):
    """JSON-поиск типов — edit-право: _view получает 403 (§14, §16)."""
    response = viewer_client.get(reverse("marketmonitoring:type_search"), data={"q": "Damage"})
    assert response.status_code == 403
    data = json.loads(response.content)
    assert data["results"] == []


# ---------------------------------------------------------------------------
# Роль _edit: управление компонентами, локациями, настройками
# ---------------------------------------------------------------------------


def test_edit_user_manages_components(editor_client, eve_types):  # noqa: F811
    """_edit создаёт/правит/удаляет компонент (§14)."""
    response = editor_client.post(reverse("marketmonitoring:component_create"), data=_component_post())
    assert response.status_code == 302
    obj = ComponentRequest.objects.get()
    assert obj.type_id == 2046 and obj.qty_target == 5
    response = editor_client.post(
        reverse("marketmonitoring:component_update", args=[obj.pk]), data=_component_post(qty_target=7)
    )
    assert response.status_code == 302
    obj.refresh_from_db()
    assert obj.qty_target == 7
    response = editor_client.post(reverse("marketmonitoring:component_delete", args=[obj.pk]))
    assert response.status_code == 302
    assert ComponentRequest.objects.count() == 0


def test_edit_user_rejects_unknown_type(editor_client, eve_types):  # noqa: F811
    """Компонент с несуществующим type_id отклоняется (SDE-справочник, §8)."""
    response = editor_client.post(reverse("marketmonitoring:component_create"), data=_component_post(type_id=999999))
    assert response.status_code == 200
    assert ComponentRequest.objects.count() == 0


def test_edit_user_creates_station_location(editor_client, sde_ids):  # noqa: F811
    """_edit добавляет станцию: имя и регион из SDE, дубль отклоняется (§14, UNIQUE)."""
    data = {
        "kind": "station",
        "solar_system_id": 30000142,
        "station_id": 60003760,
        "structure_id": "",
        "structure_name": "",
    }
    response = editor_client.post(reverse("marketmonitoring:locations"), data=data)
    assert response.status_code == 302
    location = MarketLocation.objects.get(kind="station", station_id=60003760)
    assert location.name == "Jita IV-4"
    assert location.region_id == 10000002
    response = editor_client.post(reverse("marketmonitoring:locations"), data=data)
    assert response.status_code == 200
    assert MarketLocation.objects.filter(kind="station").count() == 1


def test_edit_user_creates_structure_location(editor_client, sde_ids):  # noqa: F811
    """Структура: structure_id + временное имя; реальное имя подтянется при первом синке (§14)."""
    data = {
        "kind": "structure",
        "solar_system_id": 30000142,
        "station_id": "",
        "structure_id": 1022735589923,
        "structure_name": "My Keepstar",
    }
    response = editor_client.post(reverse("marketmonitoring:locations"), data=data)
    assert response.status_code == 302
    location = MarketLocation.objects.get(kind="structure", structure_id=1022735589923)
    assert location.name == "My Keepstar"
    assert location.region_id == 10000002


def test_edit_user_toggles_location(editor_client, sde_ids):  # noqa: F811
    """Вкл/выкл локации (POST, §14)."""
    location = MarketLocation.objects.create(
        name="Jita IV-4", kind="station", station_id=60003760, solar_system_id=30000142, region_id=10000002
    )
    response = editor_client.post(reverse("marketmonitoring:location_toggle", args=[location.pk]))
    assert response.status_code == 302
    location.refresh_from_db()
    assert location.enabled is False


def test_edit_user_saves_settings(editor_client):
    """_edit сохраняет настройки §10 и webhook (§14)."""
    response = editor_client.post(
        reverse("marketmonitoring:settings"),
        data={
            **PluginSettingForm(instance=PluginSetting.load()).initial,
            "webhook-url": "https://discord.example/hook",
        },
    )
    assert response.status_code == 302
    assert WebhookConfig.objects.get().url == "https://discord.example/hook"


# ---------------------------------------------------------------------------
# Провайдеры: привязка персонажа и скоупы (§9.1), сброс disabled (§11.5)
# ---------------------------------------------------------------------------


def test_provider_add_without_scopes_rejected(editor_client):
    """Персонаж без скоупов esi-markets.structure_markets.v1 + esi-universe.read_structures.v1 — отказ (§9.1)."""
    from .test_views_permissions_helpers import make_character

    char = make_character()
    response = editor_client.post(reverse("marketmonitoring:provider_add"), data={"character": char.pk})
    assert response.status_code == 302
    from ..models import StructureProvider

    assert StructureProvider.objects.count() == 0


def test_provider_add_with_scopes(editor_client):
    """Персонаж с обоими скоупами на токене привязывается (§9.1)."""
    from esi.models import Scope, Token

    from ..models import StructureProvider
    from .test_views_permissions_helpers import make_character

    char = make_character()
    token = Token.objects.create(character_id=95465499)
    for scope_name in ("esi-markets.structure_markets.v1", "esi-universe.read_structures.v1"):
        scope, _ = Scope.objects.get_or_create(name=scope_name)
        token.scopes.add(scope)
    response = editor_client.post(reverse("marketmonitoring:provider_add"), data={"character": char.pk})
    assert response.status_code == 302
    assert StructureProvider.objects.filter(character=char).exists()


def test_provider_enable_resets_acl(editor_client):
    """Сброс disabled → enabled обнуляет acl_fail_count и cooldown (§11.5)."""
    from datetime import timedelta

    from django.utils import timezone

    from ..constants import ProviderStatus
    from ..models import StructureProvider
    from .test_views_permissions_helpers import make_character

    char = make_character()
    provider = StructureProvider.objects.create(
        character=char,
        status=ProviderStatus.DISABLED,
        acl_fail_count=3,
        cooldown_until=timezone.now() + timedelta(hours=1),
    )
    response = editor_client.post(reverse("marketmonitoring:provider_enable", args=[provider.pk]))
    assert response.status_code == 302
    provider.refresh_from_db()
    assert provider.enabled is True
    assert provider.status == ProviderStatus.OK
    assert provider.acl_fail_count == 0
    assert provider.cooldown_until is None


# ---------------------------------------------------------------------------
# refresh_status (JSON) и блокировка ручного обновления (§12.4)
# ---------------------------------------------------------------------------


def test_refresh_status_json(viewer_client, fit_with_component):
    """refresh_status: in_progress/last_checked_at/last_result (§14)."""
    response = viewer_client.get(reverse("marketmonitoring:refresh_status"))
    assert response.status_code == 200
    data = json.loads(response.content)
    assert data["in_progress"] is False
    assert data["last_checked_at"] is None
    assert data["last_result"] is None


def test_refresh_status_shows_lock(viewer_client):
    """При удержании блокировки mm:sync_lock in_progress=True (§12.4)."""
    cache.set(SYNC_LOCK_KEY, "1234567890.0", timeout=60)
    try:
        data = json.loads(viewer_client.get(reverse("marketmonitoring:refresh_status")).content)
        assert data["in_progress"] is True
    finally:
        cache.delete(SYNC_LOCK_KEY)


def test_refresh_returns_409_when_locked(editor_client):
    """«Обновить сейчас» при занятой блокировке — 409 (§12.4); задачи не запускаются повторно."""
    cache.set(SYNC_LOCK_KEY, "1234567890.0", timeout=60)
    try:
        response = editor_client.post(reverse("marketmonitoring:refresh"))
        assert response.status_code == 409
    finally:
        cache.delete(SYNC_LOCK_KEY)


def test_refresh_starts_chain(editor_client, monkeypatch):
    """Свободная блокировка: цепочка sync_* ставится с trigger=manual и токеном блокировки (§12.4, §12.3)."""
    from django.core.cache import cache

    cache.delete(SYNC_LOCK_KEY)
    called = []

    def _fake_delay(trigger, lock_token=None):
        called.append((trigger, lock_token))

    for name in ("sync_contracts", "sync_jita_book", "sync_region_orders", "sync_structure_orders"):
        monkeypatch.setattr(
            f"marketmonitoring.views.refresh.{name}", type("T", (), {"delay": staticmethod(_fake_delay)})
        )
    response = editor_client.post(reverse("marketmonitoring:refresh"))
    assert response.status_code == 302
    lock_value = cache.get(SYNC_LOCK_KEY)
    assert lock_value is not None
    assert called == [
        ("manual", lock_value),
        ("manual", lock_value),
        ("manual", lock_value),
        ("manual", lock_value),
    ]


def test_refresh_chain_releases_lock(editor_client, monkeypatch):
    """Финальная задача цепочки (recalc_stock_levels) при trigger=manual снимает блокировку своего токена (§12.4)."""
    from django.core.cache import cache

    from ..tasks import recalc_stock_levels

    cache.delete(SYNC_LOCK_KEY)
    monkeypatch.setattr("marketmonitoring.calc.service.recalc_all", lambda: {})
    token = "chain-token-1"
    cache.set(SYNC_LOCK_KEY, token, timeout=60)
    recalc_stock_levels(trigger="manual", location_ids=[], lock_token=token)
    assert cache.get(SYNC_LOCK_KEY) is None


def test_refresh_chain_keeps_foreign_lock(monkeypatch, db):
    """Чужой токен не снимает блокировку параллельного запуска (§12.4); beat блокировку не трогает."""
    from django.core.cache import cache

    from ..tasks import recalc_stock_levels

    monkeypatch.setattr("marketmonitoring.calc.service.recalc_all", lambda: {})
    # Токен другой цепочки — блокировка остаётся.
    cache.set(SYNC_LOCK_KEY, "current-run-token", timeout=60)
    try:
        recalc_stock_levels(trigger="manual", location_ids=[], lock_token="stale-run-token")
        assert cache.get(SYNC_LOCK_KEY) == "current-run-token"
        # trigger=beat: блокировка не трогается даже при совпадении токена.
        recalc_stock_levels(trigger="beat", location_ids=[], lock_token="current-run-token")
        assert cache.get(SYNC_LOCK_KEY) == "current-run-token"
        # Без токена (beat-цепочка) — блокировка не трогается.
        recalc_stock_levels(trigger="beat", location_ids=[])
        assert cache.get(SYNC_LOCK_KEY) == "current-run-token"
    finally:
        cache.delete(SYNC_LOCK_KEY)


def test_refresh_denied_for_view_role(viewer_client):
    """_view не может запустить обновление (§14, §16)."""
    cache.delete(SYNC_LOCK_KEY)
    response = viewer_client.post(reverse("marketmonitoring:refresh"))
    assert response.status_code == 302
    assert cache.get(SYNC_LOCK_KEY) is None
