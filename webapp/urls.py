from django.urls import path

from . import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("reconciliation/", views.reconciliation, name="reconciliation"),
    path("record/<str:record_type>/<str:record_id>/", views.record, name="record"),
    path("billing/", views.billing, name="billing"),
    path("data-quality/", views.data_quality, name="data_quality"),
    path("retail/", views.retail, name="retail"),
    path("architecture/", views.architecture, name="architecture"),
    path("healthz", views.healthz, name="healthz"),
]
