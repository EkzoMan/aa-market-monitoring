"""Диагностика контрактов по фиту (§6.12): почему контракты не дают C.

Management-команда читает только БД плагина и SDE (без сети, §5.10) и
печатает цепочку решений: свежесть провайдеров контрактов, резолв локаций
области фита, контракты кэша с признаком попадания в область и числом
полных комплектов ships_c по каждому контракту.

Использование:
    python manage.py debug_contracts <fit_id>
    python manage.py debug_contracts --all
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from marketmonitoring.calc import engine
from marketmonitoring.calc.service import (
    check_contracts_freshness,
    evaluate_fit_contracts,
    resolve_contract_location_ids,
)
from marketmonitoring.models import Fit, PluginSetting


class Command(BaseCommand):
    """Печать диагностики контрактов одного фита или всех фитов (без ESI)."""

    help = "Диагностика контрактов по фиту: свежесть, область, ships_c по контрактам"

    def add_arguments(self, parser) -> None:
        parser.add_argument("fit_id", nargs="?", type=int, help="PK фита (или --all)")
        parser.add_argument("--all", action="store_true", help="Диагностика всех фитов")

    def handle(self, *args, **options) -> None:
        if options["all"]:
            fits = list(Fit.objects.all().order_by("pk"))
            if not fits:
                raise CommandError("Нет фитов")
        else:
            fit_id = options.get("fit_id")
            if fit_id is None:
                raise CommandError("Укажите fit_id или --all")
            fit = Fit.objects.filter(pk=fit_id).first()
            if fit is None:
                raise CommandError(f"Фит {fit_id} не найден")
            fits = [fit]

        settings_obj = PluginSetting.load()
        self.stdout.write(f"data_stale_after_min = {settings_obj.data_stale_after_min}")

        fresh, reasons = check_contracts_freshness(settings_obj.data_stale_after_min)
        self.stdout.write(f"Провайдеры контрактов: fresh={fresh}")
        for reason in reasons:
            self.stdout.write(f"  причина: {reason}")
        if not fresh:
            self.stdout.write(self.style.WARNING("NO_DATA: контракты не считаются, пока синк не станет свежим"))

        for fit in fits:
            self._report_fit(fit)

    def _report_fit(self, fit: Fit) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"=== Фит #{fit.pk} «{fit.name}» ==="))
        self.stdout.write(f"N={fit.qty_target}, scope={fit.scope_kind}/{fit.scope_ref}")
        self.stdout.write(f"кэш: contracts_status={fit.contracts_status_cache}, C={fit.contracts_available}")

        required = list(fit.components.order_by("pk").values_list("type_id", "qty_per_fit"))
        self.stdout.write(f"позиции фита ({len(required)}): {required}")
        if not required:
            self.stdout.write(self.style.WARNING("У фита нет компонентов — контракты не считаются"))
            return

        location_ids = resolve_contract_location_ids(fit.scope_kind, fit.scope_ref)
        self.stdout.write(f"локации области контрактов L(S): {sorted(location_ids) or '— (пусто!)'}")
        if not location_ids:
            self.stdout.write(self.style.ERROR("Область не резолвится ни в одну локацию — контракты не найдутся"))

        contracts = list(_outstanding_contracts(location_ids))
        if not contracts:
            self.stdout.write(self.style.ERROR("В кэше нет контрактов, попадающих в область фита"))
        total_ships = 0
        for contract in contracts:
            items = [
                engine.ContractItemInput(i.type_id, int(i.quantity), i.is_included, i.raw_quantity)
                for i in contract.items.all()
            ]
            totals: dict[int, int] = {}
            for it in items:
                if it.is_included and it.raw_quantity != engine.RAW_QUANTITY_BPC and it.quantity > 0:
                    totals[it.type_id] = totals.get(it.type_id, 0) + it.quantity
            ships = engine.calc_contract_fit_sets(items, required)
            total_ships += ships
            missing = [
                (type_id, qty, totals.get(type_id, 0)) for type_id, qty in required if totals.get(type_id, 0) < qty
            ]
            marker = self.style.SUCCESS(f"ships={ships}") if ships > 0 else self.style.ERROR(f"ships={0}")
            self.stdout.write(
                f"контракт {contract.contract_id}: loc={contract.start_location_id}, "
                f"items={len(items)}, {marker}, expires={contract.date_expired:%Y-%m-%d %H:%M}"
            )
            for type_id, qty, has in missing:
                self.stdout.write(f"    не хватает: type {type_id} нужно {qty}, есть {has}")
        self.stdout.write(f"итого C={total_ships}, D={max(0, fit.qty_target - total_ships)}")

        # Сверка с тем, что реально считает сервис (кэш фита мог устареть).
        result, matched, svc_reasons = evaluate_fit_contracts(fit)
        self.stdout.write(
            f"сервис: status={result.status}, C={result.ships_available}, "
            f"matched={[c.contract_id for c, _ships in matched]}, причины={svc_reasons}"
        )
        if result.ships_available != total_ships:
            self.stdout.write(
                self.style.WARNING(f"Расхождение: прямой подсчёт {total_ships} != сервис {result.ships_available}")
            )


def _outstanding_contracts(location_ids: set[int]):
    """Выборка кэша контрактов по области (§6.12), без N+1."""
    from marketmonitoring.models import ContractCache

    return (
        ContractCache.objects.filter(
            status="outstanding",
            contract_type="item_exchange",
            date_expired__gt=timezone.now(),
            start_location_id__in=location_ids,
        )
        .prefetch_related("items")
        .order_by("date_expired", "contract_id")
    )
