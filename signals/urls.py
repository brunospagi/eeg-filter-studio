from django.urls import path

from . import views

app_name = "signals"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/upload/", views.api_upload, name="api_upload"),
    path("api/tests/", views.api_tests_list, name="api_tests_list"),
    path("api/tests/delete/", views.api_test_delete, name="api_test_delete"),
    path("api/artifacts/save/", views.api_artifacts_save, name="api_artifacts_save"),
    path("api/compare/", views.api_compare, name="api_compare"),
    path("api/process/", views.api_process, name="api_process"),
    path("api/bands/", views.api_bands, name="api_bands"),
    path("api/bands/compare/", views.api_bands_compare, name="api_bands_compare"),
    path("api/bands/download/", views.api_bands_download, name="api_bands_download"),
    path("api/download/", views.api_download, name="api_download"),
]
