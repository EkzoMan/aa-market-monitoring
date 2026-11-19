"""URL-конфиг тестового проекта."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("allianceauth.urls")),
    path("market/", include("marketmonitoring.urls")),
]
