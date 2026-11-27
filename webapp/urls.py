from pathlib import Path

from django.urls import path, re_path
from django.views.static import serve

from . import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("guide/", views.guide, name="guide"),
    path("reconciliation/", views.reconciliation, name="reconciliation"),
    path("record/<str:record_type>/<str:record_id>/", views.record, name="record"),
    path("billing/", views.billing, name="billing"),
    path("data-quality/", views.data_quality, name="data_quality"),
    path("retail/", views.retail, name="retail"),
    path("architecture/", views.architecture, name="architecture"),
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
    # the design kit (CSS, JS, icons) used by /guide/: read-only files, served by Django behind Caddy
    re_path(r"^static/(?P<path>.+)$", serve, {"document_root": Path(__file__).resolve().parent / "static"}),
]
