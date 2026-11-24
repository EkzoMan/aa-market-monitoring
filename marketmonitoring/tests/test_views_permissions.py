"""Тесты прав доступа UI (§18.7, §14, §16): _view/_edit/аноним, webhook_url, refresh_status, блокировка 409.

Тесты без сети: задачи Celery патчатся (CELERY_TASK_ALWAYS_EAGER в тестовом окружении).
"""

import json

import pytest
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ..constants import LocationKind, SyncStatus, TriggerKind
from ..models import Fit, MarketLocation, PluginSetting, WebhookConfig
from ..tasks import SYNC_LOCK_KEY
from .test_calc_engine import sde_ids  # noqa: F401 — справочники eveuniverse
from .test_eft_parser import eve_types  # noqa: F401 — записи EveType

pytestmark = pytest.mark.django_db

VIEW_URLS = ["marketmonitoring:index"]


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


def _set_jita_enabled(enabled: bool):
    """Включить/отключить локацию-станцию Jita без ESI."""
    settings_obj = PluginSetting.load()
    location, _ = MarketLocation.objects.update_or_create(
        kind=LocationKind.STATION,
        station_id=settings_obj.jita_station_id,
        defaults={
            "name": "Jita IV-4",
            "solar_system_id": 30000142,
            "region_id": settings_obj.jita_region_id,
            "enabled": enabled,
            "sync_status": SyncStatus.OK if enabled else SyncStatus.STALE,
            "last_synced_at": timezone.now() if enabled else None,
        },
    )
    return location


def _delay_spy(name, called):
    """Мок .delay(), запоминающий имя задачи, trigger и токен блокировки."""

    def _delay(trigger, lock_token=None):
        called.append((name, trigger, lock_token))

    return _delay


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
        ("marketmonitoring:locations", None, "get"),
        ("marketmonitoring:location_toggle", [1], "post"),
        ("marketmonitoring:location_delete", [1], "post"),
        ("marketmonitoring:system_search", None, "get"),
        ("marketmonitoring:station_search", None, "get"),
        ("marketmonitoring:type_search", None, "get"),
        ("marketmonitoring:settings", None, "get"),
        ("marketmonitoring:contract_provider_add", None, "post"),
        ("marketmonitoring:contract_provider_toggle", [1], "post"),
        ("marketmonitoring:contract_provider_enable", [1], "post"),
        ("marketmonitoring:contract_provider_delete", [1], "post"),
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


@pytest.mark.parametrize(
    "url_name,args",
    [
        ("marketmonitoring:locations", None),
        ("marketmonitoring:settings", None),
        ("marketmonitoring:contract_provider_add", None),
        ("marketmonitoring:refresh", None),
        ("marketmonitoring:type_search", None),
    ],
)
def test_view_user_cannot_manage(viewer_client, fit_with_component, url_name, args):
    """_view не может создавать/править/удалять: redirect и отсутствие изменений (§16)."""
    url = reverse(url_name, args=args) if args else reverse(url_name)
    response = viewer_client.get(url)
    assert response.status_code in (302, 403, 405)
    response = viewer_client.post(url)
    assert response.status_code in (302, 403, 405)


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
# Роль _edit: управление локациями, настройками
# ---------------------------------------------------------------------------


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


def test_edit_user_creates_structure_location_system_not_in_sde(editor_client, sde_ids):  # noqa: F811
    """Структура в системе вне локального SDE (§9): добавляется без ошибки not found in SDE.

    Система/регион приходят из ESI-поиска и могут отсутствовать в eveuniverse:
    сохраняется best-effort (регион из результата поиска, иначе 0), дозаполняется
    при первом синке (как в sync-задаче).
    """
    data = {
        "kind": "structure",
        "solar_system_id": 31000001,  # нет в локальном SDE
        "region_id": 10000030,
        "station_id": "",
        "structure_id": 1022735589924,
        "structure_name": "Deep Keepstar",
    }
    response = editor_client.post(reverse("marketmonitoring:locations"), data=data)
    assert response.status_code == 302
    location = MarketLocation.objects.get(kind="structure", structure_id=1022735589924)
    assert location.solar_system_id == 31000001
    assert location.region_id == 10000030


def test_edit_user_creates_structure_location_unknown_system(editor_client, sde_ids):  # noqa: F811
    """Структура без системы (ESI не отдал solar_system_id): 0, без ошибки."""
    data = {
        "kind": "structure",
        "solar_system_id": "",
        "region_id": "",
        "station_id": "",
        "structure_id": 1022735589925,
        "structure_name": "Ghost Fort",
    }
    response = editor_client.post(reverse("marketmonitoring:locations"), data=data)
    assert response.status_code == 302
    location = MarketLocation.objects.get(kind="structure", structure_id=1022735589925)
    assert location.solar_system_id == 0
    assert location.region_id == 0


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
    """_edit сохраняет webhook (§14); настройки цен — через admin."""
    response = editor_client.post(
        reverse("marketmonitoring:settings"),
        data={
            "webhook-url": "https://discord.example/hook",
            "webhook-enabled": "on",
        },
    )
    assert response.status_code == 302
    assert WebhookConfig.objects.get().url == "https://discord.example/hook"


# ---------------------------------------------------------------------------
# Провайдеры структур: управление перенесено в Django-admin (§3, §14) —
# отдельные view-эндпоинты provider_add/provider_enable удалены.
# ---------------------------------------------------------------------------


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


def test_refresh_returns_409_when_locked(editor_client, monkeypatch):
    """«Обновить сейчас» при занятой блокировке — 409 (§12.4); задачи не запускаются повторно."""
    cache.set(SYNC_LOCK_KEY, "1234567890.0", timeout=60)
    called = []
    for name in ("sync_jita_book", "sync_region_orders", "sync_structure_orders", "sync_contracts"):
        monkeypatch.setattr(
            f"marketmonitoring.views.refresh.{name}",
            type("T", (), {"delay": staticmethod(_delay_spy(name, called))}),
        )
    try:
        response = editor_client.post(reverse("marketmonitoring:refresh"))
        assert response.status_code == 409
        assert called == []
    finally:
        cache.delete(SYNC_LOCK_KEY)


@pytest.mark.parametrize(
    "jita_enabled,expected_names",
    [
        (True, ("sync_jita_book", "sync_region_orders", "sync_structure_orders", "sync_contracts")),
        (False, ("sync_region_orders", "sync_structure_orders", "sync_contracts")),
    ],
)
def test_refresh_starts_chain(editor_client, monkeypatch, jita_enabled, expected_names):
    """Свободная блокировка: sync_* с trigger=manual и общим токеном; Jita — только если включена."""
    cache.delete(SYNC_LOCK_KEY)
    _set_jita_enabled(jita_enabled)
    called = []
    all_names = ("sync_jita_book", "sync_region_orders", "sync_structure_orders", "sync_contracts")
    for name in all_names:
        monkeypatch.setattr(
            f"marketmonitoring.views.refresh.{name}",
            type("T", (), {"delay": staticmethod(_delay_spy(name, called))}),
        )
    try:
        response = editor_client.post(reverse("marketmonitoring:refresh"))
        assert response.status_code == 302
        lock_value = cache.get(SYNC_LOCK_KEY)
        assert lock_value is not None
        assert called == [(name, TriggerKind.MANUAL, lock_value) for name in expected_names]
        if not jita_enabled:
            assert all(name != "sync_jita_book" for name, _trigger, _token in called)
    finally:
        cache.delete(SYNC_LOCK_KEY)


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
