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
from ..forms import ContractProviderAddForm, PluginSettingForm, ProviderAddForm, WebhookForm
from ..models import ContractProvider, PluginSetting, StructureProvider, WebhookConfig

logger = logging.getLogger("marketmonitoring")

#: Обязательные скоупы провайдера структур (§9.1).
REQUIRED_SCOPES = ["esi-markets.structure_markets.v1", "esi-universe.read_structures.v1"]


def _character_has_scopes(character_id: int) -> bool:
    """Проверка наличия обоих скоупов хотя бы на одном токене персонажа AA (без сети, §9.1)."""
    from esi.models import Token

    tokens = Token.objects.filter(character_id=character_id)
    for scope in REQUIRED_SCOPES:
        if not tokens.filter(scopes__name=scope).exists():
            return False
    return True


def _character_has_contract_scope(character_id: int, source_kind: str) -> tuple[bool, str]:
    """Проверка наличия скоупа чтения контрактов на токене персонажа AA (без сети, §9.1)."""
    from esi.models import Token

    scope = contract_scope_for_kind(source_kind)
    exists = Token.objects.filter(character_id=character_id, scopes__name=scope).exists()
    return exists, scope


def _settings_context(request, settings_form, webhook_form, provider_form, contract_provider_form=None):
    """Общий контекст страницы настроек (§14): настройки + webhook + провайдеры структур и контрактов."""
    return {
        "page_title": _("Settings"),
        "settings_form": settings_form,
        "webhook_form": webhook_form,
        "webhook": WebhookConfig.objects.first(),
        "provider_form": provider_form,
        "providers": StructureProvider.objects.select_related("character").order_by("priority", "pk"),
        "contract_provider_form": contract_provider_form or ContractProviderAddForm(),
        "contract_providers": ContractProvider.objects.select_related("character").order_by("priority", "pk"),
        "provider_status_disabled": ProviderStatus.DISABLED,
    }


@login_required
def settings_view(request):
    """Страница настроек (edit, §14): §10 + webhook + провайдеры; GET показывает форму."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    settings_form = PluginSettingForm(request.POST or None, instance=PluginSetting.load())
    webhook_config, _created = WebhookConfig.objects.get_or_create(pk=1, defaults={"url": "", "enabled": True})
    webhook_form = WebhookForm(request.POST or None, instance=webhook_config, prefix="webhook")

    if request.method == "POST" and settings_form.is_valid() and webhook_form.is_valid():
        settings_form.save()
        webhook_form.save()
        messages.success(request, _("Settings saved."))
        return redirect("marketmonitoring:settings")

    return render(
        request,
        "marketmonitoring/settings.html",
        _settings_context(request, settings_form, webhook_form, ProviderAddForm()),
    )


@login_required
def provider_add(request):
    """Привязка провайдера (POST, edit, §9.1): персонаж AA с обязательными скоупами, иначе отказ."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    provider_form = ProviderAddForm(request.POST or None)
    if request.method == "POST" and provider_form.is_valid():
        character = provider_form.cleaned_data["character"]
        if not _character_has_scopes(character.character_id):
            messages.error(
                request,
                _("Add the character with the required scopes: %(scopes)s.") % {"scopes": ", ".join(REQUIRED_SCOPES)},
            )
            return redirect("marketmonitoring:settings")
        provider, created = StructureProvider.objects.get_or_create(character=character)
        if created:
            messages.success(request, _("Provider added."))
        else:
            messages.warning(request, _("Provider already exists."))
    return redirect("marketmonitoring:settings")


@login_required
def provider_toggle(request, provider_id: int):
    """Вкл/выкл провайдера (POST, edit, §14)."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    provider = get_object_or_404(StructureProvider, pk=provider_id)
    if request.method == "POST":
        provider.enabled = not provider.enabled
        if not provider.enabled:
            provider.status = ProviderStatus.DISABLED
        else:
            provider.status = ProviderStatus.OK
        provider.save(update_fields=["enabled", "status"])
        messages.success(request, _("Provider updated."))
    return redirect("marketmonitoring:settings")


@login_required
def provider_enable(request, provider_id: int):
    """Сброс disabled → enabled (POST, edit, §11.5): статус ok, acl_fail_count = 0, cooldown снят."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    provider = get_object_or_404(StructureProvider, pk=provider_id)
    if request.method == "POST":
        provider.enabled = True
        provider.status = ProviderStatus.OK
        provider.acl_fail_count = 0
        provider.cooldown_until = None
        provider.save(update_fields=["enabled", "status", "acl_fail_count", "cooldown_until"])
        messages.success(request, _("Provider re-enabled."))
    return redirect("marketmonitoring:settings")


@login_required
def contract_provider_add(request):
    """Привязка провайдера контрактов (POST, edit, §6.12, §9.1): персонаж AA с нужным скоупом."""
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    form = ContractProviderAddForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        character = form.cleaned_data["character"]
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
