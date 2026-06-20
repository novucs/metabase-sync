"""Database manifests (read-only — names + engines only, never credentials)."""

from __future__ import annotations

from pathlib import Path

from metabase_sync.models import Database
from metabase_sync.serialize.paths import slugify
from metabase_sync.serialize.yamlio import load_yaml, write_yaml


def write_databases(state_dir: Path, dbs: list[Database]) -> None:
    out = state_dir / "databases"
    out.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for db in dbs:
        slug = slugify(db.name)
        path = out / f"{slug}.yaml"
        seen.add(path.name)
        write_yaml(
            path,
            {
                "name": db.name,
                "engine": db.engine,
                "entity_id": db.entity_id,
            },
        )
    for existing in out.glob("*.yaml"):
        if existing.name not in seen:
            existing.unlink()


def read_databases(state_dir: Path) -> list[dict]:
    out = state_dir / "databases"
    if not out.exists():
        return []
    return [load_yaml(p) for p in sorted(out.glob("*.yaml"))]
