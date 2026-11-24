"""Тесты мониторинга контрактов и комплектности кораблей в фите (§6.12, §9.1, §13).

ESI полностью замокан на уровне marketmonitoring.esi.client — сеть не используется.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from allianceauth.eveonline.models import EveCharacter
from django.utils import timezone

from ..calc import engine
from ..calc.engine import ContractInput, ContractItemInput
from ..constants import (
    ContractFitStatus,
    ContractSourceKind,
    FitStatus,
    NotificationEvent,
    ScopeKind,
    SyncStatus,
)
from ..esi import client as esi_client
from ..models import (
    ContractCache,
    ContractItemCache,
    ContractProvider,
    Fit,
    FitComponent,
    NotificationLog,
    PluginSetting,
)


@pytest.fixture(autouse=True)
def _reset_packaged_volumes_cache():
    """Сброс кеша packaged_volume между тестами (модульный кеш calc.service)."""
    from ..calc import service as calc_service

    calc_service._packaged_volumes_cache = None
    yield
    calc_service._packaged_volumes_cache = None


def _contract_raw(
    contract_id=7001, status="outstanding", contract_type="item_exchange", expires_in_days=7, **overrides
):
    """Сырой контракт ESI item_exchange для моков."""
    now = timezone.now()
    raw = {
        "contract_id": contract_id,
        "issuer_id": 9001,
        "issuer_corporation_id": 9002,
        "assignee_id": 9002,
        "acceptor_id": 0,
        "availability": "corporation",
        "contract_type": contract_type,
        "type": contract_type,
        "status": status,
        "title": "Rifter doctrine kit",
        "start_location_id": 60003760,
        "end_location_id": None,
        "price": "150000000.00",
        "collateral": "0.00",
        "buyout": "0.00",
        "date_issued": now.isoformat(),
        "date_expired": (now + timedelta(days=expires_in_days)).isoformat(),
        "days_to_complete": 0,
    }
    raw.update(overrides)
    return raw


def _item_raw(type_id, quantity, is_included=True, raw_quantity=None, record_id=None):
    """Сырая позиция контракта ESI для моков."""
    return {
        "record_id": record_id or type_id * 1000,
        "type_id": type_id,
        "quantity": quantity,
        "is_included": is_included,
        "is_singleton": raw_quantity == -1 or quantity == 1,
        "raw_quantity": raw_quantity,
    }


# --- Чистое ядро (§6.12) --------------------------------------------------


def test_calc_contract_fit_sets_full_kit_counts_ships():
    """Полный комплект: корпус + модули + заряды -> ships_c = min_i floor(Q_i / q_i)."""
    items = [
        ContractItemInput(type_id=587, quantity=2),
        ContractItemInput(type_id=114, quantity=2),
        ContractItemInput(type_id=34, quantity=300),
    ]
    components = [(587, 1), (114, 1), (34, 100)]
    assert engine.calc_contract_fit_sets(items, components) == 2


def test_calc_contract_fit_sets_missing_hull_or_component_is_zero():
    """Нет корпуса или забыт модуль/заряд -> 0 полных комплектов."""
    components = [(587, 1), (114, 1), (34, 100)]
    missing_hull = [ContractItemInput(type_id=114, quantity=1), ContractItemInput(type_id=34, quantity=300)]
    missing_module = [ContractItemInput(type_id=587, quantity=1), ContractItemInput(type_id=34, quantity=300)]
    assert engine.calc_contract_fit_sets(missing_hull, components) == 0
    assert engine.calc_contract_fit_sets(missing_module, components) == 0


def test_calc_contract_fit_sets_excludes_bpc_and_requested_items():
    """BPC (raw_quantity=-2) и requested items (is_included=False) не считаются готовыми предметами."""
    components = [(587, 1), (34, 100)]
    items = [
        ContractItemInput(type_id=587, quantity=1, raw_quantity=-2),
        ContractItemInput(type_id=587, quantity=1, is_included=False),
        ContractItemInput(type_id=34, quantity=100),
    ]
    assert engine.calc_contract_fit_sets(items, components) == 0


def test_calc_contracts_fit_deduplicates_aggregates_and_deficit():
    """Дедуп по contract_id, суммирование C, дефицит max(0, N-C), статус deficit/ok."""
    contracts = [
        ContractInput(1, [ContractItemInput(587, 1), ContractItemInput(34, 100)]),
        ContractInput(1, [ContractItemInput(587, 1), ContractItemInput(34, 100)]),
        ContractInput(2, [ContractItemInput(587, 1), ContractItemInput(34, 100)]),
    ]
    components = [(587, 1), (34, 100)]
    res = engine.calc_contracts_fit(contracts, components, qty_target=4, has_no_data=False)
    assert res.ships_available == 2
    assert res.deficit == 2
    assert res.status == ContractFitStatus.DEFICIT
    assert [m.contract_id for m in res.matched_contracts] == [1, 2]

    ok_res = engine.calc_contracts_fit(contracts[:2], components, qty_target=1, has_no_data=False)
    assert ok_res.status == ContractFitStatus.OK
    assert ok_res.deficit == 0

    no_data = engine.calc_contracts_fit(contracts, components, qty_target=3, has_no_data=True)
    assert no_data.status == ContractFitStatus.NO_DATA
    assert no_data.ships_available is None
    assert no_data.deficit is None


# --- Расчётный сервис (БД-обвязка без сети) --------------------------------


@pytest.fixture
def sde_ids(db):
    """Минимальные справочники eveuniverse: система/регион/станции/типы (§6.1)."""
    from eveuniverse.models import (
        EveCategory,
        EveConstellation,
        EveGroup,
        EveRegion,
        EveSolarSystem,
        EveStation,
        EveType,
    )

    region = EveRegion.objects.create(id=10000002, name="The Forge")
    constellation = EveConstellation.objects.create(id=20000001, name="Const", eve_region=region)
    system = EveSolarSystem.objects.create(
        id=30000142, name="Jita", eve_constellation=constellation, security_status=0.9
    )
    category, _ = EveCategory.objects.get_or_create(id=6, defaults={"name": "Ship", "published": True})
    group, _ = EveGroup.objects.get_or_create(
        id=1, defaults={"name": "ShipGroup", "eve_category": category, "published": True}
    )
    hull_type = EveType.objects.create(
        id=587, name="Rifter", eve_group=group, published=True, packaged_volume=5000, volume=5000
    )
    module_type = EveType.objects.create(
        id=114, name="Power Diagnostic System", eve_group=group, published=True, packaged_volume=5, volume=5
    )
    EveStation.objects.create(
        id=60003760,
        name="Jita IV-4",
        eve_solar_system=system,
        eve_type=hull_type,
        max_dockable_ship_volume=0,
        office_rental_cost=0,
        reprocessing_efficiency=0,
        reprocessing_stations_take=0,
    )
    return {"system": system, "station_id": 60003760, "hull_type": hull_type, "module_type": module_type}


@pytest.fixture
def settings_row(db, sde_ids):
    """Настройки плагина для тестов контрактов."""
    return PluginSetting.load()


@pytest.fixture
def fit_with_components(sde_ids):
    """Фит N=2: корпус Rifter + модуль Power Diagnostic System x100 на станции Jita IV-4."""
    fit = Fit.objects.create(
        name="Contract Doctrine Rifter",
        hull_type_id=sde_ids["hull_type"].id,
        qty_target=2,
        scope_kind=ScopeKind.STATION,
        scope_ref=sde_ids["station_id"],
    )
    FitComponent.objects.create(fit=fit, type_id=sde_ids["hull_type"].id, qty_per_fit=1)
    FitComponent.objects.create(fit=fit, type_id=sde_ids["module_type"].id, qty_per_fit=100)
    return fit


@pytest.fixture
def contract_character(db):
    """EveCharacter AA без сети (§8) для провайдеров контрактов."""
    return EveCharacter.objects.create(
        character_id=9001,
        character_name="Contract Pilot",
        corporation_id=9002,
        corporation_name="Corp",
        corporation_ticker="CRP",
    )


@pytest.mark.django_db
def test_evaluate_fit_contracts_requires_fresh_provider(settings_row, fit_with_components, contract_character):
    """Без свежего успешного синка провайдера контрактов -> NO_DATA и причины (§6.12)."""
    from ..calc.service import evaluate_fit_contracts

    ContractProvider.objects.create(
        character=contract_character, source_kind=ContractSourceKind.CORPORATION, sync_status=SyncStatus.OK
    )

    result, matched, reasons = evaluate_fit_contracts(fit_with_components)
    assert result.status == ContractFitStatus.NO_DATA
    assert matched == []
    assert any("stale" in reason or "sync_status" in reason for reason in reasons)


@pytest.mark.django_db
def test_evaluate_fit_contracts_counts_matching_outstanding_kits(settings_row, fit_with_components, contract_character):
    """Контракты учитываются только outstanding/item_exchange, не истёкшие и в области фита; полный комплект -> C."""
    from ..calc.service import evaluate_fit_contracts

    provider = ContractProvider.objects.create(
        character=contract_character, source_kind=ContractSourceKind.CORPORATION, sync_status=SyncStatus.OK
    )
    provider.last_synced_at = timezone.now()
    provider.save(update_fields=["last_synced_at"])

    hull = fit_with_components.components.first().type_id
    module = fit_with_components.components.last().type_id
    now = timezone.now()
    good = ContractCache.objects.create(
        contract_id=7001,
        source_kind=ContractSourceKind.CORPORATION,
        source_ref=9002,
        issuer_id=1,
        issuer_corporation_id=9002,
        assignee_id=9002,
        availability="corporation",
        contract_type="item_exchange",
        status="outstanding",
        start_location_id=fit_with_components.scope_ref,
        price=Decimal("1"),
        date_issued=now,
        date_expired=now + timedelta(days=1),
        fetched_at=now,
    )
    ContractItemCache.objects.bulk_create(
        [
            ContractItemCache(contract=good, record_id=1, type_id=hull, quantity=1),
            ContractItemCache(contract=good, record_id=2, type_id=module, quantity=200),
        ]
    )
    # Истёкший контракт и контракт с неполным составом игнорируются.
    expired = ContractCache.objects.create(
        contract_id=7002,
        source_kind=ContractSourceKind.CORPORATION,
        source_ref=9002,
        issuer_id=1,
        issuer_corporation_id=9002,
        assignee_id=9002,
        availability="corporation",
        contract_type="item_exchange",
        status="outstanding",
        start_location_id=fit_with_components.scope_ref,
        price=Decimal("1"),
        date_issued=now - timedelta(days=10),
        date_expired=now - timedelta(days=1),
        fetched_at=now,
    )
    ContractItemCache.objects.create(contract=expired, record_id=3, type_id=hull, quantity=1)

    other_loc = ContractCache.objects.create(
        contract_id=7003,
        source_kind=ContractSourceKind.CORPORATION,
        source_ref=9002,
        issuer_id=1,
        issuer_corporation_id=9002,
        assignee_id=9002,
        availability="alliance",
        contract_type="item_exchange",
        status="outstanding",
        start_location_id=999999999,
        price=Decimal("1"),
        date_issued=now,
        date_expired=now + timedelta(days=1),
        fetched_at=now,
    )
    ContractItemCache.objects.bulk_create(
        [
            ContractItemCache(contract=other_loc, record_id=4, type_id=hull, quantity=1),
            ContractItemCache(contract=other_loc, record_id=5, type_id=module, quantity=200),
        ]
    )

    result, matched, reasons = evaluate_fit_contracts(fit_with_components)
    assert reasons == []
    assert result.status == ContractFitStatus.DEFICIT
    assert result.ships_available == 1
    assert result.deficit == max(0, fit_with_components.qty_target - 1)
    assert [c.contract_id for c, _ships in matched] == [7001]


@pytest.mark.django_db
def test_resolve_contract_locations_system_without_stations_is_empty(sde_ids, settings_row):
    """Система без NPC-станций: L(S) пустое, а не {None} (регрессия LEFT JOIN NULL).

    values_list по обратной FK даёт строку NULL для системы без станций;
    None в локациях делал выборку start_location_id__in=[None] пустой,
    а проверку «область не пустая» — пройденной → ложный deficit.
    """
    from eveuniverse.models import EveSolarSystem

    from ..calc.service import resolve_contract_location_ids

    empty_system = EveSolarSystem.objects.create(
        id=30001555, name="Lonely", eve_constellation=sde_ids["system"].eve_constellation, security_status=0.5
    )
    location_ids = resolve_contract_location_ids(ScopeKind.SYSTEM, empty_system.id)
    assert location_ids == set()
    assert None not in location_ids


@pytest.mark.django_db
def test_evaluate_fit_contracts_system_without_stations_is_no_data(sde_ids, settings_row, contract_character):
    """Фит в системе без NPC-станций и структур → NO_DATA с причиной, не deficit (§6.12)."""
    from eveuniverse.models import EveSolarSystem

    from ..calc.service import evaluate_fit_contracts

    provider = ContractProvider.objects.create(
        character=contract_character, source_kind=ContractSourceKind.CORPORATION, sync_status=SyncStatus.OK
    )
    provider.last_synced_at = timezone.now()
    provider.save(update_fields=["last_synced_at"])
    empty_system = EveSolarSystem.objects.create(
        id=30001555, name="Lonely", eve_constellation=sde_ids["system"].eve_constellation, security_status=0.5
    )
    fit = Fit.objects.create(
        name="Empty system fit",
        hull_type_id=sde_ids["hull_type"].id,
        qty_target=1,
        scope_kind=ScopeKind.SYSTEM,
        scope_ref=empty_system.id,
    )
    FitComponent.objects.create(fit=fit, type_id=sde_ids["hull_type"].id, qty_per_fit=1)

    result, matched, reasons = evaluate_fit_contracts(fit)
    assert result.status == ContractFitStatus.NO_DATA
    assert matched == []
    assert any("no locations resolved" in reason for reason in reasons)


# --- ESI-клиент контрактов (§9.1) -----------------------------------------


@pytest.mark.django_db
def test_build_contract_cache_row_filters_active_item_exchange():
    """build_contract_cache_row пропускает completed/in_progress/auction/истёкшие контракты (§6.12)."""
    row = esi_client.build_contract_cache_row(_contract_raw(), source_kind="corporation", source_ref=9002)
    assert row is not None
    assert row["contract_id"] == 7001
    assert row["price"] == Decimal("150000000.00")

    assert (
        esi_client.build_contract_cache_row(
            _contract_raw(status="in_progress"), source_kind="corporation", source_ref=9002
        )
        is None
    )
    assert (
        esi_client.build_contract_cache_row(
            _contract_raw(contract_type="courier"), source_kind="corporation", source_ref=9002
        )
        is None
    )
    assert (
        esi_client.build_contract_cache_row(
            _contract_raw(expires_in_days=-1), source_kind="corporation", source_ref=9002
        )
        is None
    )


@pytest.mark.django_db
def test_fetch_corporation_contracts_pagination_without_network(settings_row):
    """fetch_corporation_contracts обходит страницы и использует токен провайдера (§9.1)."""
    token = mock.Mock()
    op_calls = []

    class FakeOp:
        def __call__(self, **kwargs):
            op_calls.append(kwargs)
            return self

        def result(self, **kwargs):
            page = op_calls[-1]["page"]
            response = mock.Mock(headers={"X-Pages": "3"} if page == 1 else {})
            if page >= 2:
                return [], response
            return [_contract_raw(contract_id=7000 + page)], response

    fake_client = mock.Mock()
    fake_client.client.Contracts.GetCorporationsCorporationIdContracts = FakeOp()
    with (
        mock.patch.object(esi_client, "esi", fake_client),
        mock.patch.object(esi_client, "_token_for_contracts", return_value=token),
        mock.patch.object(esi_client, "_check_error_limit"),
    ):
        contracts, partial = esi_client.fetch_corporation_contracts(9002, 9001)

    assert partial is False
    # Конец пагинации определяется по X-Pages и пустой второй странице (§9.2).
    assert len(contracts) == 1
    assert [call["corporation_id"] for call in op_calls] == [9002, 9002]


# --- Задача sync_contracts (§12.1, §6.12) ----------------------------------


@pytest.fixture
def contract_provider(settings_row, contract_character):
    """Персонаж AA + включённый corporation ContractProvider со свежим синком для тестов задачи."""
    provider = ContractProvider.objects.create(
        character=contract_character,
        source_kind=ContractSourceKind.CORPORATION,
        enabled=True,
        priority=10,
        sync_status=SyncStatus.OK,
    )
    provider.last_synced_at = timezone.now()
    provider.save(update_fields=["last_synced_at"])
    return provider


@pytest.mark.django_db
def test_sync_contracts_writes_cache_and_recalculates_chain(contract_provider, fit_with_components):
    """sync_contracts: новые контракты и позиции пишутся, items догружаются один раз, цепочка recalc запускается."""
    from ..tasks import sync_contracts

    contract = _contract_raw(contract_id=7100, start_location_id=fit_with_components.scope_ref)
    items = [
        _item_raw(fit_with_components.components.first().type_id, 1, raw_quantity=-1),
        _item_raw(fit_with_components.components.last().type_id, 200),
    ]

    with (
        mock.patch.object(esi_client, "fetch_corporation_contracts", return_value=([contract], False)) as fetch_list,
        mock.patch.object(esi_client, "fetch_corporation_contract_items", return_value=items) as fetch_items,
        mock.patch("marketmonitoring.tasks._chain_recalc_and_notify") as chain,
    ):
        result = sync_contracts(trigger="manual", lock_token="tok-1")

    assert result["ok"] is True
    cached = ContractCache.objects.get(contract_id=7100)
    assert cached.source_kind == ContractSourceKind.CORPORATION
    assert cached.start_location_id == fit_with_components.scope_ref
    assert cached.price == Decimal("150000000.00")
    assert ContractItemCache.objects.filter(contract=cached).count() == 2
    assert fetch_list.call_args.args == (9002, 9001)
    assert fetch_items.call_args.args == (9002, 7100, 9001)
    chain.assert_called_once_with("manual", [], lock_token="tok-1")

    # Повторный синк не дёргает /items/ уже закэшированного открытого контракта (решение №35).
    with (
        mock.patch.object(esi_client, "fetch_corporation_contracts", return_value=([contract], False)),
        mock.patch.object(esi_client, "fetch_corporation_contract_items") as second_fetch_items,
        mock.patch("marketmonitoring.tasks._chain_recalc_and_notify"),
    ):
        sync_contracts(trigger="manual", lock_token="tok-2")

    second_fetch_items.assert_not_called()


@pytest.mark.django_db
def test_sync_contracts_removes_disappeared_and_expired_contracts(contract_provider):
    """Исчезновение outstanding-контракта из среза удаляет его кэш при полном синке (§12.5)."""
    from ..tasks import sync_contracts

    old = ContractCache.objects.create(
        contract_id=7200,
        source_kind=ContractSourceKind.CORPORATION,
        source_ref=9002,
        issuer_id=1,
        issuer_corporation_id=9002,
        assignee_id=9002,
        availability="corporation",
        contract_type="item_exchange",
        status="outstanding",
        start_location_id=60003760,
        price=Decimal("1"),
        date_issued=timezone.now(),
        date_expired=timezone.now() + timedelta(days=1),
        fetched_at=timezone.now(),
    )
    ContractItemCache.objects.create(contract=old, record_id=1, type_id=587, quantity=1)

    with (
        mock.patch.object(esi_client, "fetch_corporation_contracts", return_value=([], False)),
        mock.patch("marketmonitoring.tasks._chain_recalc_and_notify"),
    ):
        sync_contracts(trigger="manual", lock_token="tok-3")

    assert not ContractCache.objects.filter(contract_id=7200).exists()
    assert not ContractItemCache.objects.filter(contract_id=old.pk).exists()


@pytest.mark.django_db
def test_sync_contracts_keeps_cross_endpoint_contract(contract_provider):
    """Кросс-эндпоинтовый контракт не удаляется синком одного источника (решение №38 §20).

    Контракт виден и в корпоративном, и в персонажном срезах. Корпоративный провайдер
    возвращает пустой срез, персонажный — контракт. Кэш (записанный от корпоративного
    источника) должен выжить: финальная зачистка идёт по объединённому active-множеству.
    """
    from ..tasks import sync_contracts

    # Провайдер персонажных контрактов с меньшим приоритетом (обходится вторым).
    ContractProvider.objects.create(
        character=contract_provider.character,
        source_kind=ContractSourceKind.CHARACTER,
        enabled=True,
        priority=20,
        sync_status=SyncStatus.OK,
    )
    # Кэш контракта от корпоративного источника.
    ContractCache.objects.create(
        contract_id=7300,
        source_kind=ContractSourceKind.CORPORATION,
        source_ref=9002,
        issuer_id=1,
        issuer_corporation_id=9002,
        assignee_id=9002,
        availability="corporation",
        contract_type="item_exchange",
        status="outstanding",
        start_location_id=60003760,
        price=Decimal("1"),
        date_issued=timezone.now(),
        date_expired=timezone.now() + timedelta(days=1),
        fetched_at=timezone.now(),
    )

    contract = _contract_raw(contract_id=7300)
    items = [_item_raw(587, 1)]

    with (
        mock.patch.object(esi_client, "fetch_corporation_contracts", return_value=([], False)),
        mock.patch.object(esi_client, "fetch_character_contracts", return_value=([contract], False)),
        mock.patch.object(esi_client, "fetch_character_contract_items", return_value=items),
        mock.patch("marketmonitoring.tasks._chain_recalc_and_notify"),
    ):
        result = sync_contracts(trigger="manual", lock_token="tok-x")

    assert result["ok"] is True
    # Контракт пережил зачистку корпоративного источника — активен в персонажном срезе.
    assert ContractCache.objects.filter(contract_id=7300).exists()


@pytest.mark.django_db
def test_sync_contracts_acl_failure_cooldowns_provider(contract_provider):
    """403 на списке контрактов обрабатывается как ACL-сбой провайдера (§11)."""
    from esi.exceptions import HTTPClientError

    from ..providers import service as provider_service
    from ..tasks import sync_contracts

    with (
        mock.patch.object(
            esi_client,
            "fetch_corporation_contracts",
            side_effect=HTTPClientError(status_code=403, headers={}, data=None),
        ),
        mock.patch.object(
            provider_service,
            "handle_contract_provider_acl_failure",
            wraps=provider_service.handle_contract_provider_acl_failure,
        ) as acl_handler,
        mock.patch("marketmonitoring.tasks._chain_recalc_and_notify"),
    ):
        result = sync_contracts(trigger="manual", lock_token="tok-4")

    assert result["ok"] is True
    assert result["providers_failed"] == 1
    acl_handler.assert_called_once()
    contract_provider.refresh_from_db()
    assert contract_provider.sync_status == SyncStatus.NO_ACL


# --- Discord contracts_deficit (§13) ---------------------------------------


@pytest.mark.django_db
def test_notify_deficit_sends_contracts_deficit_only_on_beat(contract_provider, fit_with_components):
    """contracts_deficit отправляется только при beat, с дедупликацией по state_hash/cooldown (§6.12, §13)."""
    from ..notify import discord
    from ..tasks import notify_deficit

    webhook_url = "https://discord.example/hook"
    PluginSetting.load()
    from ..models import WebhookConfig

    WebhookConfig.objects.update_or_create(pk=1, defaults={"url": webhook_url, "enabled": True})

    hull_comp = fit_with_components.components.first()
    mod_comp = fit_with_components.components.last()
    now = timezone.now()
    cache = ContractCache.objects.create(
        contract_id=7300,
        source_kind=ContractSourceKind.CORPORATION,
        source_ref=9002,
        issuer_id=1,
        issuer_corporation_id=9002,
        assignee_id=9002,
        availability="corporation",
        contract_type="item_exchange",
        status="outstanding",
        start_location_id=fit_with_components.scope_ref,
        price=Decimal("1"),
        date_issued=now,
        date_expired=now + timedelta(days=1),
        fetched_at=now,
    )
    ContractItemCache.objects.bulk_create(
        [
            ContractItemCache(contract=cache, record_id=1, type_id=hull_comp.type_id, quantity=1),
            ContractItemCache(contract=cache, record_id=2, type_id=mod_comp.type_id, quantity=mod_comp.qty_per_fit),
        ]
    )

    sent_events = []

    def _fake_send(embed, event, state_hash, fit=None, component=None, dedup_transition=False):
        sent_events.append(event)
        NotificationLog.objects.create(
            event=event, fit=fit, state_hash=state_hash, sent_at=timezone.now(), ok=True, error=""
        )
        return True, ""

    with (
        mock.patch.object(discord, "send_webhook", side_effect=_fake_send),
        mock.patch("requests.post"),
    ):
        manual_result = notify_deficit(trigger="manual")
        beat_result = notify_deficit(trigger="beat")
        second_beat = notify_deficit(trigger="beat")

    assert manual_result.get("skipped") is True
    assert NotificationEvent.CONTRACTS_DEFICIT in sent_events
    assert beat_result["sent"] >= 1
    assert second_beat["skipped"] >= 1
    log = NotificationLog.objects.filter(
        event=NotificationEvent.CONTRACTS_DEFICIT, fit=fit_with_components, ok=True
    ).first()
    assert log is not None


# --- UI / права (§14, §3) --------------------------------------------------


@pytest.fixture
def editor_user(db):
    """Пользователь с правом редактирования плагина (§3)."""
    from django.contrib.auth.models import Group, User

    user = User.objects.create_user(username="contract-editor", password="x")
    user.groups.add(Group.objects.get(name="market_monitoring_edit"))
    return user


@pytest.mark.django_db
def test_dashboard_and_fit_detail_show_contracts(
    client, editor_user, settings_row, fit_with_components, contract_provider
):
    """Dashboard и карточка фита отображают contracts_available/status из БД без ESI (§6.12, §14)."""
    from django.contrib.auth.models import Group
    from django.urls import reverse

    client.force_login(editor_user)
    editor_user.groups.add(Group.objects.get(name="market_monitoring_edit"))

    fit_with_components.contracts_status_cache = ContractFitStatus.DEFICIT
    fit_with_components.contracts_available = 1
    fit_with_components.save(update_fields=["contracts_status_cache", "contracts_available"])

    response = client.get(reverse("marketmonitoring:index"))
    assert response.status_code == 200
    assert b"Ships on contracts" in response.content

    detail = client.get(reverse("marketmonitoring:fit_detail", args=[fit_with_components.pk]))
    assert detail.status_code == 200
    assert b"Ships on contracts" in detail.content
