"""Raw response archive: every API page is stored as received, keyed by run.

Files go to RAW_DIR/<run_id>/<source>/<request_key>.json with a sha256 in
raw_data_manifest, so any normalized row can be traced back to the exact bytes
it came from. In deployment the same layout can be synced to S3 (P2).
Request bodies and auth headers are never archived; responses contain no
credentials.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..adapters.qbo import Page
from ..db import Database


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:150]


def archive(db: Database, raw_dir: Path, run_id: str, source: str, pages: list[Page], fetched_at: str) -> None:
    folder = raw_dir / run_id / source
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for page in pages:
        payload = json.dumps(page.body, sort_keys=True, default=str).encode()
        path = folder / f"{_safe(page.request_key)}.json"
        path.write_bytes(payload)
        rows.append({"run_id": run_id, "source": source, "request_key": page.request_key,
                     "path": str(path.relative_to(raw_dir)), "sha256": hashlib.sha256(payload).hexdigest(),
                     "bytes": len(payload), "fetched_at": fetched_at})
    db.upsert_many("raw_data_manifest", rows, ["run_id", "source", "request_key"])
