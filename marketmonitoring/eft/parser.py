"""EFT-парсер (§7 ТЗ): разбор игрового экспорта на русском и английском языках.

Парсер работает только с БД/SDE (django-eveuniverse), без запросов к ESI (§7.3.11).
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from django.apps import apps
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger("marketmonitoring")

# Заголовок фита: [Hull, Fit name]
_HEADER_RE = re.compile(r"^\[(?P<hull>[^,\[\]]+),(?P<fit_name>.+)\]$")
# Суффикс количества в конце строки: " xN"
_QTY_SUFFIX_RE = re.compile(r"\s+x(?P<qty>\d+)\s*$")

EMPTY_SLOT_KEYWORD = "empty"


@dataclass
class ParseError:
    """Ошибка разбора: текст строки и её номер (§7.3.10)."""

    line_no: int
    line: str
    message: str


@dataclass
class ParsedItem:
    """Позиция фита (§7.4)."""

    type_id: Optional[int]
    qty_per_fit: int
    is_charge: bool
    slot: Optional[str]
    raw_line: str
    # Признак корпуса — только для предпросмотра в UI; в состав не сохраняется
    # напрямую (корпус попадает в FitComponent как обычная позиция с qty=1).
    is_hull: bool = False


@dataclass
class ParsedFit:
    """Результат разбора EFT (§7.4): корпус, имя, позиции, ошибки."""

    hull_type_id: Optional[int] = None
    fit_name: str = ""
    items: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """Валиден ли состав для сохранения: корпус + позиции без ошибок (§7.5)."""
        return (
            self.hull_type_id is not None
            and len(self.items) > 0
            and not self.errors
            and all(item.type_id is not None for item in self.items)
        )


def normalize_name(name: str) -> str:
    """Нормализация имени: обрезка и схлопывание пробелов (§7.3.8)."""
    return " ".join(name.split())


def _model_eve_type():
    """Модель EveType из eveuniverse (справочники SDE — только через eveuniverse)."""
    return apps.get_model("eveuniverse", "EveType")


def _get_localized_names_model():
    """Получение модели локализованных имён SDE, если она есть в django-eveuniverse.

    В некоторых версиях django-eveuniverse локализованных имён нет — тогда
    возвращается None и резолв деградирует на EveType.name.
    """
    try:
        return apps.get_model("eveuniverse", "EveTypeName")
    except LookupError:
        return None


def _candidate_languages() -> list:
    """Порядок языков для резолва: язык интерфейса -> en (§7.3.8)."""
    langs = []
    lang = get_language()
    if lang:
        langs.append(lang)
        base = lang.split("-")[0]
        if base != lang:
            langs.append(base)
    if "en" not in langs:
        langs.append("en")
    return langs


def resolve_type_name(name: str) -> Optional[int]:
    """Резолв имени типа -> type_id по локализованным именам SDE (§7.3.8).

    Точное совпадение без регистра по всем доступным языкам с приоритетом
    языка интерфейса, затем en. При отсутствии модели локализованных имён
    или записей имён — fallback на EveType.name. Не найдено -> None.
    """
    normalized = normalize_name(name)
    if not normalized:
        return None

    localized_model = _get_localized_names_model()
    if localized_model is not None:
        field_names = {f.name for f in localized_model._meta.get_fields()}
        lang_field = "language" if "language" in field_names else None
        for lang in _candidate_languages():
            qs = localized_model.objects.all()
            if lang_field:
                qs = qs.filter(**{lang_field: lang})
            try:
                found = qs.filter(name__iexact=normalized).values_list("eve_type_id", flat=True).first()
            except Exception:
                logger.warning("Localized name lookup failed; falling back to EveType.name")
                found = None
            if found is not None:
                return found

    # Fallback: имя из EveType (английское или то, что сохранено в name).
    try:
        return _model_eve_type().objects.filter(name__iexact=normalized).values_list("id", flat=True).first()
    except Exception:
        logger.exception("EveType name lookup failed")
        return None


def _resolve_name_or_error(name: str, line_no: int, raw_line: str, errors: list) -> Optional[int]:
    """Резолв имени позиции; при неудаче добавляет ошибку с номером строки (§7.3.10)."""
    type_id = resolve_type_name(name)
    if type_id is None:
        errors.append(
            ParseError(
                line_no,
                raw_line,
                str(_("Unknown type name: %(name)s") % {"name": name}),
            )
        )
    return type_id


def _add_or_merge(
    aggregated: dict,
    order: list,
    type_id: Optional[int],
    qty: int,
    is_charge: bool,
    slot: Optional[str],
    raw_line: str,
    is_hull: bool = False,
) -> None:
    """Добавление позиции; дубликаты одного типа суммируются по qty_per_fit (§7.3.7).

    При мерже дубликатов: is_charge=True сохраняется, если хотя бы одна из
    слитых позиций была зарядом (крайний случай, когда один тип фигурирует
    и как модуль, и как заряд). Поле slot берётся от первой позиции — это
    best-effort поле (§7.3.9), в расчётах не используется.
    """
    if type_id is None:
        # Нераспознанный тип не создаёт позицию, ошибка уже записана.
        return
    if type_id in aggregated:
        aggregated[type_id].qty_per_fit += qty
        if is_charge:
            aggregated[type_id].is_charge = True
    else:
        aggregated[type_id] = ParsedItem(
            type_id=type_id, qty_per_fit=qty, is_charge=is_charge, slot=slot, raw_line=raw_line, is_hull=is_hull
        )
        order.append(type_id)


def _slot_heuristic(groups: list) -> list:
    """Best-effort эвристика слотов по группам секций (§7.3.9 «best-effort»).

    Упрощённо: первая группа строк после заголовка считается high slots,
    вторая — mid, третья — low; дальнейшие группы — rig ("rig"), затем
    draq/drain ("drain"). Имена групп не анализируются: секции разделяются
    пустыми строками, сами группы не важны. Это поле не участвует в расчётах
    (§6), пригодно только для отображения в UI.
    """
    labels = ["high", "mid", "low", "rig", "drain"]
    slots = []
    for group_index in range(len(groups)):
        slots.append(labels[group_index] if group_index < len(labels) else None)
    return slots


def parse_eft(text: str) -> ParsedFit:
    """Разбор EFT-текста (§7.2, §7.3).

    Правила: заголовок `[Hull, Name]` -> корпус qty=1 (первая непустая строка
    обязана быть заголовком, иначе — отказ); `[Empty ...]` игнорируется;
    `module, charge` -> заряд отдельной позицией qty_per_fit=1 is_charge=true,
    `xN` в конце строки применяется к заряду; суффикс ` xN` -> количество
    позиции; отсутствие множителя = 1; дубликаты типов суммируются по
    qty_per_fit; любая нераспознанная строка -> ошибка с текстом строки и её
    номером. Секции разделяются пустыми строками, сами группы не важны.
    """
    parsed = ParsedFit()
    if not text or not text.strip():
        parsed.errors.append(ParseError(0, "", str(_("EFT text is empty."))))
        return parsed

    lines = text.splitlines()

    # Заголовок: первая непустая строка (§7.3.1).
    header_idx = None
    for idx, raw in enumerate(lines, start=1):
        if raw.strip():
            header_idx = idx
            break
    if header_idx is None:
        parsed.errors.append(ParseError(0, "", str(_("EFT text is empty."))))
        return parsed

    header_line = lines[header_idx - 1].strip()
    header_match = _HEADER_RE.match(header_line)
    if not header_match:
        parsed.errors.append(
            ParseError(
                header_idx,
                header_line,
                str(_("First line must be a fit header like [Hull, Fit name].")),
            )
        )
        return parsed

    hull_name = normalize_name(header_match.group("hull"))
    parsed.fit_name = header_match.group("fit_name").strip()
    parsed.hull_type_id = resolve_type_name(hull_name)
    if parsed.hull_type_id is None:
        parsed.errors.append(
            ParseError(
                header_idx,
                header_line,
                str(_("Unknown hull type: %(name)s") % {"name": hull_name}),
            )
        )

    # Позиции: секции после заголовка (§7.3.3). Группы (секции, разделённые
    # пустыми строками) используются только для best-effort эвристики слотов.
    aggregated: dict = {}
    order: list = []

    body_lines = lines[header_idx:]
    # Разбиение тела на группы по пустым строкам (с номерами строк).
    groups: list = []
    current_group: list = []
    for idx, raw in enumerate(body_lines, start=header_idx + 1):
        if not raw.strip():
            if current_group:
                groups.append(current_group)
                current_group = []
            continue
        current_group.append((idx, raw))
    if current_group:
        groups.append(current_group)

    # Best-effort слоты по группам (§7.3.9) — не для расчётов.
    group_slots = _slot_heuristic(groups)

    for group_index, group in enumerate(groups):
        group_slot = group_slots[group_index]
        for idx, raw in group:
            line = raw.strip()

            # Скобочные строки: только [Empty ...] допустимы (§7.3.2).
            if line.startswith("[") and line.endswith("]"):
                inner = line[1:-1].strip()
                if not inner or inner.split()[0].lower() != EMPTY_SLOT_KEYWORD:
                    parsed.errors.append(
                        ParseError(idx, line, str(_("Unrecognized bracket line: %(line)s") % {"line": line}))
                    )
                continue

            # Суффикс " xN" в конце строки (§7.3.5); отсутствие — 1 (§7.3.6).
            qty_match = _QTY_SUFFIX_RE.search(line)
            if qty_match:
                qty = int(qty_match.group("qty"))
                line = line[: qty_match.start()].strip()
            else:
                qty = 1

            if "," in line:
                # Модуль + заряд (§7.3.4): заряд — отдельная позиция qty_per_fit=1,
                # is_charge=true; xN после заряда применяется к заряду.
                module_part, charge_part = line.split(",", 1)
                module_name = normalize_name(module_part)
                charge_name = normalize_name(charge_part)
                module_qty = 1

                # Позиция модуля (без слота — модуль и заряд в одной строке).
                module_type_id = _resolve_name_or_error(module_name, idx, raw.strip(), parsed.errors)
                _add_or_merge(aggregated, order, module_type_id, module_qty, False, None, raw.strip())

                # Позиция заряда: qty_per_fit=1 * N.
                charge_type_id = _resolve_name_or_error(charge_name, idx, raw.strip(), parsed.errors)
                _add_or_merge(aggregated, order, charge_type_id, qty, True, None, raw.strip())
            else:
                type_id = _resolve_name_or_error(normalize_name(line), idx, raw.strip(), parsed.errors)
                # слот — best-effort (§7.3.9): хранится, но не используется в расчётах.
                _add_or_merge(aggregated, order, type_id, qty, False, group_slot, raw.strip())

    # Корпус — первая позиция состава с qty_per_fit=1 (§6.3): попадает в
    # FitComponent как позиция с q=1, is_charge=False; в UI помечен корпусом.
    if parsed.hull_type_id is not None:
        hull_item = ParsedItem(
            type_id=parsed.hull_type_id,
            qty_per_fit=1,
            is_charge=False,
            slot=None,
            raw_line=header_line,
            is_hull=True,
        )
        # UNIQUE(fit, type_id): если тип корпуса встречается и в items (экзотика),
        # при сохранении количества суммируются; здесь корпус добавляется первой
        # строкой, а существующая позиция (если была) помечается слитой с корпусом.
        if parsed.hull_type_id in aggregated:
            hull_item.qty_per_fit += aggregated[parsed.hull_type_id].qty_per_fit
            if aggregated[parsed.hull_type_id].is_charge:
                hull_item.is_charge = True
        parsed.items = [hull_item] + [item for tid, item in aggregated.items() if tid != parsed.hull_type_id]
    else:
        parsed.items = [aggregated[type_id] for type_id in order]
    return parsed
