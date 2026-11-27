"""Deterministic internal identifiers.

Internal IDs are UUIDv5 values derived from stable business keys, so a re-run
produces the same IDs and upserts stay idempotent. External system IDs (QBO Id,
Stripe pi_...) are never used as primary keys; they live in source_id_map and
on each record's source columns.
"""
from __future__ import annotations

import uuid

NAMESPACE = uuid.UUID("6f1c1b8e-5d0a-4c55-9a39-2f4f0f9d3a11")


def internal_id(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, ":".join(parts)))


def client_id(client_key: str) -> str:
    return internal_id("client", client_key)


def location_id(client_key: str, location_key: str) -> str:
    return internal_id("location", client_key, location_key)


def record_id(source: str, entity: str, external_id: str) -> str:
    return internal_id("record", source, entity, external_id)
