"""Формы плагина (§7.5, §14): фиты, компоненты, локации, настройки."""

from django import forms
from django.utils.translation import gettext_lazy as _

from .constants import ContractSourceKind, LocationKind, ScopeKind
from .models import ComponentRequest, Fit, MarketLocation, WebhookConfig

# Число колонок в селекторе типов (без внешних CDN; обычный select по EveType).
TYPE_SELECT_ATTRS = {"class": "form-control", "size": 1}


class FitForm(forms.ModelForm):
    """Форма фита: EFT-текст, N (qty_target), область, разбор и предпросмотр (§7.5)."""

    eft_raw = forms.CharField(
        label=_("EFT fit text"),
        widget=forms.Textarea(attrs={"rows": 18, "class": "form-control"}),
        help_text=_("Paste the fit copied from the game client (English or Russian)."),
    )
    scope_kind = forms.ChoiceField(
        label=_("Scope"),
        choices=ScopeKind.choices,
        widget=forms.Select(attrs={"class": "form-control", "id": "id_scope_kind"}),
    )
    # scope_ref выбирается из списка через SDE-поиск (station/system/region),
    # а не вводится числом: скрытое поле + текстовый поиск в шаблоне.
    scope_ref = forms.IntegerField(widget=forms.HiddenInput(attrs={"id": "id_scope_ref"}))
    scope_ref_name = forms.CharField(
        label=_("Scope location"),
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "id": "scope-ref-search", "autocomplete": "off"}),
        help_text=_("Start typing a station, system or region name; pick a suggestion."),
    )

    class Meta:
        model = Fit
        fields = ["name", "eft_raw", "qty_target", "scope_kind", "scope_ref"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "qty_target": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }

    def clean_qty_target(self) -> int:
        """Количество N должно быть >= 1 (§7.5)."""
        qty = self.cleaned_data.get("qty_target") or 1
        if qty < 1:
            raise forms.ValidationError(_("Target quantity must be >= 1."))
        return qty

    def clean(self):
        """Перекрёстная валидация: имя фита по умолчанию — из заголовка EFT;
        scope_ref обязан существовать в справочнике SDE для выбранного вида области."""
        from eveuniverse.models import EveRegion, EveSolarSystem, EveStation

        cleaned = super().clean()
        if not cleaned.get("name"):
            eft = cleaned.get("eft_raw") or ""
            first_line = eft.strip().splitlines()[0] if eft.strip() else ""
            if "," in first_line and first_line.startswith("["):
                cleaned["name"] = first_line.strip()[1:-1].split(",", 1)[1].strip()

        scope_kind = cleaned.get("scope_kind")
        scope_ref = cleaned.get("scope_ref")
        if scope_kind and scope_ref is not None:
            model = {
                ScopeKind.STATION: EveStation,
                ScopeKind.SYSTEM: EveSolarSystem,
                ScopeKind.REGION: EveRegion,
            }.get(scope_kind)
            if model is None or not model.objects.filter(pk=scope_ref).exists():
                raise forms.ValidationError(_("Unknown scope reference (not found in SDE)."))
        return cleaned


class FitComponentRowForm(forms.Form):
    """Правка одной позиции предпросмотра (§7.5, FR-3/FR-4): количество, удаление,
    добавление типов через SDE-селектор, per-component override области.

    Строки предпросмотра рендерятся с префиксом ``row<i>-`` (ManagementForm-подобный
    формат): view собирает правки из POST по этому шаблону имён, поэтому правки
    переживают повторный разбор (action=parse) и применяются к итоговому составу
    при action=save.
    """

    type_id = forms.IntegerField(widget=forms.HiddenInput)
    qty_per_fit = forms.IntegerField(
        min_value=1,
        initial=1,
        widget=forms.NumberInput(
            attrs={"class": "form-control form-control-sm mm-num", "min": 1, "style": "width: 6rem"}
        ),
    )
    delete = forms.BooleanField(required=False)
    scope_override_kind = forms.ChoiceField(
        choices=[("", "---------")] + list(ScopeKind.choices),
        required=False,
        widget=forms.Select(attrs={"class": "form-control form-control-sm"}),
    )
    scope_override_ref = forms.IntegerField(
        required=False, widget=forms.NumberInput(attrs={"class": "form-control form-control-sm mm-num"})
    )
    # Служебные поля, чтобы предпросмотр после re-parse сохранил вид строки.
    is_charge = forms.BooleanField(required=False, widget=forms.HiddenInput)
    raw_line = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean(self):
        """Кросс-валидация строки (§7.5): количество >= 1, override области — парой kind+ref."""
        cleaned = super().clean()
        kind = cleaned.get("scope_override_kind")
        ref = cleaned.get("scope_override_ref")
        if kind and not ref:
            raise forms.ValidationError(_("Scope override reference is required when a scope kind is set."))
        if ref and not kind:
            raise forms.ValidationError(_("Scope override kind is required when a reference is set."))
        if cleaned.get("delete"):
            return cleaned
        qty = cleaned.get("qty_per_fit")
        if qty is None or qty < 1:
            raise forms.ValidationError(_("Quantity per fit must be >= 1."))
        return cleaned


