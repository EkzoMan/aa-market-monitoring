"""Настройки плагина (§10, §14 edit): форма настроек, webhook, провайдеры структур.

webhook_url не показывается пользователям без права _edit (§16) — страница
целиком требует _edit; проверка скоупов провайдера — по токенам AA (esi.models.Token)
без сетевых вызовов (§9.1, UI не обращается к ESI — §5.10).
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from ..auth_hooks import has_edit
from ..constants import ProviderStatus
from ..esi.client import contract_scope_for_kind
from ..forms import ContractProviderAddForm, WebhookForm
from ..models import ContractProvider, WebhookConfig

logger = logging.getLogger("marketmonitoring")


def _character_has_contract_scope(character_id: int, source_kind: str) -> tuple[bool, str]:
    """Проверка наличия скоупа чтения контрактов на токене персонажа AA (без сети, §9.1)."""
    from esi.models import Token

    scope = contract_scope_for_kind(source_kind)
    exists = Token.objects.filter(character_id=character_id, scopes__name=scope).exists()
    return exists, scope


def _user_owns_character(user, character) -> bool:
    """Персонаж должен быть закреплён за текущим пользователем AA (§8)."""
    return type(character).objects.filter(pk=character.pk, character_ownership__user=user).exists()


def _settings_context(request, webhook_form, contract_provider_form=None):
    """Общий контекст страницы настроек (§14): webhook + провайдеры контрактов.

    Настройки цен (§10) и провайдеры структур со страницы убраны: правятся через admin.
    """
    return {
        "page_title": _("Settings"),
        "webhook_form": webhook_form,
        "webhook": WebhookConfig.objects.first(),
        "contract_provider_form": contract_provider_form or ContractProviderAddForm(user=request.user),
        "contract_providers": ContractProvider.objects.select_related("character").order_by("priority", "pk"),
        "provider_status_disabled": ProviderStatus.DISABLED,
    }


@login_required
def settings_view(request):
    """Страница настроек (edit, §14): webhook + провайдеры контрактов; GET показывает форму."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    webhook_config, _created = WebhookConfig.objects.get_or_create(pk=1, defaults={"url": "", "enabled": True})
    webhook_form = WebhookForm(request.POST or None, instance=webhook_config, prefix="webhook")

    if request.method == "POST" and webhook_form.is_valid():
        webhook_form.save()
        messages.success(request, _("Settings saved."))
        return redirect("marketmonitoring:settings")

    return render(
        request,
        "marketmonitoring/settings.html",
        _settings_context(request, webhook_form),
    )


@login_required
def contract_provider_add(request):
    """Привязка провайдера контрактов (POST, edit, §6.12, §9.1): персонаж AA с нужным скоупом."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    form = ContractProviderAddForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        character = form.cleaned_data["character"]
        if not _user_owns_character(request.user, character):
            messages.error(request, _("You can only add your own characters."))
            return redirect("marketmonitoring:settings")
        source_kind = form.cleaned_data["source_kind"]
        has_scope, scope = _character_has_contract_scope(character.character_id, source_kind)
        if not has_scope:
            messages.error(
                request,
                _("Add the character with the required scope: %(scope)s.") % {"scope": scope},
            )
            return redirect("marketmonitoring:settings")
        _provider, created = ContractProvider.objects.get_or_create(
            character=character,
            source_kind=source_kind,
        )
        if created:
            messages.success(request, _("Contract provider added."))
        else:
            messages.warning(request, _("Contract provider already exists."))
    return redirect("marketmonitoring:settings")


@login_required
def contract_provider_toggle(request, provider_id: int):
    """Вкл/выкл провайдера контрактов (POST, edit, §14)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    provider = get_object_or_404(ContractProvider, pk=provider_id)
    if request.method == "POST":
        provider.enabled = not provider.enabled
        provider.status = ProviderStatus.OK if provider.enabled else ProviderStatus.DISABLED
        provider.save(update_fields=["enabled", "status"])
        messages.success(request, _("Contract provider updated."))
    return redirect("marketmonitoring:settings")


@login_required
def contract_provider_enable(request, provider_id: int):
    """Сброс disabled → enabled для провайдера контрактов (POST, edit, §11.5)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    provider = get_object_or_404(ContractProvider, pk=provider_id)
    if request.method == "POST":
        provider.enabled = True
        provider.status = ProviderStatus.OK
        provider.acl_fail_count = 0
        provider.cooldown_until = None
        provider.save(update_fields=["enabled", "status", "acl_fail_count", "cooldown_until"])
        messages.success(request, _("Contract provider re-enabled."))
    return redirect("marketmonitoring:settings")


@login_required
def contract_provider_delete(request, provider_id: int):
    """Удаление провайдера контрактов (POST, edit, §14)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    provider = get_object_or_404(ContractProvider, pk=provider_id)
    if request.method == "POST":
        provider.delete()
        messages.success(request, _("Contract provider removed."))
    return redirect("marketmonitoring:settings")
