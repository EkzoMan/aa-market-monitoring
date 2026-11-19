"""Шаблонные фильтры плагина: рендер имён типов и форматирование ISK (§17)."""

from decimal import ROUND_HALF_UP, Decimal

from django import template
from django.utils.translation import gettext as _

register = template.Library()


def resolve_type_display_name(type_id: int, language_code: str | None = None) -> str:
    """Имя типа с учётом display_language_mode (§15, §10).

    fixed_en — всегда EveType.name (английский); client_default — имя
    под LANGUAGE_CODE пользователя (если локализованное имя в SDE есть),
    fallback — английское EveType.name. На этапе 1 локализованных имён в
    справочнике нет — режим client_default деградирует до английского.
    """
    from eveuniverse.models import EveType

    name = EveType.objects.filter(id=type_id).values_list("name", flat=True).first()
    if not name:
        return f"type {type_id}"
    english_name = str(name)
    if language_code and language_code.lower() not in ("en", "en-us"):
        # Каркас локализации имён (§15): локализованных имён в SDE на этапе 1 нет,
        # поэтому любой не-английский язык деградирует до английского EveType.name.
        return english_name
    return english_name


@register.simple_tag
def type_name(type_id: int, language_code: str | None = None) -> str:
    """Имя типа из SDE одним запросом; язык — display_language_mode (§15)."""
    return resolve_type_display_name(type_id, language_code)


@register.filter
def isk(value) -> str:
    """Форматирование суммы в целые ISK (ROUND_HALF_UP) с разделителями тысяч."""
    if value is None:
        return "0"
    amount = Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{amount:,}"


@register.filter
def status_class(status: str) -> str:
    """CSS-класс статуса фита по цветам §14.2: OK зелёный, DEFICIT оранжевый, HULL_MISSING красный, NO_DATA серый."""
    return {
        "ok": "success",
        "deficit": "warning",
        "hull_missing": "danger",
        "no_data": "secondary",
    }.get(status, "secondary")


@register.filter
def sync_status_class(status: str) -> str:
    """Цвет чипа статуса синка локации (05-locations.md): ok зелёный, сбой красный, stale оранжевый, прочее серый."""
    return {
        "ok": "success",
        "error": "danger",
        "no_acl": "danger",
        "no_provider": "secondary",
        "stale": "warning",
    }.get(status, "secondary")


@register.simple_tag
def bottleneck_line(names: list, extra_count: int = 0) -> str:
    """Строка узких мест (§14): до 5 имён типов, при большем числе — «и ещё N»."""
    shown = [str(name) for name in names[:5]]
    if extra_count > 0:
        shown.append(_("+ %(count)s more") % {"count": extra_count})
    return ", ".join(shown) if shown else "—"