# ---------------------------------------------------------------------------
# Компоненты вне фитов (§14): SDE-селектор типа + количество/область/заметка
# ---------------------------------------------------------------------------


class ComponentRequestForm(forms.ModelForm):
    """Компонент вне фитов: тип (поиск имён через type_search), количество, область, заметка (§14)."""

    type_name = forms.CharField(
        label=_("Type name"),
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off", "id": "type-search"}),
        help_text=_("Start typing a type name; pick a suggestion to fill the type id."),
    )

    class Meta:
        model = ComponentRequest
        fields = ["type_id", "qty_target", "scope_kind", "scope_ref", "note"]
        widgets = {
            "type_id": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "qty_target": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "scope_kind": forms.Select(attrs={"class": "form-control"}),
            "scope_ref": forms.NumberInput(attrs={"class": "form-control"}),
            "note": forms.TextInput(attrs={"class": "form-control"}),
        }

    def clean_type_id(self) -> int:
        """Тип должен существовать в справочнике SDE (eveuniverse, §8 — своих копий нет)."""
        from eveuniverse.models import EveType

        type_id = self.cleaned_data.get("type_id")
        if type_id is None:
            return type_id
        if not EveType.objects.filter(pk=type_id).exists():
            raise forms.ValidationError(_("Unknown type id (not found in SDE)."))
        return type_id

    def clean_qty_target(self) -> int:
        """Целевое количество >= 1 (§6.5)."""
        qty = self.cleaned_data.get("qty_target") or 1
        if qty < 1:
            raise forms.ValidationError(_("Target quantity must be >= 1."))
        return qty


# ---------------------------------------------------------------------------
# Локации (§14): регион → система → станция; структура — id + желаемое имя
# ---------------------------------------------------------------------------


