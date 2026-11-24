"""URL-маршруты плагина (§14): dashboard, фиты, компоненты, локации, настройки, статус обновления."""

from django.urls import path

from .views import components, dashboard, exports, fits, locations, refresh
from .views import settings as settings_views

app_name = "marketmonitoring"

urlpatterns = [
    path("", dashboard.index, name="index"),
    path("fit/add/", fits.fit_create, name="fit_create"),
    path("fit/<int:fit_id>/", fits.fit_detail, name="fit_detail"),
    path("fit/<int:fit_id>/edit/", fits.fit_update, name="fit_update"),
    path("fit/<int:fit_id>/delete/", fits.fit_delete, name="fit_delete"),
    path("fit/<int:fit_id>/export/txt/", exports.export_txt, name="export_txt"),
    path("fit/<int:fit_id>/export/csv/", exports.export_csv, name="export_csv"),
    path("fit/<int:fit_id>/export/multibuy.json/", exports.export_multibuy, name="export_multibuy"),
    path("types/search.json", components.type_search, name="type_search"),
    path("locations/", locations.locations, name="locations"),
    path("locations/add/", locations.locations, name="location_create"),
    path("locations/<int:location_id>/toggle/", locations.location_toggle, name="location_toggle"),
    path("locations/<int:location_id>/delete/", locations.location_delete, name="location_delete"),
    path("locations/systems.json", locations.system_search, name="system_search"),
    path("locations/stations.json", locations.station_search, name="station_search"),
    path("locations/regions.json", locations.region_search, name="region_search"),
    path("locations/regions.json", locations.region_search, name="region_search"),
    path("locations/structures.json", locations.structure_search_start, name="structure_search_start"),
    path("locations/structures/result.json", locations.structure_search_result, name="structure_search_result"),
    path("settings/", settings_views.settings_view, name="settings"),
    path("settings/contract-provider/add/", settings_views.contract_provider_add, name="contract_provider_add"),
    path(
        "settings/contract-provider/<int:provider_id>/toggle/",
        settings_views.contract_provider_toggle,
        name="contract_provider_toggle",
    ),
    path(
        "settings/contract-provider/<int:provider_id>/enable/",
        settings_views.contract_provider_enable,
        name="contract_provider_enable",
    ),
    path(
        "settings/contract-provider/<int:provider_id>/delete/",
        settings_views.contract_provider_delete,
        name="contract_provider_delete",
    ),
    path("refresh/", refresh.refresh, name="refresh"),
    path("refresh/status/", refresh.refresh_status, name="refresh_status"),
]