class MarketLocationForm(forms.Form):
    """Добавление локации мониторинга (§14, §8 MarketLocation).

    Станция: выбор система → станция из SDE (имя и регион берутся из справочника).
    Структура: ввод structure_id и желаемого имени — реальное имя подтянется
    при первом синке через fetch_structure_info в задаче (UI к ESI не обращается, §5.10).
    """

    kind = forms.ChoiceField(
        label=_("Kind"), choices=LocationKind.choices, widget=forms.Select(attrs={"class": "form-control"})
    )
    region_id = forms.IntegerField(
        label=_("Region"),
        required=False,
        widget=forms.HiddenInput,
        help_text=_("Filled when a system is picked from the SDE search."),
    )
    solar_system_id = forms.IntegerField(
        label=_("Solar system"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "id": "system-search-id"}),
        help_text=_("Pick a system via the search box (SDE reference)."),
    )
    station_id = forms.IntegerField(
        label=_("Station"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "id": "station-search-id"}),
    )
    structure_id = forms.IntegerField(
        label=_("Structure id"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    structure_name = forms.CharField(
        label=_("Structure name (temporary)"),
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        help_text=_("Used until the real name is fetched during the first sync."),
    )

    def clean(self):
        """Кросс-валидация: обязательные поля по kind, уникальность (§8 UNIQUE), регион из SDE.

        Для станции/системы система обязана быть в SDE (выбирается из справочника).
        Для структуры (§9) система и регион приходят из ESI-поиска и могут
        отсутствовать в локальном SDE — они резолвятся best-effort (0 = неизвестно,
        дозаполнится при первом синке, как в sync-задаче).
        """
        from eveuniverse.models import EveSolarSystem, EveStation

        cleaned = super().clean()
        kind = cleaned.get("kind")
        system_id = cleaned.get("solar_system_id")

        if system_id is None:
            if kind == LocationKind.STRUCTURE:
                # Система структуры неизвестна (ESI не отдал solar_system_id) —
                # легитимный случай, дозаполнится при первом синке (§9).
                system_id = 0
                cleaned["solar_system_id"] = 0
                cleaned["region_id_resolved"] = 0
                system = None
            else:
                raise forms.ValidationError(_("Solar system is required."))
        else:
            system = EveSolarSystem.objects.filter(pk=system_id).first()

        region_id = None
        if system is not None:
            region_id = (
                EveSolarSystem.objects.filter(pk=system_id)
                .values_list("eve_constellation__eve_region_id", flat=True)
                .first()
            )
        if kind != LocationKind.STRUCTURE and (system is None or region_id is None):
            # Станция/система выбираются из SDE — их отсутствие означает ошибку выбора.
            if system is None:
                raise forms.ValidationError(_("Unknown solar system (not found in SDE)."))
            raise forms.ValidationError(_("Cannot resolve region for the system from SDE."))
        if region_id is None:
            # Структура вне SDE: регион берётся из результата ESI-поиска, иначе 0.
            posted_region = cleaned.get("region_id")
            region_id = posted_region if isinstance(posted_region, int) and posted_region > 0 else 0
        cleaned["region_id_resolved"] = region_id

        if system is not None:
            cleaned["system_name"] = str(system.name)

        if kind == LocationKind.SYSTEM:
            # Локация-система: поиск контрактов по всем локациям выдачи системы (§6.12).
            cleaned["name"] = str(system.name)
            cleaned["station_id"] = None
            cleaned["structure_id"] = None
            if MarketLocation.objects.filter(kind=LocationKind.SYSTEM, solar_system_id=system_id).exists():
                raise forms.ValidationError(_("This system is already monitored."))
        elif kind == LocationKind.STATION:
            station_id = cleaned.get("station_id")
            if not station_id:
                raise forms.ValidationError(_("Station is required for a station location."))
            station = EveStation.objects.filter(pk=station_id).first()
            if station is None:
                # structure_id не бывает в SDE-справочнике станций: подсказываем
                # переключить Kind на structure (§9 — структуры ищутся через ESI).
                raise forms.ValidationError(
                    _(
                        "Unknown station (not found in SDE). "
                        "Corporation structures must be added with kind 'structure' via the structure search."
                    )
                )
            cleaned["name"] = str(station.name)
            if MarketLocation.objects.filter(kind=LocationKind.STATION, station_id=station_id).exists():
                raise forms.ValidationError(_("This station is already monitored."))
        elif kind == LocationKind.STRUCTURE:
            structure_id = cleaned.get("structure_id")
            if not structure_id:
                raise forms.ValidationError(_("Structure id is required for a structure location."))
            if not cleaned.get("structure_name"):
                raise forms.ValidationError(_("A temporary structure name is required."))
            cleaned["name"] = cleaned["structure_name"]
            if MarketLocation.objects.filter(kind=LocationKind.STRUCTURE, structure_id=structure_id).exists():
                raise forms.ValidationError(_("This structure is already monitored."))
        else:
            raise forms.ValidationError(_("Unknown location kind."))
        return cleaned


# ---------------------------------------------------------------------------
# Настройки (§10) + webhook + провайдеры (§14)
# ---------------------------------------------------------------------------


class WebhookForm(forms.ModelForm):
    """Форма Discord webhook (§13); доступна только пользователям с правом _edit (§16).

    url — необязательное поле: вебхук может быть ещё не настроен.
    """

    url = forms.URLField(
        label=_("Webhook URL"),
        required=False,
        # §16 / 06-settings.md: значение webhook URL не вставляется в DOM как текст —
        # PasswordInput (render_value=False по умолчанию) отдаёт пустой input.
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )

    class Meta:
        model = WebhookConfig
        fields = ["url", "enabled"]
        widgets = {
            "enabled": forms.CheckboxInput(),
        }


class ContractProviderAddForm(forms.Form):
    """Привязка провайдера контрактов: персонаж AA и вид источника контрактов (§6.12, §9.1)."""

    character = forms.ModelChoiceField(
        label=_("Character"),
        queryset=None,
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    source_kind = forms.ChoiceField(
        label=_("Contract source"),
        choices=ContractSourceKind.choices,
        initial=ContractSourceKind.CORPORATION,
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        """Список персонажей — только закреплённые за текущим пользователем AA (§8).

        Токены плагин не хранит: владение берётся из ``character_ownership``
        Alliance Auth. Без пользователя список пуст — форму нельзя использовать
        для привязки чужого персонажа.
        """
        from allianceauth.eveonline.models import EveCharacter

        super().__init__(*args, **kwargs)
        queryset = EveCharacter.objects.all()
        if user is not None and getattr(user, "is_authenticated", False):
            queryset = queryset.filter(character_ownership__user=user)
        else:
            queryset = queryset.none()
        self.fields["character"].queryset = queryset.order_by("character_name")
